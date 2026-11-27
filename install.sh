#!/usr/bin/env bash
# 千枢运维平台 · 一键安装（产品交付入口，目标 10 分钟跑起来）
# 用法：
#   ./install.sh                          # 一键静默安装（默认 compose；缺 yum 源/基础工具/Docker/Compose 时自动准备，
#                                         #   不需要额外加 --bootstrap）
#   ./install.sh --mode compose           # 同上
#   ./install.sh --mode source            # 源码部署（venv + systemd + nginx，生产；缺 Python 3.9+/nginx/Node 时自动安装）
#   ./install.sh --mode source --dev      # 源码部署开发机（另装 vite 前端 unit，不装 nginx，按需装 Node）
#   ./install.sh --mode source --port 9810    # 后端端口避开占用（默认 8810；也认 QIANSHU_PORT）
#   离线包内若有 images/qianshu-images-*.tar（--with-images 构建），compose 形态自动 load
#   后直接启动、不在目标机构建（CentOS 7 无 python3 / 无外网可用）；--offline-images 强制此路径
#   ./install.sh --registry-mirror https://docker.m.daocloud.io
#                                         # 指定 Docker/Podman 镜像加速；默认仅在 Docker Hub 不可达时
#                                         #   自动启用 https://docker.m.daocloud.io（DOCKER_REGISTRY_MIRROR=none 禁用）
#   ./install.sh --sysprep                # 安装前先做系统调优（SELinux/防火墙/ulimit/内核参数/
#                                         #   yum 更新/docker 安装与 data-root/数据目录，幂等）
#   ./install.sh --sysprep --no-update    # 同上但跳过 yum 软件更新（内网/时间紧张）
#   ./install.sh --no-bootstrap           # 跳过首次安装自动准备（默认会自动修复 yum 源、
#                                         #   安装基础工具/Docker/Compose，source 还会装 Python/nginx/Node；
#                                         #   仅用于运行时已就绪/审计环境）
#   ./install.sh --data-dir /data/qianshu # compose 数据卷目录（不指定则默认 ./data；source 不支持）
#   tar xzf qianshu-offline.tar.gz && cd qianshu-offline && ./install.sh
#                                         # 离线包（vendor/ 存在时自动离线装依赖）
#   安装器必须与 docker-compose.yml、backend/、frontend/、deploy/ 同目录；不支持只下载本文件的 curl|bash
#
# 一键升级（自动识别 compose/source；升级前备份代码+数据库，失败自动回滚）：
#   ./install.sh --upgrade                         # 用当前安装目录里的代码升级（git pull/替换代码后）
#   ./install.sh --upgrade --pkg qianshu-offline.tar.gz  # 用发布包升级（保留 .env/数据/密钥）
#   ./install.sh --upgrade --dry-run                # 只做预检并打印计划，不改任何文件/不重启
#   ./install.sh --rollback backups/upgrade-<时间>   # 回滚到指定升级前备份（代码+数据库）
#   升级日志同样落在 /var/log/qianshu-install-<时间>.log；备份默认留在 <安装目录>/backups/
# 安装位置任意（不必 /opt/qianshu）：unit/nginx/copilotd 配置在安装时按实际路径改写
#
# 静默安装+安装日志：全程明细 tee 到 /var/log/qianshu-install-<时间>.log（不可写落安装目录）；
# 镜像构建/加载、npm/pip 依赖等长输出只写日志（控制台保留进度行，失败回显尾部），
# 任何失败都提示去查该日志。首次 compose 安装会自动执行 deploy/sysprep.sh --bootstrap：
# 修复 CentOS 7 EOL yum 源（阿里云 vault + EPEL 归档）、装基础工具和 Docker/Compose，
# 全程无需人工确认；最小化 CentOS 7.9 的前置准备可无人值守跑完。为让容器 bind mount 可写，bootstrap
# 仅在 SELinux 为 Enforcing 时改为 permissive（可逆，原配置备份），并只放行平台端口而不关闭
# 防火墙；内核参数与 yum 全量更新仍只在 --sysprep 显式触发。--no-bootstrap 跳过自动准备。
#
# 首次 source 安装会自动执行 deploy/sysprep.sh --bootstrap-source：修复 yum 源，缺 Python 3.9+
# 时从源码编译安装（CentOS 7 约 5-10 分钟），缺 nginx 时安装，缺前端 dist 时安装 Node 18+，
# 并通过 PGDG 安装 PostgreSQL 15（仅监听 127.0.0.1，自动建库建角色）。数据库只有 PostgreSQL：
# SQLite 已废除且不提供旧库迁移；接外部 PG 时显式设置 DATABASE_URL 后重跑。
# Docker Hub 不可达时自动配置公共镜像加速（仅首次/可禁用）；内网或严格离线环境请用离线镜像包。
#
# compose 形态做四件事：检查容器运行时 -> 起全家桶 -> 健康检查 -> 打印访问信息
# source 形态做五件事：装依赖（venv）-> 建前端产物 -> nginx 反代 -> systemd enable -> 健康检查
# 部署架构全量口径：docs/active/deployment.md
set -euo pipefail

cd "$(dirname "$0")"
APP_DIR="$(pwd)"

# ---------- 安装日志（全程留痕，失败提示路径） ----------
TS=$(date +%Y%m%d-%H%M%S)
LOG=""
for f in "/var/log/qianshu-install-$TS.log" "$APP_DIR/logs/qianshu-install-$TS.log"; do
  if touch "$f" 2>/dev/null; then LOG="$f"; break; fi
done
[ -n "$LOG" ] || { echo "无法创建安装日志（/var/log 与安装目录均不可写）" >&2; exit 1; }
mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
chmod 600 "$LOG" 2>/dev/null || true
exec > >(tee -a "$LOG") 2>&1
echo "===== 千枢安装 $(date '+%F %T')  安装目录: $APP_DIR =====" >> "$LOG"

info()  { echo -e "\033[32m[千枢]\033[0m $*"; }
warn()  { echo -e "\033[33m[千枢]\033[0m $*"; }
die()   { echo -e "\033[31m[千枢]\033[0m $*" >&2; warn "失败详情查安装日志：tail -100 $LOG" >&2; exit 1; }

# 长且噪声大的步骤（镜像构建/加载、npm/pip 依赖）：明细只写安装日志，
# 控制台保留一行进度；失败时回显日志尾部，避免现场只看到“失败”却无上下文。
run_logged() {  # $1=说明；其余=要执行的命令
  local desc="$1"; shift
  info "$desc（明细见日志：$LOG）"
  if "$@" >>"$LOG" 2>&1; then
    return 0
  else
    local rc=$?
    warn "$desc 失败（退出码 $rc），日志尾部："
    tail -n 40 "$LOG" >&2 || true
    return "$rc"
  fi
}

MODE="compose"
MODE_ARG=0
DEV=0
PORT_ARG=""
SYSPREP=0
BOOTSTRAP=1
NO_UPDATE=0
DATA_DIR_ARG=""
UPGRADE=0
PKG=""
ROLLBACK_DIR=""
DRY_RUN=0
OFFLINE_IMAGES=0
FORCE_OFFLINE_IMAGES=0
DOCKER_REGISTRY_MIRROR="${DOCKER_REGISTRY_MIRROR:-}"
export DOCKER_REGISTRY_MIRROR

while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="${2:?--mode 需要 compose|source}"; MODE_ARG=1; shift 2 ;;
    --dev)  DEV=1; shift ;;
    --port) PORT_ARG="${2:?--port 需要端口号}"; shift 2 ;;
    --sysprep) SYSPREP=1; shift ;;
    --bootstrap) BOOTSTRAP=1; shift ;;
    --no-bootstrap) BOOTSTRAP=0; shift ;;
    --registry-mirror) DOCKER_REGISTRY_MIRROR="${2:?--registry-mirror 需要地址（none=禁用）}"; export DOCKER_REGISTRY_MIRROR; shift 2 ;;
    --offline-images) FORCE_OFFLINE_IMAGES=1; shift ;;
    --no-update) NO_UPDATE=1; shift ;;
    --data-dir) DATA_DIR_ARG="${2:?--data-dir 需要目录}"; shift 2 ;;
    --upgrade) UPGRADE=1; shift ;;
    --pkg) PKG="${2:?--pkg 需要发布包路径}"; UPGRADE=1; shift 2 ;;
    --rollback) ROLLBACK_DIR="${2:?--rollback 需要备份目录}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) awk 'NR==1{next} /^set -euo pipefail/{exit} /^#/{sub(/^# ?/,"");print}' "$0"; exit 0 ;;
    *) die "未知参数: $1（用 --help 看用法）" ;;
  esac
done
[ "$MODE" = "compose" ] || [ "$MODE" = "source" ] || die "不支持的 --mode: $MODE（compose|source）"
if [ "$MODE" = "source" ] && [ -n "$DATA_DIR_ARG" ]; then
  die "--data-dir 仅用于 compose 数据卷；source 形态请在 backend/.env 设置 DATABASE_URL 和 BACKUP_DIR（见 README §7.4）"
fi
[ -n "$ROLLBACK_DIR" ] && [ "$UPGRADE" = "1" ] && die "--rollback 与 --upgrade 不能同时使用"
[ -n "$ROLLBACK_DIR" ] && [ "$DRY_RUN" = "1" ] && die "--dry-run 只用于升级预检；回滚请先确认备份目录"
[ "$DRY_RUN" = "1" ] && [ "$UPGRADE" = "0" ] && die "--dry-run 需要配合 --upgrade 使用"
if [ -n "$PKG" ]; then
  [ -f "$PKG" ] || die "找不到发布包：$PKG"
  PKG="$(cd "$(dirname "$PKG")" && pwd)/$(basename "$PKG")"
fi
if [ -n "$ROLLBACK_DIR" ]; then
  [ -d "$ROLLBACK_DIR" ] || die "找不到回滚目录：$ROLLBACK_DIR"
  ROLLBACK_DIR="$(cd "$ROLLBACK_DIR" && pwd)"
  [ -f "$ROLLBACK_DIR/meta" ] || die "不是有效的升级备份（缺 meta）：$ROLLBACK_DIR"
fi
COMPOSE_FILE="$APP_DIR/docker-compose.yml"

# ---------- 源码部署依赖探测（与 sysprep 的判定口径一致） ----------
PYTHON_BIN=""
python_ok() {
  local p="$1"
  [ -n "$p" ] && [ -x "$p" ] || return 1
  "$p" - <<'PY' >/dev/null 2>&1
import sys, ssl, venv, ensurepip  # noqa: F401
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY
}
find_python39() {
  local c p
  for c in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    p=$(command -v "$c" 2>/dev/null || true)
    if python_ok "$p"; then PYTHON_BIN="$p"; return 0; fi
  done
  return 1
}
node_ok() {
  local nv
  nv=$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1 || true)
  [ -n "$nv" ] && [ "$nv" -ge 18 ] 2>/dev/null && command -v npm >/dev/null 2>&1
}

# ---------- 环境预检 + 首次安装自动准备（deploy/sysprep.sh 存在时） ----------
SYSPREP_SH="$APP_DIR/deploy/sysprep.sh"
# ---------- 先校准系统时间：时间偏差会让 HTTPS 下载/镜像拉取的证书校验直接失败 ----------
# 属于写系统操作：仅 root 执行；--dry-run（升级预检）只报告不修改；QIANSHU_TIME_SYNC=off 可跳过。
if [ -f "$SYSPREP_SH" ] && [ "$(id -u)" = "0" ]; then
  TIME_ARGS=(--time-only)
  [ "$DRY_RUN" = "1" ] && TIME_ARGS+=(--dry-run)
  bash "$SYSPREP_SH" "${TIME_ARGS[@]}" || warn "系统时间校准未完成——若后续下载报 TLS 证书错误，请先手工校准时间"
