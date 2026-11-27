#!/usr/bin/env bash
# 千枢 · 多架构镜像构建：x86_64 + ARM64（鲲鹏/飞腾）
# 用法：./build-images.sh [tag] [registry]
# 龙芯 loongarch 按订单（基础镜像与依赖工具链另配）
set -euo pipefail
cd "$(dirname "$0")"
TAG="${1:-latest}"
REG="${2:-}"

build() {
  local name=$1 dir=$2
  local target="${REG:+$REG/}qianshu-$name:$TAG"
  echo "[千枢构建] $target (linux/amd64 + linux/arm64)"
  docker buildx build --platform linux/amd64,linux/arm64 \
    -t "$target" --push=false "$dir"
}
build backend backend
build frontend frontend
echo "完成。推送到私有仓库：docker buildx build --platform ... --push $REG/qianshu-backend:$TAG"
