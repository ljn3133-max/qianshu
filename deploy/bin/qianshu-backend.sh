#!/usr/bin/env bash
# 千枢后端启动包装（systemd ExecStart 用）：
#   优先 backend/.venv/bin/python（install.sh --mode source 建的 venv），
#   否则回落系统 python3（开发态/离线包）。端口可用 QIANSHU_PORT 覆盖（默认 8810）。
# 默认仅监听回环地址，由同机 nginx 反代；直连场景须显式设置 QIANSHU_BACKEND_HOST。
# 真实 IP 纵深防御第二层：--proxy-headers + --forwarded-allow-ips（只信可信代理段的
# X-Forwarded-*）。可信段可用环境变量 FORWARDED_ALLOW_IPS 或 backend/.env 同名项覆盖。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # /opt/qianshu
cd "$APP_DIR/backend"
PY=/usr/bin/python3
if [ -x "$APP_DIR/backend/.venv/bin/python" ]; then
  PY="$APP_DIR/backend/.venv/bin/python"
fi

# 可信代理段：环境变量优先，其次 backend/.env，最后默认（本机+RFC1918 内网段）
FWD_ALLOW="${FORWARDED_ALLOW_IPS:-}"
if [ -z "$FWD_ALLOW" ] && [ -f .env ]; then
  FWD_ALLOW=$(grep -E '^FORWARDED_ALLOW_IPS=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"'' || true)
fi
FWD_ALLOW="${FWD_ALLOW:-127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"

BACKEND_HOST="${QIANSHU_BACKEND_HOST:-}"
if [ -z "$BACKEND_HOST" ] && [ -f .env ]; then
  BACKEND_HOST=$(grep -E '^QIANSHU_BACKEND_HOST=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"'' || true)
fi
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"

exec "$PY" -m uvicorn app.main:app --host "$BACKEND_HOST" --port "${QIANSHU_PORT:-8810}" \
  --proxy-headers --forwarded-allow-ips="$FWD_ALLOW" "$@"
