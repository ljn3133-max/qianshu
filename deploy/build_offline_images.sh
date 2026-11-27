#!/usr/bin/env bash
# 千枢 · 离线镜像构建（适用于 CentOS 7 无 python3 目标机的 compose 交付形态）
#
# 目标机没有 python3 时源码形态不可用，只能 docker/podman compose；离线包的
# `images/qianshu-images-<版本>.tar` 由本脚本在构建机产出，安装器 load 后直接 up。
#
# 用法：./deploy/build_offline_images.sh [--tag 0.3.0] [--out-dir dist]
#   产物：dist/qianshu-images-<tag>.tar（docker-archive，docker load / podman load 通用）
#         dist/qianshu-images-<tag>.tar.sha256
#   构建前用 .dockerignore 排除 .env/数据库/备份，构建后进镜像内复检（见 verify_image）。
# 注意：镜像内代码与前端 dist 是构建机快照；升级只换新包（新 tag），不靠目标机重建。
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
TAG=""
OUT_DIR="$APP_DIR/dist"

info() { echo -e "\033[32m[千枢镜像]\033[0m $*"; }
warn() { echo -e "\033[33m[千枢镜像]\033[0m $*"; }
die()  { echo -e "\033[31m[千枢镜像]\033[0m $*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:?--tag 需要版本号}"; shift 2 ;;
    --out-dir) OUT_DIR="${2:?--out-dir 需要目录}"; shift 2 ;;
    -h|--help) awk 'NR==1{next} /^set -euo pipefail/{exit} /^#/{sub(/^# ?/,"");print}' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

# 版本唯一来源：/api/settings/version 返回值（CHANGELOG 可能含 Unreleased，不用）
if [ -z "$TAG" ]; then
  TAG=$(grep -m1 -oE '"version": "[0-9]+\.[0-9]+\.[0-9]+"' \
        "$APP_DIR/backend/app/api/v1/settings.py" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true)
fi
[ -n "$TAG" ] || die "无法从 backend/app/api/v1/settings.py 读取版本，请用 --tag 指定"
info "镜像版本：$TAG"

# 运行时：podman 优先。部分发行版的 docker 是 podman shim，
# 不能直接假设其 save 参数与 podman save -m 的多镜像归档语义一致。
RT=""
if command -v podman >/dev/null 2>&1; then
  RT=podman
elif command -v docker >/dev/null 2>&1; then
  RT=docker
fi
[ -n "$RT" ] || die "未找到 docker/podman，无法构建镜像"

[ -f "$APP_DIR/backend/requirements.txt" ] || die "缺少 backend/requirements.txt"
[ -f "$APP_DIR/frontend/package.json" ] || die "缺少 frontend/package.json"

mkdir -p "$OUT_DIR"

