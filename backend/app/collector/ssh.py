"""SSH 执行器：控制机→节点的远程命令执行（添加节点/清空/备份）。

安全：只连任务登记的目标机；命令由任务模板生成，不执行 LLM 生成的命令；
实时输出按行回调（任务日志流）。
"""
from __future__ import annotations

import logging
import subprocess
import threading

logger = logging.getLogger("qianshu.ssh")


class SSHError(RuntimeError):
    pass


def run_ssh(host: str, user: str, command: str, on_line=None, timeout: int = 600,
            password: str = "") -> dict:
    """执行远程命令（key 免密优先；password 走 sshpass 仅在提供时）。on_line 逐行回调。

    命令经 bash -o pipefail -s 从 stdin 执行，保留真实退出码：
    `kubeadm init | tail` 类管道不能被 tail 的 0 吞掉真实失败。
    stdin 传脚本同时规避引号/嵌套 heredoc 的转义问题。
    """
    remote = ["bash", "-o", "pipefail", "-s"]
    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "ConnectTimeout=8", f"{user}@{host}"] + remote
    if password:
        ssh_cmd = ["sshpass", "-p", password, "ssh",
                   "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
                   "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=8",
                   f"{user}@{host}"] + remote
    out_lines = []
    try:
        proc = subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE, stdin=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        try:
            proc.stdin.write(command + "\n")
            proc.stdin.close()
        except Exception:
            pass   # 远端秒退时 stdin 可能已关，不影响结果判定
        for line in proc.stdout:
            line = line.rstrip()
            out_lines.append(line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    pass
        proc.wait(timeout=timeout)
        return {"ok": proc.returncode == 0, "code": proc.returncode,
                "lines": out_lines[-2000:]}
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"ok": False, "code": -1, "lines": out_lines[-2000:], "error": "执行超时"}
    except Exception as e:
        return {"ok": False, "code": -1, "lines": out_lines[-2000:], "error": str(e)[:200]}


def ping_host(host: str, user: str = "root", password: str = "") -> dict:
    return run_ssh(host, user, "echo ok && hostname && uname -m", timeout=15, password=password)
