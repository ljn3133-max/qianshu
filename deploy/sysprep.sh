#!/usr/bin/env bash
# 千枢运维平台 · 环境预检与系统调优（install.sh 的前置阶段，也可独立跑）
#
# 用法：
#   ./deploy/sysprep.sh                     # 只读体检（默认，零副作用）——任何变更都不做
#   ./deploy/sysprep.sh --bootstrap         # 首次 compose 安装前置准备：yum 源救援 + 基础工具 + Docker/Compose
#                                           #   （最小化 CentOS 7 必需；不改内核/不跑全量更新）
#   ./deploy/sysprep.sh --bootstrap-source  # 首次源码安装前置准备：yum 源 + Python 3.9+（缺失时源码编译）
#                                           #   + nginx（默认）+ Node 18（--with-node；无预构建 dist 时需要）
#   ./deploy/sysprep.sh --bootstrap-source --with-postgres
#                                           #   源码部署自动安装/初始化 PostgreSQL 15（PGDG，仅监听 127.0.0.1）
#   ./deploy/sysprep.sh --postgres-only     # 只安装/初始化 PostgreSQL 15（install.sh 源码形态按需调用，
#                                           #   不跑体检/调优；已安装并运行则直接跳过）
#   ./deploy/sysprep.sh --registry-mirror-only  # 只按需配置 Docker/Podman 镜像加速（修复拉取镜像失败）
#   ./deploy/sysprep.sh --time-only         # 只校准系统时间（HTTPS 下载/镜像拉取失败时的前置排查）
#   ./deploy/sysprep.sh --fix               # 体检 + 自动调优（SELinux/防火墙/NTP 时间同步/ulimit/
#                                           #   内核参数/yum 源救援与更新/docker 安装与配置/应用数据目录），全幂等
#   ./deploy/sysprep.sh --fix --skip-update # 调优但跳过 yum 软件更新（内网/时间紧张）
#   ./deploy/sysprep.sh --fix --data-dir /data/qianshu   # 指定应用数据持久化目录（默认自动选）
#   ./deploy/sysprep.sh --fix --dry-run     # 干跑：只打印将执行的变更，不改系统（验证/预览用）
#
# 环境变量：
#   DOCKER_REGISTRY_MIRROR   镜像加速地址（逗号分隔）；none/off/0=禁用自动探测与配置。
#                            未设置时：Docker Hub 可达则不动；不可达才配置公共加速
#                            https://docker.m.daocloud.io 并明确告警。离线/内网优先用离线镜像包。
#   QIANSHU_TIME_SYNC        时间校准开关（默认 auto）；off/0/none=跳过。
#                            NTP 未同步且与参考时间偏差超过 QIANSHU_TIME_MAX_SKEW（默认 300 秒）时
#                            自动校准；参考时间优先取 HTTP Date 头（不依赖 TLS），失败再试 NTP。
#   QIANSHU_TIME_HTTP_SOURCES  空格分隔的 HTTP 对时源，默认阿里云/清华/百度镜像站。
#   QIANSHU_TIME_MAX_SKEW    允许的时间偏差秒数（默认 300）。
#   QIANSHU_NTP_SERVER       NTP 兜底服务器（默认 ntp.aliyun.com）。
#   PYTHON_VERSION           源码部署安装的 Python 版本（默认 3.9.25；CentOS 7 的 OpenSSL 1.0.2 可编译）
#   NODE_VERSION             源码部署安装的 Node 版本（默认 18.20.8）
#
# 设计约定：
# - 静默安装：控制台只出步骤行与结论，命令明细全部进日志
#   /var/log/qianshu-sysprep-<时间>.log（不可写时落当前目录）；任何失败提示去查该日志
# - 幂等：重复执行安全——已满足的项直接跳过（skip），不重复改写
# - 备份：改写系统文件前先备份（<原路径>.bak.qianshu.<时间戳>）
# - 不自动重启（SELinux config 改动需重启生效、yum 更新了内核需规划重启窗口——只提示不代做）
# - 防火墙关闭是实施交付惯例（内网专用机）；有安全合规要求时用 --skip-firewall 保留
# - --bootstrap 只修复首次安装必需项（SELinux Enforcing 时改 permissive、只放行端口）；
#   内核参数与 yum 全量更新仍由 --fix 显式触发
set -uo pipefail   # 不用 -e：检测阶段每项独立容错，fail 不炸全局；fix 阶段用 run_step 逐条把守

FIX=0; BOOTSTRAP=0; SOURCE_BOOTSTRAP=0; WITH_NODE=0; WITH_POSTGRES=0; NO_NGINX=0; MIRROR_ONLY=0; POSTGRES_ONLY=0; TIME_ONLY=0
SKIP_UPDATE=0; SKIP_FIREWALL=0; DRY_RUN=0; DATA_DIR_ARG=""; OPEN_PORTS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --fix) FIX=1; shift ;;
    --bootstrap) BOOTSTRAP=1; shift ;;
    --bootstrap-source) SOURCE_BOOTSTRAP=1; BOOTSTRAP=1; shift ;;
    --with-node) WITH_NODE=1; shift ;;
    --with-postgres) WITH_POSTGRES=1; shift ;;
    --postgres-only) POSTGRES_ONLY=1; shift ;;
    --no-nginx) NO_NGINX=1; shift ;;
    --registry-mirror-only) MIRROR_ONLY=1; shift ;;
    --time-only) TIME_ONLY=1; shift ;;
    --skip-update) SKIP_UPDATE=1; shift ;;
    --skip-firewall) SKIP_FIREWALL=1; shift ;;
    --data-dir) DATA_DIR_ARG="${2:?--data-dir 需要目录}"; shift 2 ;;
    --ports) OPEN_PORTS="${2:?--ports 需要逗号分隔端口}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) echo "未知参数: $1（用 --help 看用法）" >&2; exit 1 ;;
  esac
done

TS=$(date +%Y%m%d-%H%M%S)
LOG_CANDIDATES=("/var/log/qianshu-sysprep-$TS.log" "$PWD/qianshu-sysprep-$TS.log")
LOG=""
for f in "${LOG_CANDIDATES[@]}"; do
  if touch "$f" 2>/dev/null; then LOG="$f"; break; fi
done
[ -n "$LOG" ] || { echo "无法创建日志文件（/var/log 与当前目录均不可写）" >&2; exit 1; }
chmod 600 "$LOG" 2>/dev/null || true

info()  { echo -e "\033[32m[千枢]\033[0m $*"; }
warn()  { echo -e "\033[33m[千枢]\033[0m $*"; }
die()   { echo -e "\033[31m[千枢]\033[0m $*" >&2; die_hint; exit 1; }
die_hint() { echo -e "\033[31m[千枢]\033[0m 失败详情查日志：tail -100 $LOG" >&2; }

# 并发保护：避免同机并发执行造成包管理器锁等待和重复改写
# 干跑只读不加锁；正常执行持有独占锁，第二份直接提示退出而不是干等
if [ "$DRY_RUN" = "0" ]; then
  LOCK_FILE="${TMPDIR:-/tmp}/qianshu-sysprep.lock"
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE" || die "无法创建锁文件 $LOCK_FILE"
    flock -n 9 || die "另一个 sysprep 正在运行（同一时刻只允许一份，避免 yum 锁互等/重复改写）——等它结束后再试"
  fi
fi

# run_step <描述> <命令...>：fix 阶段统一入口——明细进日志，控制台只出行结果；dry-run 只打印
run_step() {
  local desc="$1"; shift
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将执行: $desc"
    printf '    CMD: %q ' "$@" >> "$LOG"; echo >> "$LOG"
    return 0
  fi
  info "$desc ..."
  if "$@" >> "$LOG" 2>&1; then
    info "  ✔ $desc"
    return 0
  fi
  warn "  ✘ $desc 失败——详情查日志：tail -50 $LOG"
  return 1
}

# download_verified <url> <目标文件> <期望 SHA256>
# 正常走系统 CA。仅当 TLS 证书校验失败且调用方已提供固定 SHA256 时，才允许用
# curl -k 重试；重试后仍强制比对 SHA256，内容不匹配即丢弃。没有 SHA256 时绝不关闭 TLS。
download_verified() {
  local url="$1" target="$2" expected="$3"
  local tmp="$target.tmp" err actual
  [ -n "$expected" ] || { warn "  未提供 SHA256，拒绝下载：$url"; return 1; }
  err=$(mktemp "${TMPDIR:-/tmp}/qianshu-curl.XXXXXX") || return 1
  local -a curl_common=(
    curl -fL --retry 3 --connect-timeout 20 --max-time 900
    --speed-limit 10240 --speed-time 30
  )
  if ! "${curl_common[@]}" -o "$tmp" "$url" 2>"$err"; then
    cat "$err" >> "$LOG" 2>/dev/null || true
    if grep -qiE 'certificate|SSL|TLS' "$err"; then
      warn "  TLS 证书校验失败（系统时间/CA 可能过期）；已固定 SHA256，改用 --insecure 重试（内容仍校验）"
      if ! "${curl_common[@]}" -k -o "$tmp" "$url" 2>"$err"; then
        cat "$err" >> "$LOG" 2>/dev/null || true
        rm -f "$tmp" "$err"; return 1
      fi
      cat "$err" >> "$LOG" 2>/dev/null || true
    else
      rm -f "$tmp" "$err"; return 1
    fi
  else
    cat "$err" >> "$LOG" 2>/dev/null || true
  fi
  actual=$(sha256sum "$tmp" 2>/dev/null | awk '{print $1}')
  if [ "$actual" != "$expected" ]; then
    warn "  SHA256 不匹配（期望 $expected，实际 ${actual:-空}），丢弃下载文件"
    rm -f "$tmp" "$err"; return 1
  fi
  if ! mv -f "$tmp" "$target"; then
    rm -f "$tmp" "$err"; return 1
  fi
  rm -f "$err"
  return 0
}

# ---- 系统时间校准：时间偏差会直接让 HTTPS 证书校验失败（Node/Python/镜像拉取） ----
time_sync_enabled() {
  case "${QIANSHU_TIME_SYNC:-auto}" in off|0|none|false|no) return 1 ;; esac
  return 0
}

