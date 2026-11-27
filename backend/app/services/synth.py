"""拨测执行器：HTTP/TCP/DNS 单点拨测。

HTTP 支持浏览器探测（状态码/期望校验/总耗时/证书到期天数）及渲染级 JS 执行/截图。
失败连续 N 次转告警（AlertRecord rule_id=0，与 host_watch 同模式）。
"""
from __future__ import annotations

import logging
import socket
import ssl
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.synth import SynthCheck, SynthResult

logger = logging.getLogger("qianshu.synth")

FAIL_ALERT_THRESHOLD = 3   # 连续失败 N 次转告警


def _check_http(target: str, timeout: int, expect: str,
                accept_self_signed: bool = False) -> tuple:
    """HTTP 拨测：socket 级分段计时——
    dns / tcp建联 / tls握手 / ttfb首字节 / transfer传输 / total总计。
    返回 (ok, latency_ms, status_code, detail, timing dict)。
    accept_self_signed：内网自签证书目标跳过 TLS 证书校验。"""
    import socket
    import ssl as _ssl
    import time as _t

    url = target if target.startswith(("http://", "https://")) else f"https://{target}"
    https = url.startswith("https://")
    rest = url.split("//", 1)[1]
    hostport, _, path = rest.partition("/")
    path = "/" + path
    host = hostport.split(":")[0]
    port = int(hostport.split(":")[1]) if ":" in hostport else (443 if https else 80)

    timing = {}
    t_all = _t.time()
    try:
        # 1) DNS
        t0 = _t.time()
        addr = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0]
        timing["dns_ms"] = round((_t.time() - t0) * 1000, 1)
        # 2) TCP 建联
        t0 = _t.time()
        sock = socket.create_connection(addr[4], timeout=timeout)
        timing["tcp_ms"] = round((_t.time() - t0) * 1000, 1)
        # 3) TLS 握手（仅 https 记录；http 没有这一步，不落 0ms 假分段）
        if https:
            t0 = _t.time()
            ctx = _ssl.create_default_context()
            if accept_self_signed:
                # 自签证书目标只放松证书校验，关闭后证书有效性不判失败
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
            sock = ctx.wrap_socket(sock, server_hostname=host)
            timing["tls_ms"] = round((_t.time() - t0) * 1000, 1)
        sock.settimeout(timeout)
        # 4) 发请求 + 首字节（TTFB）
        t0 = _t.time()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
               "User-Agent: QianShu-Synth/1.0\r\nConnection: close\r\n\r\n")
        sock.sendall(req.encode())
        first = sock.recv(1)
        timing["ttfb_ms"] = round((_t.time() - t0) * 1000, 1)
        # 5) 传输（读到 EOF）
        t0 = _t.time()
        data = first
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
            if len(data) > 512 * 1024:
                break
        timing["transfer_ms"] = round((_t.time() - t0) * 1000, 1)
        sock.close()
        timing["total_ms"] = round((_t.time() - t_all) * 1000, 1)
        timing["size_bytes"] = len(data)

        # 状态码 + 期望校验
        status = int(data.split(b" ", 2)[1]) if len(data.split(b" ", 2)) > 1 else 0
        lo, hi = 200, 399
        if "-" in (expect or ""):
            try:
                a, b = expect.split("-", 1)
                lo, hi = int(a), int(b)
            except Exception:
                pass
        elif expect:
            try:
                lo = hi = int(expect)
            except Exception:
                pass
        ok = lo <= status <= hi
        parts = [f"HTTP {status}", f"DNS {timing['dns_ms']}ms", f"建联 {timing['tcp_ms']}ms"]
        if https:
            parts.append(f"TLS {timing['tls_ms']}ms")
        parts += [f"首字节 {timing['ttfb_ms']}ms", f"传输 {timing['transfer_ms']}ms"]
        detail = "｜".join(parts) + f"｜{timing['size_bytes'] / 1024:.1f}KB"
        if https:
            days = _cert_days_left(url, timeout, accept_self_signed=accept_self_signed)
            if days is not None:
                detail += f"｜证书 {days} 天后到期"
                if days < 0:
                    ok = False
        return (ok, timing["total_ms"], status, detail, timing)
    except Exception as e:
        timing["total_ms"] = round((_t.time() - t_all) * 1000, 1)
        return (False, timing["total_ms"], 0,
                f"{type(e).__name__}: {str(e)[:100]}", timing)