# ---------- 1. 构建 ----------
# 后端必须使用离线包同一套 vendor wheel；requirements 未完全锁版本，
# 在线安装可能取得与源码形态不一致且未经验证的依赖。
VENDOR="${VENDOR_DIR:-$APP_DIR/dist/.vendor-cache}"
[ -d "$VENDOR" ] || VENDOR="$APP_DIR/dist/qianshu-offline/vendor"   # 兼容旧布局
[ -d "$VENDOR" ] && ls "$VENDOR"/*.whl >/dev/null 2>&1 \
  || die "缺少离线 wheel 目录：$VENDOR（先跑 deploy/build_offline.sh 生成 vendor/，或用 VENDOR_DIR= 指定）"
STAGE=$(mktemp -d /tmp/qianshu-image-build.XXXXXX)
trap 'rm -rf "$STAGE"' EXIT
info "准备构建上下文（backend + vendor wheel，零外网）..."
cp -a "$APP_DIR/backend/." "$STAGE/backend/"
cp -a "$VENDOR" "$STAGE/backend/vendor"
info "构建 backend 镜像（qianshu-backend:$TAG）..."
"$RT" build -t "qianshu-backend:$TAG" -f "$STAGE/backend/Dockerfile" "$STAGE/backend" \
  || die "backend 镜像构建失败"

# 前端用离线包预构建 dist（node 镜像与 npm ci 都需要外网，离线产物应与此 dist 完全一致）
[ -f "$APP_DIR/frontend/dist/index.html" ] \
  || die "缺少 frontend/dist（先跑 build_offline.sh 构建前端产物）"
info "构建 frontend 镜像（qianshu-frontend:$TAG，预构建 dist，不跑 npm）..."
"$RT" build -t "qianshu-frontend:$TAG" -f "$APP_DIR/deploy/Dockerfile.frontend-offline" "$APP_DIR/frontend" \
  || die "frontend 镜像构建失败"

# ---------- 2. 内容复检：密钥/运行库/日志绝不能被 COPY . . 带进镜像 ----------
verify_image() {  # $1=镜像名
  info "复检镜像内容（$1：不得含 .env/数据库/日志）..."
  # 判空使用显式 if：busybox ash 中 `[ -e x ] && exit 9` 为假时返回 1，
  # 配合 set -e 会把检查通过误判为失败退出。
  "$RT" run --rm --entrypoint sh "$1" -c '
set -e
for p in /app/.env /app/.env.bak /app/data; do
  if [ -e "$p" ]; then echo "镜像内发现禁止路径: $p" >&2; exit 9; fi
done
bad=$(find /app -maxdepth 3 \( -name ".env" -o -name ".env.*" -o -name "*.db" \
      -o -name "*.db-wal" -o -name "*.db-shm" -o -name "*.log" -o -name "*.bak*" \) \
      -print -quit 2>/dev/null || true)
if [ -n "$bad" ]; then echo "镜像内发现禁止文件: $bad" >&2; exit 9; fi
test -f /app/app/main.py
test -f /app/requirements.txt
' || die "镜像内容复检失败（$1）：疑似把运行数据/密钥打进镜像，已中止"
}

verify_image "qianshu-backend:$TAG"
# frontend 是 nginx 镜像，代码在 /usr/share/nginx/html；只校验产物存在且无源码环境文件
"$RT" run --rm --entrypoint sh "qianshu-frontend:$TAG" -c '
set -e
test -f /usr/share/nginx/html/index.html
if [ -e /usr/share/nginx/html/.env ]; then echo "镜像内发现 .env" >&2; exit 9; fi
' || die "frontend 镜像内容复检失败"

# ---------- 3. 导出 docker-archive ----------
# 无外网目标机无法访问 registry-1.docker.io，compose 的基础镜像
# （VictoriaMetrics/VictoriaLogs/otel/postgres）必须一并打进归档，否则
# load 千枢镜像后，up 仍会尝试拉取基础镜像。
# 版本与 docker-compose.yml 保持一致；BASE_IMAGES 可用环境变量覆盖。
BASE_IMAGES="${BASE_IMAGES:-victoriametrics/victoria-metrics:v1.102.0 victoriametrics/victoria-logs:v1.53.0 otel/opentelemetry-collector-contrib:0.96.0 postgres:16}"
for img in $BASE_IMAGES; do
  if ! "$RT" image inspect "$img" >/dev/null 2>&1; then
    info "本机缺少基础镜像 $img，拉取..."
    "$RT" pull "$img" || die "基础镜像拉取失败：$img（构建机需能访问 Docker Hub，或预先用 VENDOR 机导入）"
  fi
done

ARCHIVE="$OUT_DIR/qianshu-images-$TAG.tar"
rm -f "$ARCHIVE" "$ARCHIVE.sha256"
info "导出镜像归档 -> $ARCHIVE ..."
if [ "$RT" = "podman" ]; then
  # -m 必加：不加时 podman 会把后续镜像当作第一个镜像的额外 tag，
  # 使归档中的多个名称指向同一份镜像内容。
  podman save -m --format docker-archive -o "$ARCHIVE" \
    "qianshu-backend:$TAG" "qianshu-frontend:$TAG" $BASE_IMAGES || die "podman save 失败"
else
  docker save -o "$ARCHIVE" "qianshu-backend:$TAG" "qianshu-frontend:$TAG" $BASE_IMAGES || die "docker save 失败"
fi
# 归档结构自检：N 个镜像 = N 个 manifest 条目、N 份不同 Config。
# （podman 少 -m 时多个 tag 会指向同一镜像内容，归档大小正常也可能内容错误）
python3 - "$ARCHIVE" <<'PY' || die "镜像归档结构异常（条目/配置数不足，见上）"
import json, sys, tarfile
with tarfile.open(sys.argv[1]) as t:
    m = json.load(t.extractfile("manifest.json"))
configs = {e["Config"] for e in m}
print(f"归档自检: manifest 条目 {len(m)}，独立镜像配置 {len(configs)}")
if len(m) < 2 or len(configs) != len(m):
    sys.exit(1)
PY
( cd "$OUT_DIR" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE").sha256" )
SIZE=$(du -h "$ARCHIVE" | awk '{print $1}')
info "完成 ✔ $ARCHIVE（$SIZE，sha256 $(cut -c1-16 "$ARCHIVE.sha256")…）"
echo "  随包位置：images/$(basename "$ARCHIVE")（build_offline.sh --with-images 自动放入）"