time_reference_epoch() {
  # 用 HTTP Date 头取参考时间：不走 TLS，避免“时间错→证书校验失败→无法对时”的死循环。
  command -v curl >/dev/null 2>&1 || return 1
  local src hdr epoch
  for src in ${QIANSHU_TIME_HTTP_SOURCES:-http://mirrors.aliyun.com http://mirrors.tuna.tsinghua.edu.cn http://www.baidu.com}; do
    hdr=$(curl -sSI -m 4 "$src" 2>/dev/null | tr -d '\r' \
      | awk -F': ' 'tolower($1)=="date"{print $2; exit}')
    [ -n "$hdr" ] || continue
    epoch=$(date -u -d "$hdr" +%s 2>/dev/null) || continue
    [ -n "$epoch" ] && { printf '%s' "$epoch"; return 0; }
  done
  return 1
}

ntp_step() {
  # 一次性步进校时：优先 chronyd -q（不常驻），失败或未装再试 ntpdate。
  # 不把“装了 chronyd”当成“chronyd 可用”：守护进程占用/配置损坏时仍应尝试 ntpdate。
  local srv="${QIANSHU_NTP_SERVER:-ntp.aliyun.com}"
  if command -v chronyd >/dev/null 2>&1; then
    timeout 20 chronyd -q "server $srv iburst" >> "$LOG" 2>&1 && return 0
  fi
  if command -v ntpdate >/dev/null 2>&1; then
    timeout 20 ntpdate -u "$srv" >> "$LOG" 2>&1
    return $?
  fi
  return 127
}

sync_system_time() {
  time_sync_enabled || { info "系统时间校准已禁用（QIANSHU_TIME_SYNC=${QIANSHU_TIME_SYNC:-auto}）"; return 0; }
  [ "$(id -u)" = "0" ] || { warn "非 root，跳过系统时间校准"; return 0; }
  if command -v timedatectl >/dev/null 2>&1 && timedatectl 2>/dev/null | grep -q 'synchronized: yes'; then
    info "系统时间已由 NTP 同步（$(date -u '+%F %T')Z），跳过校准"
    return 0
  fi
  local max_skew="${QIANSHU_TIME_MAX_SKEW:-300}" now ref skew
  case "$max_skew" in ''|*[!0-9]*) warn "QIANSHU_TIME_MAX_SKEW=$max_skew 不是数字，按 300 处理"; max_skew=300 ;; esac
  now=$(date +%s)
  ref=$(time_reference_epoch || true)
  case "$ref" in ''|*[!0-9]*) ref="" ;; esac
  if [ -z "$ref" ]; then
    warn "无法从 HTTP 时间源获取参考时间，尝试 NTP 兜底"
    if [ "$DRY_RUN" = "1" ]; then
      info "（干跑）将尝试 NTP 校时"
      return 0
    fi
    if ntp_step; then
      info "  ✔ 已通过 NTP 校准：$(date -u '+%F %T')Z"
    else
      warn "NTP 校时也不可用；跳过自动校准。如后续下载报 TLS 证书错误，请手工校准时间或设置 QIANSHU_TIME_HTTP_SOURCES"
    fi
    return 0
  fi
  if [ "$now" -ge "$ref" ]; then skew=$((now - ref)); else skew=$((ref - now)); fi
  if [ "$skew" -le "$max_skew" ] 2>/dev/null; then
    info "系统时间偏差 ${skew}s（容差 ${max_skew}s），无需校准"
    return 0
  fi
  warn "系统时间偏差 ${skew}s：本机 $(date -u '+%F %T')Z，参考 $(date -u -d "@$ref" '+%F %T')Z"
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将把系统时间校准到参考时间"
    return 0
  fi
  if date -u -s "@$ref" >> "$LOG" 2>&1; then
    info "  ✔ 系统时间已校准：$(date -u '+%F %T')Z"
  else
    warn "  date 设置失败，尝试 NTP 兜底"
    ntp_step || warn "  NTP 一次性校时失败"
  fi
  command -v hwclock >/dev/null 2>&1 && hwclock --systohc >> "$LOG" 2>&1 || true
  # 长期保持准确：有 chronyd 且无 systemd 情况下不代启服务，仅提示。
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] \
     && systemctl list-unit-files chronyd.service >/dev/null 2>&1; then
    systemctl enable --now chronyd >> "$LOG" 2>&1 || true
  fi
  return 0
}

if [ "$TIME_ONLY" = "1" ]; then
  echo "===== --time-only 开始 $(date '+%F %T') =====" >> "$LOG"
  sync_system_time
  rc=$?
  echo "===== --time-only 完成 $(date '+%F %T') rc=$rc =====" >> "$LOG"
  exit "$rc"
fi
# 会联网/写系统的模式在下载前先校准，避免证书时间错误把整个安装拖死；
# 只读体检（无参数）保持零副作用，不对时。
if [ "$FIX" = "1" ] || [ "$BOOTSTRAP" = "1" ] || [ "$MIRROR_ONLY" = "1" ] || [ "$POSTGRES_ONLY" = "1" ]; then
  sync_system_time || true
fi


echo "===== 千枢环境预检/调优 $(date '+%F %T')  日志: $LOG =====" >> "$LOG"
[ "$DRY_RUN" = "1" ] && info "干跑模式：只打印将执行的变更，不改系统"

# ============================================================
# 体检结果收集（只读，零副作用）
# ============================================================
PASS=0; WARN_N=0; FAIL_N=0
declare -a REPORT=()
mark() {  # mark <ok|warn|fail> <项> <说明>
  local level="$1" item="$2" msg="$3"
  REPORT+=("[$level] $item：$msg")
  case "$level" in
    ok)   PASS=$((PASS+1)) ;;
    warn) WARN_N=$((WARN_N+1)) ;;
    fail) FAIL_N=$((FAIL_N+1)) ;;
  esac
}

OS_ID=""; OS_VER=""
if [ -r /etc/os-release ]; then
  . /etc/os-release
  OS_ID="${ID:-}"; OS_VER="${VERSION_ID:-}"
fi
case "$OS_ID" in
  rocky|almalinux|centos|rhel|ol|openEuler|anolis|fedora)
    mark ok "操作系统" "$OS_ID $OS_VER（RHEL 系，支持）" ;;
  ubuntu|debian)
    mark warn "操作系统" "$OS_ID $OS_VER（可用，但部分调优路径按 RHEL 系写——apt 场景以检测报告为准）" ;;
  "")
    mark fail "操作系统" "读不到 /etc/os-release，非标准 Linux？" ;;
  *)
    mark warn "操作系统" "$OS_ID $OS_VER（未验证过，遇到问题反馈给交付）" ;;
esac

# --- 硬件 ---
CPU=$(nproc 2>/dev/null || echo 0)
MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)
if [ "${CPU:-0}" -ge 4 ] 2>/dev/null; then mark ok "CPU" "${CPU} 核"
else mark warn "CPU" "${CPU} 核（建议 4 核+，低于 2 核不建议部署）"; fi
if [ "${MEM_GB:-0}" -ge 8 ] 2>/dev/null; then mark ok "内存" "${MEM_GB} GB"
else mark warn "内存" "${MEM_GB} GB（建议 8 GB+，低于 4 GB 会频繁 OOM）"; fi

ROOT_AVAIL_GB=$(df -k / 2>/dev/null | awk 'NR==2 {print int($4/1048576)}')
if [ "$ROOT_AVAIL_GB" -ge 20 ] 2>/dev/null; then mark ok "根分区空间" "可用 ${ROOT_AVAIL_GB} GB"
else mark warn "根分区空间" "可用仅 ${ROOT_AVAIL_GB} GB（建议 20 GB+：镜像+指标+日志都在长）"; fi