def _cert_days_left(url: str, timeout: int, accept_self_signed: bool = False):
    """证书剩余天数。自签（accept_self_signed）时握手跳过校验，证书用 cryptography 解码。"""
    import httpx

    try:
        hostport = url.split("//", 1)[1].split("/", 1)[0]
        host = hostport.split(":")[0]
        # 尊重 URL 显式端口，非 443 的 TLS 目标也要能取得证书信息
        port = int(hostport.split(":")[1]) if ":" in hostport else 443
        ctx = ssl.create_default_context()
        if accept_self_signed:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        der = None
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                if not cert:
                    # CERT_NONE 下 getpeercert() 为空字典，必须取 DER 证书自行解码
                    der = ssock.getpeercert(binary_form=True)
        if cert:
            not_after = ssl.cert_time_to_seconds(cert["notAfter"])
        elif der:
            from cryptography import x509

            not_after = x509.load_der_x509_certificate(der).not_valid_after_utc.timestamp()
        else:
            return None
        return int((not_after - time.time()) / 86400)
    except Exception:
        return None


def _check_browser(target: str, timeout: int, accept_self_signed: bool = False) -> tuple:
    """真实浏览器探测：chrome headless + DevTools 协议（CDP）——
    截图/DOM/标题/页面净加载（导航到渲染完成）。

    不用 --virtual-time-budget：它会与 vite dev 的 HMR websocket 死锁；
    CDP 是确定性方案（导航→等待→截图/取 DOM），与质量体系的浏览器测试同源。
    """
    import os as _os
    import shutil
    import subprocess
    import tempfile
    import time as _t
    import urllib.request as _ur

    url = target if target.startswith(("http://", "https://")) else f"https://{target}"
    chrome = None
    from shutil import which

    for c in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if which(c):
            chrome = c
            break
    if chrome is None:
        return (False, 0.0, 0, "未安装 Chrome/Chromium（渲染级探测需要浏览器）",
                {"total_ms": 0, "need_browser": True})

    # 硬超时保险：chrome 挂死不能拖住 API 请求
    import threading

    result_box = {}

    def _worker():
        try:
            result_box["r"] = _check_browser_cdp(url, timeout, chrome,
                                                 accept_self_signed=accept_self_signed)
        except Exception as e:
            result_box["r"] = (False, 0.0, 0, f"{type(e).__name__}: {str(e)[:100]}", {})

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    th.join(timeout + 45)
    if th.is_alive() or "r" not in result_box:
        return (False, 0.0, 0, "浏览器探测超时（已放弃等待，进程不阻塞）",
                {"total_ms": (timeout + 45) * 1000})
    return result_box["r"]


def _snapshot_fallback(chrome: str, profile_dir: str, url: str, t0: float,
                       accept_self_signed: bool = False) -> tuple:
    """降级快照模式：浏览器调试通道异常时用 --screenshot 直出快照——
    保证探测可用（口径注明"快照模式"），通道恢复后自动走 CDP 精确模式。"""
    import os as _os
    import subprocess
    import time as _t

    shot_path = _os.path.join(_os.getcwd(), "data", "screenshots",
                              f"synth_{int(_t.time())}.png")
    try:
        # 降权 chrome 写不了 data/screenshots——先落到 profile 再由 python 搬运
        tmp_shot = _os.path.join(profile_dir, "snap.png")
        from app.services.chrome_runner import chrome_popen

        extra = ["--ignore-certificate-errors"] if accept_self_signed else []
        p = chrome_popen(
            [chrome, "--headless=new", "--disable-gpu",
             f"--user-data-dir={profile_dir}", f"--screenshot={tmp_shot}",
             "--timeout=12000", "--window-size=1280,800", "--hide-scrollbars", *extra, url],
            profile_dir)
        p.wait(timeout=25)
        if _os.path.exists(tmp_shot):
            _os.replace(tmp_shot, shot_path)
        size = _os.path.getsize(shot_path) if _os.path.exists(shot_path) else 0
        total = round((_t.time() - t0) * 1000, 1)
        ok = size > 4000   # 快照有内容（>4KB 非纯背景）
        detail = (f"快照模式（浏览器调试通道异常，已降级）｜截图 {size / 1024:.0f}KB｜"
                  f"含浏览器启动 {total}ms")
        return (ok, total, 200 if ok else 0, detail,
                {"total_ms": total, "shot_bytes": size,
                 "screenshot": _os.path.basename(shot_path) if size else "",
                 "fallback": True})
    except Exception as e:
        # 失败也记录真实耗时：latency_ms 与 timing.total_ms 同源，
        # 否则结果表有实际耗时，瀑布弹窗却显示 0ms 总耗时
        elapsed = round((_t.time() - t0) * 1000, 1)
        return (False, elapsed, 0, f"快照降级失败: {type(e).__name__}",
                {"total_ms": elapsed})


