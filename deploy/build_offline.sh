#!/usr/bin/env bash
# 千枢 · 自包含离线包构建（"二进制"部署形态的主交付，docs/active/deployment.md §2.3）
# 用法：./deploy/build_offline.sh [--with-images] [--pyinstaller]
#   产出 dist/qianshu-offline.tar.gz：源码 + vendor/ 依赖 wheel 全闭包（目标机零外网零编译）
#   + 预构建 frontend/dist + install.sh + docker-compose.yml + 配置模板。
#   目标机仅需 python3.9+（源码形态另需 nginx，OS 仓库自带）：
#     tar xzf qianshu-offline.tar.gz && cd qianshu-offline && ./install.sh --mode source # 有 python3 的机器
#   --with-images  compose 形态专用（无 python3 的机器，如 CentOS 7）：
#     调 deploy/build_offline_images.sh 产出 qianshu-images-<版本>.tar 放进 images/，
#     安装器自动 load 后直接 up，不在目标机构建（目标机通常无外网、也无法编译依赖）。
#   --pyinstaller  额外产出单文件 qianshu-backend（备用形态，见 §2.3.1 四条不采用理由）
set -euo pipefail

cd "$(dirname "$0")/.."          # /opt/qianshu
APP_DIR="$(pwd)"
OUT_DIR="$APP_DIR/dist"
PKG_NAME="qianshu-offline"
# wheel 缓存必须放在包目录之外：包目录每次清场重建，缓存放里面会让 --skip-vendor
# 和重打包只能复用到空目录。
VENDOR_DIR="$OUT_DIR/.vendor-cache"

info() { echo -e "\033[32m[千枢离线包]\033[0m $*"; }
warn() { echo -e "\033[33m[千枢离线包]\033[0m $*"; }
die()  { echo -e "\033[31m[千枢离线包]\033[0m $*" >&2; exit 1; }

WITH_PYI=0
WITH_IMAGES=0
SKIP_FE=0
SKIP_VENDOR=0
while [ $# -gt 0 ]; do
  case "$1" in
    --pyinstaller) WITH_PYI=1; shift ;;
    --with-images) WITH_IMAGES=1; shift ;;
    --skip-frontend-build) SKIP_FE=1; shift ;;
    --skip-vendor) SKIP_VENDOR=1; shift ;;
    -h|--help) awk 'NR==1{next} /^set -euo pipefail/{exit} /^#/{sub(/^# ?/,"");print}' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

# ---------- 0. 清场 + 目录 ----------
rm -rf "$OUT_DIR/$PKG_NAME"
mkdir -p "$OUT_DIR/$PKG_NAME" "$VENDOR_DIR"