# 数据盘建议：找可用空间最大的挂载点（排除 / 与临时文件系统）——应用数据应落大盘
BEST_DIR="/"; BEST_AVAIL="$ROOT_AVAIL_GB"
while read -r mp; do
  case "$mp" in /|/boot|/dev/*|/proc/*|/sys/*|/run/*|/tmp*) continue ;; esac
  av=$(df -k "$mp" 2>/dev/null | awk 'NR==2 {print int($4/1048576)}')
  [ -n "$av" ] && [ "$av" -gt "$BEST_AVAIL" ] 2>/dev/null && { BEST_DIR="$mp"; BEST_AVAIL="$av"; }
done < <(df --output=target 2>/dev/null | tail -n +2)
if [ "$BEST_DIR" != "/" ] && [ "$BEST_AVAIL" -gt "$ROOT_AVAIL_GB" ] 2>/dev/null; then
  mark warn "数据盘" "检测到更合适的持久化挂载点 $BEST_DIR（可用 ${BEST_AVAIL} GB > 根分区 ${ROOT_AVAIL_GB} GB）——建议 --data-dir $BEST_DIR/qianshu"
  SUGGEST_DATA_DIR="$BEST_DIR/qianshu"
else
  SUGGEST_DATA_DIR=""
fi

# --- 网络（在线/离线判定决定 yum 更新与 docker 安装策略） ---
# HTTPS 可能因旧 CA/代理失败，HTTP 与 wget 作为兜底；yum 本身能通时仍按在线处理。
ONLINE=0
if command -v curl >/dev/null 2>&1; then
  for u in https://mirrors.aliyun.com http://mirrors.aliyun.com; do
    if curl -fsS -o /dev/null -m 8 "$u" 2>/dev/null; then ONLINE=1; break; fi
  done
elif command -v wget >/dev/null 2>&1; then
  for u in https://mirrors.aliyun.com http://mirrors.aliyun.com; do
    if wget -q -T 8 -O /dev/null "$u" 2>/dev/null; then ONLINE=1; break; fi
  done
fi
if [ "$ONLINE" = "1" ]; then
  mark ok "外网连通" "mirrors.aliyun.com 可达（在线安装模式）"
else
  mark warn "外网连通" "不可达（或无 curl/wget）——按离线模式处理：yum 更新与 docker 在线安装将跳过"
fi
if getent hosts mirrors.aliyun.com >/dev/null 2>&1; then
  mark ok "DNS 解析" "正常"
else
  mark warn "DNS 解析" "mirrors.aliyun.com 解析失败（离线环境属正常；在线环境请查 /etc/resolv.conf）"
fi
if command -v timedatectl >/dev/null 2>&1; then
  if timedatectl 2>/dev/null | grep -q "synchronized: yes"; then
    mark ok "时间同步" "NTP 已同步"
  else
    mark warn "时间同步" "NTP 未同步——告警时序/基线/账单日期全部依赖时间正确"
  fi
fi

# --- ulimit ---
NOFILE=$(ulimit -n 2>/dev/null || echo 0)
if [ "$NOFILE" -ge 65535 ] 2>/dev/null; then mark ok "ulimit(nofile)" "$NOFILE"
else mark warn "ulimit(nofile)" "$NOFILE（低于 65535——后端 24 个采集循环文件句柄需求大，--fix 可调）"; fi

# --- SELinux ---
SEL_STATUS="unknown"
if command -v getenforce >/dev/null 2>&1; then
  SEL_STATUS=$(getenforce 2>/dev/null || echo unknown)
elif [ -r /sys/fs/selinux/enforce ]; then
  [ "$(cat /sys/fs/selinux/enforce 2>/dev/null)" = "1" ] && SEL_STATUS=Enforcing || SEL_STATUS=Permissive
elif grep -qE '^SELINUX=enforcing' /etc/selinux/config 2>/dev/null; then
  SEL_STATUS=Enforcing
elif grep -qE '^SELINUX=permissive' /etc/selinux/config 2>/dev/null; then
  SEL_STATUS=Permissive
elif grep -qE '^SELINUX=disabled' /etc/selinux/config 2>/dev/null; then
  SEL_STATUS=Disabled
fi
case "$SEL_STATUS" in
  Disabled) mark ok "SELinux" "已关闭" ;;
  Permissive) mark warn "SELinux" "Permissive（容器/端口可能被拦记录，--fix 可关闭）" ;;
  Enforcing) mark warn "SELinux" "Enforcing——docker 挂载/绑定挂载常见权限报错，--fix 可关闭" ;;
  *) mark warn "SELinux" "状态未知（无 getenforce）" ;;
esac

# --- 防火墙 ---
if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active firewalld >/dev/null 2>&1; then
  mark warn "防火墙" "firewalld 运行中——实施交付惯例关闭（内网专用机）；有合规要求则用 --skip-firewall 并自行放行端口 80/8810/8080"
else
  mark ok "防火墙" "未运行（或未安装 firewalld）"
fi

# --- 内核参数 ---
kp_check() {  # kp_check <键> <建议下限|建议值> <说明>
  local key="$1" want="$2" desc="$3" cur
  cur=$(sysctl -n "$key" 2>/dev/null || echo "")
  if [ -z "$cur" ]; then mark warn "内核参数" "$key 读不到（$desc）"; return; fi
  case "$want" in
    =*) [ "$cur" = "${want#=}" ] && mark ok "内核参数" "$key=$cur（$desc）" \
       || mark warn "内核参数" "$key=$cur（建议 ${want#=}，$desc）" ;;
    *)  [ "$cur" -ge "$want" ] 2>/dev/null && mark ok "内核参数" "$key=$cur" \
       || mark warn "内核参数" "$key=$cur（建议 ≥$want，$desc）" ;;
  esac
}
kp_check net.core.somaxconn 32768 "高并发 accept 队列"
kp_check fs.file-max 1048576 "全机文件句柄上限"
kp_check vm.max_map_count 262144 "进程内存映射段上限（ VictoriaMetrics/索引类组件依赖）"
kp_check vm.overcommit_memory =1 "防止内存分配被过严拒绝（OOM 误杀）"
SWAP=$(sysctl -n vm.swappiness 2>/dev/null || echo 60)
if [ "$SWAP" -le 10 ] 2>/dev/null; then mark ok "swappiness" "$SWAP"
else mark warn "swappiness" "$SWAP（建议 ≤10：平台是常驻服务，频繁换页拖慢采集与查询）"; fi

# --- 容器运行时 ---
RT="none"; RT_IS_PODMAN_SHIM=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if docker --version 2>/dev/null | grep -qi podman; then
    RT_IS_PODMAN_SHIM=1   # docker 是 podman 兼容 shim（podman 5.x 提供）——data-root 语义不适用
    RT=podman
    mark ok "容器运行时" "podman（docker 兼容 shim：$(docker --version 2>/dev/null | head -1)）——compose 形态可用 podman-compose"
  else
    RT=docker
  fi
fi
if [ "$RT" = "docker" ]; then
  if grep -q '"data-root"' /etc/docker/daemon.json 2>/dev/null; then
    mark ok "容器运行时" "docker（data-root 已持久化: $(grep -o '"data-root":[^,}]*' /etc/docker/daemon.json | head -1 | cut -d'"' -f4)）"
  else
    mark warn "容器运行时" "docker 在用默认 $(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)——未持久化到数据盘（镜像/容器会吃满根分区），--fix 可配"
  fi
  if grep -q '"max-size"' /etc/docker/daemon.json 2>/dev/null; then
    mark ok "docker 日志轮转" "log-opts 已配置"
  else
    mark warn "docker 日志轮转" "未配 log-opts——容器 stdout 不轮转会吃满磁盘，--fix 可配"
  fi
elif [ "$RT_IS_PODMAN_SHIM" = "1" ]; then
  :   # shim 已在上分支报告
elif command -v docker >/dev/null 2>&1; then
  mark warn "容器运行时" "docker 已装但 daemon 未运行（--fix 可尝试启动）"
  RT=docker-stopped
elif command -v podman >/dev/null 2>&1; then
  mark ok "容器运行时" "podman（compose 形态自动兜底用 podman-compose）"
  RT=podman
else
  mark warn "容器运行时" "docker/podman 均未安装（compose 形态需要，--fix 可装）"
fi

# --- 端口占用（compose 形态核心端口） ---
for p in 8810 8080 5432; do
  if command -v ss >/dev/null 2>&1 && ss -tlnH "sport = :$p" 2>/dev/null | grep -q .; then
    OWNER=$(ss -tlnpH "sport = :$p" 2>/dev/null | grep -o 'users:(([^)]*))' | head -1)
    mark warn "端口占用" "$p 已被占用（$OWNER）——若是历史安装，先 stop 再装；或 install.sh --port 换端口"
  fi
done

# ============================================================
# 打印体检报告
# ============================================================
echo
echo "================ 环境体检报告 ================"
for line in "${REPORT[@]}"; do
  case "$line" in
    "[ok]")   echo -e "  \033[32m✔\033[0m ${line#\[ok\] }" ;;
    "[warn]") echo -e "  \033[33m▲\033[0m ${line#\[warn\] }" ;;
    "[fail]") echo -e "  \033[31m✘\033[0m ${line#\[fail\] }" ;;
    *) echo "  $line" ;;
  esac
done
echo "=============================================="
echo "  汇总: $PASS 通过 / $WARN_N 警告 / $FAIL_N 阻断     日志: $LOG"

if [ "$FAIL_N" -gt 0 ] && [ "$FIX" != "1" ] && [ "$BOOTSTRAP" != "1" ] && [ "$MIRROR_ONLY" != "1" ] && [ "$POSTGRES_ONLY" != "1" ]; then
  die "存在 $FAIL_N 项阻断问题——先解决再安装（或修复后重跑本脚本确认）"
fi

# ============================================================
# --fix：系统调优（全部幂等，改前备份）
# ============================================================
if [ "$FIX" != "1" ] && [ "$BOOTSTRAP" != "1" ] && [ "$MIRROR_ONLY" != "1" ] && [ "$POSTGRES_ONLY" != "1" ]; then
  echo
  info "以上问题可自动修复：$0 --fix （--dry-run 先预览；--skip-update 跳过 yum 更新）"
  info "最小化 CentOS 7 首次安装：$0 --bootstrap（只准备 yum/基础工具/Docker，不改内核）"
  info "源码部署依赖：$0 --bootstrap-source；镜像拉取失败：$0 --registry-mirror-only"
  exit 0
fi

# ---- 4.5 yum 源救援 + 首次安装前置准备 ----
# CentOS 7 于 2024-06 EOL：mirrorlist.centos.org 已停，yum 任何操作都报
# "Cannot find a valid baseurl"。最小化镜像没有 python3，也无法靠用户手工换源。
# 判定不能再用 `yum repolist`（镜像解析失败时它仍可能 exit 0），必须看 makecache。
is_centos7() {
  [ "$OS_ID" = "centos" ] || return 1
  case "$OS_VER" in 7|7.*) return 0 ;; *) return 1 ;; esac
}
is_podman_docker_shim() {  # RHEL9/CentOS9 常见：/usr/bin/docker 由 podman-docker 提供
  command -v docker >/dev/null 2>&1 || return 1
  docker --version 2>/dev/null | grep -qi podman
}
yum_makecache_ok() {  # 全仓库缓存（非 CentOS 7 的常规检查）
  local pm="${1:-$PKG_MGR_YUM}"
  [ -n "$pm" ] || return 1
  # 国内到归档源可能较慢；首次建缓存 10 分钟上限，避免慢链路被误判为源不可用
  timeout 600 "$pm" -q makecache >> "$LOG" 2>&1
}
yum_base_ready() {    # 只验证 base，避免第三方源/慢镜像误判主源不可用
  local pm="${1:-$PKG_MGR_YUM}"
  [ -n "$pm" ] || return 1
  timeout 600 "$pm" -q --disablerepo='*' --enablerepo=base makecache >> "$LOG" 2>&1
}
write_centos7_vault_repo() {
  local f=/etc/yum.repos.d/CentOS-Base.repo
  cat > "$f.tmp" <<'EOF'
# 千枢安装器生成：CentOS 7 已 EOL，使用阿里云 vault 归档源（HTTPS + GPG 校验）
[base]
name=CentOS-7.9.2009 - Base - aliyun vault
baseurl=https://mirrors.aliyun.com/centos-vault/7.9.2009/os/$basearch/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7

[updates]
name=CentOS-7.9.2009 - Updates - aliyun vault
baseurl=https://mirrors.aliyun.com/centos-vault/7.9.2009/updates/$basearch/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7

[extras]
name=CentOS-7.9.2009 - Extras - aliyun vault
baseurl=https://mirrors.aliyun.com/centos-vault/7.9.2009/extras/$basearch/
enabled=1
gpgcheck=1
gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-CentOS-7
EOF
  mv -f "$f.tmp" "$f"
  # EPEL 单独文件，避免与用户已有 epel 定义重名
  if [ ! -f /etc/yum.repos.d/qianshu-epel7.repo ]; then
    cat > /etc/yum.repos.d/qianshu-epel7.repo <<'EOF'
[qianshu-epel]
name=EPEL 7 archive - aliyun
baseurl=https://mirrors.aliyun.com/epel-archive/7/$basearch/
enabled=1
gpgcheck=1
gpgkey=https://mirrors.aliyun.com/epel/RPM-GPG-KEY-EPEL-7
# EPEL 只是补充源，不可达时不应阻断 base 的工具安装
skip_if_unavailable=1
EOF
  fi
}
fix_yum_repos() {
  command -v yum >/dev/null 2>&1 || return 0
  local pm="${PKG_MGR_YUM:-$(command -v yum)}" eol=0
  # CentOS 7 官方 mirrorlist 已于 2024-06 下线：直接换源，避免 makecache 在死域名上
  # 等满超时（最小化镜像首次安装常见，用户会误以为脚本卡死）。
  if is_centos7 && grep -qs 'mirrorlist.centos.org' /etc/yum.repos.d/CentOS-Base.repo; then
    eol=1
    warn "  检测到 CentOS 7 已下线的 mirrorlist.centos.org，直接切换阿里云 vault 源..."
  fi
  if [ "$eol" = "0" ]; then
    # 全仓库可用优先（兼容客户内网源/自定义 repo ID）；单个第三方源坏时再看 base 是否可用
    if yum_makecache_ok "$pm"; then
      info "yum 源可用，跳过救援"
      return 0
    fi
    if is_centos7; then
      if yum_base_ready "$pm"; then
        info "yum 基础源可用（其他源有异常），跳过救援"
        return 0
      fi
    else
      warn "  yum 源不可用（当前发行版非 CentOS 7，不自动改写源；请先修复系统源）"
      return 1
    fi
  fi

  # 外网探测失败不代表 yum 不可用（旧 CA/代理常见）：继续尝试，失败由日志定位
  [ "$ONLINE" = "1" ] || info "  外网探测不可用，仍尝试 CentOS 7 vault 源（如经代理可继续）"
  warn "  yum 源不可用（CentOS 7 EOL，mirrorlist 已下线）——切换阿里云 vault 归档源..."
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将备份 /etc/yum.repos.d/CentOS-*.repo 并写入 CentOS 7.9.2009 vault + EPEL 7 归档源"
    return 0
  fi
  local backup_dir="/etc/yum.repos.d/qianshu-backup-$TS"
  mkdir -p "$backup_dir"
  local moved=0 repo
  for repo in /etc/yum.repos.d/CentOS-*.repo; do
    [ -f "$repo" ] || continue
    if mv -f "$repo" "$backup_dir/"; then
      moved=$((moved+1))
    else
      warn "  ✘ 无法备份旧源文件：$repo（权限/挂载异常）"
      return 1
    fi
  done
  write_centos7_vault_repo
  "$pm" clean all >> "$LOG" 2>&1 || true
  if yum_base_ready "$pm"; then
    info "  ✔ yum 源已切换为 CentOS 7.9.2009 vault（原 CentOS-*.repo 共 $moved 个已移至 $backup_dir，未删除）"
    return 0
  fi
  warn "  ✘ yum 源修复未成功——详情查日志：tail -100 $LOG"
  return 1
}

# ---- 4.6 首次安装前置准备（--bootstrap / install.sh 默认调用） ----
ensure_base_tools() {
  local missing="" p repo_opts="--disablerepo=qianshu-epel"
  for p in curl ca-certificates tar gzip unzip xz iproute libselinux-utils; do
    rpm -q "$p" >/dev/null 2>&1 || missing="$missing $p"
  done
  [ -n "$missing" ] || { info "基础工具齐全，跳过"; return 0; }
  # 这些包都在 CentOS 7 的 base；只启用 base，避免首次安装被 updates/extras/EPEL
  # 的大体积元数据拖住。非 CentOS 7 只排除安装器自己加的 EPEL。
  if is_centos7; then
    repo_opts="$repo_opts --disablerepo=updates --disablerepo=extras"
  fi
  # 不以 ONLINE 探测为准：curl 缺失时探测本身就不准；yum 源已修复就直接尝试。
  # shellcheck disable=SC2086
  run_step "安装基础工具:$missing" "$PKG_MGR_YUM" install -y $repo_opts $missing
}

# ---- 4.6a 源码部署依赖：Python 3.9+ / nginx / Node（最小化 CentOS 7 没有） ----
PYTHON_VERSION="${PYTHON_VERSION:-3.9.25}"
PYTHON_SHA256="${PYTHON_SHA256:-a7438eabd3a48139f42d4e058096af8d880b0bb6e8fb8c78838892e4ce5583f2}"
PYTHON_PREFIX="${PYTHON_PREFIX:-/usr/local}"
PYTHON_BIN=""
NODE_VERSION="${NODE_VERSION:-18.20.8}"
NODE_SHA256="${NODE_SHA256:-}"
NODE_PREFIX="${NODE_PREFIX:-/usr/local/node-v$NODE_VERSION}"

python_usable() {  # 版本 >=3.9 且 venv/ssl 可用，避免选到残缺的系统 python（SQLite 已废除）
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
    if python_usable "$p"; then PYTHON_BIN="$p"; return 0; fi
  done
  return 1
}
ensure_python() {
  if find_python39; then
    info "Python 已满足（$("$PYTHON_BIN" --version 2>&1)，$PYTHON_BIN），跳过安装"
    return 0
  fi
  info "未找到可用的 Python 3.9+，准备从源码安装 Python $PYTHON_VERSION（约 5-10 分钟）..."
  [ "$DRY_RUN" = "1" ] && { info "（干跑）将安装 gcc/make/开发库并编译 Python $PYTHON_VERSION 到 $PYTHON_PREFIX"; return 0; }

  # CentOS 7 的 OpenSSL 1.0.2 能编译 Python 3.9；3.10+ 需要 OpenSSL 1.1.1，故固定 3.9.x。
  local repo_opts="--disablerepo=qianshu-epel"
  if is_centos7; then
    repo_opts="$repo_opts --disablerepo=updates --disablerepo=extras"
  fi
  # shellcheck disable=SC2086
  run_step "安装 Python 编译依赖" "$PKG_MGR_YUM" install -y $repo_opts \
    gcc make zlib-devel openssl-devel libffi-devel bzip2-devel readline-devel xz-devel \
    || return 1

  local short="${PYTHON_VERSION%.*}" build_dir tarball url
  local -a urls=(
    "https://mirrors.huaweicloud.com/python/$PYTHON_VERSION/Python-$PYTHON_VERSION.tgz"
    "https://mirrors.aliyun.com/python-release/source/Python-$PYTHON_VERSION.tgz"
    "https://registry.npmmirror.com/-/binary/python/$PYTHON_VERSION/Python-$PYTHON_VERSION.tgz"
    "https://www.python.org/ftp/python/$PYTHON_VERSION/Python-$PYTHON_VERSION.tgz"
  )
  build_dir="/usr/local/src/qianshu-python-$PYTHON_VERSION-$TS"
  mkdir -p "$build_dir" || return 1
  tarball="$build_dir/Python-$PYTHON_VERSION.tgz"
  for url in "${urls[@]}"; do
    info "  下载 $url"
    if download_verified "$url" "$tarball" "$PYTHON_SHA256"; then
      break
    fi
    warn "  下载/校验失败，尝试下一个镜像"
  done
  [ -f "$tarball" ] || { warn "  Python 源码下载失败——请检查网络/代理/系统时间（date -u）和 ca-certificates，或设置 PYTHON_VERSION/镜像"; return 1; }
  if ! ( cd "$build_dir" && tar -xzf "$tarball" && cd "Python-$PYTHON_VERSION" \
         && ./configure --prefix="$PYTHON_PREFIX" --with-ensurepip=install \
         && make -j"$(nproc 2>/dev/null | awk '{print ($1>4)?4:$1}')" \
         && make altinstall ) >> "$LOG" 2>&1; then
    warn "  Python 编译失败——详情查日志：tail -60 $LOG"
    return 1
  fi
  PYTHON_BIN="$PYTHON_PREFIX/bin/python$short"
  if ! python_usable "$PYTHON_BIN"; then
    warn "  Python 安装后自检失败（ssl/venv 缺失）：$PYTHON_BIN"
    return 1
  fi
  # 仅在系统完全没有 python3 时提供 python3 入口；已有 3.6 等版本不覆盖，避免影响系统工具。
  if ! command -v python3 >/dev/null 2>&1; then
    ln -sf "$PYTHON_BIN" /usr/local/bin/python3
    [ -x "$PYTHON_PREFIX/bin/pip$short" ] && ln -sf "$PYTHON_PREFIX/bin/pip$short" /usr/local/bin/pip3
  fi
  info "  ✔ Python $PYTHON_VERSION 已安装：$PYTHON_BIN"
  return 0
}


# ---- 4.6a2 源码部署数据库：PostgreSQL 15（PGDG；只监听 127.0.0.1） ----
# 库/角色由 install.sh 在拿到 .env 口令后创建；本函数只负责软件包、initdb、启动。
PG_VERSION="${PG_VERSION:-15}"
PG_REPO_MIRRORS=(
  "https://mirrors.huaweicloud.com/postgresql/repos/yum"
  "https://mirrors.aliyun.com/postgresql/repos/yum"
  "https://download.postgresql.org/pub/repos/yum"
)

pg_server_installed() { rpm -q "postgresql${PG_VERSION}-server" >/dev/null 2>&1; }
pg_server_active() {
  command -v systemctl >/dev/null 2>&1 && systemctl is-active "postgresql-$PG_VERSION" >/dev/null 2>&1
}

ensure_postgres() {
  if [ -n "${DATABASE_URL:-}" ]; then
    info "已显式设置 DATABASE_URL（外部 PostgreSQL），跳过本机 PostgreSQL 安装"
    return 0
  fi
  local os_major="${OS_VER%%.*}" arch rel base chosen="" key=/etc/pki/rpm-gpg/RPM-GPG-KEY-PGDG
  local data="/var/lib/pgsql/$PG_VERSION/data"
  [ -n "$os_major" ] || { warn "无法识别系统主版本（/etc/os-release VERSION_ID）"; return 1; }
  arch=$(uname -m)
  rel="rhel-$os_major-$arch"

  if pg_server_installed && [ -s "$data/PG_VERSION" ] && pg_server_active; then
    info "PostgreSQL $PG_VERSION 已安装并运行，跳过"
    return 0
  fi
  # 5432 被其他实例占用时本机 PG 无法启动（常见于机器已有 PostgreSQL）；
  # 明确提示改用外部 DATABASE_URL，而不是等 systemctl 起不来再排查。
  if command -v ss >/dev/null 2>&1 && ss -tlnH "sport = :5432" 2>/dev/null | grep -q . && ! pg_server_active; then
    warn "  端口 5432 已被其他进程占用：$(ss -tlnpH "sport = :5432" 2>/dev/null | head -1)"
    warn "  请释放 5432，或设置 DATABASE_URL 指向该 PostgreSQL 后重跑（走外部库路径）"
    return 1
  fi
  [ "$(id -u)" = "0" ] || { warn "安装/初始化 PostgreSQL 需要 root"; return 1; }
  [ -n "${PKG_MGR_YUM:-}" ] || { warn "未找到 yum/dnf"; return 1; }

  if ! pg_server_installed; then
    # 选择可达的 PGDG 镜像（官方源在国内常较慢）
    for base in "${PG_REPO_MIRRORS[@]}"; do
      if curl -fsI --retry 1 -m 20 "$base/$PG_VERSION/redhat/$rel/repodata/repomd.xml" \
           >/dev/null 2>&1; then chosen="$base"; break; fi
    done
    [ -n "$chosen" ] || { warn "  PGDG 仓库不可达（已尝试华为/阿里云/官方）——请检查网络或手工安装 PostgreSQL $PG_VERSION"; return 1; }
    mkdir -p /etc/pki/rpm-gpg
    if [ ! -s "$key" ]; then
      if ! run_step "下载 PGDG 签名公钥" curl -fsSL --retry 2 -m 60 "$chosen/RPM-GPG-KEY-PGDG" -o "$key"; then
        return 1
      fi
    fi
    cat > /etc/yum.repos.d/qianshu-pgdg.repo <<EOF
[qianshu-pgdg]
name=PostgreSQL $PG_VERSION (qianshu)
baseurl=$chosen/$PG_VERSION/redhat/$rel
enabled=1
gpgcheck=1
repo_gpgcheck=1
gpgkey=file://$key
EOF
    run_step "安装 PostgreSQL $PG_VERSION（PGDG）" \
      "$PKG_MGR_YUM" install -y "postgresql${PG_VERSION}-server" "postgresql${PG_VERSION}" \
      || return 1
  fi

  if [ ! -s "$data/PG_VERSION" ]; then
    local setup="/usr/pgsql-$PG_VERSION/bin/postgresql-$PG_VERSION-setup"
    [ -x "$setup" ] || { warn "  找不到 $setup（PGDG 包不完整）"; return 1; }
    run_step "初始化 PostgreSQL 数据目录" "$setup" initdb || return 1
  fi
  # 只监听回环：应用与库同机；不向网络暴露 5432
  if [ -f "$data/postgresql.conf" ] && \
     ! grep -qE "^[[:space:]]*listen_addresses[[:space:]]*=[[:space:]]*'127\.0\.0\.1'" "$data/postgresql.conf"; then
    backup_file "$data/postgresql.conf"
    sed -i "s/^[#[:space:]]*listen_addresses.*/listen_addresses = '127.0.0.1'/" "$data/postgresql.conf"
  fi
  # 应用走本机 TCP + 口令（scram-sha-256）：不同发行版 initdb 默认 host 认证可能是
  # ident/trust，必须显式落一条规则；local socket（runuser -u postgres 建库用）不动。
  local hba="$data/pg_hba.conf"
  if [ -f "$hba" ] && \
     ! grep -qE "^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1/32[[:space:]]+scram-sha-256" "$hba"; then
    backup_file "$hba"
    sed -i -E '/^[[:space:]]*host[[:space:]]+all[[:space:]]+all[[:space:]]+127\.0\.0\.1\/32[[:space:]]/d' "$hba"
    printf '\n# 千枢：应用走本机 TCP + 口令认证\nhost    all             all             127.0.0.1/32            scram-sha-256\n' >> "$hba"
  fi
  run_step "启动并设置开机自启（postgresql-$PG_VERSION）" systemctl enable --now "postgresql-$PG_VERSION" \
    || return 1
  local i
  for i in $(seq 1 60); do
    pg_server_active && break
    sleep 1
  done
  pg_server_active || { warn "  PostgreSQL $PG_VERSION 未能启动——查看 journalctl -u postgresql-$PG_VERSION"; return 1; }
  info "  ✔ PostgreSQL $PG_VERSION 已就绪（127.0.0.1:5432）"
  return 0
}