fi
source_bootstrap_prepare() {
  # 只在缺 Python 3.9+/nginx/Node/PostgreSQL 时动手；全部就绪则不动系统。
  local need=0 need_node=0 need_pg=0
  local -a src_args=(--bootstrap-source)
  find_python39 || need=1
  if [ "$DEV" = "0" ] && ! command -v nginx >/dev/null 2>&1; then need=1; fi
  if [ "$DEV" = "1" ] || [ ! -f "$APP_DIR/frontend/dist/index.html" ]; then
    need_node=1
    node_ok || need=1
  fi
  # 数据库只有 PostgreSQL：显式 DATABASE_URL（外部 PG）或本机 PG15 已运行即视为就绪。
  # 注意本函数在 read_env_val 定义之前执行，这里直接读文件。
  local db_url="${DATABASE_URL:-}"
  [ -z "$db_url" ] && db_url=$(sed -n 's/^DATABASE_URL=//p' "$APP_DIR/backend/.env" 2>/dev/null | tail -1 | tr -d '\r' || true)
  if [ -z "$db_url" ] && ! { [ -x /usr/pgsql-15/bin/pg_ctl ] && systemctl is-active --quiet postgresql-15 2>/dev/null; }; then
    need_pg=1
    need=1
  fi
  if [ "$need" = "0" ]; then
    info "源码部署依赖已就绪（Python/nginx/Node/PostgreSQL），跳过自动准备"
    return 0
  fi
  src_args+=(--ports "80,${PORT_ARG:-${QIANSHU_PORT:-8810}}")
  [ "$DEV" = "1" ] && src_args+=(--no-nginx)
  [ "$need_node" = "1" ] && src_args+=(--with-node)
  [ "$need_pg" = "1" ] && src_args+=(--with-postgres)
  info "首次源码部署自动准备（yum 源/Python 3.9+/nginx/Node/PostgreSQL 15，无需人工确认）..."
  bash "$SYSPREP_SH" "${src_args[@]}" \
    || die "源码部署前置准备失败——按上方日志处理后重跑；或手工预装 Python 3.9+/nginx/Node/PostgreSQL 后加 --no-bootstrap"
}
if [ "$UPGRADE" = "0" ] && [ -z "$ROLLBACK_DIR" ]; then
  if [ -f "$SYSPREP_SH" ]; then
    if [ "$SYSPREP" = "1" ]; then
      info "系统调优（--sysprep）：SELinux/防火墙/ulimit/内核参数/yum 更新/docker 安装与持久化配置..."
      FIX_ARGS=""
      [ "$NO_UPDATE" = "1" ] && FIX_ARGS="--skip-update"
      [ -n "$DATA_DIR_ARG" ] && FIX_ARGS="$FIX_ARGS --data-dir $DATA_DIR_ARG"
      bash "$SYSPREP_SH" --fix $FIX_ARGS || die "系统调优未完成——解决后重跑（详见上方日志路径）"
      [ "$MODE" = "source" ] && [ "$BOOTSTRAP" = "1" ] && source_bootstrap_prepare
    elif [ "$MODE" = "compose" ] && [ "$BOOTSTRAP" = "1" ]; then
      # 运行时/编排已就绪时不重复动系统；否则自动修复 yum 源并安装 Docker（最小化镜像必需）
      RUNTIME_READY=0
      # 已装 docker+compose 但 daemon 未启动也算就绪：下面第 1 步会启动 daemon，
      # 不因 yum 源问题阻断（最小化镜像没装 docker 时才走 bootstrap）。
      if command -v docker >/dev/null 2>&1 \
         && { docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; }; then
        RUNTIME_READY=1
      elif command -v podman >/dev/null 2>&1 \
         && { podman-compose version >/dev/null 2>&1 || podman compose version >/dev/null 2>&1; }; then
        RUNTIME_READY=1
      fi
      if [ "$RUNTIME_READY" = "1" ]; then
        info "容器运行时与 compose 已就绪，跳过自动准备"
      else
        PRE_BACKEND_PORT="${PORT_ARG:-${BACKEND_PORT:-8810}}"
        PRE_FRONTEND_PORT="${FRONTEND_PORT:-8080}"
        info "首次安装自动准备（yum 源/基础工具/Docker+Compose，无需人工确认）..."
        bash "$SYSPREP_SH" --bootstrap --ports "$PRE_FRONTEND_PORT,$PRE_BACKEND_PORT" \
          || die "首次安装自动准备失败——按上方日志处理后重跑；或手工预装 Docker 20.10+/Compose v2 后加 --no-bootstrap"
      fi
    elif [ "$MODE" = "source" ] && [ "$BOOTSTRAP" = "1" ]; then
      source_bootstrap_prepare
    else
      info "环境预检（只读）..."
      bash "$SYSPREP_SH" || die "环境预检有阻断项——按报告处理后重跑；或 ./deploy/sysprep.sh --fix 自动修复"
    fi
  else
    warn "deploy/sysprep.sh 不在安装包里（curl|bash 单文件场景）——跳过环境预检"
  fi
fi

gen_env_file() {  # $1 = 目标 .env 路径（compose=root/.env，source=backend/.env）
  local f="$1"
  gen_hex() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
  gen_pg_pw() { head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }  # 32 位十六进制
  if [ -f "$f" ]; then
    info ".env 已存在，复用前检查密钥：$f"
    local secret_key master_key
    secret_key=$(read_env_val "$f" SECRET_KEY)
    master_key=$(read_env_val "$f" MASTER_KEY)
    case "$secret_key" in
      ""|change-me*|*CHANGE*ME*) die "$f 的 SECRET_KEY 为空或仍是占位值；请填入至少 32 字符的随机值，安装器不会覆盖既有密钥" ;;
    esac
    [ "${#secret_key}" -ge 32 ] \
      || die "$f 的 SECRET_KEY 少于 32 字符；请人工更换随机值，安装器不会覆盖既有密钥"
    printf '%s' "$master_key" | grep -Eq '^[0-9a-fA-F]{64}$' \
      || die "$f 的 MASTER_KEY 必须是 64 位十六进制值；请保留原密钥或人工修复，安装器不会覆盖既有密钥"
    # 数据库（唯一数据库=PostgreSQL）：老 .env 可能没有口令，补一个随机的；
    # 已有值必须是字母数字（compose URL / SQL 都不做转义，避免特殊字符破坏连接）。
    local pg_pw
    pg_pw=$(read_env_val "$f" POSTGRES_PASSWORD)
    if [ -z "$pg_pw" ]; then
      pg_pw="${POSTGRES_PASSWORD:-}"
      [ -n "$pg_pw" ] || pg_pw=$(gen_pg_pw)
      if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then echo >> "$f"; fi
      echo "POSTGRES_PASSWORD=$pg_pw" >> "$f"
      info "已为现有 $f 补充 POSTGRES_PASSWORD"
    fi
    case "$pg_pw" in
      *[!A-Za-z0-9]*) die "$f 的 POSTGRES_PASSWORD 含非字母数字字符；请改成字母数字（安装器不会覆盖既有口令）" ;;
    esac
    # 仅用于安装完成提示；旧 .env 没写该键或用户已改密时，以实际为准
    INIT_PWD=$(read_env_val "$f" INIT_ADMIN_PASSWORD)
    return
  fi
  # 初始管理员密码：显式 INIT_ADMIN_PASSWORD 优先；否则 16 位随机（含大写+数字，
  # 规避"纯数字/纯字母"弱口令校验）。开源/公网部署不再使用固定 admin123。
  local init_pwd="${INIT_ADMIN_PASSWORD:-}"
  if [ -z "$init_pwd" ]; then
    local rnd
    rnd=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')
    init_pwd="${rnd:0:6}Q7${rnd:8:8}"
  fi
  INIT_PWD="$init_pwd"
  # PostgreSQL 口令：显式 POSTGRES_PASSWORD > 随机生成；只允许字母数字（URL/SQL 不转义）。
  local pg_pw="${POSTGRES_PASSWORD:-}"
  [ -n "$pg_pw" ] || pg_pw=$(gen_pg_pw)
  case "$pg_pw" in
    *[!A-Za-z0-9]*) die "POSTGRES_PASSWORD 只支持字母数字（避免 URL/SQL 转义问题）：请改用 openssl rand -hex 24 生成" ;;
  esac
  local pg_db="${POSTGRES_DB:-qianshu}" pg_user="${POSTGRES_USER:-qianshu}"
  case "$pg_db" in ''|[0-9]*|*[!A-Za-z0-9_]*) die "POSTGRES_DB 只支持字母开头的字母/数字/下划线：$pg_db" ;; esac
  case "$pg_user" in ''|[0-9]*|*[!A-Za-z0-9_]*) die "POSTGRES_USER 只支持字母开头的字母/数字/下划线：$pg_user" ;; esac
  local env_mode="${ENV:-prod}"
  [ "${DEV:-0}" = "1" ] && env_mode="${ENV:-dev}"
  cat > "$f" <<EOF
# 千枢安装向导生成 $(date +%F) —— 请妥善保存；丢失 MASTER_KEY 会导致已存云凭证无法解密
ENV=$env_mode
SECRET_KEY=$(gen_hex)
MASTER_KEY=$(gen_hex)
INIT_ADMIN_USERNAME=admin
# 初始密码只在首启生效一次（首登强制改密；注释独立成行——部分 .env 解析器不支持行尾注释）
INIT_ADMIN_PASSWORD=$init_pwd

# ---- 数据库（唯一数据库=PostgreSQL；SQLite 已废除且不提供旧库迁移）----
# compose 形态由 compose 用下列三项组装 DATABASE_URL（指向栈内 postgres 服务）；
# source 形态由安装器在创建本机库后写入 DATABASE_URL（连接外部 PG 时保留显式值）。
POSTGRES_DB=$pg_db
POSTGRES_USER=$pg_user
POSTGRES_PASSWORD=$pg_pw
EOF
  cat >> "$f" <<EOF
# 换环境适配（运行时配置唯一入口=安装向导/系统设置 UI，写库即生效；本文件只作 bootstrap 初始值+密钥载体）：
# SLS_ENDPOINT=cn-hangzhou.log.aliyuncs.com
# SLS_SLB_TARGETS=[["proj","logstore","outer"],["proj","logstore","inner"]]
# SLS_APP_LOG=["proj","logstore"]
# SLS_CDN_TARGET=["proj","logstore"]
# 手改以上键需入册才写库持久（否则与库不一致时按"谁后改谁生效"对账并站内信留痕，绝不静默）：
#   source 形态：cd backend && python3 -m app.services.env_store
#   compose 形态：docker compose exec backend python3 -m app.services.env_store
#   （重启后端也会自动对账；正式配置请一律走 安装向导-补数据/系统设置；密钥类仅存 .env 不入册）
EOF
  chmod 600 "$f"
  info "已生成 $f（SECRET_KEY/MASTER_KEY/数据库口令随机，初始密码随机生成，首登强制改密）"
}

wait_backend() {  # 健康检查（最多 90 秒；$PORT=后端/宿主映射端口）
  info "等待服务就绪..."
  local ok=""
  for _ in $(seq 1 30); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/bootstrap/status" || true)
    if [ "$code" = "200" ]; then ok=1; break; fi
    sleep 3
  done
  [ -n "$ok" ] || return 1
  return 0
}

rewrite_copilotd_config() {
  # 生命周期守护配置里的绝对路径/端口改写为本次安装位置（幂等）。
  # 仅 source 形态需要：compose 形态容器内固定挂载 /opt/qianshu，配置原样即正确。
  # 优先用 venv 解释器（源码安装可能把 3.9 装在 python3.9 而非系统 python3）。
  local py="$APP_DIR/backend/.venv/bin/python"
  [ -x "$py" ] || py="${PYTHON_BIN:-python3}"
  APP_DIR="$APP_DIR" COPD_PORT="$PORT" \
    COPD_HOST="${QIANSHU_BACKEND_HOST:-$(read_env_val "$APP_DIR/backend/.env" QIANSHU_BACKEND_HOST)}" \
    "$py" - <<'PY'
import json
import os
import re

app = os.environ["APP_DIR"]
port = int(os.environ["COPD_PORT"])
host = os.environ.get("COPD_HOST") or "127.0.0.1"
if not re.fullmatch(r"[A-Za-z0-9_.:-]+", host):
    raise SystemExit("QIANSHU_BACKEND_HOST 含非法字符")
p = os.path.join(app, "config", "qianshu-copilotd.json")
if not os.path.isfile(p):
    raise SystemExit(0)
with open(p, encoding="utf-8") as f:
    data = json.load(f)
data["platform_dir"] = app
data["token_file"] = os.path.join(app, "config", "qianshu-copilotd.token")
data["actions_path"] = os.path.join(app, "data", "copilotd", "actions.jsonl")
data["api_url"] = f"http://127.0.0.1:{port}"
data["start_cmd"] = re.sub(r"--host\s+\S+", f"--host {host}", data.get("start_cmd", ""))
data["start_cmd"] = re.sub(r"--port\s+\d+", f"--port {port}", data.get("start_cmd", ""))
# 看门狗必须使用 venv 解释器，否则 source 形态安装的依赖不可见，进程虽能拉起但健康检查会失败。
# venv 解释器路径在 copilotd 白名单内；venv 不存在时保留原解释器。
venv_py = os.path.join(app, "backend", ".venv", "bin", "python")
parts = data.get("start_cmd", "").split()
if parts and os.path.exists(venv_py):
    parts[0] = venv_py
    data["start_cmd"] = " ".join(parts)
tmp = p + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, p)
print(f"copilotd 配置已对齐安装位置: platform_dir={app} api_port={port}")
PY
}

