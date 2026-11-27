# 部署、架构与运维

> 更新：2026-11-20。本文是当前部署真相源；具体参数以安装脚本和 compose 文件为实现依据。

## 部署形态

| 形态 | 适用 | 默认入口 |
|---|---|---|
| Compose 全家桶 | 推荐首次体验与单机交付 | `http://<主机>:8080` |
| 源码生产 | systemd + nginx | `http://<主机>` |
| 源码开发 | 本机开发与质量走查 | 前端 `5273`，后端 `8810` |
| 自包含离线包 | 无外网目标机 | 取决于安装形态；当前未封版，不生成新包 |

基础要求：Python 3.9+；现场构建前端需 Node.js 18+；Compose 需 Docker 20.10+/Compose v2 或 Podman 4+/podman-compose。源码生产另需 nginx。

最小化 CentOS 7.9（无 python3、默认 yum 源已 EOL）可直接使用 compose 形态：首次 `install.sh` 会自动运行 `deploy/sysprep.sh --bootstrap`，切换阿里云 CentOS 7.9.2009 vault + EPEL 7 归档源、安装基础工具和 Docker CE + Compose v2。bootstrap 只做首次安装必需项：SELinux 为 Enforcing 时改为 permissive（可逆，原配置备份），防火墙只放行平台端口而不整体关闭；内核参数和 yum 全量更新仍只在 `--sysprep` 显式触发。运行时已就绪会自动跳过，`--no-bootstrap` 可关闭自动准备。离线且未预装 Docker 的目标机仍需提前准备运行时或离线 RPM。

源码形态在同样的最小化系统上首次执行 `./install.sh --mode source` 会自动运行 `deploy/sysprep.sh --bootstrap-source`：修复 yum 源，缺 Python 3.9+ 时从源码编译 Python 3.9.25（CentOS 7 的 OpenSSL 1.0.2 可编译，3.10+ 不行；约 5–10 分钟），缺 nginx 时安装，缺前端 `dist` 时安装 Node 18+。CentOS 7 的 glibc 2.17 无法运行 Node 官方主包，安装器会自动选择 Node 官方 glibc-217 构建。只放行 80/后端端口。`--no-bootstrap` 可关闭；完全离线且无 Python 的目标机应使用离线 compose 包。

安装器在联网动作之前先校准系统时间：NTP 已同步则跳过；否则从 HTTP `Date` 头取参考时间（不走 TLS，避免“时间错→证书校验失败→无法对时”的死循环），偏差超过 `QIANSHU_TIME_MAX_SKEW`（默认 300 秒）才修改时钟，并尝试启用 chronyd 长期同步。HTTP 源不可达时退到 NTP 一次性校时；都不可用只告警、不阻断安装。`QIANSHU_TIME_SYNC=off` 可关闭，`QIANSHU_TIME_HTTP_SOURCES` 可替换为内网对时源，`QIANSHU_NTP_SERVER` 可指定 NTP 服务器。只读体检（`sysprep.sh` 无参数）和 `--dry-run` 不改时钟；安装失败后可单独执行 `deploy/sysprep.sh --time-only`（`--dry-run` 先预览），该模式先于体检分发，不会重复跑体检。

CentOS 7 上 glibc-217 的 Node 包只由 `unofficial-builds.nodejs.org` 提供；该机系统时间不准或 CA 过旧时，TLS 校验可能失败。安装器在已有固定 SHA256 的前提下，仅对该下载用 `curl -k` 重试，并强制比对 SHA256，内容不匹配仍拒绝；没有 SHA256 时不会关闭 TLS。若仍失败，先用 `date -u` / `timedatectl` 校准时间并更新 `ca-certificates`，或手工预装 Node 18+ 后加 `--no-bootstrap`。

镜像拉取：Docker Hub 不可达时，安装器会自动配置公共镜像加速 `https://docker.m.daocloud.io` 并告警；可用 `--registry-mirror <地址>` 覆盖，`DOCKER_REGISTRY_MIRROR=none` 禁用。已有 `/etc/docker/daemon.json` 时只增量合并缺失的 `registry-mirrors`/日志轮转键，先备份原文件，保留 `data-root` 等其他键；JSON 无法解析则不改动。写入后还会校验运行中的 Docker 是否真正加载了加速：未加载先 `systemctl reload docker`（SIGHUP，不中断容器）；仍无效且无运行中容器才重启 Docker；有运行中容器时只告警并给出 `systemctl restart docker` 的手工步骤，不静默继续。安装失败时错误信息会带上运行态加速列表，便于区分“配置未写”和“写了没生效”。内网/严格离线环境不得依赖第三方镜像，应使用 `images/qianshu-images-*.tar` 离线包。第三方加速只影响镜像拉取，不改变业务数据流向。

后端镜像构建默认先走清华 PyPI；遇到 403/缺包/不可达时按 `PIP_FALLBACK_INDEX` 自动回退华为、阿里云、官方 PyPI，避免单个镜像站故障阻断安装（阿里云索引存在滞后，新版本包可能尚未同步）。内网镜像源应显式设置 `PIP_INDEX` 并把 `PIP_FALLBACK_INDEX` 设为空，禁止回退公网。