# ---- 单独供给 PostgreSQL（install.sh 源码形态按需调用）：不跑体检/调优 ----
if [ "$POSTGRES_ONLY" = "1" ]; then
  [ "$(id -u)" = "0" ] || die "安装 PostgreSQL 需要 root"
  command -v rpm >/dev/null 2>&1 || die "当前不是 RPM 系发行版（缺 rpm/yum）"
  PKG_MGR_YUM="${PKG_MGR_YUM:-$(command -v dnf || command -v yum || true)}"
  [ -n "$PKG_MGR_YUM" ] || die "未找到 yum/dnf，无法安装 PostgreSQL"
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将安装/启动 PostgreSQL $PG_VERSION（PGDG，仅监听 127.0.0.1）"
    exit 0
  fi
  ensure_postgres || die "PostgreSQL $PG_VERSION 安装失败——详见日志"
  exit 0
fi

ensure_nginx() {
  if command -v nginx >/dev/null 2>&1; then
    info "nginx 已安装（$(nginx -v 2>&1)），跳过"
    return 0
  fi
  if run_step "安装 nginx（EPEL）" "$PKG_MGR_YUM" install -y nginx; then
    return 0
  fi
  warn "  EPEL 安装 nginx 失败，改用 nginx.org 官方仓库重试..."
  cat > /etc/yum.repos.d/qianshu-nginx.repo <<'EOF'
[nginx-stable]
name=nginx stable repo
baseurl=https://nginx.org/packages/centos/7/$basearch/
gpgcheck=1
enabled=1
gpgkey=https://nginx.org/keys/nginx_signing.key
EOF
  run_step "安装 nginx（nginx.org）" "$PKG_MGR_YUM" install -y nginx || return 1
  command -v nginx >/dev/null 2>&1
}