check_node() {  # 前端构建需要 node 18+/npm（vite 5 引擎要求）
  command -v npm >/dev/null 2>&1 || die "缺 npm（前端构建需要；或把预构建 frontend/dist 放进包里）"
  local nv
  nv=$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1 || true)
  [ -n "$nv" ] && [ "$nv" -ge 18 ] 2>/dev/null \
    || die "node 版本过低（vite 构建需 18+）：$(node -v 2>/dev/null || echo '未安装 node')。可升级 node，或放入预构建 frontend/dist 跳过构建"
}

print_access() {  # $1 = 端口（空=80）
  local ip port
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  ip=${ip:-127.0.0.1}
  port="${1:-80}"
  echo
  info "安装完成 ✔"
  echo "  访问地址:     http://$ip${port:+:$port}"
  echo "  管理员账号:   admin"
  if [ -n "${INIT_PWD:-}" ]; then
    echo "  初始密码:     $INIT_PWD  （仅首次安装有效；首次登录后强制修改，改完才能使用系统）"
  else
    echo "  初始密码:     见 ${ENV_FILE:-.env} 的 INIT_ADMIN_PASSWORD（旧安装/已改密则以此前设置为准）"
  fi
  echo "  下一步:       登录后打开「安装向导」——填 AK 五面探测 -> 底座体检报告 -> 按建议补数据"
  echo
}

# ============================================================
# 一键升级 / 回滚
#   设计约束：升级前必须同时具备「代码快照 + 数据库快照」；健康检查失败自动回滚；
#   .env / data / secrets / copilotd token 永不覆盖；正在运行的 install.sh 自身不覆盖。
#   破坏性动作（停服务、恢复数据库）只在用户显式执行 --upgrade / --rollback 时发生。
# ============================================================

read_env_val() {  # $1=文件 $2=键（取最后一行，去引号/CR；不打印任何密钥）
  [ -f "$1" ] || return 0
  sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -1 | tr -d '\r' \
    | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//" || true
}

detect_installed_mode() {
  if command -v systemctl >/dev/null 2>&1 && systemctl cat qianshu-backend >/dev/null 2>&1; then
    echo source; return 0
  fi
  if [ -x "$APP_DIR/backend/.venv/bin/python" ] && [ -f "$APP_DIR/backend/.env" ]; then
    echo source; return 0
  fi
  if [ -f "$APP_DIR/.env" ] && [ -f "$APP_DIR/docker-compose.yml" ]; then
    echo compose; return 0
  fi
  echo ""
}

detect_port() {  # $1=source|compose；显式 --port > 环境变量 > .env > 默认 8810
  local p="${PORT_ARG:-}"
  case "$1" in
    source)
      [ -z "$p" ] && p="${QIANSHU_PORT:-}"
      if [ -z "$p" ] && command -v systemctl >/dev/null 2>&1; then
        p=$(systemctl show -p Environment --value qianshu-backend 2>/dev/null \
            | tr ' ' '\n' | sed -n 's/^QIANSHU_PORT=//p' | tail -1 || true)
      fi
      [ -z "$p" ] && p=$(read_env_val "$APP_DIR/backend/.env" QIANSHU_PORT)
      ;;
    compose)
      [ -z "$p" ] && p="${BACKEND_PORT:-}"
      [ -z "$p" ] && p=$(read_env_val "$APP_DIR/.env" BACKEND_PORT)
      ;;
  esac
  echo "${p:-8810}"
}

detect_data_root() {
  local d
  d=$(read_env_val "$APP_DIR/.env" QIANSHU_DATA_DIR)
  echo "${d:-$APP_DIR/data}"
}

detect_db_url() {  # 只返回 URL 给后续子进程，绝不 echo 到终端
  # source：以 backend/.env 的 DATABASE_URL 为准（安装器写入本机 PG 或用户显式外部 PG）；
  # compose：数据库固定为栈内 postgres，URL 由 POSTGRES_* 组装（与 compose 完全同源），
  # 不读取 .env 里的 DATABASE_URL，避免备份/恢复目标与应用实际连接不一致。
  local f u user pw db
  if [ "$MODE" = "source" ]; then
    f="$APP_DIR/backend/.env"
    u=$(read_env_val "$f" DATABASE_URL)
  else
    f="$APP_DIR/.env"
    user=$(read_env_val "$f" POSTGRES_USER); user="${user:-qianshu}"
    pw=$(read_env_val "$f" POSTGRES_PASSWORD)
    db=$(read_env_val "$f" POSTGRES_DB); db="${db:-qianshu}"
    [ -n "$pw" ] && u="postgresql://$user:$pw@postgres:5432/$db"
  fi
  printf '%s' "$u"
}

legacy_sqlite_file() {  # 输出仍存在的旧库文件（只读检查）；compose 看数据卷 db/，source 看 backend/
  if [ "$MODE" = "source" ]; then
    [ -f "$APP_DIR/backend/qianshu.db" ] && printf '%s' "$APP_DIR/backend/qianshu.db"
  else
    local dr="${DATA_ROOT:-$APP_DIR/data}"
    [ -f "$dr/db/qianshu.db" ] && printf '%s' "$dr/db/qianshu.db"
  fi
  return 0
}

local_pg_ready() {
  [ -x /usr/pgsql-15/bin/pg_ctl ] && systemctl is-active --quiet postgresql-15 2>/dev/null
}

ensure_compose_db_config() {  # compose：固定栈内 PG；拒绝旧 SQLite 配置/数据（不做迁移）
  local f="$APP_DIR/.env" u
  u=$(read_env_val "$f" DATABASE_URL)
  case "$u" in
    sqlite://*) die "检测到旧 SQLite 配置（$f 的 DATABASE_URL）。千枢已改为仅支持 PostgreSQL，且不提供 SQLite 数据迁移；请删除该行后重跑" ;;
    postgres*) warn "$f 的 DATABASE_URL 不再生效：compose 固定使用栈内 postgres（POSTGRES_*）；外部 PG 请用 source 形态" ;;
  esac
  local legacy pw
  legacy=$(legacy_sqlite_file)
  [ -z "$legacy" ] || die "检测到旧 SQLite 数据库文件：$legacy。千枢已改为仅支持 PostgreSQL，且不提供 SQLite 数据迁移；请确认无需保留后将该文件移出数据目录再重跑"
  pw=$(read_env_val "$f" POSTGRES_PASSWORD)
  if [ -z "$pw" ]; then
    local dr="${DATA_ROOT:-$APP_DIR/data}"
    if [ -f "$dr/postgres/PG_VERSION" ]; then
      die "$f 缺少 POSTGRES_PASSWORD，但已存在 PostgreSQL 数据目录（$dr/postgres）。无法推断既有口令：请确认原口令后补写到 .env，再重跑"
    fi
    pw=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')
    if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then echo >> "$f"; fi
    echo "POSTGRES_PASSWORD=$pw" >> "$f"
    info "已为现有 .env 生成随机 POSTGRES_PASSWORD（栈内 PostgreSQL 尚未初始化）"
  fi
  case "$pw" in *[!A-Za-z0-9]*) die "$f 的 POSTGRES_PASSWORD 含非字母数字字符；请改成字母数字后重跑" ;; esac
  return 0
}

ensure_source_db_config() {  # source：本机 PG15 自动供给或外部 DATABASE_URL；仅 PostgreSQL
  local f="$APP_DIR/backend/.env" u user db pw psql legacy
  u=$(read_env_val "$f" DATABASE_URL)
  if [ -n "$u" ]; then
    case "$u" in
      sqlite://*) die "检测到旧 SQLite 配置（$f 的 DATABASE_URL）。千枢已改为仅支持 PostgreSQL，且不提供 SQLite 数据迁移；请删除该行并按 docs/active/deployment.md 配置 PostgreSQL 后重跑" ;;
      postgresql://*|postgres://*|postgresql+*) return 0 ;;
      *) die "无法识别的 DATABASE_URL（仅支持 postgresql:// / postgres:// / postgresql+...）；SQLite 已废除" ;;
    esac
  fi
  legacy=$(legacy_sqlite_file)
  [ -z "$legacy" ] || die "检测到旧 SQLite 数据库文件：$legacy。千枢已改为仅支持 PostgreSQL，且不提供 SQLite 数据迁移；请确认无需保留后将该文件移出 backend/ 再重跑"

  # 显式导出的 DATABASE_URL = 接外部 PostgreSQL：只落盘并交给后面的连通性探测，不建本机库。
  if [ -n "${DATABASE_URL:-}" ]; then
    echo "DATABASE_URL=$DATABASE_URL" >> "$f"
    info "数据库: 外部 PostgreSQL（DATABASE_URL 已写入 $f）"
    return 0
  fi

  if ! local_pg_ready; then
    [ "$BOOTSTRAP" = "1" ] || die "--no-bootstrap 且未检测到运行中的本机 PostgreSQL 15；请先安装并启动 PostgreSQL（或设置 DATABASE_URL 指向外部 PG）后重跑"
    [ -f "$APP_DIR/deploy/sysprep.sh" ] || die "缺 deploy/sysprep.sh，无法自动安装 PostgreSQL 15"
    info "安装本机 PostgreSQL 15（仅监听 127.0.0.1；首次约 1-3 分钟）..."
    bash "$APP_DIR/deploy/sysprep.sh" --postgres-only \
      || die "PostgreSQL 15 安装未完成——按上方日志处理后重跑"
  fi
  local_pg_ready || die "本机 PostgreSQL 15 未运行（systemctl status postgresql-15 查看原因）"

  user=$(read_env_val "$f" POSTGRES_USER); user="${user:-qianshu}"
  db=$(read_env_val "$f" POSTGRES_DB); db="${db:-qianshu}"
  pw=$(read_env_val "$f" POSTGRES_PASSWORD)
  if [ -z "$pw" ]; then
    pw=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')
    if [ -s "$f" ] && [ -n "$(tail -c 1 "$f")" ]; then echo >> "$f"; fi
    echo "POSTGRES_PASSWORD=$pw" >> "$f"
    info "已为现有 .env 生成随机 POSTGRES_PASSWORD"
  fi
  case "$user" in ''|[0-9]*|*[!A-Za-z0-9_]*) die "POSTGRES_USER 只支持字母开头的字母/数字/下划线：$user" ;; esac
  case "$db" in ''|[0-9]*|*[!A-Za-z0-9_]*) die "POSTGRES_DB 只支持字母开头的字母/数字/下划线：$db" ;; esac
  case "$pw" in ''|*[!A-Za-z0-9]*) die "POSTGRES_PASSWORD 只支持字母数字；请检查 $f" ;; esac
  psql=$(command -v psql 2>/dev/null || true)
  [ -n "$psql" ] || psql=/usr/pgsql-15/bin/psql
  [ -x "$psql" ] || die "找不到 psql（PostgreSQL 15 安装不完整）；请检查安装日志"

  info "初始化本机数据库（角色 $user / 库 $db，幂等）..."
  if [ "$(runuser -u postgres -- "$psql" -tAc "SELECT 1 FROM pg_roles WHERE rolname='$user'")" = "1" ]; then
    runuser -u postgres -- "$psql" -v ON_ERROR_STOP=1 -c "ALTER ROLE \"$user\" LOGIN PASSWORD '$pw'" >/dev/null \
      || die "更新数据库角色口令失败（$user）"
  else
    runuser -u postgres -- "$psql" -v ON_ERROR_STOP=1 -c "CREATE ROLE \"$user\" LOGIN PASSWORD '$pw'" >/dev/null \
      || die "创建数据库角色失败（$user）"
  fi
  if [ "$(runuser -u postgres -- "$psql" -tAc "SELECT 1 FROM pg_database WHERE datname='$db'")" != "1" ]; then
    runuser -u postgres -- "$psql" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$db\" OWNER \"$user\"" >/dev/null \
      || die "创建数据库失败（$db）"
  fi
  echo "DATABASE_URL=postgresql://$user:$pw@127.0.0.1:5432/$db" >> "$f"
  info "数据库: 本机 PostgreSQL 15（$db，仅监听 127.0.0.1）"
  return 0
}