# ---------- 1. vendor 依赖轮子（目标机零外网、零编译） ----------
# 复用规则：requirements.txt 与构建机 Python 主版本都没变时才复用缓存（内容寻址），
# 不靠人记；--skip-vendor 仅在明确知道没变时强制复用（不匹配则仍重建，避免打错包）。
REQ_SHA=$(sha256sum "$APP_DIR/backend/requirements.txt" | cut -d' ' -f1)
PY_BASE=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
CACHE_MARK="$VENDOR_DIR/.cache-key"
CACHE_KEY="$REQ_SHA-py$PY_BASE"
vendor_ok=0
if ls "$VENDOR_DIR"/*.whl >/dev/null 2>&1 && [ -f "$CACHE_MARK" ] \
   && [ "$(cat "$CACHE_MARK")" = "$CACHE_KEY" ]; then
  vendor_ok=1
fi
if [ "$vendor_ok" = "1" ]; then
  info "复用 wheel 缓存（$CACHE_KEY）"
elif [ "$SKIP_VENDOR" = "1" ]; then
  die "--skip-vendor 但缓存与 requirements.txt/Python 版本不匹配（缓存 $(cat "$CACHE_MARK" 2>/dev/null || echo 无) ≠ $CACHE_KEY）；去掉该参数重建"
else
# 1a. 全量闭包转 wheel：纯 Python sdist（如 aliyun-log-python-sdk 无官方 wheel）
#     在构建机现场构建成 py3-none-any wheel——纯 Python 构建不需要 C 编译器；
#     C 扩展依赖（cryptography/bcrypt/pydantic-core 等）都有 manylinux 官方轮。
rm -rf "$VENDOR_DIR"/*.whl "$VENDOR_DIR"/*.tar.gz "$CACHE_MARK"   # 防止旧版本 wheel 混入
info "构建依赖 wheel 闭包 -> $VENDOR_DIR ..."
python3 -m pip wheel -q -r "$APP_DIR/backend/requirements.txt" -w "$VENDOR_DIR" \
  || die "pip wheel 失败（检查构建机外网/镜像源；C 扩展轮需构建机=linux x86_64）"

# 1b. 跨 Python minor 版本补 C 扩展轮（默认 3.10/3.11/3.12，WHEEL_PYVERS 可覆盖）：
#     从 1a 产出的 cp 标签 wheel 提取 **包名+精确版本**，逐包同版本 --no-deps 补齐。
#     只按包名补最新版会拆散依赖闭包；例如 pydantic 2.13.5 要求 pydantic-core==2.46.5，
#     若按包名补到 2.50.4，Python 3.11 镜像会安装失败。
#     逐包=个别包缺轮不拖垮整批；py3-none-any 轮全版本通用，无需补。
#     个别包（如 crcmod）在 PyPI 无任何预编译轮：回退把**同版本** sdist 放进 vendor
#     （目标机 3.10+ 装到它时需 gcc；3.9 目标机用 1a 的本地 wheel 零编译）。
BIN_SPECS=$(python3 - "$VENDOR_DIR" <<'PY'
import os, sys
specs = {}
for f in sorted(os.listdir(sys.argv[1])):
    if not f.endswith(".whl") or "-cp" not in f:
        continue
    parts = f[:-4].split("-")
    specs[parts[0]] = parts[1]
for name in sorted(specs):
    print(f"{name}=={specs[name]}")
PY
)
[ -n "$BIN_SPECS" ] || die "vendor/ 里没有 C 扩展 wheel（1a 应产出 cryptography/pydantic-core 等）"
MISSING=""
for SPEC in $BIN_SPECS; do
  for V in ${WHEEL_PYVERS:-310 311 312}; do
    python3 -m pip download -q --only-binary=:all: --no-deps \
      --python-version "$V" --implementation cp --abi "cp$V" \
      --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
      "$SPEC" -d "$VENDOR_DIR" 2>/dev/null || MISSING="$MISSING $SPEC@$V"
  done
done
for SPEC in $BIN_SPECS; do
  case "$MISSING" in
    *"$SPEC@"*) python3 -m pip download -q --no-binary :all: --no-deps "$SPEC" -d "$VENDOR_DIR" 2>/dev/null || true ;;
  esac
done
[ -n "$MISSING" ] && warn "以下包无对应版本预编译轮，已回退放同版本 sdist（目标机装到时需 gcc；$MISSING）"
true
fi

# 1c. 完整性自检：requirements 每个依赖在 vendor/ 都有对应产物，
#     任一 wheel 缺失都会导致目标机离线安装失败。
info "自检 vendor/ 覆盖 requirements..."
python3 - "$APP_DIR/backend/requirements.txt" "$VENDOR_DIR" <<'PY' \
  || die "vendor/ 轮子不全（缺件见上）——重跑本脚本；若刚改过 requirements.txt 请先稳定再打包"
import os
import re
import sys


def norm(s: str) -> str:
    return re.sub(r"[-_.]+", "-", s).lower()


req_file, vendor = sys.argv[1], sys.argv[2]
artifacts = {norm(f.split("-")[0]) for f in os.listdir(vendor)}
names = []
for ln in open(req_file, encoding="utf-8"):
    ln = ln.split("#", 1)[0].strip()
    if not ln or ln.startswith("-"):
        continue
    name = re.split(r"[<>=!~;\[\s]", ln, maxsplit=1)[0].strip()
    if name:
        names.append(name)
missing = [n for n in names if norm(n) not in artifacts]
if missing:
    print("缺少轮子: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
print(f"覆盖检查通过: requirements {len(names)} 项，vendor 产物 {len(artifacts)} 类")
PY
# 自检通过后才写缓存键：失败的下次必须重建，不能把半成品当缓存复用
echo "$CACHE_KEY" > "$CACHE_MARK"

# ---------- 2. 前端产物（目标机不装 node） ----------
# 默认每次都重建：只在 dist 缺失时构建会把旧页面打进包。
# --skip-frontend-build 仅用于确认 dist 与源码一致的重复打包场景。
if [ "$SKIP_FE" = "1" ] && [ -f "$APP_DIR/frontend/dist/index.html" ]; then
  warn "跳过前端构建：使用现有 dist（请自行确认已包含本次源码改动）"
else
  info "构建前端 dist（目标机无 node，必须预构建）..."
  (cd "$APP_DIR/frontend" && npm ci --no-audit --no-fund && npm run build) || die "前端构建失败"
fi

# ---------- 3. 组装包 ----------
info "组装离线包..."
cp -a "$APP_DIR/backend" "$OUT_DIR/$PKG_NAME/backend"
# 绝不打包运行数据/密钥/备份（产品包只含代码与依赖）：
# qianshu.db* = 业务库+WAL（含客户数据/加密凭证）；.env / .env.bak* = 密钥与其历史备份；
# *.bak* / *.log = 其他备份与日志。必须用 find 删除：glob 不匹配点文件，会漏掉 .env.bak*。
rm -rf "$OUT_DIR/$PKG_NAME/backend/.venv" "$OUT_DIR/$PKG_NAME/backend/data" 2>/dev/null || true
rm -f "$OUT_DIR/$PKG_NAME/backend/.wtest"
find "$OUT_DIR/$PKG_NAME/backend" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$OUT_DIR/$PKG_NAME/backend" -type f \( -name "qianshu.db*" -o -name ".env" -o -name ".env.*" \
      -o -name "*.bak*" -o -name "*.log" -o -name "*.lock" -o -name ".wtest" \) -delete
# vendor/ 位于包根（install.sh 按 $APP_DIR/vendor 装依赖），从缓存复制并剔除缓存键文件
mkdir -p "$OUT_DIR/$PKG_NAME/vendor"
cp -a "$VENDOR_DIR"/. "$OUT_DIR/$PKG_NAME/vendor/"
rm -f "$OUT_DIR/$PKG_NAME/vendor/.cache-key"
cp -a "$APP_DIR/frontend/dist" "$OUT_DIR/$PKG_NAME/frontend-dist"
mkdir -p "$OUT_DIR/$PKG_NAME/frontend"
mv "$OUT_DIR/$PKG_NAME/frontend-dist" "$OUT_DIR/$PKG_NAME/frontend/dist"
cp -a "$APP_DIR/config" "$OUT_DIR/$PKG_NAME/config"
# 生命周期通道 token 不随包分发（0600 本机密钥；装机时 copilotd 首启自动生成新 token）
rm -f "$OUT_DIR/$PKG_NAME/config/qianshu-copilotd.token"
cp -a "$APP_DIR/deploy" "$OUT_DIR/$PKG_NAME/deploy"
mkdir -p "$OUT_DIR/$PKG_NAME/docs"
# 只分发当前公开文档；本机归档、临时验收截图与摘要不得进入发布包。
cp -a "$APP_DIR/docs/active" "$OUT_DIR/$PKG_NAME/docs/active"
# 版本化升级 SQL 随包分发（后端启动时按版本自动应用；只含 SQL/README，不含数据）
cp -a "$APP_DIR/update-data" "$OUT_DIR/$PKG_NAME/update-data"
cp "$APP_DIR/install.sh" "$APP_DIR/docker-compose.yml" "$APP_DIR/README.md" \
   "$APP_DIR/.env.example" "$APP_DIR/upgrade.sh" "$OUT_DIR/$PKG_NAME/"
# 开源交付物随包分发（许可证/变更记录/协作契约；不存在则跳过，不阻断打包）
for f in LICENSE README.en.md CHANGELOG.md SECURITY.md CONTRIBUTING.md CODE_OF_CONDUCT.md; do
  [ -f "$APP_DIR/$f" ] && cp "$APP_DIR/$f" "$OUT_DIR/$PKG_NAME/"
done
# compose 的 frontend 服务 build ./frontend 需要 Dockerfile+nginx.conf（dist 已预构建，
# 仍保留源以便 --build 走缓存层）
cp "$APP_DIR/frontend/Dockerfile" "$APP_DIR/frontend/nginx.conf" "$APP_DIR/frontend/package.json" \
   "$APP_DIR/frontend/package-lock.json" "$OUT_DIR/$PKG_NAME/frontend/"
[ -f "$APP_DIR/frontend/.dockerignore" ] && cp "$APP_DIR/frontend/.dockerignore" "$OUT_DIR/$PKG_NAME/frontend/"
chmod +x "$OUT_DIR/$PKG_NAME/install.sh" "$OUT_DIR/$PKG_NAME/upgrade.sh"

# ---------- 3b. （可选）离线镜像 ----------
if [ "$WITH_IMAGES" = "1" ]; then
  info "构建离线镜像（backend/frontend，目标机免构建、免外网）..."
  VENDOR_DIR="$VENDOR_DIR" bash "$APP_DIR/deploy/build_offline_images.sh" --out-dir "$OUT_DIR" \
    || die "离线镜像构建失败（docker/podman、外网或镜像源依赖）"
  mkdir -p "$OUT_DIR/$PKG_NAME/images"
  cp "$OUT_DIR"/qianshu-images-*.tar "$OUT_DIR"/qianshu-images-*.tar.sha256 \
     "$OUT_DIR/$PKG_NAME/images/"
  TAG=$(grep -m1 -oE '"version": "[0-9]+\.[0-9]+\.[0-9]+"' \
        "$APP_DIR/backend/app/api/v1/settings.py" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')
  [ -n "$TAG" ] && echo "$TAG" > "$OUT_DIR/$PKG_NAME/images/image-tag"
  info "镜像已放入 images/（安装器将自动 load 并跳过构建）"
else
  warn "未带离线镜像：目标机需有 python3.9+（源码形态）或外网可构建镜像（compose 形态）"
fi

# ---------- 4. （可选）PyInstaller 单文件 ----------
if [ "$WITH_PYI" = "1" ]; then
  info "构建 PyInstaller 单文件（备用形态）..."
  PYI_DIR=$(mktemp -d /tmp/qianshu-pyi.XXXXXX)
  cat > "$PYI_DIR/entry.py" <<'EOF'
import os
from app.main import app  # noqa
import uvicorn

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("QIANSHU_PORT", "8810")))
EOF
  python3 -m venv --system-site-packages "$PYI_DIR/venv"
  "$PYI_DIR/venv/bin/pip" install -q pyinstaller
  (cd "$APP_DIR/backend" && "$PYI_DIR/venv/bin/pyinstaller" --noconfirm --onefile \
     --name qianshu-backend --paths "$APP_DIR/backend" \
     --distpath "$OUT_DIR/$PKG_NAME/bin" --workpath "$PYI_DIR/build" --specpath "$PYI_DIR" \
     "$PYI_DIR/entry.py") || die "PyInstaller 构建失败"
  rm -rf "$PYI_DIR"
  info "单文件已产出：$PKG_NAME/bin/qianshu-backend（注意 §2.3.1：.env/库文件仍需外置）"
fi

# ---------- 5. 打 tar ----------
info "打包..."
tar -C "$OUT_DIR" -czf "$OUT_DIR/$PKG_NAME.tar.gz" "$PKG_NAME"
SIZE=$(du -h "$OUT_DIR/$PKG_NAME.tar.gz" | awk '{print $1}')
info "完成 ✔  $OUT_DIR/$PKG_NAME.tar.gz（$SIZE）"
echo "  目标机安装：tar xzf $PKG_NAME.tar.gz && cd $PKG_NAME && ./install.sh"
echo "  零外网验证：tar 包内 vendor/ 全轮子 + frontend/dist 预构建，目标机仅需 python3.9+"
