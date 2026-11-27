#!/usr/bin/env python3
"""千枢轻量 Agent：单文件、零依赖（仅标准库）。

用途：云外机器（IDC/托管）接入千枢——主机指标 + 进程 top + 资产信息上报。
安全定位（产品红线延伸）：**只上报不执行**——没有任何远程命令通道；
注册 token 单向（平台发 token，agent 只持有）；支持 HTTPS。

用法：
  python3 qianshu-agent.py --server http://<千枢>:8810 --token <注册token> [--interval 60]

指标语义对齐阿里云 CMS（cpu/memory/disk），诊断工具与画像引擎零改动复用。
"""
import argparse
import json
import os
import platform
import socket
import subprocess
import time
import urllib.request

GB = 1024 ** 3


def collect_host() -> dict:
    """主机指标（标准库实现，无第三方依赖）。"""
    out = {"hostname": socket.gethostname(), "os": platform.platform(),
           "arch": platform.machine()}
    # CPU 使用率：两次采样 /proc/stat
    def _cpu():
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(x) for x in parts]
        return sum(vals), vals[3] + (vals[4] if len(vals) > 4 else 0)

    t1, i1 = _cpu()
    time.sleep(0.3)
    t2, i2 = _cpu()
    out["cpu_pct"] = round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)

    # 内存
    if os.name == "nt":
        try:
            r = subprocess.run(["wmic", "OS", "get", "TotalVisibleMemorySize,FreePhysicalMemory", "/VALUE"],
                               capture_output=True, text=True, timeout=8)
            kv = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
            total_kb = int(kv.get("TotalVisibleMemorySize", 0))
            free_kb = int(kv.get("FreePhysicalMemory", 0))
            out["mem_pct"] = round(100 * (1 - free_kb / max(1, total_kb)), 1)
            out["mem_total_gb"] = round(total_kb / 1024 / 1024, 1)
            return out
        except Exception:
            pass
    meminfo = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            meminfo[k] = int(v.strip().split()[0])  # kB
    total = meminfo.get("MemTotal", 1)
    out["mem_pct"] = round(100 * (1 - meminfo.get("MemAvailable", 0) / total), 1)
    out["mem_total_gb"] = round(total / 1024 / 1024, 1)

    # 磁盘分区
    disks = []
    try:
        r = subprocess.run(["df", "-kP"], capture_output=True, text=True, timeout=5)
        for line in r.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 6 and not parts[5].startswith(("/proc", "/sys", "/dev")):
                size_kb, used_kb = int(parts[1]), int(parts[2])
                disks.append({"mount": parts[5], "total_gb": round(size_kb / 1024 / 1024, 1),
                              "used_pct": round(100 * used_kb / max(1, size_kb), 1)})
    except Exception:
        pass
    out["disks"] = disks[:16]
    out["disk_pct"] = max((d["used_pct"] for d in disks), default=0)
    return out