compose_runtime() {
  COMPOSE=""
  RT=""
  if command -v docker >/dev/null 2>&1; then
    RT=docker
    if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
    fi
  fi
  if [ -z "$COMPOSE" ] && command -v podman >/dev/null 2>&1; then
    RT=podman
    if podman-compose version >/dev/null 2>&1; then COMPOSE="podman-compose"
    elif podman compose version >/dev/null 2>&1; then COMPOSE="podman compose"
    fi
  fi
  [ -n "$COMPOSE" ] || return 1
  return 0
}

image_tag_current() {  # 当前安装应使用的镜像标签（.env 优先，其次包内 image-tag）
  local t
  t=$(grep -E '^QIANSHU_IMAGE_TAG=' "$APP_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- \
      | tr -d '[:space:]' || true)
  [ -n "$t" ] && { printf '%s' "$t"; return; }
  t=$(tr -d '[:space:]' < "$APP_DIR/images/image-tag" 2>/dev/null || true)
  [ -n "$t" ] && { printf '%s' "$t"; return; }
  printf '0.3.1'
}

set_env_image_tag() {  # $1=标签；让 compose 用本次加载/回滚对应的镜像
  local tag="$1" f="$APP_DIR/.env"
  [ -n "$tag" ] && [ -f "$f" ] || return 0
  if grep -q '^QIANSHU_IMAGE_TAG=' "$f"; then
    sed -i "s/^QIANSHU_IMAGE_TAG=.*/QIANSHU_IMAGE_TAG=$tag/" "$f"
  else
    echo "QIANSHU_IMAGE_TAG=$tag" >> "$f"
  fi
}

maybe_load_offline_images() {
  # 包内有 images/qianshu-images-<tag>.tar 则 load（校验和+镜像存在性）；
  # 无镜像且未强制 → 返回 0 走原构建路径；强制但缺包 → 返回 1 并输出明确原因。
  local tag file tars
  tars=$(ls "$APP_DIR"/images/qianshu-images-*.tar 2>/dev/null || true)
  if [ -z "$tars" ]; then
    if [ "$FORCE_OFFLINE_IMAGES" = "1" ]; then
      warn "指定了 --offline-images，但安装包里没有 images/qianshu-images-*.tar"
      warn "请用 deploy/build_offline.sh --with-images 重新构建发布包"
      return 1
    fi
    return 0
  fi
  tag=$(tr -d '[:space:]' < "$APP_DIR/images/image-tag" 2>/dev/null || true)
  if [ -n "$tag" ]; then
    file="$APP_DIR/images/qianshu-images-$tag.tar"
    if [ ! -f "$file" ]; then
      warn "images/image-tag=$tag，但找不到 images/qianshu-images-$tag.tar"
      return 1
    fi
  else
    file=$(ls -t $tars | head -1)
    tag=$(basename "$file" | sed 's/^qianshu-images-//; s/\.tar$//')
  fi
  [ -n "${RT:-}" ] || RT=docker
  if [ -f "$file.sha256" ] && command -v sha256sum >/dev/null 2>&1; then
    ( cd "$(dirname "$file")" && sha256sum -c "$(basename "$file").sha256" >/dev/null 2>&1 ) \
      || { warn "镜像归档校验和不匹配（传输损坏/包不完整）：$file"; return 1; }
  fi
  run_logged "加载离线镜像：$(basename "$file")（$tag，目标机不构建）" \
    "$RT" load -i "$file" || { warn "镜像加载失败：$file"; return 1; }
  # podman save 的 docker-archive 把本地构建镜像存成 localhost/<name>:<tag>，
  # docker load 后引用名带 localhost 前缀，而 compose 用的是 qianshu-backend:<tag>。
  # 先按原名查，查不到再用 localhost/ 前缀查并打回原名。
  ensure_image_ref() {
    local ref="$1"
    "$RT" image inspect "$ref" >/dev/null 2>&1 && return 0
    "$RT" image inspect "localhost/$ref" >/dev/null 2>&1 || return 1
    "$RT" tag "localhost/$ref" "$ref" >/dev/null 2>&1 || return 1
    "$RT" image inspect "$ref" >/dev/null 2>&1
  }
  if ! ensure_image_ref "qianshu-backend:$tag" \
     || ! ensure_image_ref "qianshu-frontend:$tag"; then
    warn "镜像加载后仍找不到 qianshu-backend/frontend:$tag（归档内容与 image-tag 不一致）"
    return 1
  fi
  # 归档里的基础镜像（victoriametrics/victoria-logs/otel/postgres 等）同样带
  # localhost/ 前缀；去掉前缀打回 compose 引用的原名，否则离线目标机 up 时仍去拉镜像。
  ("$RT" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null || true) | grep '^localhost/' \
    | while IFS= read -r ref; do
        "$RT" tag "$ref" "${ref#localhost/}" >/dev/null 2>&1 || true
      done || true
  set_env_image_tag "$tag"
  OFFLINE_IMAGES=1
  return 0
}

detect_pg_container() {
  PG_CONTAINER=""
  [ -n "${RT:-}" ] || RT=docker
  if [ -n "${COMPOSE:-}" ] && [ -f "$COMPOSE_FILE" ]; then
    local id
    id=$($COMPOSE -f "$COMPOSE_FILE" ps -q postgres 2>/dev/null | head -1 || true)
    if [ -n "$id" ]; then
      PG_CONTAINER=$($RT inspect -f '{{.Name}}' "$id" 2>/dev/null | sed 's#^/##' || true)
    fi
  fi
  if [ -z "$PG_CONTAINER" ] && command -v docker >/dev/null 2>&1; then
    PG_CONTAINER=$(docker ps --format '{{.Names}}' 2>/dev/null \
      | grep -E '^qianshu-(pg|postgres)$' | head -1 || true)
    # 必须用 if：`[ -n ] && ...` 作为函数最后一条且条件为假时函数返回 1，
    # set -e 会让升级静默退出。
    if [ -n "$PG_CONTAINER" ]; then RT=docker; fi
  fi
  return 0
}

# ---------- 无 python3 目标机（CentOS 7 compose 形态）的数据库备份/恢复 ----------
# 目标机不依赖 python3：直接 docker/podman exec 进 postgres 容器用其自带 pg_dump/pg_restore
# （容器内 local socket 信任，免口令；版本与服务端天然一致）。
sql_url_user_db() {  # $1=postgres URL -> 输出 "user<TAB>db"（安装器生成的口令是字母数字，无需解码）
  local u="$1" rest authority user db
  case "$u" in
    postgresql://*|postgres://*|postgresql+*) ;;
    *) return 1 ;;
  esac
  rest="${u#*://}"
  authority="${rest%%/*}"
  db="${rest#*/}"
  db="${db%%\?*}"
  user="${authority%@*}"     # user:pass@host -> user:pass
  user="${user%%:*}"         # -> user
  [ -n "$user" ] && [ -n "$db" ] && [ "$db" != "$rest" ] || return 1
  printf '%s\t%s' "$user" "$db"
}

backup_database_container() {
  local db_url parsed user db size
  db_url=$(detect_db_url)
  [ -n "$db_url" ] || { warn "未配置数据库连接（DATABASE_URL/POSTGRES_*），无法备份"; return 1; }
  parsed=$(sql_url_user_db "$db_url") || { warn "DATABASE_URL 不是 PostgreSQL，无法备份（SQLite 已废除）"; return 1; }
  user="${parsed%%$'\t'*}"; db="${parsed#*$'\t'}"
  detect_pg_container
  [ -n "$PG_CONTAINER" ] || { warn "目标机没有 python3 且找不到运行中的 postgres 容器（${RT:-docker} ps），无法备份数据库"; return 1; }
  info "备份 PostgreSQL（容器 $PG_CONTAINER 内 pg_dump -Fc）..."
  "$RT" exec "$PG_CONTAINER" pg_dump -U "$user" -Fc --no-owner --no-privileges "$db" \
    > "$BK/database.dump" || { warn "容器内 pg_dump 失败"; rm -f "$BK/database.dump"; return 1; }
  size=$(stat -c %s "$BK/database.dump" 2>/dev/null || echo 0)
  [ "$size" -ge 1024 ] || { warn "pg_dump 输出为空或过小，已中止升级（不做无回滚点的升级）"; rm -f "$BK/database.dump"; return 1; }
  cat > "$BK/db.meta" <<EOF
kind=postgresql
file=database.dump
transport=container
user=$user
host=container
port=5432
dbname=$db
container=$PG_CONTAINER
EOF
  info "数据库已备份：PostgreSQL（容器内 pg_dump，$((size / 1048576)) MB）"
}

restore_database_container() {  # 输入全局 BK/DATA_ROOT
  local kind file user db
  kind=$(sed -n 's/^kind=//p' "$BK/db.meta" 2>/dev/null | head -1)
  file=$(sed -n 's/^file=//p' "$BK/db.meta" 2>/dev/null | head -1)
  user=$(sed -n 's/^user=//p' "$BK/db.meta" 2>/dev/null | head -1)
  db=$(sed -n 's/^dbname=//p' "$BK/db.meta" 2>/dev/null | head -1)
  [ "$kind" = "postgresql" ] && [ -n "$file" ] && [ -f "$BK/$file" ] || {
    warn "备份不是 PostgreSQL dump，无法恢复（SQLite 已废除，不做旧库恢复）：$BK/db.meta"; return 1; }
  [ -n "$user" ] && [ -n "$db" ] || { warn "备份缺少 user/dbname：$BK/db.meta"; return 1; }
  detect_pg_container
  [ -n "$PG_CONTAINER" ] || { warn "找不到运行中的 postgres 容器，无法恢复"; return 1; }
  info "恢复 PostgreSQL（容器 $PG_CONTAINER 内 pg_restore）..."
  "$RT" exec -i "$PG_CONTAINER" pg_restore --clean --if-exists --no-owner --no-privileges \
    --exit-on-error -U "$user" -d "$db" < "$BK/$file" || return 1
  info "数据库已恢复（PostgreSQL 升级前快照）"
}