def _check_browser_cdp(url: str, timeout: int, chrome: str,
                       accept_self_signed: bool = False) -> tuple:
    """CDP 探测主体（被 _check_browser 硬超时包裹）。"""
    import os as _os
    import shutil
    import subprocess
    import tempfile
    import time as _t
    import urllib.request as _ur

    shot_dir = _os.path.join(_os.getcwd(), "data", "screenshots")
    _os.makedirs(shot_dir, exist_ok=True)
    shot_path = _os.path.join(shot_dir, f"synth_{int(_t.time())}.png")
    profile_dir = tempfile.mkdtemp(prefix="qianshu-chrome-")
    port = 9400 + (_os.getpid() % 400)   # 随机端口避免并发冲突
    t0 = _t.time()
    proc = None
    try:
        from app.services.chrome_runner import chrome_popen

        extra_flags = ["--ignore-certificate-errors"] if accept_self_signed else []
        proc = chrome_popen(
            [chrome, "--headless=new", "--disable-gpu",
             f"--user-data-dir={profile_dir}", f"--remote-debugging-port={port}",
             "--remote-allow-origins=*", "--window-size=1280,800", *extra_flags, "about:blank"],
            profile_dir)
        # 等 CDP 就绪
        ws_url = ""
        for _ in range(30):
            _t.sleep(0.5)
            try:
                targets = _ur.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3).read()
                import json as _j

                pages = [t for t in _j.loads(targets) if t.get("type") == "page"]
                if pages:
                    ws_url = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                continue
        if not ws_url:
            return (False, round((_t.time() - t0) * 1000, 1), 0,
                    "浏览器调试端口未就绪", {"total_ms": round((_t.time() - t0) * 1000, 1)})
        t_cdp = _t.time()   # 浏览器进程启动 + 调试端口就绪（分段计时起点）

        import base64 as _b64
        import json as _j

        import websocket

        try:
            ws = websocket.create_connection(ws_url, timeout=8)
        except Exception:
            return _snapshot_fallback(chrome, profile_dir, url, t0,
                                      accept_self_signed=accept_self_signed)
        mid = [0]

        broken = [False]

        def _send(method, params=None):
            mid[0] += 1
            try:
                ws.send(_j.dumps({"id": mid[0], "method": method, "params": params or {}}))
            except Exception:
                broken[0] = True
                return {}
            while True:
                try:
                    msg = _j.loads(ws.recv())
                except Exception:
                    broken[0] = True
                    return {}   # 通道异常：置标志，调用方判定降级
                if msg.get("id") == mid[0]:
                    return msg.get("result", {})

        _send("Page.enable")
        if broken[0]:
            try:
                ws.close()
            except Exception:
                pass
            return _snapshot_fallback(chrome, profile_dir, url, t0,
                                      accept_self_signed=accept_self_signed)
        t_nav = _t.time()   # 通道就绪（ws 连接 + Page.enable 完成）
        _send("Page.navigate", {"url": url})
        if broken[0]:
            try:
                ws.close()
            except Exception:
                pass
            return _snapshot_fallback(chrome, profile_dir, url, t0,
                                      accept_self_signed=accept_self_signed)
        # 等渲染（文本就绪或超时）；0.2s 轮询，navigate_ms 粒度不被 0.5s 粗化
        dom = ""
        for _ in range(int(max(10, timeout) / 0.2)):
            _t.sleep(0.2)
            r = _send("Runtime.evaluate",
                      {"expression": "document.body ? document.body.innerText.length : 0",
                       "returnByValue": True})
            if broken[0]:
                try:
                    ws.close()
                except Exception:
                    pass
                return _snapshot_fallback(chrome, profile_dir, url, t0,
                                      accept_self_signed=accept_self_signed)
            if (r.get("result") or {}).get("value", 0) > 30:
                break
        t_render = _t.time()   # 导航到渲染就绪（首屏文字出现）
        r = _send("Runtime.evaluate",
                  {"expression": "document.documentElement.outerHTML", "returnByValue": True})
        dom = (r.get("result") or {}).get("value") or ""
        title_r = _send("Runtime.evaluate",
                        {"expression": "document.title", "returnByValue": True})
        title = ((title_r.get("result") or {}).get("value") or "")[:80]
        shot = _send("Page.captureScreenshot", {"format": "png"})
        size = 0
        if shot.get("data"):
            data = _b64.b64decode(shot["data"])
            size = len(data)
            open(shot_path, "wb").write(data)
        ws.close()
        t_end = _t.time()
        total = round((t_end - t0) * 1000, 1)
        ok = bool(dom) and size > 0
        startup = round((t_cdp - t0) * 1000, 1)
        setup = round((t_nav - t_cdp) * 1000, 1)
        nav = round((t_render - t_nav) * 1000, 1)
        dom_t = round((t_end - t_render) * 1000, 1)
        detail = (f"渲染完成｜标题「{title or '无'}」｜DOM {len(dom) / 1024:.1f}KB｜"
                  f"截图 {size / 1024:.0f}KB｜启动 {startup}ms · 导航 {nav}ms · "
                  f"DOM/截图 {dom_t}ms")
        timing = {"total_ms": total, "startup_ms": startup, "setup_ms": setup,
                  "navigate_ms": nav, "dom_ms": dom_t,
                  "dom_bytes": len(dom), "title": title,
                  "screenshot": _os.path.basename(shot_path) if size else "",
                  "shot_bytes": size}
        return (ok, total, 200 if ok else 0, detail, timing)
    except Exception as e:
        return (False, round((_t.time() - t0) * 1000, 1), 0,
                f"{type(e).__name__}: {str(e)[:100]}",
                {"total_ms": round((_t.time() - t0) * 1000, 1)})
    finally:
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(profile_dir, ignore_errors=True)