node_major() { node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1; }
# Node 18/20 官方二进制要求 glibc >= 2.28；CentOS 7 是 2.17，需换官方 unofficial glibc-217 构建。
glibc_lt() {
  local want="$1" v maj min
  v=$(ldd --version 2>/dev/null | awk 'NR==1{print $NF}')
  [ -n "$v" ] || return 1
  maj=${v%%.*}; min=${v#*.}; min=${min%%.*}
  case "$maj$min" in *[!0-9]*) return 1 ;; esac
  [ "$maj" -lt 2 ] || { [ "$maj" -eq 2 ] && [ "$min" -lt "$want" ]; }
}
node_usable() {
  local nv
  nv=$(node_major || true)
  [ -n "$nv" ] && [ "$nv" -ge 18 ] 2>/dev/null && command -v npm >/dev/null 2>&1
}
ensure_node() {
  if node_usable; then
    info "Node 已满足（$(node -v 2>/dev/null)，$(npm -v 2>/dev/null)），跳过安装"
    return 0
  fi
  info "未找到可用的 Node 18+，准备安装 Node $NODE_VERSION（$NODE_PREFIX）..."
  [ "$DRY_RUN" = "1" ] && { info "（干跑）将下载并解压 Node $NODE_VERSION 到 $NODE_PREFIX"; return 0; }
  local arch node_arch build_dir tarball url expected suffix="" pkg
  arch=$(uname -m 2>/dev/null || echo x86_64)
  # Node 官方包使用 linux-x64 / linux-arm64 命名，不是 uname 的 x86_64/aarch64。
  case "$arch" in
    x86_64|amd64) node_arch=x64 ;;
    aarch64|arm64) node_arch=arm64 ;;
    *) warn "  不支持的 Node 架构：$arch"; return 1 ;;
  esac
  if glibc_lt 28; then
    if [ "$node_arch" = "x64" ]; then
      suffix="-glibc-217"
      info "  系统 glibc 低于 2.28（CentOS 7），使用 Node 官方 glibc-217 构建"
    else
      warn "  系统 glibc 低于 2.28，且 Node 官方未提供 $node_arch 的 glibc-217 构建"
      warn "  请预装 Node 18+，或在发布包中提供预构建 frontend/dist 后跳过前端构建"
      return 1
    fi
  fi
  pkg="node-v$NODE_VERSION-linux-$node_arch$suffix.tar.xz"
  build_dir="/usr/local/src/qianshu-node-$NODE_VERSION-$TS"
  mkdir -p "$build_dir" || return 1
  tarball="$build_dir/$pkg"
  expected="$NODE_SHA256"
  if [ -z "$expected" ]; then
    if [ "$NODE_VERSION" = "18.20.8" ] && [ "$node_arch" = "x64" ] && [ -n "$suffix" ]; then
      expected=37517caab02095e96ad3ae4198777b1c365adaad53cb071e6014867c2e9fb127
    elif [ "$NODE_VERSION" = "18.20.8" ] && [ "$node_arch" = "x64" ]; then
      expected=5467ee62d6af1411d46b6a10e3fb5cacc92734dbcef465fea14e7b90993001c9
    else
      local sha_base="https://mirrors.huaweicloud.com/nodejs/v$NODE_VERSION"
      [ -n "$suffix" ] && sha_base="https://unofficial-builds.nodejs.org/download/release/v$NODE_VERSION"
      expected=$(curl -fsSL -m 20 "$sha_base/SHASUMS256.txt" 2>/dev/null \
        | awk -v f="$pkg" '$2==f{print $1; exit}')
    fi
  fi
  [ -n "$expected" ] || { warn "  无法获得 Node $NODE_VERSION 的 SHA256，拒绝未校验下载"; return 1; }
  local -a urls=()
  if [ -n "$suffix" ]; then
    [ "$node_arch" = "x64" ] || { warn "  CentOS 7 上暂不提供 $node_arch 的 glibc-217 Node 构建"; return 1; }
    urls+=("https://unofficial-builds.nodejs.org/download/release/v$NODE_VERSION/$pkg")
  else
    urls+=(
      "https://mirrors.huaweicloud.com/nodejs/v$NODE_VERSION/$pkg"
      "https://registry.npmmirror.com/-/binary/node/v$NODE_VERSION/$pkg"
      "https://nodejs.org/dist/v$NODE_VERSION/$pkg"
    )
  fi
  for url in "${urls[@]}"; do
    info "  下载 $url"
    if download_verified "$url" "$tarball" "$expected"; then
      break
    fi
    warn "  下载失败，尝试下一个镜像"
  done
  [ -f "$tarball" ] || {
    warn "  Node 下载失败——请检查网络/代理/系统时间（date -u）和 ca-certificates；"
    warn "  也可手工预装 Node 18+ 后加 --no-bootstrap；详情查日志：tail -100 $LOG"
    return 1
  }
  rm -rf "$build_dir/extract" "$NODE_PREFIX"
  mkdir -p "$build_dir/extract"
  if ! tar -xJf "$tarball" -C "$build_dir/extract" >> "$LOG" 2>&1; then
    warn "  Node 解压失败——详情查日志：tail -60 $LOG"; return 1
  fi
  if ! mv "$build_dir/extract/node-v$NODE_VERSION-linux-$node_arch$suffix" "$NODE_PREFIX"; then
    warn "  Node 解压目录不符合预期（node-v$NODE_VERSION-linux-$node_arch$suffix）"
    return 1
  fi
  if ! node_usable; then
    ln -sfn "$NODE_PREFIX/bin/node" /usr/local/bin/node
    ln -sfn "$NODE_PREFIX/bin/npm" /usr/local/bin/npm
    [ -x "$NODE_PREFIX/bin/npx" ] && ln -sfn "$NODE_PREFIX/bin/npx" /usr/local/bin/npx
    hash -r 2>/dev/null || true
  fi
  if node_usable; then
    info "  ✔ Node $NODE_VERSION 已安装：$(node -v 2>/dev/null)，npm $(npm -v 2>/dev/null)"
    return 0
  fi
  warn "  Node 安装后自检失败：node=$(node -v 2>/dev/null || echo 未找到) npm=$(npm -v 2>/dev/null || echo 未找到)"
  return 1
}

bootstrap_source_main() {
  [ "$(id -u)" = "0" ] || die "源码部署前置准备需要 root（yum/编译/systemd 操作）——请用 root 或 sudo 重跑"
  [ -n "${PKG_MGR_YUM:-}" ] || die "未找到 yum/dnf，无法准备源码部署依赖"
  command -v rpm >/dev/null 2>&1 || die "当前不是 RPM 系发行版（缺 rpm/yum）——--bootstrap-source 仅支持 RHEL/CentOS 系"
  info "源码部署前置准备：修复 yum 源 → Python 3.9+ → nginx/Node → PostgreSQL 15（不改内核、不跑全量更新）..."
  if [ "$DRY_RUN" = "1" ]; then
    local dry_extra=""
    [ "$NO_NGINX" != "1" ] && dry_extra="$dry_extra、nginx"
    [ "$WITH_NODE" = "1" ] && dry_extra="$dry_extra、Node $NODE_VERSION"
    [ "$WITH_POSTGRES" = "1" ] && dry_extra="$dry_extra、PostgreSQL $PG_VERSION"
    info "（干跑）将修复 yum 源、安装基础工具和编译依赖、按需编译 Python $PYTHON_VERSION$dry_extra"
    return 0
  fi
  fix_yum_repos || die "yum 源修复失败——CentOS 7 需能访问 mirrors.aliyun.com（详见日志）"
  ensure_base_tools || die "基础工具安装失败——详见日志"
  ensure_python || die "Python 3.9+ 安装失败——详见日志"
  if [ "$NO_NGINX" != "1" ]; then
    ensure_nginx || die "nginx 安装失败——详见日志；开发模式可加 --dev/--no-nginx 跳过"
  fi
  [ "$WITH_NODE" = "1" ] && { ensure_node || die "Node 18+ 安装失败——详见日志"; }
  [ "$WITH_POSTGRES" = "1" ] && { ensure_postgres || die "PostgreSQL $PG_VERSION 安装失败——详见日志"; }
  open_firewall_ports
  local node_note=""
  [ "$WITH_NODE" = "1" ] && node_note="，Node: $(node -v 2>/dev/null || echo 未安装)"
  [ "$WITH_POSTGRES" = "1" ] && node_note="$node_note，PostgreSQL: $(rpm -q --qf '%{VERSION}' "postgresql${PG_VERSION}-server" 2>/dev/null || echo 未安装)"
  info "源码部署前置准备完成 ✔ Python: $("$PYTHON_BIN" --version 2>&1)$node_note"
  info "可继续首次源码安装（无需再手工准备 Python/nginx/Node）"
}
ensure_docker() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    if is_podman_docker_shim; then
      # 不能把 podman-docker 兼容层当 Docker CE 再装一遍；compose 交给 ensure_compose
      info "docker 由 podman 兼容层提供，跳过 Docker CE 安装"; return 0
    fi
    info "docker 已可用，跳过安装"; return 0
  fi
  if is_podman_docker_shim; then
    warn "  docker 命令由 podman 兼容层提供但当前不可用——改走 podman 兜底"
    return 2
  fi
  PKG_MGR=$(command -v dnf || command -v yum || true)
  [ -n "$PKG_MGR" ] || { warn "  未识别包管理器，无法自动安装 docker"; return 1; }
  # repo 添加：dnf 走 dnf-plugins-core + dnf config-manager；
  # el7（yum，无 dnf-plugins-core）走 yum-utils + yum-config-manager（独立命令，非 yum 子命令）
  local repo_plugin="dnf-plugins-core" repo_add="$PKG_MGR config-manager"
  if ! command -v dnf >/dev/null 2>&1; then
    repo_plugin="yum-utils" repo_add="yum-config-manager"
  fi
  # 源策略：阿里云镜像源（国内快） -> 官方源 -> 发行版自带 docker 包 -> podman 兜底
  run_step "安装仓库管理插件（$repo_plugin）" "$PKG_MGR" install -y "$repo_plugin" || return 1
  if ! docker info >/dev/null 2>&1; then
    $repo_add --add-repo "https://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo" >> "$LOG" 2>&1 \
      && sed -i 's#https://download.docker.com#https://mirrors.aliyun.com/docker-ce#g' /etc/yum.repos.d/docker-ce.repo 2>/dev/null || true
    if ! run_step "安装 docker-ce（阿里云源）" "$PKG_MGR" install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin; then
      info "  阿里云源失败，回退 docker 官方源重试..."
      $repo_add --add-repo "https://download.docker.com/linux/centos/docker-ce.repo" >> "$LOG" 2>&1 || true
      run_step "安装 docker-ce（官方源）" "$PKG_MGR" install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin \
        || run_step "安装发行版 docker 包" "$PKG_MGR" install -y docker \
        || { warn "  docker 全部源均失败——改试 podman 兜底"; return 2; }
    fi
  fi
  run_step "启动并自启 docker" systemctl enable --now docker || return 1
  local i
  for i in $(seq 1 60); do
    docker info >/dev/null 2>&1 && break
    sleep 1
  done
  if docker info >/dev/null 2>&1; then
    info "  ✔ docker 就绪（$(docker --version 2>/dev/null | awk '{print $3}')）"
    return 0
  fi
  warn "  docker 装了但 daemon 60 秒内未就绪——详情查日志"
  return 1
}
ensure_podman() {
  PKG_MGR=$(command -v dnf || command -v yum || true)
  [ -n "$PKG_MGR" ] || return 1
  run_step "安装 podman（docker 兜底运行时）" "$PKG_MGR" install -y podman || return 1
  run_step "安装 podman-compose" "$PKG_MGR" install -y podman-compose \
    || warn "  podman-compose 安装失败；若已有其他 compose 提供方可继续"
  return 0
}
ensure_compose() {
  if is_podman_docker_shim; then
    # 兼容层的 compose 由 podman compose 提供；装 Docker CE 的插件不适用于此形态
    docker compose version >/dev/null 2>&1 && return 0
    command -v podman-compose >/dev/null 2>&1 && return 0
    [ -n "${PKG_MGR:-}" ] || PKG_MGR=$(command -v dnf || command -v yum || true)
    [ -n "$PKG_MGR" ] || { warn "  podman 兼容层缺 compose，且找不到 yum/dnf"; return 1; }
    run_step "安装 podman-compose（podman 兼容层）" "$PKG_MGR" install -y podman-compose || true
    docker compose version >/dev/null 2>&1 || command -v podman-compose >/dev/null 2>&1
    return
  fi
  if command -v docker >/dev/null 2>&1; then
    docker compose version >/dev/null 2>&1 && return 0
    command -v docker-compose >/dev/null 2>&1 && return 0
    [ -n "${PKG_MGR:-}" ] || PKG_MGR=$(command -v dnf || command -v yum || true)
    [ -n "$PKG_MGR" ] || { warn "  docker 已装但缺 compose，且找不到 yum/dnf"; return 1; }
    run_step "安装 docker compose 插件" "$PKG_MGR" install -y docker-compose-plugin \
      || run_step "安装 docker-compose（兼容包）" "$PKG_MGR" install -y docker-compose
    docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1
    return
  fi
  if command -v podman >/dev/null 2>&1; then
    podman-compose version >/dev/null 2>&1 && return 0
    podman compose version >/dev/null 2>&1 && return 0
  fi
  return 1
}
REGISTRY_MIRRORS=""
REGISTRY_MIRROR_SOURCE=""

