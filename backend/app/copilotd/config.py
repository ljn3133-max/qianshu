"""qianshu-copilotd 配置：加载/首次生成 + 白名单校验。

配置文件默认 /opt/qianshu/config/qianshu-copilotd.json，缺失时按默认值首次生成
（0600）；token 文件缺失时启动生成（0600）。

白名单校验（config 层是"只管理千枢自身"的第一道机制，不是口号）：
  - start_cmd 只许指向千枢服务：python/uvicorn 起 app.main:app，路径参数必须落在
    platform_dir 内，拒绝 shell 元字符（无任意命令通道）；
  - api_url / listen_host 只许本机地址（仅本机生命周期通道）；
  - listen_port 禁占 8000（邻居服务）与 8810（平台自身端口）。
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import stat
from dataclasses import dataclass, field, fields
from urllib.parse import urlparse

DEFAULT_CONFIG_PATH = "/opt/qianshu/config/qianshu-copilotd.json"

# start_cmd 中出现任何一个即拒绝（防配置沦为任意命令通道）
_SHELL_META = re.compile(r"[;|&$`<>()\\\n\r]")
_PYTHON_BIN = re.compile(r"^(uvicorn|python|python3|python3\.\d+|python\.\d+)$")
_LOCAL_HOSTS = ("127.0.0.1", "localhost")
# 平台 ASGI 目标——"start_cmd 只能指向千枢服务"的识别标记
PLATFORM_APP_TARGET = "app.main:app"
PLATFORM_MARKER_FILE = os.path.join("backend", "app", "main.py")
# 绝不监听/触碰的端口：8000=邻居服务（其进程同样跑 app.main:app，端口才是分辨
# 依据），8810=千枢平台自身（由平台进程持有）
FORBIDDEN_LISTEN_PORTS = (8000, 8810)
FORBIDDEN_TOUCH_PORTS = (8000,)   # start/stop 拒绝指向的端口（邻居服务红线）


class ConfigError(ValueError):
    """配置不合法（白名单校验失败）。"""


@dataclass
class CopilotdConfig:
    platform_dir: str = "/opt/qianshu"
    start_cmd: str = "python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8810"
    workdir: str = ""                       # 缺省 = {platform_dir}/backend
    api_url: str = "http://127.0.0.1:8810"
    health_path: str = "/api/bootstrap/status"
    log_path: str = "/tmp/qianshu-backend.log"   # 平台进程日志（start_cmd 输出重定向到这里）
    token_file: str = "/opt/qianshu/config/qianshu-copilotd.token"
    actions_path: str = "/opt/qianshu/data/copilotd/actions.jsonl"
    listen_host: str = "127.0.0.1"
    listen_port: int = 18810
    check_interval: float = 15.0            # 看门狗自检周期（秒）
    auto_heal: bool = True
    debounce_checks: int = 2                # 连续不健康 N 次才认定故障（防抖）
    heal_backoff_base: float = 30.0         # 自动修复指数退避起步（秒）
    heal_backoff_max: float = 300.0
    start_timeout: float = 90.0             # 拉起后健康轮询上限（秒）
    disk_min_gb: float = 1.0                # 磁盘余量告警门槛
    bridge_token: str = ""                  # 调平台桥接口的 token；留空=从平台 .env 的
                                            # COPILOTD_BRIDGE_TOKEN 读取（单一事实源）

    @property
    def resolved_workdir(self) -> str:
        return self.workdir or os.path.join(self.platform_dir, "backend")

    @property
    def health_url(self) -> str:
        return self.api_url.rstrip("/") + (self.health_path if self.health_path.startswith("/")
                                           else "/" + self.health_path)


def validate(cfg: CopilotdConfig) -> CopilotdConfig:
    """白名单校验，失败抛 ConfigError（信息直白，指出违反哪条）。"""
    # --- platform_dir 必须是千枢平台目录（防把生命周期通道指向别处）---
    if not os.path.isdir(cfg.platform_dir):
        raise ConfigError(f"platform_dir 不存在: {cfg.platform_dir}")
    marker = os.path.join(cfg.platform_dir, PLATFORM_MARKER_FILE)
    if not os.path.isfile(marker):
        raise ConfigError(f"platform_dir 不是千枢平台目录（缺 {PLATFORM_MARKER_FILE}）: {cfg.platform_dir}")

    # --- start_cmd 白名单 ---
    raw = cfg.start_cmd.strip()
    if not raw:
        raise ConfigError("start_cmd 为空")
    if _SHELL_META.search(raw):
        raise ConfigError(f"start_cmd 含 shell 元字符，拒绝（无任意命令通道）: {raw}")
    try:
        argv = shlex.split(raw)
    except ValueError as e:
        raise ConfigError(f"start_cmd 解析失败: {e}")
    if not argv:
        raise ConfigError("start_cmd 为空")
    if not _PYTHON_BIN.match(os.path.basename(argv[0])):
        raise ConfigError(f"start_cmd 首个参数必须是 python/uvicorn: {argv[0]}")
    if PLATFORM_APP_TARGET not in argv:
        raise ConfigError(f"start_cmd 必须启动千枢服务（{PLATFORM_APP_TARGET}）: {raw}")
    platform_real = os.path.realpath(cfg.platform_dir)
    for tok in argv[1:]:
        if "/" not in tok:
            continue
        # 允许 python 解释器绝对路径；其余路径参数必须落在 platform_dir 内
        if _PYTHON_BIN.match(os.path.basename(tok)) and os.path.isabs(tok):
            continue
        real = os.path.realpath(tok if os.path.isabs(tok)
                                else os.path.join(cfg.resolved_workdir, tok))
        if not (real == platform_real or real.startswith(platform_real + os.sep)):
            raise ConfigError(f"start_cmd 路径参数越出 platform_dir: {tok}")

    # --- api_url：只许本机（daemon 只管理本机千枢）---
    parsed = urlparse(cfg.api_url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(f"api_url 必须是 http(s): {cfg.api_url}")
    if parsed.hostname not in _LOCAL_HOSTS:
        raise ConfigError(f"api_url 只许本机地址: {cfg.api_url}")
    if not cfg.health_path.startswith("/"):
        raise ConfigError(f"health_path 必须以 / 开头: {cfg.health_path}")
    api_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if api_port in FORBIDDEN_TOUCH_PORTS:
        raise ConfigError(f"api_url 端口 {api_port} 是邻居服务，禁止管理")
    m = re.search(r"--port\s+(\d+)", raw)
    if m and int(m.group(1)) != api_port:
        raise ConfigError(
            f"start_cmd 的 --port {m.group(1)} 与 api_url 端口 {api_port} 不一致"
            "（必须指向同一千枢实例，防止错管到邻居服务）")

    # --- 生命周期通道只许本机监听，禁占邻居/平台端口 ---
    if cfg.listen_host not in _LOCAL_HOSTS:
        raise ConfigError(f"listen_host 只许本机: {cfg.listen_host}")
    if not (1 <= int(cfg.listen_port) <= 65535):
        raise ConfigError(f"listen_port 不合法: {cfg.listen_port}")
    if int(cfg.listen_port) in FORBIDDEN_LISTEN_PORTS:
        raise ConfigError(f"listen_port 禁用 {cfg.listen_port}（8000=邻居服务 / 8810=千枢自身）")

    if cfg.check_interval < 2:
        raise ConfigError("check_interval 不得小于 2 秒")
    if cfg.debounce_checks < 1:
        raise ConfigError("debounce_checks 不得小于 1")
    return cfg


def load_or_create(path: str = DEFAULT_CONFIG_PATH) -> CopilotdConfig:
    """读配置；缺失按默认值首次生成（0600），随后统一走白名单校验。"""
    if not os.path.isfile(path):
        cfg = CopilotdConfig()
        _write_config(path, cfg)
    else:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        known = {f.name for f in fields(CopilotdConfig)}
        cfg = CopilotdConfig(**{k: v for k, v in data.items() if k in known})
    return validate(cfg)


def _write_config(path: str, cfg: CopilotdConfig) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {f.name: getattr(cfg, f.name) for f in fields(CopilotdConfig)}
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def ensure_token(token_file: str) -> str:
    """token 文件 0600；缺失时启动生成 64 位 hex。返回当前 token。"""
    os.makedirs(os.path.dirname(token_file) or ".", exist_ok=True)
    if not os.path.isfile(token_file):
        token = secrets.token_hex(32)
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        return token
    os.chmod(token_file, stat.S_IRUSR | stat.S_IWUSR)   # 收紧到 0600
    with open(token_file, "r", encoding="utf-8") as f:
        return f.read().strip()


def resolve_bridge_token(cfg: CopilotdConfig) -> str:
    """调平台桥接口的 token：配置显式优先，否则读平台 backend/.env 的
    COPILOTD_BRIDGE_TOKEN（单一事实源，避免两处配置漂移）。"""
    if cfg.bridge_token:
        return cfg.bridge_token
    env_path = os.path.join(cfg.resolved_workdir, ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("COPILOTD_BRIDGE_TOKEN="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""
