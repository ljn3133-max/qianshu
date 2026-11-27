#!/usr/bin/env bash
# 千枢 PostgreSQL 手动备份，与后端内置调度保持相同口径。
# 运行方式（不绑定固定安装目录）：
#   BACKUP_DIR=/data/qianshu/backups ./deploy/bin/pg-backup.sh
#   - BACKUP_DIR 未设置时使用 <安装目录>/backups（脚本按自身位置推导安装目录）；
#   - 库在容器内时自动探测 podman/docker 的 qianshu-pg / qianshu-postgres / postgres，
#     也可用 PG_DUMP_CONTAINER 显式指定；容器内 local socket 信任，无需口令；
#   - 库不在容器内时使用宿主机 pg_dump，连接参数走标准 libpq 环境变量
#     （PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE）或 ~/.pgpass。
#
# 口径（与 backend/services/db_backup.py 一致）：
#   - 日常备份主口径=后端内置每日调度（03:37，PostgreSQL pg_dump，当日已有备份自动跳过不重打）；
#   - 本脚本=手动/宿主机 cron 形态的等价实现（强制重打也用本脚本）；
#   - 同目录、同前缀 qianshu-pg-*.dump、保留最新 14 份（BACKUP_KEEP_DAYS 可改）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_COUNT="${BACKUP_KEEP_DAYS:-14}"
STAMP=$(date +%F)
DUMP="$OUT_DIR/qianshu-pg-${STAMP}.dump"
DB_NAME="${PGDATABASE:-qianshu}"
mkdir -p "$OUT_DIR"

# 定位执行方式：容器内 pg_dump 优先（版本与服务端一致、免口令），否则宿主机 pg_dump。
ENGINE=""
CONTAINER="${PG_DUMP_CONTAINER:-}"
for engine in podman docker; do
  command -v "$engine" >/dev/null 2>&1 || continue
  names=$("$engine" ps --format '{{.Names}}' 2>/dev/null || true)
  if [ -n "$CONTAINER" ]; then
    if printf '%s\n' "$names" | grep -qx "$CONTAINER"; then
      ENGINE="$engine"
      break
    fi
    continue
  fi
  for name in qianshu-pg qianshu-postgres postgres; do
    if printf '%s\n' "$names" | grep -qx "$name"; then
      ENGINE="$engine"
      CONTAINER="$name"
      break 2
    fi
  done
done

if [ -n "${PG_DUMP_CONTAINER:-}" ] && [ -z "$ENGINE" ]; then
  echo "备份失败：PG_DUMP_CONTAINER=$PG_DUMP_CONTAINER 不是运行中的 podman/docker 容器" >&2
  exit 1
fi

if [ -n "$ENGINE" ]; then
  "$ENGINE" exec "$CONTAINER" pg_dump -U "${PGUSER:-qianshu}" -Fc "$DB_NAME" > "$DUMP" \
    || { rm -f "$DUMP"; echo "备份失败：容器 $CONTAINER 内 pg_dump 执行失败" >&2; exit 1; }
else
  command -v pg_dump >/dev/null 2>&1 \
    || { echo "找不到 pg_dump，也没有运行中的 qianshu-pg/qianshu-postgres/postgres 容器" >&2; exit 1; }
  pg_dump -Fc "$DB_NAME" > "$DUMP" \
    || { rm -f "$DUMP"; echo "备份失败：宿主机 pg_dump 执行失败" >&2; exit 1; }
fi

if [ ! -s "$DUMP" ]; then
  rm -f "$DUMP"
  echo "备份失败：pg_dump 输出为空，已删除不完整文件" >&2
  exit 1
fi
# 滚动保留最新 KEEP_COUNT 份（ls -1t 新→旧，第 KEEP_COUNT+1 份起删；与内置调度同口径）
ls -1t "$OUT_DIR"/qianshu-pg-*.dump 2>/dev/null | tail -n +$((KEEP_COUNT + 1)) | xargs -r rm -f
echo "$(date '+%F %T') backup ok: $DUMP ($(du -h "$DUMP" | cut -f1))"