backup_database() {  # 输入全局 BK/MODE/DATA_ROOT/PG_CONTAINER/RT
  local db_url
  if ! command -v python3 >/dev/null 2>&1; then
    backup_database_container
    return $?
  fi
  db_url=$(detect_db_url)
  [ -n "$db_url" ] || { warn "未配置数据库连接（DATABASE_URL/POSTGRES_*），无法备份"; return 1; }
  DB_URL="$db_url" BACKUP_DIR="$BK" \
  PG_CONTAINER="${PG_CONTAINER:-}" CONTAINER_RT="${RT:-docker}" \
  python3 - <<'PY' || return 1
import glob
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

url = os.environ.get("DB_URL", "").strip()
bdir = os.environ["BACKUP_DIR"]
pg_container = os.environ.get("PG_CONTAINER", "").strip()
rt = os.environ.get("CONTAINER_RT", "docker")
meta = {}

def fail(msg):
    print(f"数据库备份失败：{msg}", file=sys.stderr)
    sys.exit(1)

def write_meta():
    lines = [f"{k}={v}" for k, v in meta.items()]
    with open(os.path.join(bdir, "db.meta"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

def pg_tool(name):
    """选版本最高的 pg_dump/pg_restore：PATH 与 PGDG /usr/pgsql-*/bin（源码形态不在 PATH）。"""
    cands = []
    found = shutil.which(name)
    if found:
        cands.append(found)
    cands.extend(glob.glob(f"/usr/pgsql-*/bin/{name}"))
    best, best_v = None, -1
    for exe in dict.fromkeys(cands):
        if not os.access(exe, os.X_OK):
            continue
        v = 0
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            m = re.search(r"(\d+)\.", out.stdout or "")
            v = int(m.group(1)) if m else 0
        except Exception:
            v = 0
        if v > best_v:
            best, best_v = exe, v
    return best

if not url.startswith(("postgresql://", "postgres://", "postgresql+")):
    fail("DATABASE_URL 不是 PostgreSQL（SQLite 已废除，不做 SQLite 备份）")

# 去掉 SQLAlchemy 驱动后缀（postgresql+psycopg2:// -> postgresql://）
_, rest = url.split("://", 1)
pu = urllib.parse.urlsplit("postgresql://" + rest)
user = urllib.parse.unquote(pu.username or "")
password = urllib.parse.unquote(pu.password or "")
host = pu.hostname or "127.0.0.1"
port = str(pu.port or 5432)
dbname = urllib.parse.unquote((pu.path or "").lstrip("/")) or "qianshu"
if "\n" in password or "\r" in password:
    fail("数据库口令含换行符，无法安全传给 pg_dump；请改用标准口令后重试")
out = os.path.join(bdir, "database.dump")

def run_host(cmd):
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    r = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    if r.returncode != 0:
        msg = (r.stderr or b"").decode("utf-8", "replace")[-400:]
        raise RuntimeError(msg.strip() or f"pg_dump 退出码 {r.returncode}")

def run_container(cmd):
    envf = os.path.join(bdir, ".pgpass.env")
    fd = os.open(envf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"PGPASSWORD={password}\n")
    try:
        r = subprocess.run([rt, "exec", "--env-file", envf, "-i", pg_container] + cmd,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
        if r.returncode != 0:
            msg = (r.stderr or b"").decode("utf-8", "replace")[-400:]
            raise RuntimeError(msg.strip() or f"容器内 pg_dump 退出码 {r.returncode}")
        return r.stdout
    finally:
        try:
            os.unlink(envf)
        except OSError:
            pass

host_pg_dump = pg_tool("pg_dump")
use_container = host not in ("127.0.0.1", "localhost", "::1") and bool(pg_container)
if host in ("127.0.0.1", "localhost", "::1") and not host_pg_dump and pg_container:
    use_container = True
try:
    if use_container:
        data = run_container(["pg_dump", "-U", user, "-Fc", "--no-owner", "--no-privileges", dbname])
        with open(out, "wb") as f:
            f.write(data)
        transport = "container"
    else:
        if not host_pg_dump:
            fail("找不到 pg_dump（请安装 PostgreSQL 客户端，或确保 postgres 容器在运行以便容器内备份）")
        run_host([host_pg_dump, "-h", host, "-p", port, "-U", user, "-Fc",
                  "--no-owner", "--no-privileges", "-f", out, dbname])
        transport = "host"
except Exception as e:
    fail(f"PostgreSQL 备份失败：{type(e).__name__} {str(e)[:300]}")
if not os.path.isfile(out) or os.path.getsize(out) < 1024:
    fail("pg_dump 输出为空或过小，已中止升级（不做无回滚点的升级）")
meta.update(kind="postgresql", file="database.dump", transport=transport,
            user=user, host=host, port=port, dbname=dbname, container=pg_container)
write_meta()
print(f"数据库已备份：PostgreSQL（{os.path.getsize(out) / 1048576:.1f} MB，{transport} 通道）")
PY
}

backup_create() {
  BK="$APP_DIR/backups/upgrade-$TS"
  [ -e "$BK" ] && BK="$BK-$RANDOM"
  mkdir -p "$BK" || return 1
  chmod 700 "$BK" 2>/dev/null || true

  local avail_kb
  avail_kb=$(df -Pk "$APP_DIR" 2>/dev/null | awk 'NR==2{print $4}' || true)
  if [ -n "$avail_kb" ] && [ "$avail_kb" -lt 1048576 ] 2>/dev/null; then
    warn "磁盘可用空间不足 1 GB（$((avail_kb / 1024)) MB），升级备份可能失败"
  fi

  info "升级前备份代码 -> $BK/code.tar.gz"
  tar czf "$BK/code.tar.gz" -C "$APP_DIR" \
    --exclude='./backend/.venv' --exclude='./frontend/node_modules' \
    --exclude='./data' --exclude='./backups' --exclude='./logs' \
    --exclude='./dist' --exclude='./images' --exclude='./.git' --exclude='./secrets' \
    --exclude='./.env' --exclude='./backend/.env' \
    --exclude='./backend/qianshu.db*' --exclude='./qianshu.db*' \
    --exclude='./config/qianshu-copilotd.token' \
    --exclude='./.upgrade-pkg.*' . 2>/dev/null || {
      warn "代码备份失败（磁盘空间/权限？），已中止升级，未做任何改动"
      return 1
    }

  info "升级前备份数据库（.env 中的 DATABASE_URL 指向的库）..."
  if ! backup_database; then
    warn "数据库备份失败，已中止升级，未做任何改动"
    rm -rf "$BK"
    return 1
  fi

  cat > "$BK/meta" <<EOF
MODE=$MODE
PORT=$PORT
DATA_ROOT=$DATA_ROOT
IMAGE_TAG=$(image_tag_current)
CREATED=$(date '+%F %T')
PKG=$(basename "${PKG:-}")
EOF
  chmod 600 "$BK/meta" "$BK/db.meta" 2>/dev/null || true
  info "备份完成：$BK（确认升级稳定前请勿删除）"
  return 0
}

package_version() {  # $1=解压后的发布包根（优先取最近的语义化版本，Unreleased 不算版本号）
  grep -m1 '^## \[[0-9]' "$1/CHANGELOG.md" 2>/dev/null | sed 's/^## //' \
    || grep -m1 '^## \[' "$1/CHANGELOG.md" 2>/dev/null | sed 's/^## //' || true
}

apply_package() {
  [ -n "$PKG" ] || { PKG_HAS_DIST=0; return 0; }
  PKG_TMP=$(mktemp -d "$APP_DIR/.upgrade-pkg.XXXXXX") || return 1
  case "$PKG" in
    *.zip)
      command -v unzip >/dev/null 2>&1 || { warn "缺少 unzip，无法解压 zip 发布包"; rm -rf "$PKG_TMP"; return 1; }
      unzip -q "$PKG" -d "$PKG_TMP" || { warn "解压失败：$PKG"; rm -rf "$PKG_TMP"; return 1; }
      ;;
    *.tar.gz|*.tgz) tar xzf "$PKG" -C "$PKG_TMP" || { warn "解压失败：$PKG"; rm -rf "$PKG_TMP"; return 1; } ;;
    *.tar.xz) tar xJf "$PKG" -C "$PKG_TMP" || { warn "解压失败：$PKG"; rm -rf "$PKG_TMP"; return 1; } ;;
    *) warn "不支持的发布包格式（支持 .tar.gz/.tgz/.tar.xz/.zip）：$PKG"; rm -rf "$PKG_TMP"; return 1 ;;
  esac
  local main src
  main=$(find "$PKG_TMP" -maxdepth 5 -type f -path '*/backend/app/main.py' -print -quit)
  if [ -z "$main" ]; then
    warn "发布包结构不对：找不到 backend/app/main.py"
    rm -rf "$PKG_TMP"; return 1
  fi
  src=$(cd "$(dirname "$main")/../.." && pwd)
  [ -f "$src/backend/requirements.txt" ] || { warn "发布包缺少 backend/requirements.txt"; rm -rf "$PKG_TMP"; return 1; }
  [ -f "$src/frontend/package.json" ] || { warn "发布包缺少 frontend/package.json"; rm -rf "$PKG_TMP"; return 1; }

  PKG_HAS_DIST=0
  [ -f "$src/frontend/dist/index.html" ] && PKG_HAS_DIST=1
  info "发布包：$(basename "$PKG")  版本：$(package_version "$src")"
  info "覆盖代码（保留 .env / data / backups / secrets / copilotd token）..."
  if ! ( cd "$src" && tar cf - \
      --exclude='./.env' --exclude='./backend/.env' --exclude='./data' \
      --exclude='./backups' --exclude='./logs' --exclude='./secrets' \
      --exclude='./backend/.venv' --exclude='./frontend/node_modules' \
      --exclude='./.git' --exclude='./dist' --exclude='./install.sh' \
      --exclude='./.install.sh.swp' --exclude='./.upgrade-pkg.*' . ) \
     | tar xf - -C "$APP_DIR"; then
    warn "代码覆盖失败（磁盘空间/权限？）"
    rm -rf "$PKG_TMP"; return 1
  fi
  if [ -f "$src/install.sh" ]; then
    if grep -q -- '--upgrade' "$src/install.sh"; then
      install -m 755 "$src/install.sh" "$APP_DIR/install.sh.new" || true
    else
      warn "发布包内 install.sh 是旧版（不支持 --upgrade），保留当前安装器，只升级业务代码"
    fi
  fi
  rm -rf "$PKG_TMP"
  PKG_TMP=""
  return 0
}

