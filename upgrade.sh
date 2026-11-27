#!/usr/bin/env bash
# 千枢运维平台 · 一键升级（兼容入口，2026-11-11 起统一由 install.sh 提供）
#
# 原 upgrade.sh 只支持 compose，且不备份代码、不自动回滚；新流程同时支持
# compose / source（systemd+venv+nginx），升级前备份代码 + 数据库，健康检查失败自动回滚。
#
# 用法：
#   ./upgrade.sh                                  # 用当前代码升级（自动识别部署形态）
#   ./upgrade.sh --pkg qianshu-offline.tar.gz     # 用发布包升级（保留 .env/数据/密钥）
#   ./upgrade.sh --dry-run                        # 只预检，不改文件/不重启
#   ./upgrade.sh --rollback backups/upgrade-<时间> # 回滚（代码+数据库）
#
# 完整参数与说明见 ./install.sh --help
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "${1:-}" = "--rollback" ]; then
  exec "$APP_DIR/install.sh" "$@"
fi
exec "$APP_DIR/install.sh" --upgrade "$@"
