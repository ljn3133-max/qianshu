#!/usr/bin/env bash
# 千枢前端开发服务器启动包装（vite，systemd ExecStart 用，仅开发机）。
# 生产形态不用本脚本：frontend/dist 由 nginx 托管（见 deploy/nginx/qianshu.conf）。
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # /opt/qianshu
cd "$APP_DIR/frontend"
if [ ! -x node_modules/.bin/vite ]; then
  echo "[千枢前端] node_modules 缺失，请先：cd frontend && npm ci" >&2
  exit 1
fi
exec node node_modules/.bin/vite --port "${QIANSHU_FRONTEND_PORT:-5273}" --host 0.0.0.0