def _check_tcp(target: str, timeout: int) -> tuple:
    host, _, port = target.partition(":")
    port = int(port or 80)
    t0 = time.time()
    timing = {}
    try:
        t1 = time.time()
        addr = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)[0]
        timing["dns_ms"] = round((time.time() - t1) * 1000, 1)
        t1 = time.time()
        with socket.create_connection(addr[4], timeout=timeout):
            timing["tcp_ms"] = round((time.time() - t1) * 1000, 1)
        timing["total_ms"] = round((time.time() - t0) * 1000, 1)
        return (True, timing["total_ms"], 0,
                f"TCP {host}:{port} 可达｜DNS {timing['dns_ms']}ms · 建联 {timing['tcp_ms']}ms", timing)
    except Exception as e:
        timing["total_ms"] = round((time.time() - t0) * 1000, 1)
        return (False, timing["total_ms"], 0, f"{type(e).__name__}: {str(e)[:100]}", timing)


def _check_dns(target: str, timeout: int) -> tuple:
    """DNS 拨测：连测 3 次报最快/平均——
    单次会撞系统 resolver 重试（如 8.8.8.8 超时重试≈5s），只报单次会误导。"""
    samples = []
    ips = set()
    err = ""
    t_all = time.time()   # 失败也要有耗时，不能把"没解析出来"画成 0ms
    for _ in range(3):
        t0 = time.time()
        try:
            infos = socket.getaddrinfo(target, None, type=socket.SOCK_STREAM)
            samples.append((time.time() - t0) * 1000)
            ips |= {i[4][0] for i in infos}
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:80]}"
    if not samples:
        elapsed = round((time.time() - t_all) * 1000, 1)
        return (False, elapsed, 0, err or "解析失败", {"total_ms": elapsed})
    best = round(min(samples), 1)
    avg = round(sum(samples) / len(samples), 1)
    ips = sorted(ips)
    return (True, best, 0,
            f"解析 {len(ips)} 个 IP: {', '.join(ips[:3])}｜DNS 最快 {best}ms · 平均 {avg}ms"
            f"（3 次采样，平均含 resolver 重试）",
            {"dns_ms": best, "dns_avg_ms": avg, "total_ms": best, "samples": [round(x, 1) for x in samples]})