source_prepare() {
  if [ -x "$APP_DIR/backend/.venv/bin/python" ]; then
    local pip_opts=""
    if [ -d "$APP_DIR/vendor" ]; then
      pip_opts="--no-index --find-links $APP_DIR/vendor"
      info "检测到 vendor/，离线安装依赖"
    fi
    "$APP_DIR/backend/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
    run_logged "更新 Python 依赖（backend/.venv）" \
      "$APP_DIR/backend/.venv/bin/pip" install $pip_opts -r "$APP_DIR/backend/requirements.txt" \
      || { warn "Python 依赖安装失败${pip_opts:+（离线模式：vendor/ 轮子不全）}"; return 1; }
  else
    # 兼容没有 venv 的历史部署，deploy/bin/qianshu-backend.sh 回落系统 python3。
    # 升级时不强行改变运行方式；校验核心依赖，缺失则明确报错。
    local py="${PYTHON_BIN:-}"
    if [ -z "$py" ] || ! command -v "$py" >/dev/null 2>&1; then
      py=$(find_python39 || command -v python3 || true)
    fi
    [ -n "$py" ] || { warn "缺 Python 3.9+ 且没有 backend/.venv；去掉 --no-bootstrap 重跑或手工安装"; return 1; }
    info "未检测到 backend/.venv，沿用 Python（$py）当前运行方式，跳过 pip 安装"
    "$py" - <<'PY' || { warn "系统 Python 缺少运行依赖；请手工 pip install -r backend/requirements.txt 或重装 source 形态"; return 1; }
import importlib
import sys
missing = [m for m in ("uvicorn", "fastapi", "sqlalchemy", "pydantic") if not importlib.util.find_spec(m)]
if missing:
    print("缺少模块: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
PY
  fi
  rewrite_copilotd_config || { warn "copilotd 配置改写失败"; return 1; }

  if [ "$PKG_HAS_DIST" = "1" ]; then
    info "使用发布包内预构建前端（frontend/dist），跳过 npm 构建"
  else
    if ! command -v npm >/dev/null 2>&1; then
      warn "缺 npm：无法构建前端。请在发布包中放入 frontend/dist，或在目标机安装 node 18+/npm"
      return 1
    fi
    run_logged "构建前端（npm ci && npm run build）" \
      bash -c 'cd "$1" && npm ci && npm run build' _ "$APP_DIR/frontend" \
      || { warn "前端构建失败"; return 1; }
  fi
  return 0
}

source_restart() {
  systemctl daemon-reload || true
  systemctl restart qianshu-backend || { warn "qianshu-backend 重启失败"; return 1; }
  if systemctl cat qianshu-copilotd >/dev/null 2>&1; then
    systemctl restart qianshu-copilotd || warn "qianshu-copilotd 重启失败（后端已启动）"
  fi
  if systemctl cat qianshu-frontend >/dev/null 2>&1; then
    systemctl restart qianshu-frontend || warn "qianshu-frontend 重启失败"
  fi
  if command -v nginx >/dev/null 2>&1 && [ -f /etc/nginx/conf.d/qianshu.conf ]; then
    nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 \
      || warn "nginx 重载失败（不影响后端健康检查，请手工 nginx -t 检查）"
  fi
  return 0
}

stop_services() {
  if [ "$MODE" = "source" ]; then
    systemctl stop qianshu-backend qianshu-copilotd >/dev/null 2>&1 || true
    systemctl cat qianshu-frontend >/dev/null 2>&1 && systemctl stop qianshu-frontend >/dev/null 2>&1 || true
  else
    $COMPOSE -f "$COMPOSE_FILE" stop backend copilotd frontend >/dev/null 2>&1 || true
  fi
}

start_services() {
  if [ "$MODE" = "source" ]; then
    systemctl start qianshu-copilotd >/dev/null 2>&1 || true
    systemctl start qianshu-backend || return 1
    systemctl cat qianshu-frontend >/dev/null 2>&1 && systemctl start qianshu-frontend >/dev/null 2>&1 || true
  else
    # 离线包形态：镜像已在库（安装/加载过），严禁现场构建（目标机常无外网/无编译环境）
    if [ -d "$APP_DIR/images" ] && ls "$APP_DIR"/images/qianshu-images-*.tar >/dev/null 2>&1; then
      $COMPOSE -f "$COMPOSE_FILE" up -d || return 1
    else
      run_logged "重建镜像并启动容器" \
        $COMPOSE -f "$COMPOSE_FILE" up -d --build || return 1
    fi
  fi
  return 0
}

restore_code() {
  [ -f "$BK/code.tar.gz" ] || { warn "回滚包缺失：$BK/code.tar.gz"; return 1; }
  info "恢复升级前代码..."
  local tmp
  tmp=$(mktemp -d "$APP_DIR/.restore-code.XXXXXX") || { warn "回滚临时目录创建失败"; return 1; }
  # update-data 是纯代码目录（无运行数据）。失败升级新增的版本目录不会被覆盖式解压删除，
  # 回滚后老代码会误执行"未来版本"的 SQL（甚至再次启动失败），因此先备好备份里的
  # update-data，再整体替换；备份中不存在该目录（功能引入前的旧包）则回滚后也不保留。
  tar xzf "$BK/code.tar.gz" -C "$tmp" ./update-data 2>/dev/null || true
  # 不覆盖正在运行的 install.sh（升级时也未覆盖它，新版本落在 install.sh.new）
  if ! tar xzf "$BK/code.tar.gz" -C "$APP_DIR" --exclude='./install.sh' 2>/dev/null; then
    warn "代码恢复失败"
    rm -rf "$tmp"
    return 1
  fi
  rm -rf "$APP_DIR/update-data"
  if [ -d "$tmp/update-data" ]; then
    mv "$tmp/update-data" "$APP_DIR/update-data" || { warn "update-data 恢复失败"; rm -rf "$tmp"; return 1; }
  fi
  rm -rf "$tmp"
  rm -f "$APP_DIR/install.sh.new"
  return 0
}

restore_database() {
  local kind file
  if ! command -v python3 >/dev/null 2>&1; then
    restore_database_container
    return $?
  fi
  kind=$(sed -n 's/^kind=//p' "$BK/db.meta" 2>/dev/null | head -1)
  file=$(sed -n 's/^file=//p' "$BK/db.meta" 2>/dev/null | head -1)
  [ -n "$kind" ] && [ -n "$file" ] && [ -f "$BK/$file" ] \
    || { warn "数据库备份缺失或不完整：$BK/db.meta"; return 1; }
  [ "$kind" = "postgresql" ] || {
    warn "备份类型是 $kind：千枢已废除 SQLite，不做旧库恢复；请从 PostgreSQL 备份目录回滚"
    return 1
  }

  local db_url
  db_url=$(detect_db_url)
  [ -n "$db_url" ] || { warn "未配置数据库连接（DATABASE_URL/POSTGRES_*），无法恢复"; return 1; }
  DB_URL="$db_url" DUMP_FILE="$BK/$file" BACKUP_DIR="$BK" \
  PG_CONTAINER="${PG_CONTAINER:-}" CONTAINER_RT="${RT:-docker}" \
  python3 - <<'PY' || return 1
import glob
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

url = os.environ["DB_URL"].strip()
dump = os.environ["DUMP_FILE"]
bdir = os.environ["BACKUP_DIR"]
container = os.environ.get("PG_CONTAINER", "").strip()
rt = os.environ.get("CONTAINER_RT", "docker")
transport = ""
try:
    with open(os.path.join(bdir, "db.meta"), encoding="utf-8") as f:
        for line in f:
            if line.startswith("transport="):
                transport = line.split("=", 1)[1].strip()
except OSError:
    pass

def pg_tool(name):
    cands = []
    found = shutil.which(name)
    if found:
        cands.append(found)
    cands.extend(glob.glob(f"/usr/pgsql-*/bin/{name}"))
    best, best_v = None, -1
    for exe in dict.fromkeys(cands):
        if not os.access(exe, os.X_OK):
            continue
        v = 0
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            m = re.search(r"(\d+)\.", out.stdout or "")
            v = int(m.group(1)) if m else 0
        except Exception:
            v = 0
        if v > best_v:
            best, best_v = exe, v
    return best

if "://" not in url or not url.startswith(("postgresql://", "postgres://", "postgresql+")):
    print("当前 DATABASE_URL 不是 PostgreSQL，无法用 pg 备份恢复（SQLite 已废除）", file=sys.stderr)
    sys.exit(1)
_, rest = url.split("://", 1)
pu = urllib.parse.urlsplit("postgresql://" + rest)
user = urllib.parse.unquote(pu.username or "")
password = urllib.parse.unquote(pu.password or "")
host = pu.hostname or "127.0.0.1"
port = str(pu.port or 5432)
dbname = urllib.parse.unquote((pu.path or "").lstrip("/")) or "qianshu"

args = ["--clean", "--if-exists", "--no-owner", "--no-privileges",
        "--exit-on-error", "-U", user, "-d", dbname]

def run_host():
    exe = pg_tool("pg_restore")
    if not exe:
        print("找不到 pg_restore（请安装 PostgreSQL 客户端，或确保 postgres 容器在运行以便容器内恢复）", file=sys.stderr)
        return None
    env = os.environ.copy()
    env["PGPASSWORD"] = password
    cmd = [exe, "-h", host, "-p", port] + args
    with open(dump, "rb") as f:
        return subprocess.run(cmd, env=env, stdin=f, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=1800)

def run_container():
    envf = os.path.join(bdir, ".pgpass.env")
    fd = os.open(envf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"PGPASSWORD={password}\n")
    try:
        cmd = [rt, "exec", "--env-file", envf, "-i", container, "pg_restore"] + args
        with open(dump, "rb") as f:
            return subprocess.run(cmd, stdin=f, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, timeout=1800)
    finally:
        try:
            os.unlink(envf)
        except OSError:
            pass

try:
    if transport == "container" and container:
        r = run_container()
    else:
        r = run_host()
        if r is None:
            sys.exit(1)
except Exception as e:
    print(f"PostgreSQL 恢复异常：{type(e).__name__} {str(e)[:300]}", file=sys.stderr)
    sys.exit(1)
if r.returncode != 0:
    msg = (r.stderr or b"").decode("utf-8", "replace")[-500:]
    print(f"PostgreSQL 恢复失败（退出码 {r.returncode}）：{msg.strip()}", file=sys.stderr)
    sys.exit(1)
print("PostgreSQL 已恢复到升级前快照")
PY
  return 0
}

rollback_apply() {  # $1=备份目录；成功 0 / 失败 1（不调用 die，便于升级失败路径统一处理）
  BK="$1"
  MODE=$(sed -n 's/^MODE=//p' "$BK/meta" 2>/dev/null | head -1)
  PORT=$(sed -n 's/^PORT=//p' "$BK/meta" 2>/dev/null | head -1)
  DATA_ROOT=$(sed -n 's/^DATA_ROOT=//p' "$BK/meta" 2>/dev/null | head -1)
  [ "$MODE" = "source" ] || [ "$MODE" = "compose" ] || { warn "备份 meta 缺少有效 MODE"; return 1; }
  case "$PORT" in ''|*[!0-9]*) PORT=8810 ;; esac

  if [ "$MODE" = "compose" ]; then
    compose_runtime || { warn "找不到 compose 编排，无法回滚"; return 1; }
    detect_pg_container
  else
    [ "$(id -u)" = "0" ] || { warn "源码形态回滚需要 root"; return 1; }
    detect_pg_container
  fi

  info "停止服务（释放数据库连接）..."
  stop_services
  sleep 2
  if ! restore_database; then
    warn "数据库恢复失败"
    return 1
  fi
  if ! restore_code; then
    return 1
  fi
  # compose 形态：回滚必须回到升级前那套镜像（离线机不在现场重建）。
  # .env 不随代码回滚，因此用 meta 里记录的标签显式切回。
  if [ "$MODE" = "compose" ]; then
    local prev_tag
    prev_tag=$(sed -n 's/^IMAGE_TAG=//p' "$BK/meta" 2>/dev/null | head -1)
    [ -n "$prev_tag" ] && set_env_image_tag "$prev_tag"
  fi

  if [ "$MODE" = "source" ]; then
    info "恢复 Python 依赖并启动..."
    if [ -x "$APP_DIR/backend/.venv/bin/pip" ]; then
      local pip_opts=""
      [ -d "$APP_DIR/vendor" ] && pip_opts="--no-index --find-links $APP_DIR/vendor"
      run_logged "恢复旧版本 Python 依赖" \
        "$APP_DIR/backend/.venv/bin/pip" install $pip_opts -r "$APP_DIR/backend/requirements.txt" \
        || warn "旧版本依赖恢复失败（继续尝试启动）"
    fi
    rewrite_copilotd_config || warn "copilotd 配置恢复失败（继续尝试启动）"
  fi
  start_services || { warn "服务启动失败"; return 1; }
  if ! wait_backend; then
    warn "回滚后健康检查未通过（:${PORT}）"
    return 1
  fi
  return 0
}

upgrade_abort() {  # $1=备份目录 $2=原因
  warn "升级失败：$2"
  warn "自动回滚到升级前版本（代码 + 数据库）..."
  if rollback_apply "$1"; then
    die "已自动回滚到升级前版本；备份保留在 $1。请查看日志排查后重试"
  else
    die "自动回滚未完成！请勿继续操作。备份目录=$1 日志=$LOG；按备份内 meta/db.meta 手工恢复"
  fi
}

upgrade_dry_run() {
  info "升级预检（dry-run：不备份、不覆盖文件、不重启服务）"
  echo "  安装目录:     $APP_DIR"
  echo "  部署形态:     $MODE"
  echo "  后端端口:     $PORT"
  echo "  数据目录:     $DATA_ROOT"
  local db_url kind legacy
  db_url=$(detect_db_url)
  case "$db_url" in
    sqlite://*) kind="SQLite（不支持，正式升级会中止）" ;;
    postgres*) kind=PostgreSQL ;;
    "") kind="未配置（正式升级时将自动初始化本机 PostgreSQL）" ;;
    *) kind="未知协议（仅支持 PostgreSQL）" ;;
  esac
  echo "  数据库类型:   $kind"
  legacy=$(legacy_sqlite_file)
  [ -z "$legacy" ] || warn "检测到旧 SQLite 数据文件：$legacy——千枢已废除 SQLite 且不提供数据迁移，正式升级会中止"
  [ -n "$PKG" ] && echo "  发布包:       $PKG"
  local pkg_has_dist=0
  if [ -n "$PKG" ]; then
    local tmp main src
    tmp=$(mktemp -d /tmp/qianshu-pkg-check.XXXXXX)
    case "$PKG" in
      *.zip) unzip -q "$PKG" -d "$tmp" 2>/dev/null || warn "zip 解压失败（缺 unzip？）" ;;
      *.tar.gz|*.tgz) tar xzf "$PKG" -C "$tmp" 2>/dev/null || true ;;
      *.tar.xz) tar xJf "$PKG" -C "$tmp" 2>/dev/null || true ;;
    esac
    main=$(find "$tmp" -maxdepth 5 -type f -path '*/backend/app/main.py' -print -quit 2>/dev/null || true)
    if [ -n "$main" ]; then
      src=$(cd "$(dirname "$main")/../.." && pwd)
      echo "  发布包版本:   $(package_version "$src")"
      if [ -f "$src/frontend/dist/index.html" ]; then
        pkg_has_dist=1
        echo "  预构建前端:   有（源码形态将直接使用，跳过 npm 构建）"
      else
        echo "  预构建前端:   无（源码形态需要 npm 构建）"
      fi
    else
      warn "发布包结构校验失败：找不到 backend/app/main.py"
    fi
    rm -rf "$tmp"
  fi
  if [ "$MODE" = "source" ]; then
    command -v systemctl >/dev/null 2>&1 || warn "缺少 systemd"
    systemctl cat qianshu-backend >/dev/null 2>&1 || warn "未找到 qianshu-backend.service（源码形态升级需要）"
    [ -x "$APP_DIR/backend/.venv/bin/python" ] \
      || warn "未检测到 backend/.venv，将沿用系统 python3（当前运行方式）"
    if [ "$pkg_has_dist" != "1" ] && ! command -v npm >/dev/null 2>&1; then
      warn "缺少 npm 且发布包无预构建 frontend/dist：正式升级会失败"
    fi
    [ "$(id -u)" = "0" ] || warn "当前不是 root：正式升级/回滚需要 root"
  else
    compose_runtime || die "找不到 docker/podman compose 编排"
    echo "  容器编排:     $COMPOSE"
  fi
  local avail_kb
  avail_kb=$(df -Pk "$APP_DIR" 2>/dev/null | awk 'NR==2{print $4}' || true)
  [ -n "$avail_kb" ] && echo "  磁盘可用:     $((avail_kb / 1024)) MB"
  echo
  info "预检完成。正式升级将执行：备份代码+数据库 -> 覆盖代码 -> 依赖/前端 -> 重启 -> 迁移 -> 健康检查（失败自动回滚）"
  return 0
}