choose_registry_mirrors() {
  # 未显式配置时只在 Docker Hub 不可达时启用公共加速；可达则保持系统原样。
  local configured="${DOCKER_REGISTRY_MIRROR:-}" code
  REGISTRY_MIRRORS=""; REGISTRY_MIRROR_SOURCE=""
  case "$configured" in
    none|off|0) REGISTRY_MIRROR_SOURCE=disabled; return 0 ;;
  esac
  if [ -n "$configured" ]; then
    REGISTRY_MIRRORS=$(printf '%s' "$configured" | tr -d '[:space:]')
    REGISTRY_MIRROR_SOURCE=configured
    return 0
  fi
  command -v curl >/dev/null 2>&1 || return 0
  code=$(curl -sS -o /dev/null -m 8 -w '%{http_code}' https://registry-1.docker.io/v2/ 2>/dev/null || true)
  case "$code" in
    200|401) REGISTRY_MIRROR_SOURCE=direct; return 0 ;;
  esac
  REGISTRY_MIRRORS="https://docker.m.daocloud.io"
  REGISTRY_MIRROR_SOURCE=auto
  return 0
}

configure_podman_registry_mirrors() {
  local conf=/etc/containers/registries.conf.d/999-qianshu-mirrors.conf m mhost wrote=0
  [ -n "$REGISTRY_MIRRORS" ] || return 0
  command -v podman >/dev/null 2>&1 || return 0
  mkdir -p "$(dirname "$conf")" || return 1
  {
    echo "# 千枢安装器生成：Docker Hub 镜像加速（DOCKER_REGISTRY_MIRROR 可覆盖/禁用）"
    echo '[[registry]]'
    echo 'prefix = "docker.io"'
    echo 'location = "docker.io"'
    for m in $(echo "$REGISTRY_MIRRORS" | tr ',' ' '); do
      printf '%s' "$m" | grep -Eq '^https?://[A-Za-z0-9._:/-]+$' || continue
      mhost=${m#https://}; mhost=${mhost#http://}; mhost=${mhost%/}
      echo '[[registry.mirror]]'
      echo "location = \"$mhost\""
      wrote=1
    done
  } > "$conf.tmp"
  [ "$wrote" = 1 ] || { rm -f "$conf.tmp"; return 0; }
  if cmp -s "$conf.tmp" "$conf" 2>/dev/null; then
    rm -f "$conf.tmp"
  else
    mv -f "$conf.tmp" "$conf"
    info "  ✔ podman 镜像加速已配置：$conf"
  fi
}

docker_mirrors_loaded() {
  # 运行中的 docker 是否已加载 registry-mirrors（空数组视为未加载）
  [ -n "$(docker info --format '{{range .RegistryConfig.Mirrors}}{{println .}}{{end}}' 2>/dev/null)" ]
}

ensure_docker_mirrors_effective() {
  # 配置写入磁盘不等于运行态生效：daemon.json 已有 registry-mirrors 时校验运行中的 docker。
  # 未加载先 SIGHUP 无损重载；仍无效且无运行中容器才重启；有容器则只告警，不中断业务。
  local conf="${DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"
  grep -q '"registry-mirrors"' "$conf" 2>/dev/null || return 0
  command -v docker >/dev/null 2>&1 || return 0
  is_podman_docker_shim && return 0
  docker info >/dev/null 2>&1 || return 0
  if docker_mirrors_loaded; then
    info "  ✔ 运行中的 docker 已加载镜像加速"
    return 0
  fi
  if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    systemctl reload docker >> "$LOG" 2>&1 || true
  else
    kill -HUP "$(cat /var/run/docker.pid 2>/dev/null)" 2>/dev/null || true
  fi
  sleep 2
  if docker_mirrors_loaded; then
    info "  ✔ 已无损重载 docker，镜像加速生效"
    return 0
  fi
  if [ -z "$(docker ps -q 2>/dev/null)" ]; then
    run_step "重启 docker（应用镜像加速）" systemctl restart docker || true
    local i
    for i in $(seq 1 30); do
      docker info >/dev/null 2>&1 && break
      sleep 1
    done
    if docker_mirrors_loaded; then
      info "  ✔ 已重启 docker，镜像加速生效"
      return 0
    fi
  fi
  warn "  镜像加速已写入 /etc/docker/daemon.json，但运行中的 docker 未加载。"
  if [ -n "$(docker ps -q 2>/dev/null)" ]; then
    warn "  本机有运行中容器，未自动重启 docker；请执行 systemctl restart docker 后重跑安装器"
  else
    warn "  请查 journalctl -u docker 后执行 systemctl restart docker"
  fi
  warn "  或设置 DOCKER_REGISTRY_MIRROR 指向可用加速/代理；内网环境用离线镜像包"
  return 1
}

configure_docker_logs() {
  # 只补日志轮转/镜像加速，不改 data-root（避免已有镜像/容器停机迁移）。已有配置不盲改。
  command -v docker >/dev/null 2>&1 || return 0
  if is_podman_docker_shim; then
    info "podman 兼容层不使用 /etc/docker/daemon.json，跳过 docker 配置"; return 0
  fi
  local -a mlist=()
  local m i reg_line=""
  for m in $(echo "$REGISTRY_MIRRORS" | tr ',' ' '); do
    printf '%s' "$m" | grep -Eq '^https?://[A-Za-z0-9._:/-]+$' && mlist+=("$m")
  done
  if [ "${#mlist[@]}" -gt 0 ]; then
    reg_line='  "registry-mirrors": ['
    for i in "${!mlist[@]}"; do
      [ "$i" -gt 0 ] && reg_line="$reg_line, "
      reg_line="$reg_line\"${mlist[$i]}\""
    done
    reg_line="$reg_line]"
  fi
  if [ -f /etc/docker/daemon.json ]; then
    local need_merge=0
    grep -q '"max-size"' /etc/docker/daemon.json 2>/dev/null || need_merge=1
    if [ -n "$REGISTRY_MIRRORS" ] && ! grep -q '"registry-mirrors"' /etc/docker/daemon.json 2>/dev/null; then
      need_merge=1
    fi
    if [ "$need_merge" = "0" ]; then
      info "  已有 /etc/docker/daemon.json 已含日志轮转/镜像加速，保持不变"
      ensure_docker_mirrors_effective || return 1
      return 0
    fi
    # 已有配置只做增量合并，保留其他键；解析失败或写回校验失败则不改动原文件。
    local py merged_tmp
    py=$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)
    if [ -z "$py" ]; then
      warn "  已有 /etc/docker/daemon.json 需补充配置，但缺少 python，未自动合并"
      warn "  请手工加入日志轮转${REGISTRY_MIRRORS:+或 \"registry-mirrors\": [$REGISTRY_MIRRORS]} 后重启 docker"
      return 0
    fi
    merged_tmp="/etc/docker/daemon.json.qianshu-merge.$$"
    if "$py" - /etc/docker/daemon.json "$merged_tmp" "$REGISTRY_MIRRORS" <<'PY' >> "$LOG" 2>&1
# -*- coding: utf-8 -*-
import io, json, sys
src, dst, mirrors = sys.argv[1], sys.argv[2], [m for m in sys.argv[3].split(',') if m]
with io.open(src, 'r', encoding='utf-8') as f:
    data = json.load(f)
if not isinstance(data, dict):
    raise SystemExit('daemon.json 顶层不是对象')
changed = False
if 'log-driver' not in data:
    data['log-driver'] = 'json-file'; changed = True
if not isinstance(data.get('log-opts'), dict):
    data['log-opts'] = {}; changed = True
if 'max-size' not in data['log-opts']:
    data['log-opts']['max-size'] = '100m'; changed = True
if 'max-file' not in data['log-opts']:
    data['log-opts']['max-file'] = '3'; changed = True
if mirrors and 'registry-mirrors' not in data:
    data['registry-mirrors'] = mirrors; changed = True
if not changed:
    raise SystemExit(0)
payload = json.dumps(data, indent=2, ensure_ascii=True)
if isinstance(payload, bytes):
    payload = payload.decode('ascii')
with io.open(dst, 'w', encoding='utf-8') as f:
    f.write(payload)
    f.write(u'\n')
PY
       "$py" -c 'import json,sys; json.load(open(sys.argv[1]))' "$merged_tmp" >> "$LOG" 2>&1; then
      backup_file /etc/docker/daemon.json
      mv -f "$merged_tmp" /etc/docker/daemon.json
      info "  ✔ 已增量合并 /etc/docker/daemon.json（保留原有键，原文件已备份）"
      if docker info >/dev/null 2>&1; then
        if [ -z "$(docker ps -aq 2>/dev/null)" ]; then
          if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
            run_step "重载 docker（应用日志/镜像加速配置）" systemctl restart docker || true
          else
            info "  无 systemd，跳过 docker 重载；配置在 docker 下次启动后生效"
          fi
        else
          warn "  已有容器在运行，本次不重启 docker；新增配置在下次重启后生效"
        fi
      fi
      ensure_docker_mirrors_effective || return 1
    else
      rm -f "$merged_tmp"
      warn "  已有 /etc/docker/daemon.json 解析/合并失败，未改动原文件；请手工检查"
    fi
    return 0
  fi
  mkdir -p /etc/docker
  {
    printf '{\n'
    printf '  "log-driver": "json-file",\n'
    printf '  "log-opts": {"max-size": "100m", "max-file": "3"}'
    [ -n "$reg_line" ] && printf ',\n%s' "$reg_line"
    printf '\n}\n'
  } > /etc/docker/daemon.json.tmp
  mv -f /etc/docker/daemon.json.tmp /etc/docker/daemon.json
  if docker info >/dev/null 2>&1; then
    if [ -z "$(docker ps -aq 2>/dev/null)" ]; then
      if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
        run_step "重载 docker（应用日志/镜像加速配置）" systemctl restart docker || true
      else
        info "  无 systemd，跳过 docker 重载；配置在 docker 下次启动后生效"
      fi
    else
      warn "  已有容器在运行，本次不重启 docker；日志轮转与镜像加速在下次重启后生效"
    fi
  fi
  info "  ✔ docker 日志轮转已配置（100m × 3）${reg_line:+，镜像加速已配置}"
  ensure_docker_mirrors_effective || return 1
}