def run_check(db: Session, check: SynthCheck) -> SynthResult:
    """执行一次拨测并落库；连续失败转告警。"""
    accept_self_signed = bool(getattr(check, "accept_self_signed", False))   # 继承任务的自签证书设置
    if check.check_type == "browser":
        ok, latency, code, detail, timing = _check_browser(check.target, check.timeout_sec,
                                                           accept_self_signed=accept_self_signed)
    elif check.check_type == "tcp":
        ok, latency, code, detail, timing = _check_tcp(check.target, check.timeout_sec)
    elif check.check_type == "dns":
        ok, latency, code, detail, timing = _check_dns(check.target, check.timeout_sec)
    else:
        ok, latency, code, detail, timing = _check_http(check.target, check.timeout_sec,
                                                        check.expect_status,
                                                        accept_self_signed=accept_self_signed)

    import json as _json
    res = SynthResult(check_id=check.id, checked_at=datetime.now(), ok=ok,
                      latency_ms=round(latency, 1), status_code=code, detail=detail[:500],
                      timing=_json.dumps(timing, ensure_ascii=False))
    db.add(res)
    check.last_run_at = res.checked_at
    check.last_ok = ok
    check.last_latency_ms = res.latency_ms
    check.consecutive_fail = 0 if ok else (check.consecutive_fail or 0) + 1

    if not ok and check.consecutive_fail == FAIL_ALERT_THRESHOLD:
        _raise_alert(db, check, detail)
    elif ok:
        _close_alert(db, check)   # 恢复闭环：转好即置 recovered，告警不悬挂
    db.commit()
    return res


def _close_alert(db: Session, check: SynthCheck) -> None:
    """拨测恢复：把该目标 firing 的拨测告警置 recovered。"""
    try:
        from datetime import datetime as _dt

        from app.models.alert_record import AlertRecord

        (db.query(AlertRecord)
         .filter(AlertRecord.rule_id == 0, AlertRecord.rule_name == "拨测连续失败",
                 AlertRecord.target == check.target[:190], AlertRecord.status == "firing")
         .update({AlertRecord.status: "recovered",
                  AlertRecord.recovered_at: _dt.now()}, synchronize_session=False))
    except Exception as e:
        db.rollback()   # 吞掉异常前必须回滚，悬挂事务会持有写锁
        logger.info("拨测告警恢复处理跳过: %s", str(e)[:60])


def _raise_alert(db: Session, check: SynthCheck, detail: str) -> None:
    """连续失败转告警（rule_id=0 内置规则模式，同 host_watch）。"""
    try:
        from app.models.alert_record import AlertRecord

        rec = AlertRecord(
            rule_id=0, rule_name="拨测连续失败", source="synth", target=check.target[:190],
            target_name=(check.name or check.target)[:190], level="critical", status="firing",
            account_id=0, module="synth",
            title=f"拨测失败：{check.name or check.target}"[:250],
            content=f"{check.check_type} 拨测连续 {check.consecutive_fail} 次失败：{detail}",
        )
        db.add(rec)
        db.flush()
        # 走统一通知出口，确保拨测失败不仅留在平台内，还能按配置触达用户
        from app.services.notify import notify_all_channels
        res = notify_all_channels(
            db, rec.title,
            f"拨测连续 {check.consecutive_fail} 次失败，已在告警中心记录（可认领）。\n"
            f"- 类型: {check.check_type}\n- 目标: {check.target}\n- 详情: {detail}",
            level="critical", module="synth", account_id=0)
        if res.get("sent"):
            rec.notify_count = (rec.notify_count or 0) + 1
            rec.last_notified_at = _dt.now()
            db.commit()
        logger.warning("拨测连续失败告警（已通知）: %s", check.target)
    except Exception as e:
        db.rollback()
        logger.warning("拨测告警写入失败: %s", str(e)[:80])


def run_due_checks(db: Session) -> dict:
    """执行所有到期拨测任务（定时器调用）。"""
    now = datetime.now()
    ran = ok_n = 0
    for c in db.query(SynthCheck).filter(SynthCheck.enabled == True).all():  # noqa: E712
        due = c.last_run_at is None or (now - c.last_run_at).total_seconds() >= c.interval_sec
        if not due:
            continue
        try:
            r = run_check(db, c)
            ran += 1
            ok_n += 1 if r.ok else 0
        except Exception as e:
            logger.warning("拨测执行异常 %s: %s", c.target, str(e)[:80])
    return {"ran": ran, "ok": ok_n}