finalize_upgrade() {
  if [ -f "$APP_DIR/install.sh.new" ]; then
    chmod 755 "$APP_DIR/install.sh.new" 2>/dev/null || true
    mv -f "$APP_DIR/install.sh.new" "$APP_DIR/install.sh" || warn "新 install.sh 替换失败（旧入口仍可用）"
  fi
  info "升级完成 ✔"
  echo "  回滚点:       $BK"
  echo "  回滚命令:     $APP_DIR/install.sh --rollback $BK"
  echo "  升级日志:     $LOG"
  warn "请先在浏览器强刷（Ctrl+Shift+R）确认页面/接口正常；稳定后再清理旧备份"
}

upgrade_main() {
  if [ "$MODE_ARG" = "0" ]; then
    MODE=$(detect_installed_mode)
    [ -n "$MODE" ] || die "未检测到已安装的千枢（既没有 systemd 服务，也没有 backend/.venv + backend/.env 或 compose .env）；首次安装请去掉 --upgrade"
  fi
  PORT=$(detect_port "$MODE")
  case "$PORT" in ''|*[!0-9]*) die "无法识别后端端口：$PORT" ;; esac
  DATA_ROOT=$(detect_data_root)

  if [ "$MODE" = "compose" ]; then
    compose_runtime || die "找不到 docker/podman compose 编排"
    detect_pg_container
  else
    command -v systemctl >/dev/null 2>&1 || die "源码形态升级需要 systemd"
    detect_pg_container
    [ "$DRY_RUN" = "1" ] || [ "$(id -u)" = "0" ] || die "源码形态升级/回滚需要 root（systemctl 停启服务）"
  fi

  PKG_HAS_DIST=0
  if [ -n "$PKG" ]; then
    # dry-run 也先校验包结构（只读解压到临时目录，不碰安装目录）
    if [ "$DRY_RUN" = "1" ]; then
      upgrade_dry_run
      exit 0
    fi
  elif [ "$DRY_RUN" = "1" ]; then
    upgrade_dry_run
    exit 0
  fi

  # 数据库只支持 PostgreSQL：旧 SQLite 配置/数据直接中止（不做迁移），并确保升级后
  # 新版本启动前 .env 已有可用连接（旧安装可能没有 POSTGRES_PASSWORD/DATABASE_URL）。
  if [ "$MODE" = "compose" ]; then
    ensure_compose_db_config
  else
    ensure_source_db_config
  fi

  info "开始升级：$MODE 形态，后端端口 $PORT，当前版本 $(grep -m1 '^## \[' "$APP_DIR/CHANGELOG.md" 2>/dev/null | sed 's/^## //' || echo unknown)"
  [ -n "$PKG" ] && info "目标发布包：$(basename "$PKG")"

  backup_create || die "升级前备份未完成，已中止（安装目录未改动）"

  if ! apply_package; then
    upgrade_abort "$BK" "发布包解压/覆盖失败"
  fi

  if [ "$MODE" = "compose" ]; then
    if ! maybe_load_offline_images; then
      upgrade_abort "$BK" "离线镜像加载失败（新包 images/ 缺失或损坏）"
    fi
  fi

  if [ "$MODE" = "source" ]; then
    if ! source_prepare; then
      upgrade_abort "$BK" "依赖或前端构建失败"
    fi
    info "重启后端与 copilotd（启动时自动执行幂等数据库迁移）..."
    if ! source_restart; then
      upgrade_abort "$BK" "服务重启失败"
    fi
  else
    if [ "$OFFLINE_IMAGES" = "1" ]; then
      info "离线镜像已就绪，直接滚动启动（不构建）..."
      if ! $COMPOSE -f "$COMPOSE_FILE" up -d; then
        upgrade_abort "$BK" "容器启动失败"
      fi
    else
      if ! run_logged "重建镜像并滚动启动" \
          $COMPOSE -f "$COMPOSE_FILE" up -d --build; then
        upgrade_abort "$BK" "容器重建/启动失败"
      fi
    fi
  fi

  if ! wait_backend; then
    upgrade_abort "$BK" "升级后 90 秒内健康检查未通过（:${PORT}/api/bootstrap/status）"
  fi
  finalize_upgrade
}

rollback_main() {
  MODE=$(sed -n 's/^MODE=//p' "$ROLLBACK_DIR/meta" 2>/dev/null | head -1)
  PORT=$(sed -n 's/^PORT=//p' "$ROLLBACK_DIR/meta" 2>/dev/null | head -1)
  DATA_ROOT=$(sed -n 's/^DATA_ROOT=//p' "$ROLLBACK_DIR/meta" 2>/dev/null | head -1)
  [ "$MODE" = "source" ] || [ "$MODE" = "compose" ] || die "备份 meta 缺少有效 MODE"
  case "$PORT" in ''|*[!0-9]*) PORT=8810 ;; esac
  [ "$MODE" = "compose" ] || [ "$(id -u)" = "0" ] || die "源码形态回滚需要 root"

  info "开始回滚：$ROLLBACK_DIR（代码 + 数据库）"
  warn "回滚会恢复到备份时刻的数据库状态；升级后新产生的业务数据会丢失（VM 指标数据不受影响）"
  if rollback_apply "$ROLLBACK_DIR"; then
    info "回滚完成 ✔ 后端已通过健康检查（:${PORT}）"
    echo "  备份仍保留:   $ROLLBACK_DIR"
    echo "  日志:         $LOG"
    warn "请浏览器强刷（Ctrl+Shift+R）确认页面状态"
    return 0
  fi
  die "回滚未完成！备份保留在 $ROLLBACK_DIR；请查看日志 $LOG 后手工恢复（勿删除备份）"
}

if [ -n "$ROLLBACK_DIR" ] || [ "$UPGRADE" = "1" ]; then
  if [ "$UPGRADE" = "1" ]; then
    upgrade_main
  else
    rollback_main
  fi
  exit 0
fi

# ============================================================
# compose 形态（默认）
# ============================================================
if [ "$MODE" = "compose" ]; then
  ENV_FILE="$APP_DIR/.env"
  # 宿主映射端口（docker-compose.yml 同名变量；--port 优先）——避开已占用端口
  PORT="${PORT_ARG:-${BACKEND_PORT:-8810}}"
  case "$PORT" in ''|*[!0-9]*) die "--port 需要数字端口: $PORT" ;; esac

  # ---------- 1. 检查容器运行时与编排（docker 优先，podman 兜底） ----------
  # RHEL9/CentOS9 默认只有 podman——客户机器常见形态，不能死在 docker 上
  COMPOSE=""
  RT=""
  if command -v docker >/dev/null 2>&1; then
    RT=docker
    if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
    fi
  fi
  if [ -z "$COMPOSE" ] && command -v podman >/dev/null 2>&1; then
    RT=podman
    if podman-compose version >/dev/null 2>&1; then COMPOSE="podman-compose"
    elif podman compose version >/dev/null 2>&1; then COMPOSE="podman compose"
    fi
  fi
  if [ -z "$COMPOSE" ]; then
    if command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1; then
      die "有容器运行时但缺 compose 编排：默认安装会自动补装 docker-compose-plugin；若使用 --no-bootstrap，请手动安装后重跑"
    fi
    die "未检测到容器运行时。默认安装会自动准备（yum 源 + Docker + Compose）；使用 --no-bootstrap 时请先手动安装 Docker 20.10+/Compose v2 或 Podman+podman-compose"
  fi
  # docker 已装但未启动（--no-bootstrap/历史安装）：安装流程内直接拉起，仍保持幂等
  if [ "$RT" = "docker" ] && ! docker info >/dev/null 2>&1; then
    info "docker 未运行，正在启动..."
    systemctl enable --now docker || die "docker 启动失败——详情查安装日志"
    for _ in $(seq 1 60); do
      docker info >/dev/null 2>&1 && break
      sleep 1
    done
    docker info >/dev/null 2>&1 || die "docker 60 秒内未就绪——请查 journalctl -u docker 与安装日志"
  fi
  info "容器运行时: $RT（编排: $COMPOSE）"

  # ---------- 2. 生成 .env（密钥一次性生成，绝不内置默认） ----------
  gen_env_file "$ENV_FILE"
  # 应用数据持久化目录：--data-dir 指定才写；缺省 ./data 跟安装目录走
  if [ -n "$DATA_DIR_ARG" ]; then
    if ! grep -q '^QIANSHU_DATA_DIR=' "$ENV_FILE"; then
      echo "QIANSHU_DATA_DIR=$DATA_DIR_ARG" >> "$ENV_FILE"
      mkdir -p "$DATA_DIR_ARG/db" "$DATA_DIR_ARG/backups" "$DATA_DIR_ARG/victoriametrics" \
               "$DATA_DIR_ARG/victorialogs" "$DATA_DIR_ARG/copilotd" "$DATA_DIR_ARG/postgres"
      info "应用数据持久化目录: $DATA_DIR_ARG（全部数据卷指向此处，重建容器不丢）"
    fi
  fi
  # 非默认端口持久化进 .env（后续手工 docker compose up 保持同一端口）
  if [ "$PORT" != "8810" ] && ! grep -q '^BACKEND_PORT=' "$ENV_FILE"; then
    echo "BACKEND_PORT=$PORT" >> "$ENV_FILE"
  fi
  if [ -n "${FRONTEND_PORT:-}" ] && [ "$FRONTEND_PORT" != "8080" ] && ! grep -q '^FRONTEND_PORT=' "$ENV_FILE"; then
    echo "FRONTEND_PORT=$FRONTEND_PORT" >> "$ENV_FILE"
  fi
  # 其余宿主端口同理持久化（本次指定了才写）
  for v in COPILOTD_PORT OTLP_GRPC_PORT OTLP_HTTP_PORT; do
    eval "val=\${$v:-}"
    if [ -n "$val" ] && ! grep -q "^$v=" "$ENV_FILE"; then
      echo "$v=$val" >> "$ENV_FILE"
    fi
  done

  # 数据子目录：QIANSHU_DATA_DIR 指到外部数据盘时建在那里，否则跟安装目录
  DATA_ROOT="${QIANSHU_DATA_DIR:-$APP_DIR/data}"
  grep -q '^QIANSHU_DATA_DIR=' "$ENV_FILE" 2>/dev/null \
    && DATA_ROOT=$(grep '^QIANSHU_DATA_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2-)
  mkdir -p "$DATA_ROOT/db" "$DATA_ROOT/victoriametrics" "$DATA_ROOT/victorialogs" \
           "$DATA_ROOT/copilotd" "$DATA_ROOT/postgres" "$APP_DIR/config"
  # 唯一数据库=栈内 PostgreSQL；旧 SQLite 配置/数据直接中止（不做迁移）
  [ -n "${DATABASE_URL:-}" ] && warn "compose 形态固定使用栈内 PostgreSQL，已忽略环境变量 DATABASE_URL（外部 PG 请用 source 形态）"
  ensure_compose_db_config
  info "数据库: 栈内 PostgreSQL 16（${DATA_ROOT}/postgres）"

  # ---------- 3. 起全家桶 ----------
  if ! maybe_load_offline_images; then
    die "离线镜像不可用：请用带 images/ 的发布包，或去掉 --offline-images 走在线拉取/构建"
  fi
  # 在线构建前检查镜像仓库可达性；不可达时按需配置加速（离线镜像已加载则不需要）
  if [ "$OFFLINE_IMAGES" = "0" ] && [ -f "$SYSPREP_SH" ]; then
    bash "$SYSPREP_SH" --registry-mirror-only \
      || warn "镜像加速配置未完成——若拉取镜像失败，请设置 DOCKER_REGISTRY_MIRROR 后重跑，或使用离线镜像包"
  fi
  if [ "$OFFLINE_IMAGES" = "1" ]; then
    info "启动（离线镜像已加载，目标机不构建）..."
    $COMPOSE -f "$COMPOSE_FILE" up -d \
      || die "容器启动失败，请看日志：$RT logs qianshu-backend"
  else
    if ! run_logged "构建并启动（backend / frontend / PostgreSQL / VictoriaMetrics / VictoriaLogs / otel / copilotd）" \
        $COMPOSE -f "$COMPOSE_FILE" up -d --build; then
      # 只认 Dockerfile 在“所有 pip 源均失败”时输出的唯一标记：
      # 单个源 403 后回退成功也会留下 ERROR 行，不能据此判定整次构建失败。
      if grep -q 'PIP_INSTALL_ALL_INDEXES_FAILED' "$LOG" 2>/dev/null; then
        PIP_SHOW="${PIP_INDEX:-$(read_env_val "$APP_DIR/.env" PIP_INDEX)}"
        PIP_SHOW="${PIP_SHOW:-https://pypi.tuna.tsinghua.edu.cn/simple}"
        die "镜像构建失败：Python 依赖下载失败（pip 源返回 403/缺包/不可达）。
  当前 PIP_INDEX=${PIP_SHOW}；安装器已按 PIP_FALLBACK_INDEX 依次尝试华为/阿里云/官方源仍失败。
  可在 .env 设置 PIP_INDEX=https://mirrors.huaweicloud.com/repository/pypi/simple 后重跑；
  内网镜像源请显式设置 PIP_INDEX，并把 PIP_FALLBACK_INDEX 设为空避免回退公网。
  详细日志：tail -150 $LOG"
      elif grep -qE 'npm ERR!|ERR_SOCKET|ECONNREFUSED' "$LOG" 2>/dev/null; then
        die "镜像构建失败：前端 npm 依赖下载失败。
  可在 .env 设置 NPM_REGISTRY=https://registry.npmmirror.com 后重跑；
  完全离线环境请使用带 images/qianshu-images-*.tar 的发布包。
  详细日志：tail -150 $LOG"
      else
        MIRROR_STATE=$(docker info --format '{{json .RegistryConfig.Mirrors}}' 2>/dev/null || echo '未知')
        die "镜像构建/拉取失败：基础镜像仓库不可达或构建过程出错。
  当前 docker 运行态镜像加速：$MIRROR_STATE（配置在 /etc/docker/daemon.json，未生效时执行 systemctl restart docker 后重跑）
  1) 可设置 DOCKER_REGISTRY_MIRROR=<你的加速/代理地址> 后重跑；DOCKER_REGISTRY_MIRROR=none 可关闭自动加速
  2) 确认本机可访问外网/代理后再试
  3) 完全离线环境请使用带 images/qianshu-images-*.tar 的发布包（./install.sh 会自动加载）
  详细日志：tail -100 $LOG"
      fi
    fi
  fi

  # ---------- 4. 健康检查（最多等 90 秒） ----------
  wait_backend || die "后端 90 秒内未就绪，请看日志：$RT logs qianshu-backend"

  # ---------- 5. 打印访问信息 ----------
  print_access "${FRONTEND_PORT:-8080}"
  warn "升级：改代码后 $COMPOSE -f "$COMPOSE_FILE" up -d --build；数据在 ${DATA_ROOT}（.env 可设 QIANSHU_DATA_DIR 挪数据盘），重建容器不丢"
  warn "开机自启：systemctl enable docker（容器自带 restart: unless-stopped）"
  exit 0