registry_mirror_main() {
  choose_registry_mirrors
  case "$REGISTRY_MIRROR_SOURCE" in
    disabled) info "镜像加速已禁用（DOCKER_REGISTRY_MIRROR=$DOCKER_REGISTRY_MIRROR），不修改容器配置"; return 0 ;;
    direct)   info "Docker Hub 可达，未配置镜像加速"; return 0 ;;
  esac
  [ -n "$REGISTRY_MIRRORS" ] || return 0
  [ "$(id -u)" = "0" ] || { warn "配置镜像加速需要 root，已跳过"; return 1; }
  if [ "$REGISTRY_MIRROR_SOURCE" = "auto" ]; then
    warn "Docker Hub 不可达，自动启用公共镜像加速：$REGISTRY_MIRRORS"
    warn "  如不希望使用第三方镜像，设置 DOCKER_REGISTRY_MIRROR=none 后重跑；内网/离线环境请用带 images/ 的离线包"
  else
    info "使用配置的镜像加速：$REGISTRY_MIRRORS"
  fi
  configure_podman_registry_mirrors || warn "podman 镜像加速配置失败"
  configure_docker_logs || warn "docker 镜像加速配置失败"
  return 0
}
handle_selinux_for_containers() {
  # Enforcing 下 compose 的 bind mount 无 :z 标签，容器写数据卷会 permission denied。
  # 与 --fix 的交付惯例一致设为 permissive（可逆），不直接 disabled。
  # 最小化镜像可能没有 getenforce：直接读 /sys/fs/selinux/enforce 兜底。
  local sel="${SEL_STATUS:-unknown}"
  if command -v getenforce >/dev/null 2>&1; then
    sel=$(getenforce 2>/dev/null || echo "$sel")
  elif [ -r /sys/fs/selinux/enforce ]; then
    [ "$(cat /sys/fs/selinux/enforce 2>/dev/null)" = "1" ] && sel=Enforcing || sel=Permissive
  elif grep -qE '^SELINUX=enforcing' /etc/selinux/config 2>/dev/null; then
    sel=Enforcing
  fi
  [ "$sel" = "Enforcing" ] || { info "SELinux 非 Enforcing（${sel}），跳过容器兼容处理"; return 0; }
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将 setenforce 0 并把 /etc/selinux/config 改为 permissive"
    return 0
  fi
  if command -v setenforce >/dev/null 2>&1; then
    run_step "SELinux 临时设为 permissive" setenforce 0 || true
  else
    run_step "SELinux 临时设为 permissive（写 enforce）" sh -c 'echo 0 > /sys/fs/selinux/enforce' || true
  fi
  if [ -f /etc/selinux/config ] && grep -qE '^SELINUX=enforcing' /etc/selinux/config; then
    cp -a /etc/selinux/config "/etc/selinux/config.bak.qianshu.$TS" 2>/dev/null || true
    sed -i 's/^SELINUX=.*/SELINUX=permissive/' /etc/selinux/config
    warn "  SELinux 已持久化为 permissive（原文件备份为 config.bak.qianshu.$TS）；如需 Enforcing 请自行加 :z 标签后改回"
  fi
}
open_firewall_ports() {
  # 最小化 CentOS 7 可能开着 firewalld。只放行平台端口，不整体关闭防火墙。
  command -v firewall-cmd >/dev/null 2>&1 || { info "firewalld 未安装，跳过端口放行"; return 0; }
  systemctl is-active firewalld >/dev/null 2>&1 || { info "firewalld 未运行，跳过端口放行"; return 0; }
  local ports="${OPEN_PORTS:-8080,8810}" p
  ports="${ports},8080,8810"
  local args=()
  for p in $(echo "$ports" | tr ',' ' '); do
    case "$p" in ''|*[!0-9]*) continue ;; esac
    args+=(--add-port="$p/tcp")
  done
  [ "${#args[@]}" -gt 0 ] || return 0
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将放行防火墙端口：${args[*]}"
    return 0
  fi
  run_step "放行防火墙端口（${args[*]}）" firewall-cmd --permanent "${args[@]}" \
    && run_step "重载防火墙规则" firewall-cmd --reload \
    || warn "  防火墙端口放行失败（不影响本机健康检查，请手工放行 8080/8810）"
}
bootstrap_main() {
  [ "$(id -u)" = "0" ] || die "首次安装前置准备需要 root（yum/docker/systemd 操作）——请用 root 或 sudo 重跑"
  [ -n "${PKG_MGR_YUM:-}" ] || die "未找到 yum/dnf，无法准备安装环境"
  command -v rpm >/dev/null 2>&1 || die "当前不是 RPM 系发行版（缺 rpm/yum）——--bootstrap 仅支持 RHEL/CentOS 系"
  info "首次安装前置准备：修复 yum 源 → 基础工具 → Docker/Compose（不改内核、不跑全量更新）..."
  if [ "$DRY_RUN" = "1" ]; then
    fix_yum_repos || true
    info "（干跑）将安装基础工具、Docker CE + compose 插件并启动 docker"
    info "（干跑）将按需处理 SELinux Enforcing 与防火墙端口"
    return 0
  fi
  fix_yum_repos || die "yum 源修复失败——CentOS 7 需能访问 mirrors.aliyun.com（详见日志）"
  ensure_base_tools || die "基础工具安装失败——详见日志"
  # 容器 bind mount 兼容与端口放行先于启动，避免起完才发现写不了卷/访问不了页面
  handle_selinux_for_containers
  open_firewall_ports
  local rc=0
  ensure_docker || rc=$?
  if [ "$rc" != "0" ]; then
    warn "docker 安装未完成（rc=$rc），尝试 podman 兜底..."
    ensure_podman || die "docker 与 podman 均安装失败——详见日志"
  fi
  ensure_compose || die "容器编排 compose 不可用——请检查 docker-compose-plugin 安装日志"
  # 镜像加速先于 compose 启动配置；Docker Hub 可达时不改系统，仅补日志轮转。
  registry_mirror_main || true
  configure_docker_logs
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    info "前置准备完成 ✔ 容器运行时就绪：$(docker --version 2>/dev/null | head -1)"
  elif command -v podman >/dev/null 2>&1; then
    info "前置准备完成 ✔ 容器运行时就绪：$(podman --version 2>/dev/null | head -1)"
  else
    die "前置准备后仍未找到可用容器运行时——详见日志"
  fi
  info "前置准备完成，可继续首次安装（无需再手工准备 yum/docker）"
}
PKG_MGR_YUM=$(command -v dnf || command -v yum || true)
# ---- 备份工具（bootstrap/镜像配置在 --fix 分发前调用，必须提前定义） ----
backup_file() {  # backup_file <路径>：<路径>.bak.qianshu.<ts>（幂等：备份只做一次）
  local f="$1" bak
  [ -f "$f" ] || return 0
  bak="$f.bak.qianshu.$TS"
  [ -e "$bak" ] || cp -a "$f" "$bak"
}

# ---- 镜像加速单独配置：修复已装环境拉取镜像失败，不重装/不改其他系统项 ----
if [ "$MIRROR_ONLY" = "1" ]; then
  echo "===== --registry-mirror-only 开始 $(date '+%F %T') =====" >> "$LOG"
  registry_mirror_main
  rc=$?
  echo "===== --registry-mirror-only 完成 $(date '+%F %T') rc=$rc =====" >> "$LOG"
  exit "$rc"
fi
# ---- bootstrap 分发：只准备首次安装必需项，不进入全量调优 ----
if [ "$SOURCE_BOOTSTRAP" = "1" ] && [ "$FIX" != "1" ]; then
  echo "===== --bootstrap-source 前置准备开始 $(date '+%F %T') =====" >> "$LOG"
  bootstrap_source_main
  echo "===== --bootstrap-source 前置准备完成 $(date '+%F %T') =====" >> "$LOG"
  exit 0
fi
if [ "$BOOTSTRAP" = "1" ] && [ "$FIX" != "1" ]; then
  echo "===== --bootstrap 前置准备开始 $(date '+%F %T') =====" >> "$LOG"
  bootstrap_main
  echo "===== --bootstrap 前置准备完成 $(date '+%F %T') =====" >> "$LOG"
  exit 0
fi

echo "===== --fix 调优开始 $(date '+%F %T') =====" >> "$LOG"

