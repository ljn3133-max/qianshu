"""chrome 启动器：统一降权 nobody 运行（与 tests/quality/chrome_run.py 同源）。

Chrome 146 在 root + --no-sandbox 下会触发
`Failed global descriptor lookup: 7`（shared_memory_switch），导致网络请求挂起、
渲染级探测持续失败或截图异常；降权普通用户可避开该问题。
"""
from __future__ import annotations

import os
import pwd
import subprocess


def chrome_popen(chrome_args: list, profile_dir: str = "") -> subprocess.Popen:
    """以 nobody 身份启动 chrome（args 含可执行文件）。"""
    pw = pwd.getpwnam("nobody")
    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        os.chown(profile_dir, pw.pw_uid, pw.pw_gid)
    env = dict(os.environ)
    env["HOME"] = profile_dir or "/tmp/chrome-home"

    def _demote():
        os.setgid(pw.pw_gid)
        os.setuid(pw.pw_uid)

    return subprocess.Popen(
        chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, preexec_fn=_demote)