## 逻辑组件

| 组件 | 职责 | 当前边界 |
|---|---|---|
| FastAPI backend | API、认证、采集、业务服务、迁移和调度 | 进程内调度文件锁选主，不是独立调度服务 |
| Vue 3 frontend | 安装向导、管理页和证据交互 | Vite 仅开发；生产由 nginx 提供 |
| VictoriaMetrics | 内置指标存储与查询 | 外部源是否统一查询看具体适配 |
| VictoriaLogs | 部署预留 | 未形成统一 VL 写入/查询能力 |
| OTel Collector | 接收 OTLP 并转发 | 组件启动不代表应用已接入 |
| copilotd | 智能体后台过程与生命周期配合 | 不执行 LLM 生成的任意命令 |
| 数据库 | 业务配置、任务、事件和审计 | **唯一支持 PostgreSQL**：compose 用栈内 PG16；source 自动装 PG15 或显式 `DATABASE_URL` 接外部 PG；SQLite 已废除且不迁移旧库 |

当前正式拓扑是**单机**。源码形态可通过显式 `DATABASE_URL` 接入外部 PostgreSQL；compose 固定使用栈内 PostgreSQL。多 backend、调度拆分、通知幂等和跨机 HA 是路线图，不是当前能力。

## 安装

```bash
# Compose
./install.sh

# 源码生产
sudo ./install.sh --mode source

# 源码开发
sudo ./install.sh --mode source --dev
```

安装器必须在完整目录内执行（依赖同目录的 `docker-compose.yml`、`backend/`、`frontend/`、`deploy/`），不支持只下载 `install.sh` 的在线管道安装。安装器负责初始配置、服务启动和健康检查。使用安装器生成或显式设置的初始口令，首次登录后立即修改。云凭证通过 UI 保存，不写入脚本或命令历史。

`--data-dir` 只调整 compose 数据卷，源码形态会直接拒绝该参数。source 形态首次安装时，安装器通过 PGDG 安装 PostgreSQL 15（仅监听 `127.0.0.1`）、创建专用库和角色、把连接串写入 `backend/.env`；显式设置的 `DATABASE_URL` 视为外部 PostgreSQL，安装器只做连通性探测，不建本机库。源码形态的数据位置由 `backend/.env` 的 `DATABASE_URL`、`BACKUP_DIR` 和 copilotd 配置决定；新装时安装器会写入 `<安装目录>/backups`，避免非默认安装目录把备份落到 `/opt/qianshu/backups`。检测到旧 SQLite `DATABASE_URL` 时安装器直接拒绝，不提供数据迁移。

支持离线交付流程不等于当前已生成发布物；是否允许打包以 [status.md](status.md) 和用户明确指令为准。

## 端口

| 场景 / 组件 | 默认口径 |
|---|---|
| Compose 前端 | 宿主 `8080` → 容器 `80` |
| 源码生产前端 | nginx `80` |
| 后端 API | `8810`，默认只绑 `127.0.0.1` |
| Vite 开发前端 | `5273` |
| 本机开发内置 VictoriaMetrics | `8429` |
| Compose VM / VictoriaLogs | 容器网络 `8428` / `9428`，默认不发布宿主端口 |
| copilotd | `127.0.0.1:18810` |
| OTLP | `4317` gRPC、`4318` HTTP，默认只绑 `127.0.0.1` |
| Compose PostgreSQL | 随栈默认启动，仅容器网络，不发布宿主端口 |

通过反向代理终结 TLS，只暴露前端入口；后端和数据组件不直接暴露公网。

## 本地开发

```bash
# 先准备一个本地 PostgreSQL（SQLite 已废除；示例用一次性容器，端口避开本机 5432）
docker run -d --name qianshu-devpg -p 127.0.0.1:5433:5432 \
  -e POSTGRES_USER=qianshu -e POSTGRES_PASSWORD=devpass \
  -e POSTGRES_DB=qianshu postgres:16

cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env
# 在 .env 设置 DATABASE_URL=postgresql://qianshu:devpass@127.0.0.1:5433/qianshu
# 并填好本地开发密钥后启动
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8810

# 另开终端
cd frontend
npm ci
npm run dev
```

Vite 将 `/api`、`/ws` 代理到后端。API 文档位于 `/api/docs`，OpenAPI 位于 `/api/openapi.json`。

## 配置模型

- 运行时业务配置以安装向导和系统设置 UI 为唯一入口；需要命令行入册时执行 `python3 -m app.services.env_store`，不指导手改 `.env`。
- `.env` 承担 bootstrap 初始值、数据库连接和密钥载体。
- 普通配置按现有 `env_store` 机制双写 `.env` 与 `env_kv`，读取库优先；密钥类字段和数据库 URL 有特殊处理。双轨来源出现差异时谁后改谁生效，不能简化成启动时库值总是覆盖文件。
- 手改一处配置不代表所有进程已生效；换密钥后需核对继承旧环境变量的进程。
- `.env` 权限保持 `0600`，`SECRET_KEY`、`MASTER_KEY` 使用随机值并妥善备份。
- `MASTER_KEY` 丢失会导致既有云凭证无法解密。