# ---- 1. SELinux ----
if [ "$SEL_STATUS" = "Enforcing" ] || [ "$SEL_STATUS" = "Permissive" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将关闭 SELinux（setenforce 0 + /etc/selinux/config 持久化，重启后完全生效）"
  else
    run_step "SELinux 临时关闭" setenforce 0 || true   # config 已是 disabled 时该命令会报错，容忍
    if [ -f /etc/selinux/config ] && grep -qE '^SELINUX=(enforcing|permissive)' /etc/selinux/config; then
      backup_file /etc/selinux/config
      sed -i 's/^SELINUX=.*/SELINUX=disabled/' /etc/selinux/config
      info "  ✔ SELinux 持久化关闭（/etc/selinux/config）——需重启机器后完全生效（本脚本不代重启）"
    fi
  fi
else
  info "SELinux 已关闭，跳过"
fi

# ---- 2. 防火墙 ----
if [ "$SKIP_FIREWALL" = "1" ]; then
  info "按参数保留防火墙（--skip-firewall）——请自行放行端口：80/8080(前端) 8810(后端) 5432(pg,仅本机)"
elif command -v systemctl >/dev/null 2>&1 && systemctl is-active firewalld >/dev/null 2>&1; then
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将关闭并禁用 firewalld"
  else
    run_step "关闭防火墙" systemctl disable --now firewalld \
      || warn "  firewalld 关闭失败（可能被上层策略接管），详情查日志"
  fi
else
  info "防火墙未运行，跳过"
fi

# ---- 2.5 NTP 时间同步（告警时序/基线/账单日期全依赖系统时间；与体检项成对） ----
NTP_SYNCED=0
if command -v timedatectl >/dev/null 2>&1 && timedatectl 2>/dev/null | grep -q "synchronized: yes"; then
  NTP_SYNCED=1
fi
if [ "$NTP_SYNCED" = "1" ]; then
  info "NTP 时间已同步，跳过"
elif command -v chronyd >/dev/null 2>&1; then
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将启用 chronyd（NTP 时间同步）"
  else
    run_step "启用 NTP 时间同步（chronyd）" systemctl enable --now chronyd \
      || warn "  chronyd 启用失败，详情查日志"
    if [ "$ONLINE" = "1" ]; then
      sleep 2
      run_step "立即校时（chronyc makestep）" chronyc makestep \
        || warn "  立即校时未成（NTP 源未就绪/123/UDP 受限）——chronyd 后台继续同步，稍后复查 timedatectl"
    fi
  fi
elif command -v ntpd >/dev/null 2>&1; then
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将启用 ntpd（NTP 时间同步）"
  else
    run_step "启用 NTP 时间同步（ntpd）" systemctl enable --now ntpd \
      || warn "  ntpd 启用失败，详情查日志"
  fi
else
  warn "未安装 chrony/ntpd——建议安装 chrony 保证时间准确（告警时序/基线依赖）"
fi

# ---- 3. ulimit ----
LIMITS_CONF=/etc/security/limits.d/99-qianshu.conf
if [ "$NOFILE" -lt 65535 ]; then
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将写入 $LIMITS_CONF（nofile/nproc 65535）"
    info "（干跑）将写入 /etc/systemd/system.conf.d/99-qianshu.conf 并重载 systemd"
  else
    cat > "$LIMITS_CONF.tmp" <<EOF
# 千枢 AIOps 平台（deploy/sysprep.sh $TS 生成）——文件句柄/进程数上调
*  soft  nofile  65535
*  hard  nofile  65535
*  soft  nproc   65535
*  hard  nproc   65535
EOF
    backup_file "$LIMITS_CONF"
    mv "$LIMITS_CONF.tmp" "$LIMITS_CONF"
    info "  ✔ ulimit 已写入 $LIMITS_CONF（重新登录生效）"
    # systemd 服务不读取 limits.d，需单独配置 DefaultLimitNOFILE。
    mkdir -p /etc/systemd/system.conf.d
    cat > /etc/systemd/system.conf.d/99-qianshu.conf <<EOF
# 千枢 AIOps 平台（deploy/sysprep.sh $TS 生成）
[Manager]
DefaultLimitNOFILE=65535
EOF
    run_step "systemd DefaultLimitNOFILE 生效" systemctl daemon-reexec
  fi
else
  info "ulimit 已达标（$NOFILE），跳过"
fi

# ---- 4. 内核参数 ----
SYSCTL_CONF=/etc/sysctl.d/99-qianshu.conf
NEED_SYSCTL=0
while read -r key want; do
  cur=$(sysctl -n "$key" 2>/dev/null || echo "")
  case "$want" in
    =) [ "$cur" = "${want#=}" ] || NEED_SYSCTL=1 ;;
    *) [ "$cur" -ge "$want" ] 2>/dev/null || NEED_SYSCTL=1 ;;
  esac
done <<EOF
net.core.somaxconn 32768
fs.file-max 1048576
vm.max_map_count 262144
vm.swappiness 10
vm.overcommit_memory =1
net.ipv4.ip_forward 1
EOF
if [ "$NEED_SYSCTL" = "1" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将写入 $SYSCTL_CONF（somaxconn/file-max/max_map_count/swappiness/overcommit/ip_forward）"
  else
    cat > "$SYSCTL_CONF.tmp" <<EOF
# 千枢 AIOps 平台（deploy/sysprep.sh $TS 生成）——内核参数调优
net.core.somaxconn = 32768
net.ipv4.tcp_max_syn_backlog = 8192
fs.file-max = 1048576
vm.max_map_count = 262144
vm.swappiness = 10
vm.overcommit_memory = 1
net.ipv4.ip_forward = 1
EOF
    backup_file "$SYSCTL_CONF"
    mv "$SYSCTL_CONF.tmp" "$SYSCTL_CONF"
    run_step "内核参数生效（sysctl --system）" sysctl --system \
      || warn "  部分内核参数未生效（内核未编译该键属正常），详情查日志"
  fi
else
  info "内核参数已达标，跳过"
fi


# ---- 4.7 yum 源救援（--fix 在更新前执行；bootstrap 已在上面自行调用） ----
fix_yum_repos || warn "yum 源未修复——后续 yum 更新/docker 安装可能失败，详情查日志"

# ---- 5. yum 软件更新 ----
if [ "$SKIP_UPDATE" = "1" ]; then
  info "按参数跳过 yum 更新（--skip-update）"
elif [ "$ONLINE" != "1" ]; then
  info "外网不可达，跳过 yum 更新（离线环境属预期——走 vendor/ 离线包交付）"
elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
  PKG_MGR=$(command -v dnf || command -v yum)
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将执行 $PKG_MGR -y update（系统软件全量更新，可能耗时 10-30 分钟）"
  else
    info "yum 软件更新（后台进行，明细看日志；可能耗时较长）..."
    if "$PKG_MGR" -y update >> "$LOG" 2>&1; then
      info "  ✔ 系统软件已更新到最新"
    else
      warn "  ✘ yum 更新失败（源不通/锁冲突常见）——不阻断安装，详情查日志"
    fi
  fi
else
  info "未识别 dnf/yum，跳过软件更新"
fi

# ---- 6. docker 安装（函数定义见 4.6，bootstrap 与 --fix 共用） ----
DOCKER_RC=0
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  info "docker 已就绪，跳过安装"
elif [ "$DRY_RUN" = "1" ]; then
  info "（干跑）将安装 docker（阿里云源 -> 官方源 -> 发行版包 -> podman 兜底）并 enable --now"
else
  ensure_docker || DOCKER_RC=$?
  if [ "$DOCKER_RC" != "0" ]; then
    # docker 装失败（含返回 2 的“全部源失败”）——再试 podman
    ensure_podman || die "docker 与 podman 均安装失败"
  fi
fi

# --fix 也要保证后续 install.sh 有 compose 编排；缺插件时补装
if [ "$DRY_RUN" != "1" ] && { command -v docker >/dev/null 2>&1 || command -v podman >/dev/null 2>&1; }; then
  ensure_compose || warn "容器运行时已就绪但 compose 不可用——后续 install.sh 可能失败，详情查日志"
fi

# ---- 7. docker 配置：data-root 持久化 + 日志轮转 ----
DATA_DIR="${DATA_DIR_ARG:-$SUGGEST_DATA_DIR}"
[ -n "$DATA_DIR" ] || DATA_DIR="/data/qianshu"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  if [ "$RT_IS_PODMAN_SHIM" = "1" ]; then
    info "podman shim 模式：data-root/log-opts 走 containers/storage.conf 语义，跳过 daemon.json 配置"
  else
  NEED_DAEMON=0
  [ -f /etc/docker/daemon.json ] || NEED_DAEMON=1
  grep -q '"data-root"' /etc/docker/daemon.json 2>/dev/null || NEED_DAEMON=1
  grep -q '"max-size"' /etc/docker/daemon.json 2>/dev/null || NEED_DAEMON=1
  if [ "$NEED_DAEMON" = "1" ]; then
    mkdir -p "$DATA_DIR/docker"
    if [ "$DRY_RUN" = "1" ]; then
      info "（干跑）将合并写入 /etc/docker/daemon.json：data-root=$DATA_DIR/docker + 日志轮转(100m×3)"
    elif command -v python3 >/dev/null 2>&1; then
      backup_file /etc/docker/daemon.json
      # 迁移判断固定默认路径（新版 docker/podman shim 的 info 输出都没有 Docker Root Dir 行）
      if [ -d /var/lib/docker/containers ] && [ -n "$(ls -A /var/lib/docker/containers 2>/dev/null)" ]; then
        warn "  ▲ /var/lib/docker 已有容器/镜像——data-root 自动迁移涉及停机搬运，本脚本不代做。"
        warn "    手动迁移步骤（见日志同款）：stop docker -> rsync -aHAX /var/lib/docker/ $DATA_DIR/docker/ -> 改 daemon.json -> start docker"
      fi
      if python3 - "$DATA_DIR" <<'PY' >> "$LOG" 2>&1; then
import json, os, sys
p = "/etc/docker/daemon.json"
data = {}
if os.path.exists(p):
    try:
        data = json.load(open(p))
    except Exception:
        data = {}
data.setdefault("data-root", os.path.join(sys.argv[1], "docker"))
data.setdefault("log-driver", "json-file")
data.setdefault("log-opts", {"max-size": "100m", "max-file": "3"})
tmp = p + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp, p)
print("daemon.json 已更新:", json.dumps(data, ensure_ascii=False))
PY
        run_step "重载 docker（应用 daemon.json）" systemctl restart docker
        sleep 3
        docker info >/dev/null 2>&1 && info "  ✔ docker data-root=$DATA_DIR/docker + 日志轮转已生效" \
          || die "docker 重启后 daemon 异常——请查日志后手动检查 daemon.json"
      else
        warn "  daemon.json 合并失败（python3 异常）——详情查日志，可手动配置"
      fi
    else
      # 无 python3：只在文件不存在时直接写全新文件，已有文件不盲改
      if [ ! -f /etc/docker/daemon.json ]; then
        printf '{\n  "data-root": "%s/docker",\n  "log-driver": "json-file",\n  "log-opts": {"max-size": "100m", "max-file": "3"}\n}\n' "$DATA_DIR" > /etc/docker/daemon.json
        run_step "重载 docker（应用 daemon.json）" systemctl restart docker
        info "  ✔ docker data-root=$DATA_DIR/docker + 日志轮转已生效"
      else
        warn "  已有 daemon.json 但缺 python3 合并工具——请手动加 data-root/log-opts（步骤见日志）"
      fi
    fi
  else
    info "docker daemon.json 已配置（data-root+日志轮转），跳过"
  fi
  fi   # RT_IS_PODMAN_SHIM
fi

# ---- 8. 应用数据持久化目录 ----
# 按子目录判断（勿用 [ -d "$DATA_DIR" ]：上面 docker 步骤已 mkdir $DATA_DIR/docker，
# 只检查父目录会误判"已存在"，导致 data/backups/logs 未创建）
if [ -d "$DATA_DIR/data" ] && [ -d "$DATA_DIR/backups" ] && [ -d "$DATA_DIR/logs" ]; then
  info "应用数据目录已就绪，跳过创建：$DATA_DIR"
else
  if [ "$DRY_RUN" = "1" ]; then
    info "（干跑）将创建应用数据持久化目录 $DATA_DIR/{data,backups,logs}"
  elif mkdir -p "$DATA_DIR/data" "$DATA_DIR/backups" "$DATA_DIR/logs" 2>/dev/null; then
    info "  ✔ 应用数据持久化目录：$DATA_DIR/{data,backups,logs}"
  else
    warn "  ✘ $DATA_DIR 创建失败（挂载点不存在/无权限）——安装会退回应用目录内存储"
  fi
fi

echo "===== --fix 调优完成 $(date '+%F %T') =====" >> "$LOG"
echo
info "系统调优完成 ✔  日志: $LOG"
[ "$SEL_STATUS" = "Enforcing" ] && warn "提醒：SELinux 刚持久化关闭，重启机器后完全生效（本脚本不代重启）"
info "下一步：./install.sh --sysprep  （或已调优过则直接 ./install.sh）"
exit 0