fi

# ============================================================
# source 形态（venv + systemd + nginx）
# ============================================================
ENV_FILE="$APP_DIR/backend/.env"
PORT="${PORT_ARG:-${QIANSHU_PORT:-8810}}"
case "$PORT" in ''|*[!0-9]*) die "--port 需要数字端口: $PORT" ;; esac

# ---------- 1. 依赖检查（默认安装已自动准备；--no-bootstrap 时给出明确缺失项） ----------
find_python39 || die "缺可用的 Python 3.9+（含 venv/ssl）。去掉 --no-bootstrap 重跑，安装器会自动编译安装 Python 3.9.25；或手工预装后重跑"
PYV=$("$PYTHON_BIN" -c 'import sys; print("%d%02d" % sys.version_info[:2])')
[ "$PYV" -ge 309 ] || die "Python 版本过低（需 3.9+）：$("$PYTHON_BIN" --version 2>&1)"
info "Python: $("$PYTHON_BIN" --version 2>&1)（$PYTHON_BIN）"
if [ "$DEV" = "0" ] && [ ! -d "$APP_DIR/frontend/dist" ]; then
  check_node
fi
if [ "$DEV" = "0" ]; then
  command -v nginx >/dev/null 2>&1 || die "缺 nginx（生产源码形态由 nginx 托管前端并反代后端）。去掉 --no-bootstrap 重跑，安装器会自动安装 nginx"
fi
command -v systemctl >/dev/null 2>&1 || die "缺 systemd（源码形态依赖 systemd 开机自启）"
if command -v ss >/dev/null 2>&1 && ss -tlnH "sport = :$PORT" | grep -q .; then
  die "端口 $PORT 已被占用（可能是已手工启动的千枢/其他服务）。请先停止现有进程或改用其一后重跑"
fi

# ---------- 2. .env（source 形态给 backend/.env，pydantic 自动加载） ----------
gen_env_file "$ENV_FILE"
# 备份目录跟安装位置走：代码缺省 BACKUP_DIR 是 /opt/qianshu/backups，非默认安装目录会写错位置。
if ! grep -q '^BACKUP_DIR=' "$ENV_FILE"; then
  echo "BACKUP_DIR=$APP_DIR/backups" >> "$ENV_FILE"
fi
mkdir -p "$APP_DIR/backups"

# ---------- 2.5 数据库（唯一数据库=PostgreSQL；本机自动供给或外部 DATABASE_URL） ----------
ensure_source_db_config

# ---------- 3. Python 依赖（venv；有 vendor/ 自动离线） ----------
info "安装 Python 依赖（backend/.venv）..."
"$PYTHON_BIN" -m venv "$APP_DIR/backend/.venv"
PIP_OPTS=""
if [ -d "$APP_DIR/vendor" ]; then
  PIP_OPTS="--no-index --find-links $APP_DIR/vendor"
  info "检测到 vendor/，离线安装（目标机零外网）"
fi
"$APP_DIR/backend/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
run_logged "安装 Python 依赖" \
  "$APP_DIR/backend/.venv/bin/pip" install $PIP_OPTS -r "$APP_DIR/backend/requirements.txt" \
  || die "依赖安装失败${PIP_OPTS:+（离线模式：vendor/ 轮子不全）}"
rewrite_copilotd_config   # venv 建好后再改写（start_cmd 解释器指向 backend/.venv）

# ---------- 3.5 数据库连通性探测（PostgreSQL；启动服务前先验配置） ----------
DB_URL_CONF=$(read_env_val "$ENV_FILE" DATABASE_URL)
[ -n "$DB_URL_CONF" ] || die "数据库未配置：$ENV_FILE 缺 DATABASE_URL（安装器应已写入，请查安装日志）"
DB_URL="$DB_URL_CONF" "$APP_DIR/backend/.venv/bin/python" - <<'PY' \
  || die "PostgreSQL 连接失败：请核对 DATABASE_URL/网络/账号授权后重跑（docs/active/deployment.md）"
import os
import sys

url = os.environ["DB_URL"].strip()
if url.startswith("postgres://"):
    url = "postgresql://" + url[len("postgres://"):]
if not url.startswith(("postgresql://", "postgresql+")):
    print("DATABASE_URL 必须指向 PostgreSQL（SQLite 已废除）", file=sys.stderr)
    sys.exit(1)
try:
    from sqlalchemy import create_engine, text
    eng = create_engine(url, pool_pre_ping=True)
    with eng.connect() as conn:
        conn.execute(text("SELECT 1"))
except ImportError as e:
    print(f"缺 PostgreSQL 驱动: {e}（requirements 已含 psycopg2-binary，重装依赖即可）", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"连接失败: {str(e)[:200]}", file=sys.stderr)
    sys.exit(1)
print("pg ok")
PY
info "数据库: PostgreSQL（连通正常）"

# ---------- 4. 前端产物 ----------
if [ "$DEV" = "1" ]; then
  if [ ! -x "$APP_DIR/frontend/node_modules/.bin/vite" ]; then
    check_node
    run_logged "安装前端依赖（npm ci）" \
      bash -c 'cd "$1" && npm ci' _ "$APP_DIR/frontend" \
      || die "前端依赖安装失败"
  fi
else
  if [ ! -f "$APP_DIR/frontend/dist/index.html" ]; then
    run_logged "构建前端（npm ci && npm run build）" \
      bash -c 'cd "$1" && npm ci && npm run build' _ "$APP_DIR/frontend" \
      || die "前端构建失败"
  else
    info "frontend/dist 已存在，跳过构建"
  fi
fi

# ---------- 5. nginx 站点（生产） ----------
if [ "$DEV" = "0" ]; then
  info "配置 nginx（/etc/nginx/conf.d/qianshu.conf）..."
  # 按实际安装位置/后端端口改写模板（root 路径 + /api/、/ws/ 反代端口）
  sed -e "s|/opt/qianshu|$APP_DIR|g" \
      -e "s|127.0.0.1:8810|127.0.0.1:$PORT|g" \
      "$APP_DIR/deploy/nginx/qianshu.conf" > /etc/nginx/conf.d/qianshu.conf
  nginx -t || die "nginx 配置校验失败"
  # nginx 静态目录可达性：worker 以 nginx 用户读 dist——装在 /root 等 700 路径链下会 403/500
  NGX_USER=$(nginx -T 2>&1 | sed -n 's/^user[ \t]\+\([A-Za-z0-9_-]\+\).*/\1/p' | head -1)
  NGX_USER="${NGX_USER:-nginx}"
  if id "$NGX_USER" >/dev/null 2>&1 && \
     ! su -s /bin/sh "$NGX_USER" -c "test -r '$APP_DIR/frontend/dist/index.html'" 2>/dev/null; then
    warn "nginx 用户（$NGX_USER）读不到 $APP_DIR/frontend/dist——打开页面会 403/500。"
    warn "  常见原因：安装路径在 root 家目录链上（如 /root，权限 700）。修复二选一："
    warn "  1) 换装到普通路径（推荐 /opt/qianshu）后重跑 ./install.sh --mode source"
    warn "  2) chmod o+x 逐级放行路径链（如 chmod o+x /root）"
  fi
  systemctl enable --now nginx 2>/dev/null || true
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
fi

# ---------- 6. systemd unit（开机自启） ----------
info "安装 systemd unit 并启用..."
# unit 模板里的 /opt/qianshu 按实际安装位置改写；非默认端口注入 QIANSHU_PORT
UNIT_TMP=$(mktemp -d)
for u in qianshu-backend qianshu-copilotd; do
  sed "s|/opt/qianshu|$APP_DIR|g" "$APP_DIR/deploy/systemd/$u.service" > "$UNIT_TMP/$u.service"
done
if [ "$DEV" = "1" ]; then
  sed "s|/opt/qianshu|$APP_DIR|g" "$APP_DIR/deploy/systemd/qianshu-frontend.service" > "$UNIT_TMP/qianshu-frontend.service"
fi
if [ "$PORT" != "8810" ]; then
  sed -i "/^Environment=TZ=/a Environment=QIANSHU_PORT=$PORT" "$UNIT_TMP/qianshu-backend.service"
fi
cp "$UNIT_TMP/qianshu-backend.service" "$UNIT_TMP/qianshu-copilotd.service" /etc/systemd/system/
if [ "$DEV" = "1" ]; then
  cp "$UNIT_TMP/qianshu-frontend.service" /etc/systemd/system/
fi
rm -rf "$UNIT_TMP"
systemctl daemon-reload
systemctl enable --now qianshu-backend qianshu-copilotd
if [ "$DEV" = "1" ]; then
  systemctl enable --now qianshu-frontend
fi

# ---------- 7. 健康检查 ----------
wait_backend || die "后端 90 秒内未就绪，请看日志：journalctl -u qianshu-backend -n 50"

# ---------- 8. 打印访问信息 ----------
if [ "$DEV" = "1" ]; then
  print_access 5273
else
  print_access
fi
warn "升级：更新代码 -> backend/.venv/bin/pip install -r backend/requirements.txt -> npm run build -> systemctl restart qianshu-backend"
warn "开机自启：systemctl is-enabled qianshu-backend qianshu-copilotd（应为 enabled）"
