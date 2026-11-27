"""chrome 启动器：统一 chrome 的启动方式。

Chrome 146 在 root + --no-sandbox 下触发
`Failed global descriptor lookup: 7`（shared_memory_switch），网络请求全部吊死——
页面永不加载、evaluate 永不回包。降权到普通用户（nobody）后网络立即正常。
普通用户（本地开发或非特权 CI runner）本身不受该问题影响，保持当前用户启动即可。

所有测试/探测的 chrome 启动一律走这里，禁止各自再写 Popen。
"""
import os
import pwd
import subprocess


def _demote_fn(uid: int, gid: int):
    def _fn():
        os.setgid(gid)
        os.setuid(uid)
    return _fn


def _demote_target():
    """root 时返回 (uid, gid) 降权目标；普通用户或系统无 nobody 时返回 None。"""
    if os.geteuid() != 0:
        return None
    try:
        pw = pwd.getpwnam("nobody")
    except KeyError:
        return None
    return pw.pw_uid, pw.pw_gid


def chrome_popen(chrome_args: list, profile_dir: str = "") -> subprocess.Popen:
    """启动 chrome（args 含 chrome 可执行文件）。profile_dir 需可写。"""
    target = _demote_target()
    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        if target:
            # 仅 root 需要把 profile 交给降权用户；普通用户自己就拥有它。
            uid, gid = target
            os.chown(profile_dir, uid, gid)
            for root, dirs, _files in os.walk(profile_dir):
                for d in dirs:
                    try:
                        os.chown(os.path.join(root, d), uid, gid)
                    except Exception:
                        pass
    env = dict(os.environ)
    env["HOME"] = profile_dir or "/tmp/chrome-home"
    preexec = _demote_fn(*target) if target else None
    return subprocess.Popen(
        chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env, preexec_fn=preexec)