compose 数据根由 `QIANSHU_DATA_DIR` 或 `--data-dir` 调整，默认 `./data`，PostgreSQL 数据在 `<数据根>/postgres`；源码形态不读取 `QIANSHU_DATA_DIR`，数据库连接由 `backend/.env` 的 `DATABASE_URL` 决定（安装器默认写入本机 PG15），每日备份默认 `<安装目录>/backups`（`BACKUP_DIR` 可改）、copilotd 留痕由 `config/qianshu-copilotd.json` 的 `actions_path` 决定。数据库、指标、日志、备份和 copilotd 留痕必须持久化。

## 启动、迁移与调度

启动时通过文件锁串行执行建表、旧迁移、版本化 SQL、数据迁移、初始管理员和配置对账；失败会中止启动。升级健康检查据此进入失败/回滚路径。

调度通过非阻塞文件锁选主，其他 worker 只服务 API。`QIANSHU_SCHEDULER_DISABLED=1` 可禁止某进程运行调度。不要把历史文档中的调度器数量当成稳定架构常量，实际启动器以 `backend/app/main.py` 为准。

## 数据保留

当前默认业务口径：

| 数据 | 默认保留 |
|---|---:|
| VM 指标 | 180 天 |
| VL 日志 | 30 天 |
| K8s 日志 | 7 天 |
| LLM 明细 | 90 天 |
| 审计 | 180 天 |
| 站内信 | 90 天 |
| 已收口事件类 | 180 天 |

VM/VL 另有部署期引擎上限，默认分别为 365/180 天。上限内调整可按现有机制热生效；超过上限或启用 VL 删除 API 可能需重建容器。“0=永久”不能突破引擎物理上限。

修改保留策略前同时检查业务口径、引擎上限和磁盘容量。事件清理只处理已收口记录。

## 备份与恢复

- 默认每日 03:37 执行数据库备份。
- 固定使用 PostgreSQL `pg_dump -Fc`；SQLite 已废除，不再有热备分支。
- 默认滚动保留最新 14 份。
- 备份目录：compose 为数据根下 `backups/`（容器内 `/backups`）；source 为 `BACKUP_DIR`，新装由安装器写入 `<安装目录>/backups`。
- 手动/宿主机 cron 可用 `deploy/bin/pg-backup.sh`：读取 `BACKUP_DIR` 和 `BACKUP_KEEP_DAYS`，自动探测 podman/docker 的 `qianshu-pg`/`qianshu-postgres` 容器；没有容器时回退宿主机 `pg_dump`，连接参数走标准 libpq 环境变量或 `~/.pgpass`。
- 数据库备份不包含 VM 中的全部指标历史。
- 升级或破坏性操作前，必须在隔离副本验证备份可恢复；文件存在不等于可恢复。

恢复数据库会回到备份时刻，之后产生的业务数据可能丢失。不得把代码回滚自动扩大为数据库恢复或数据删除。

## 升级与回滚

```bash
./install.sh --upgrade --dry-run
# 获得升级授权后：
./install.sh --upgrade
```

升级入口支持发布包、原地升级和 dry-run。执行前备份代码与数据库，健康检查失败可进入回滚。回滚可能同时恢复代码和数据库，之后产生的数据可能丢失，执行前必须明确授权。

## 停止与数据保留

没有自动卸载脚本。停止服务不删除数据；删除必须人工确认并先完成可恢复备份。

- compose：`docker compose down` 停止并删除容器（含 PostgreSQL），绑定目录保留。
- source：`systemctl disable --now qianshu-backend qianshu-copilotd`；安装过开发前端时一并停 `qianshu-frontend`。
- 保留项：`.env` 或 `backend/.env`、数据根或 `DATABASE_URL` 指向的库、`BACKUP_DIR`、copilotd token、`secrets/`。
- 彻底清理：停服 → 备份 → 删除安装/数据目录 → 删除 systemd unit 和 nginx 站点 → 清理不再使用的镜像/卷。每一步都是破坏性操作，需单独授权。

`MASTER_KEY` 或数据目录丢失后，已加密云凭证和业务历史无法恢复。

## 只读排障

```bash
curl http://127.0.0.1:8810/api/bootstrap/status
systemctl status qianshu-backend --no-pager
docker compose ps
```

先确认实际运行形态、端口和进程，再检查迁移、调度和分产品证据。区分无数据、无权限、查询失败、尚未调度和不支持。重启、删除、恢复数据库和外部变更需要明确授权；进程 active 或接口 200 不等于任务恢复。

## 安全摘要

- 云账号使用满足任务范围的最小只读权限，不使用主账号密钥。
- 测试使用一次性账号，不重置既有用户密码或强制改密标志。
- 原始日志、完整密钥、客户敏感内容不得进入模型上下文或公开 artifact。
- 外部写、真实通知、购买和收费产品开通只能由用户主动授权。
- 完整工程与测试红线见 [engineering.md](engineering.md)。