def collect_processes(top_n: int = 10) -> list:
    """进程 top（语义对齐 CMS process.cpu/process.memory，画像引擎复用）。
    Linux 走 ps；Windows 走 tasklist。"""
    procs = []
    try:
        if os.name == "nt":
            r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=8)
            for line in r.stdout.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 5:
                    try:
                        mem_kb = int(parts[4].replace(".", "").replace(",", "").replace('"', "")
                                     .replace(" K", ""))
                    except Exception:
                        mem_kb = 0
                    procs.append({"cpu_pct": 0.0, "mem_pct": 0.0,
                                  "rss_mb": round(mem_kb / 1024, 1), "name": parts[0]})
            procs.sort(key=lambda p: -p["rss_mb"])
        else:
            r = subprocess.run(["ps", "-eo", "pcpu,pmem,rss,comm", "--no-headers", "-a"],
                               capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                parts = line.split(None, 3)
                if len(parts) == 4:
                    procs.append({"cpu_pct": float(parts[0]), "mem_pct": float(parts[1]),
                                  "rss_mb": round(int(parts[2]) / 1024, 1), "name": parts[3].strip()})
            procs.sort(key=lambda p: -p["cpu_pct"])
    except Exception:
        pass
    return procs[:top_n]


def collect_file_logs(paths: list, tail_lines: int = 50) -> list:
    """文件日志采集：每个匹配路径取尾部 N 行。
    用法：--log "/var/log/app/*.log"（可多个）。只读尾部，不做全量搬运。"""
    import glob

    out = []
    for pat in paths[:5]:
        for f in glob.glob(pat)[:5]:
            try:
                with open(f, "r", errors="replace") as fh:
                    lines = fh.readlines()[-tail_lines:]
                out.append({"file": f, "lines": lines})
            except Exception:
                continue
    return out


def report(server: str, token: str, payload: dict) -> bool:
    req = urllib.request.Request(
        f"{server.rstrip('/')}/api/agent/report",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Agent-Token": token},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"[agent] 上报失败: {e}")
        return False


AGENT_VERSION = "1.1.0"


def install_service(server: str, token: str, interval: int) -> None:
    """服务化安装：Linux=systemd unit / Windows=计划任务。
    只上报不执行的安全定位不变。"""
    import platform as _pf

    script = os.path.abspath(__file__)
    if _pf.system() == "Windows":
        # Windows：注册计划任务（开机自启+每分钟上报由 agent 自身循环保证）
        cmd = (f'schtasks /Create /TN "QianShuAgent" /SC ONSTART '
               f'/TR "python.exe {script} --server {server} --token {token} --interval {interval}" /F')
        print("[agent] Windows 计划任务安装命令（管理员运行）：")
        print("   ", cmd)
        print("[agent] 卸载：schtasks /Delete /TN QianShuAgent /F")
    else:
        unit = f"""[Unit]
Description=QianShu Monitoring Agent (report-only)
After=network.target

[Service]
ExecStart=/usr/bin/python3 {script} --server {server} --token {token} --interval {interval}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
        path = "/etc/systemd/system/qianshu-agent.service"
        print("[agent] systemd 安装（需 root）：")
        print(f"   sudo tee {path} <<'UNIT'")
        print(unit)
        print("UNIT")
        print(f"   sudo systemctl daemon-reload && sudo systemctl enable --now qianshu-agent")
        print("[agent] 卸载：sudo systemctl disable --now qianshu-agent && sudo rm -f", path)


def check_upgrade(server: str) -> None:
    """升级检查：服务端版本 vs 本地版本。"""
    try:
        with urllib.request.urlopen(f"{server.rstrip('/')}/api/settings/public/version", timeout=10) as r:
            remote = json.loads(r.read())
        rv = remote.get("agent_version", "")
        if rv and rv != AGENT_VERSION:
            print(f"[agent] 发现新版本 {rv}（本地 {AGENT_VERSION}）——下载新版替换脚本后重启服务")
        else:
            print(f"[agent] 已是最新版本 {AGENT_VERSION}")
    except Exception as e:
        print(f"[agent] 版本检查失败: {e}")


def main():
    ap = argparse.ArgumentParser(description="千枢轻量 Agent（只上报不执行）")
    ap.add_argument("--server", required=True, help="千枢地址，如 http://10.0.0.5:8810")
    ap.add_argument("--token", default="", help="注册 token（平台生成；--upgrade-check 可不填）")
    ap.add_argument("--interval", type=int, default=60, help="上报周期秒")
    ap.add_argument("--log", action="append", default=[],
                    help="文件日志路径通配（可多个）：--log '/var/log/app/*.log'")
    ap.add_argument("--install", action="store_true",
                    help="服务化安装（Linux systemd / Windows 计划任务），输出安装命令")
    ap.add_argument("--upgrade-check", action="store_true",
                    help="检查新版本（服务端 vs 本地）")
    ap.add_argument("--version", action="version", version=f"qianshu-agent {AGENT_VERSION}")
    args = ap.parse_args()

    if not args.token and not args.upgrade_check:
        ap.error("该模式需要 --token")
    if args.install:
        install_service(args.server, args.token, args.interval)
        return
    if args.upgrade_check:
        check_upgrade(args.server)
        return

    print(f"[agent] 启动: {socket.gethostname()} -> {args.server}（每 {args.interval}s，v{AGENT_VERSION}）")
    while True:
        payload = {"host": collect_host(), "processes": collect_processes(),
                   "logs": collect_file_logs(args.log) if args.log else [],
                   "ts": time.time()}
        ok = report(args.server, args.token, payload)
        print(f"[agent] 上报{'成功' if ok else '失败'} {payload['host']['cpu_pct']}% "
              f"mem {payload['host']['mem_pct']}%")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
