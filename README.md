# 千枢运维平台（QianShu）

[简体中文](README.md) | [English](README.en.md)

> **公有云上的私人运维 AI 团队——平时替人巡检、战时替人排障、人只做决策；机房里的非云机器也一并管住。**
>
> 面向以公有云为主体、同时有少量非云机器的中小运维团队。千枢把「资产接入 → 指标/日志/账单/变更采集 →
> 异常检测 → 诊断归因 → 审计留痕」做成一条可私有化交付的链路，默认只读、写操作人工确认。

许可证：[Apache License 2.0](LICENSE)。安全报告见 [SECURITY.md](SECURITY.md)，参与贡献见 [CONTRIBUTING.md](CONTRIBUTING.md)。

产品目标、当前状态、能力边界与正在进行的任务统一见 **[文档入口](docs/active/README.md)**；按安装、运维和开发任务浏览请进入 **[Repo Wiki](docs/active/README.md)**。历史方案不作为当前能力声明。

---

## 目录

- [1. 功能概览](#1-功能概览)
- [2. 部署形态怎么选](#2-部署形态怎么选)
- [3. 环境要求](#3-环境要求)
- [4. 安装部署](#4-安装部署)
  - [4.1 docker / podman compose（推荐首次体验）](#41-docker--podman-compose推荐首次体验)
  - [4.2 源码部署（systemd + nginx，生产）](#42-源码部署systemd--nginx生产)
  - [4.3 离线包部署](#43-离线包部署)
  - [4.4 首次登录与安装向导](#44-首次登录与安装向导)
- [5. 端口与数据目录](#5-端口与数据目录)
- [6. 一键升级与回滚](#6-一键升级与回滚)
- [7. 备份、恢复与迁移](#7-备份恢复与迁移)
- [8. 配置说明](#8-配置说明)
- [9. 常见问题与报错处理](#9-常见问题与报错处理)
- [10. 安全设计](#10-安全设计)
- [11. 开发、测试与目录结构](#11-开发测试与目录结构)

---

## 1. 功能概览

| 模块 | 能力 |
|---|---|
| 资产接入 | 阿里云 AK 只读探测（ECS/RDS/Redis/SLB/CDN/OSS…）、VMware vCenter/ESXi、腾讯云、主机代理 |
| 监控 | 云监控指标采集到本地 VictoriaMetrics；实例图表、主机资源、VMware 虚拟机/主机 |
| 告警 | 指标告警、噪声收敛、维护窗口、告警规则自动分析建议、一键诊断 |
| 巡检 | 定时巡检、日报/周报、到期资产（域名/证书/包年包月）、就绪度检查 |
| 成本 | 账单同步、产品/地域/账号维度分析、CDN 域名流量、费用异动 |
| 日志与链路 | SLS/CLS/Loki/Elasticsearch 查询、链路拓扑、慢 SQL、日志异常与 5xx 下钻；VictoriaLogs 当前仅部署预留，尚未接入写入与查询 |
| 异动与画像 | 周期基线异常检测（同日型基线、节假日/调休）、进程/CDN/账单/SLB URI/日志 App 行为画像 |
| 智能体 | 千枢智能体（诊断场景路由、只读工具编排、共享诊断）、AI 结论仅作文本建议 |
| 平台治理 | 审计日志、站内业务通知、用户/角色、数据保留策略、系统参数 |

> 平台自身崩溃/自愈只写审计，不再产生站内信或红点；站内通知只承载有业务属性的事件。

---

## 2. 部署形态怎么选

| 形态 | 适合场景 | 依赖 | 前端入口 | 升级方式 |
|---|---|---|---|---|
| **compose 全家桶** | 首次体验、单机快速交付 | Docker 20.10+ / Compose v2，或 Podman + podman-compose | `http://<IP>:8080` | `./install.sh --upgrade` |
| **源码部署** | 生产、需要 systemd 托管与 nginx | Python 3.9+、Node 18+（构建前端）、nginx、systemd | `http://<IP>`（80） | `./install.sh --upgrade` |
| **自包含离线包** | 内网/零外网目标机 | 目标机仅 Python 3.9+；源码形态另需 nginx | 同上 | `./install.sh --upgrade --pkg qianshu-offline.tar.gz` |

三套形态的数据模型、后端 API 与功能一致；区别只在进程/容器编排。部署架构、保留策略、缓存与
高可用路线图见 [docs/active/deployment.md](docs/active/deployment.md)。

---

## 3. 环境要求

### 3.1 硬件（单机）

| 项 | 最低 | 推荐 |
|---|---|---|
| CPU | 2 核 | 4 核及以上 |
| 内存 | 4 GB（易 OOM） | 8 GB 及以上 |
| 磁盘 | 20 GB 可用 | 100 GB+ SSD（指标/日志持续增长） |
| 网络 | 能访问目标云 API；使用 AI 时需要能访问所配置的模型服务 | 同左 |

> compose 形态可用 `./install.sh --data-dir /data/qianshu` 把数据卷放到独立数据盘；
> 源码形态的数据位置由 `backend/.env` 的 `DATABASE_URL`、`BACKUP_DIR` 和 copilotd 配置决定（见 §5、§7.4）。
> 安装前可运行 `./deploy/sysprep.sh` 做只读体检；`./install.sh --sysprep` 可自动做系统调优（幂等）。

### 3.2 软件

**compose 形态**

- Linux x86_64（RHEL/CentOS/Rocky 9、Ubuntu 20.04+ 等；最小化 CentOS 7.9 可用）
- Docker Engine 20.10+ + Docker Compose v2，或 Podman 4+ + podman-compose
- `curl`、`tar`
- **最小化镜像无需预装 Docker**：首次 `./install.sh` 会自动执行 `deploy/sysprep.sh --bootstrap`，
  修复 CentOS 7 EOL yum 源（阿里云 vault + EPEL 归档）、安装基础工具和 Docker CE + Compose v2；
  仅在 SELinux 为 Enforcing 时改为 permissive（可逆），只放行平台端口而不关闭防火墙。
  运行时已就绪时自动跳过；`--no-bootstrap` 可显式关闭该行为。
- 首次构建镜像需要能访问基础镜像仓库、PyPI/npm（可用 `.env` 的 `PIP_INDEX` / `NPM_REGISTRY` 换源）。
  后端构建默认先走清华 PyPI，遇到 403/缺包时自动回退华为、阿里云和官方 PyPI；内网请显式设置
  `PIP_INDEX` 并把 `PIP_FALLBACK_INDEX` 设为空，避免回退公网。
  **Docker Hub 不可达时**，安装器会自动配置公共镜像加速 `https://docker.m.daocloud.io` 并明确告警；
  也可用 `./install.sh --registry-mirror <地址>` 指定自己的加速器，或 `DOCKER_REGISTRY_MIRROR=none`
  禁用自动配置。已有 `/etc/docker/daemon.json` 时只增量补充缺失的镜像/日志键并先备份，不覆盖其他配置；
  写入后还会校验运行中的 Docker 是否真正加载加速：未加载先无损 `reload`，有运行中容器时不强制重启并给出明确步骤。
  内网/严格离线环境请用带 `images/` 的离线包，不要依赖第三方镜像。
- 安装器在联网下载前会校准系统时间：NTP 未同步且偏差超过 300 秒时，用 HTTP `Date` 头或 NTP
  自动对时，避免时钟错误导致 HTTPS 证书校验失败。`QIANSHU_TIME_SYNC=off` 可关闭；
  安装失败后可单独执行 `./deploy/sysprep.sh --time-only`（先加 `--dry-run` 预览）。

**源码形态**

- Python 3.9+（`python3 --version`）；缺失时首次安装会自动从源码编译 Python 3.9.25（约 5–10 分钟）。
  CentOS 7 自带 OpenSSL 1.0.2，3.10+ 无法直接编译，因此固定 3.9.x；新环境优先用 compose 形态。
- Node.js 18+ 与 npm（仅构建前端；发布包已带 `frontend/dist` 时可跳过）；缺失时自动安装 Node 18。
  CentOS 7 的 glibc 2.17 无法运行 Node 官方主包，安装器会自动改用 Node 官方 glibc-217 构建。
- nginx（生产模式）；缺失时自动安装（EPEL，失败回退 nginx.org 官方仓库）。
- systemd
- 源码形态升级/回滚需要 root（执行 `systemctl`）
- 自动准备可用 `--no-bootstrap` 关闭；完全离线且无 Python 的机器请改用离线 compose 包。

### 3.3 运行成本与外部依赖

- 平台本身按 [Apache License 2.0](LICENSE) 提供，不收取软件费用，也不附带任何云资源、模型配额或短信/语音额度。
- 云 API 只读取你授权账号内的数据；API 请求量、日志存储、模型调用和短信/语音发送产生的费用，由你在对应服务商结算。
- 大模型是可选依赖：不配置模型连接时，确定性采集、巡检、告警和已配置数据源上的只读查询照常运行；需要模型的诊断/解释会提示「设置 → AI 模型」进行配置，不伪造结论。
- 平台不会自动开通收费产品、购买资源包或发起采购；涉及计费的外部动作必须由你在对应控制台主动完成。

---

## 4. 安装部署

安装日志统一写到 `/var/log/qianshu-install-<时间>.log`（不可写时落安装目录 `logs/`）。
脚本失败会提示该路径；排障先看日志。

### 4.1 docker / podman compose（推荐首次体验）

```bash
# 必须在完整目录内执行：install.sh 会读取同目录的 docker-compose.yml、backend/、frontend/、deploy/
# 获取方式：解压发布归档，或 git clone 本仓库后进入目录（不支持只下载 install.sh 的 curl|bash）
cd <安装目录>
./install.sh                 # 一键静默安装（默认 compose；缺 Docker/Compose 时自动准备，无需加 --bootstrap）
./install.sh --port 9810     # 后端宿主端口避开占用（默认 8810）
./install.sh --data-dir /data/qianshu   # 数据卷放到独立数据盘（仅 compose 形态）
./install.sh --registry-mirror https://docker.m.daocloud.io  # Docker Hub 不可达时指定镜像加速
```

源码形态（最小化 CentOS 7.9 也可直接执行，安装器会补 Python 3.9+/nginx/Node）：

```bash
sudo ./install.sh --mode source          # 生产：nginx + systemd
sudo ./install.sh --mode source --dev    # 开发：vite，不装 nginx
```

脚本执行：检查容器运行时与 compose（缺 Docker/Compose 时自动修复 yum 源并安装）→
生成 `.env`（随机 `SECRET_KEY`/`MASTER_KEY`/初始密码）→ 构建并启动全家桶 → 健康检查 →
打印访问地址和初始密码。全过程无交互确认；最小化 CentOS 7.9 的 yum/Docker 前置准备可无人值守跑完，
失败时按提示查看
`/var/log/qianshu-install-<时间>.log` 和 `/var/log/qianshu-sysprep-<时间>.log`。

服务清单：`backend`（8810）、`frontend`（8080→80）、`vm`、`victorialogs`、`otel-collector`、`copilotd`。
VictoriaMetrics / VictoriaLogs 不映射宿主端口，仅容器网络内可达。

默认只有前端 `8080` 对所有网卡开放；后端 `8810`、copilotd `18810` 和 OTLP `4317/4318` 只绑
`127.0.0.1`。需要直连后端 API 时，在 `.env` 设 `BACKEND_BIND_HOST`，并同时用防火墙限制来源。

常用命令：

```bash
docker compose ps
docker compose logs -f backend
docker compose restart backend
docker compose down          # 停止（数据在 ./data 或 QIANSHU_DATA_DIR，不丢）
```

> Podman 同样支持；RHEL 9 默认只有 Podman 时脚本会优先使用 `podman-compose`/`podman compose`。

### 4.2 源码部署（systemd + nginx，生产）

```bash
# 生产：venv + 前端 build + nginx 反代 + systemd 开机自启
sudo ./install.sh --mode source

# 开发机：额外安装 vite 前端 unit，不装 nginx
sudo ./install.sh --mode source --dev

# 非默认后端端口（nginx 仍监听 80）
sudo ./install.sh --mode source --port 9810
```

安装内容：

- 创建 `backend/.venv` 并安装 `backend/requirements.txt`（存在 `vendor/` 时自动离线安装）
- `npm ci && npm run build` 生成 `frontend/dist`
- 写入 `/etc/nginx/conf.d/qianshu.conf`：静态托管 `dist`，`/api/` 反代 `127.0.0.1:8810`
- 安装并启用 `qianshu-backend`、`qianshu-copilotd` systemd unit；`--dev` 额外启用 `qianshu-frontend`（Vite 5273）
- 健康检查 `http://127.0.0.1:8810/api/bootstrap/status`

安装位置可以不是 `/opt/qianshu`；unit、nginx、copilotd 配置中的路径会按实际目录改写。数据库固定为
PostgreSQL：安装器自动安装本机 PG15、建库建角色并把 `DATABASE_URL` 写入 `backend/.env`，每日备份目录写入
`BACKUP_DIR=<安装目录>/backups`。
**不要把安装目录放在 `/root` 等 700 权限目录下**，否则 nginx worker 读不到 `frontend/dist`（见 FAQ）。

### 4.3 离线包部署

构建机（可联网）生成自包含离线包：

```bash
./deploy/build_offline.sh                # 有 python3.9+ 的目标机：源码形态
./deploy/build_offline.sh --with-images  # 无 python3（CentOS 7 等）：compose + 离线镜像
# 产出 dist/qianshu-offline.tar.gz：源码 + vendor/ 全量 wheel + 预构建 frontend/dist
# + 安装脚本；--with-images 另带 images/qianshu-images-<版本>.tar
```

目标机（零外网，有 python3.9+；离线源码包不携带 Python 源码，无法在线编译补装）：

```bash
tar xzf qianshu-offline.tar.gz
cd qianshu-offline        # 包名以实际解压目录为准
sudo ./install.sh --mode source
```

目标机**没有 python3**（CentOS 7 等，需 Docker + compose v2）：

```bash
tar xzf qianshu-offline.tar.gz
cd qianshu-offline
sudo ./install.sh         # 检测到 images/*.tar 自动 load 后直接启动，不现场构建
```

> 离线镜像由 `deploy/build_offline_images.sh` 产出：后端使用包内同一套 vendor wheel、
> 前端使用预构建 dist，构建过程不依赖目标机外网；构建后自动复检镜像内无 `.env`/
> 数据库/日志。不要用旧 `build-images.sh + docker save` 流程手工拼接。
>
> 完全离线且**未预装 Docker** 的目标机仍需提前准备运行时（或离线 RPM）；`--bootstrap`
> 只能在能访问 yum 源时自动安装 Docker。在线目标机（包括最小化 CentOS 7.9）无需预装。

### 4.4 首次登录与安装向导

1. 打开访问地址（compose 默认 `8080`，源码生产默认 `80`）。
2. 用户名 `admin`，初始密码在安装输出中打印一次，也可在 `.env` 的 `INIT_ADMIN_PASSWORD` 查看
   （若已改密则以实际密码为准）。
3. **首次登录强制改密**，改完才能进入系统。
4. 按「安装向导」操作：填写云账号只读 AK → 五面自动探测 → 查看底座体检报告 → 按建议补数据源。
5. 云凭证在 UI 中加密保存；不要写入脚本、命令行历史或工单。

不接云、不接模型或暂时没有数据时，平台按实际状态降级，不输出确定结论：

- **没有云账号**：可以登录、查看安装状态和系统设置；接入类任务显示「未接入」，不生成资产、成本或健康结论。
- **没有模型连接**：采集、巡检、告警和已配置数据源上的只读查询可用；诊断/解释类入口提示到「设置 → AI 模型」配置，不返回伪造的 AI 结论。
- **没有数据或无权限**：页面和智能体显示缺失的数据面、失败原因和待补权限；探活成功不等于已有指标、日志或账单。
- **没有价格或样本**：成本和日志语义只显示证据缺口，不给确定收益、节省金额或业务归属。

---

## 5. 端口与数据目录

| 形态 | 作用 | 默认端口 |
|---|---|---|
| compose | 前端（对外） | `8080`（`FRONTEND_PORT` 可改） |
| compose / source | 后端 API | `8810`（compose 用 `BACKEND_PORT`，source 用 `--port`/`QIANSHU_PORT`） |
| source 生产 | nginx 前端 | `80` |
| source 开发 | Vite 前端 | `5273` |
| 两种形态 | copilotd 生命周期通道 | `127.0.0.1:18810`（仅本机） |
| compose | OTLP gRPC / HTTP | `4317` / `4318` |
| compose | PostgreSQL（随栈默认启动） | 仅容器网络，不映射宿主 |

数据目录：

| 路径 | 内容 |
|---|---|
| `.env`（compose）或 `backend/.env`（source） | `SECRET_KEY`、`MASTER_KEY`、数据库 URL、集成配置。**必须备份** |
| compose 数据根 `./data`（`QIANSHU_DATA_DIR` 可改） | PostgreSQL 数据 `postgres/`、VictoriaMetrics、VictoriaLogs、copilotd 留痕；`db/` 仅用于旧 SQLite 文件检测 |
| source 数据库 | 安装器自动安装 PG15、建库建角色并写入 `backend/.env` 的 `DATABASE_URL`；显式 `DATABASE_URL` 则使用外部 PostgreSQL |
| source 每日备份 | 默认 `<安装目录>/backups`（安装器写入 `BACKUP_DIR`；`/opt/qianshu/backups` 只是未写该变量时的代码兜底） |
| source copilotd 留痕 | `<安装目录>/data/copilotd/actions.jsonl`（见 `config/qianshu-copilotd.json` 的 `actions_path`） |
| `<安装目录>/backups/` | 升级代码/数据库回滚点；source 形态每日备份也默认在这里 |
| `${QIANSHU_DATA_DIR:-./data}/backups/` | compose 形态每日数据库备份（容器内挂载为 `/backups`） |
| `config/qianshu-copilotd.token` | copilotd 本机桥接令牌（0600，不要外传） |
| `secrets/` | 授权签名等运行密钥（不要提交、不要打包给客户之外的人） |

> `QIANSHU_DATA_DIR` 只由 compose 读取；源码形态不读取该变量，数据位置以 `DATABASE_URL`、
> `BACKUP_DIR` 和 copilotd 配置为准。

---

## 6. 一键升级与回滚

> 升级入口统一为 `install.sh --upgrade`（旧 `upgrade.sh` 是兼容包装，参数相同）。
> 升级前会备份**代码 + 数据库**；健康检查失败自动回滚。备份默认在 `<安装目录>/backups/upgrade-<时间>/`。

### 6.1 三种升级方式

```bash
# 1) 发布包升级（推荐）：保留 .env / data / backups / secrets / copilotd token
sudo ./install.sh --upgrade --pkg /path/to/qianshu-offline.tar.gz

# 2) 原地升级：已通过 git pull / 手工替换获得新代码
sudo ./install.sh --upgrade

# 3) 只预检，不改任何文件、不重启
sudo ./install.sh --upgrade --dry-run
sudo ./install.sh --upgrade --pkg /path/to/qianshu-offline.tar.gz --dry-run
```

脚本会自动识别 compose 还是 source；也可显式指定：`--mode compose` / `--mode source`。
源码形态要求 root；compose 形态要求当前用户能执行 compose。

### 6.2 升级做了什么

1. 读取当前部署形态、后端端口、数据目录、`DATABASE_URL`。
2. 备份代码（不含 `.venv`、`node_modules`、`data`、`backups`、`secrets`、`.env`）到
   `backups/upgrade-<时间>/code.tar.gz`。
3. 备份数据库到 `backups/upgrade-<时间>/database.dump`（PostgreSQL）：
   - 优先用宿主机 `pg_dump`；目标库在容器内且宿主无客户端时，自动改为容器内 `pg_dump`；
   - 使用 `pg_dump -Fc` 在线备份，不要求停服务。
4. 解压发布包并覆盖业务代码；`.env`/数据/密钥/token 永不覆盖。
5. 源码形态：更新 Python 依赖；发布包带 `frontend/dist` 时直接使用，否则 `npm ci && npm run build`；
   重启 `qianshu-backend`、`qianshu-copilotd` 和（如存在）`qianshu-frontend`。
   compose 形态：`up -d --build`。
6. 后端启动时自动执行幂等数据库迁移；如发布包带有 `update-data/<版本>/*.sql`，
   按版本顺序执行尚未应用的升级 SQL（`schema_update_log` 记账，单文件事务，
   失败中止启动并触发自动回滚）。详见 `update-data/README.md`。
7. 健康检查 `http://127.0.0.1:<端口>/api/bootstrap/status`，90 秒未通过则自动回滚代码和数据库并报错。

升级完成后请：

- 浏览器 **Ctrl+Shift+R 强制刷新**，避免旧前端缓存；
- 检查 `/api/bootstrap/status`、登录、监控图表、异动页；
- 确认稳定后再清理旧备份。

### 6.3 回滚

```bash
sudo ./install.sh --rollback backups/upgrade-20261111-210000
# 或 ./upgrade.sh --rollback backups/upgrade-20261111-210000
```

回滚会停止服务 → 恢复数据库快照 → 恢复代码 → 恢复依赖/启动 → 健康检查。
**回滚会丢失备份时刻之后的数据库业务数据**（VictoriaMetrics 指标数据不在数据库快照内，不受影响）。
因此不要把回滚当作日常操作；升级前先确认备份目录存在且大小正常。

### 6.4 升级前置检查清单

- [ ] 磁盘可用空间 > 数据库大小 × 2 + 1 GB
- [ ] PostgreSQL 部署：宿主机有 `pg_dump`/`pg_restore`（或 qianshu-pg/qianshu-postgres 容器在运行）
- [ ] 源码部署：`npm`/Node 18+ 可用，或发布包带 `frontend/dist`
- [ ] 看过发布包 `CHANGELOG.md`，确认无破坏性配置变更
- [ ] 业务低峰期执行；先 `--dry-run`

---

## 7. 备份、恢复与迁移

### 7.1 必须备份什么

优先级：`.env`（密钥）> 数据库（业务数据）> `backups/` > VictoriaMetrics/VictoriaLogs 数据卷。

- **`MASTER_KEY` 丢失后，已保存的云凭证无法解密**，必须重新录入；
- `SECRET_KEY` 丢失会导致登录令牌全部失效（用户需重新登录）；
- 数据库包含用户、资产、配置、告警与异动历史，必须定期备份。

### 7.2 每日自动备份

平台内置每日备份（默认 03:37，错开 03:17 清理）：固定使用 PostgreSQL `pg_dump -Fc`，
写入 `BACKUP_DIR`，按 `BACKUP_KEEP_DAYS`（默认 14 天）滚动清理。
配置项见 `.env.example` 或「系统设置」。

### 7.3 手工备份/恢复

> 恢复会覆盖当前数据库。执行前先确认备份文件可读、路径对应实际 `DATABASE_URL`，并取得明确授权；
> 不确定时先在隔离副本验证。平台内置每日备份见 §7.2，通常优先用它。

数据库只有 PostgreSQL，统一使用 `pg_dump -Fc` / `pg_restore`。

```bash
cd <安装目录>
mkdir -p backups

# source：口令走 PGPASSWORD/.pgpass，避免写进命令历史
pg_dump -h 127.0.0.1 -p 5432 -U qianshu -Fc -f backups/qianshu-manual.dump qianshu

# compose：库只在容器网络内，从容器执行
docker compose exec -T postgres pg_dump -U qianshu -Fc qianshu \
  > backups/qianshu-manual.dump

# source 恢复（先停后端，避免连接占用）
sudo systemctl stop qianshu-backend
pg_restore --clean --if-exists --no-owner --no-privileges \
  -h 127.0.0.1 -U qianshu -d qianshu backups/qianshu-manual.dump
sudo systemctl start qianshu-backend

# compose 恢复
docker compose stop backend
docker compose exec -T postgres pg_restore \
  --clean --if-exists --no-owner --no-privileges -U qianshu -d qianshu \
  < backups/qianshu-manual.dump
docker compose start backend
```

> 旧 SQLite 库不再读取，也没有迁移入口：新装或升级后后端连接的始终是 PostgreSQL。
> 旧 `qianshu.db` 仅作为启动检测点保留，发现旧文件且目标 PG 为空库时后端拒绝启动，避免静默建空库。

### 7.4 迁移到新的数据盘

**compose**

```bash
docker compose down
sudo mkdir -p /data/qianshu
sudo cp -a data/. /data/qianshu/
# 在 .env 写 QIANSHU_DATA_DIR=/data/qianshu
docker compose up -d
```

**source**

源码形态不读取 `QIANSHU_DATA_DIR`。先停服务，按新位置修改 `backend/.env`：

```dotenv
DATABASE_URL=postgresql://qianshu:PASSWORD@127.0.0.1:5432/qianshu
BACKUP_DIR=/data/qianshu/backups
```

数据库本体由 PostgreSQL 管理，不能直接改路径。迁移到新数据盘时先做逻辑备份再恢复：

```bash
sudo systemctl stop qianshu-backend qianshu-copilotd
pg_dump -h 127.0.0.1 -U qianshu -Fc -f /tmp/qianshu-before-move.dump qianshu
sudo mkdir -p /data/qianshu/backups
sudo cp -a backups/. /data/qianshu/backups/
# 新位置 PostgreSQL 就绪后恢复（目标库需为空库或已备份）
pg_restore --clean --if-exists --no-owner --no-privileges \
  -h 127.0.0.1 -U qianshu -d qianshu /tmp/qianshu-before-move.dump
# 以实际运行用户为准调整属主后启动
sudo systemctl start qianshu-backend qianshu-copilotd
```

copilotd 动作留痕由 `config/qianshu-copilotd.json` 的 `actions_path` 决定，默认在安装目录；
需要一起迁移时修改该字段并重启 `qianshu-copilotd`。VictoriaMetrics/VictoriaLogs 只在 compose 形态存在。

> compose 的 PostgreSQL 数据在 `data/postgres`，必须与 `QIANSHU_DATA_DIR` 一起迁移；
> 不确定时先整体停机冷备，再迁移。source 形态的 PostgreSQL 数据目录由系统服务管理，
> 迁移方式以逻辑备份/恢复为准。

### 7.5 停止与数据保留（无卸载脚本）

本项目**不提供自动卸载脚本**。停止服务不会删除数据；删除任何数据前必须完成备份并取得明确授权。

```bash
# compose：停止并删除容器，保留数据卷目录
docker compose down

# source：停止并取消开机自启
sudo systemctl disable --now qianshu-backend qianshu-copilotd
# 如安装了开发前端
sudo systemctl disable --now qianshu-frontend
```

需要保留的数据：`.env`（compose）或 `backend/.env`（source）、数据根目录、`backups/`、
`config/qianshu-copilotd.token`、`secrets/`。只删代码目录而不备份密钥和数据，等于永久丢失已加密的
云凭证和全部业务历史。

彻底清理属于人工破坏性操作，必须逐项确认：

1. 停止服务并完成可恢复的备份；
2. 删除安装目录与数据目录（compose 还要删除不再使用的镜像/卷）；
3. 删除 `/etc/systemd/system/qianshu-*.service` 并 `systemctl daemon-reload`；
4. 生产源码形态按需移除 `/etc/nginx/conf.d/qianshu.conf` 并重载 nginx。

---

## 8. 配置说明

- **运行时配置的唯一入口是 UI（安装向导 / 系统设置）**：写库即生效，重启不丢。
- `.env` 是 bootstrap 初始值与密钥载体：`SECRET_KEY`、`MASTER_KEY`、`DATABASE_URL`、
  `INIT_ADMIN_PASSWORD`、端口、数据目录、SLS/产品卡片等。
- 手改 `.env` 中的业务配置需要入册才会持久（否则按「谁后改谁生效」对账）：
  - source：`cd backend && python3 -m app.services.env_store`
  - compose：`docker compose exec backend python3 -m app.services.env_store`
- 换环境不改代码：通过 `.env`/UI 注入 `SLS_ENDPOINT`、`SLS_SLB_TARGETS`、`SLS_APP_LOG`、
  `SLS_CDN_TARGET`、`PRODUCT_CARDS` 等；未配置的能力会优雅降级为「未配置」。

常用配置：

```dotenv
# 端口与数据目录（compose）
BACKEND_PORT=8810
FRONTEND_PORT=8080
QIANSHU_DATA_DIR=/data/qianshu

# 数据库（唯一选择 PostgreSQL）
# compose：栈内 postgres 随栈默认启动，口令由 install.sh 首次生成，URL 由 POSTGRES_* 组装
POSTGRES_DB=qianshu
POSTGRES_USER=qianshu
# POSTGRES_PASSWORD=安装器自动生成并写入 .env，请勿手工覆盖既有库口令

# source：安装器自动安装本机 PG15 并写入 URL；接外部 PG 时显式设置
# DATABASE_URL=postgresql://qianshu:PASSWORD@127.0.0.1:5432/qianshu
# 源码形态备份目录（compose 用 BACKUP_DIR=/backups 卷，无需设置）
# BACKUP_DIR=/data/qianshu/backups

# 依赖源（受限网络）
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
NPM_REGISTRY=https://registry.npmmirror.com

# 数据保留（天，0=永久；VM/VL 上限由引擎启动参数决定）
VM_RETENTION_DAYS=180
VL_RETENTION_DAYS=30
RETENTION_AUDIT_DAYS=180
RETENTION_MSG_DAYS=90
```

---

## 9. 常见问题与报错处理

### 9.1 安装阶段

| 现象/报错 | 原因 | 处理 |
|---|---|---|
| `有容器运行时但缺 compose 编排` | 只装了 Docker，没装 Compose 插件 | `dnf install docker-compose-plugin` 或安装 podman-compose |
| `Docker 安装失败` | 源不可用/无外网 | 手工安装 Docker 或 Podman 后重跑；离线机用源码形态 |
| `python3 版本过低（需 3.9+）` | 系统 Python 太旧 | 升级 Python 或换受支持 OS |
| `缺 npm` / `node 版本过低` | 源码形态需要构建前端 | 安装 Node 18+，或使用带 `frontend/dist` 的发布包 |
| `缺 nginx` | source 生产模式依赖 nginx | `dnf install nginx` 或 `apt install nginx` |
| `端口 8810/8080 已被占用` | 已有服务占用 | 换端口：`--port 9810`；compose 还可用 `FRONTEND_PORT` |
| 只下载 `install.sh` 后执行失败 | 安装器需要同目录的 compose/backend/frontend/deploy 文件 | 解压完整发布包或克隆完整仓库后在目录内执行；不要用 `curl \| bash` |
| `--data-dir` 在源码形态不生效 | 该参数只用于 compose 数据卷 | 源码形态改 `backend/.env` 的 `DATABASE_URL` 和 `BACKUP_DIR`（见 §7.4） |
| `PostgreSQL 连接失败` | URL/网络/账号授权错误 | 核对 `DATABASE_URL`、pg 是否启动、账号是否可连；见 `docs/active/deployment.md` |
| 安装输出中没有初始密码 | 旧安装或已改密 | 查 `.env` 的 `INIT_ADMIN_PASSWORD`；已改密则用实际密码，**不要重置生产管理员** |
| 安装日志在哪 | 脚本统一留痕 | `tail -100 /var/log/qianshu-install-*.log` |

### 9.2 启动/访问阶段

| 现象 | 排查 |
|---|---|
| 打开页面 403/500，nginx 日志 `Permission denied` | 安装目录在 `/root` 等 700 路径下，nginx 用户读不到 `frontend/dist`。换到 `/opt/qianshu` 重装，或逐级 `chmod o+x` |
| 页面空白/还是旧版本 | 浏览器强刷 `Ctrl+Shift+R`；compose 确认 `frontend` 容器已重建；source 确认 `frontend/dist` 是新的 |
| 后端 90 秒未就绪 | compose：`docker compose logs backend`；source：`journalctl -u qianshu-backend -n 100` |
| `502 Bad Gateway` | 后端未启动或端口不一致；看 unit/日志，确认 nginx 反代端口与后端端口一致 |
| 登录后又跳回登录页 | `SECRET_KEY` 变更导致令牌失效；重新登录。多 worker 必须共享同一 `.env` |
| 迁移后管理员回到初始密码 | 连到了另一个 PostgreSQL 库（空库）或旧数据未恢复；核对 `DATABASE_URL`/`POSTGRES_DB` 与备份恢复目标 |
| compose 重启后数据丢失 | 数据没挂到 `QIANSHU_DATA_DIR`；检查 `.env` 与 `docker compose config` |

### 9.3 监控与 VMware

**VMware 虚拟机/主机没有指标图，提示「已接入 vCenter，但本轮取到 0 个性能样本」**

这不是采集按钮失效。千枢会**自动读取你 vCenter 的统计级别，并在 0 样本时逐档探测**
（20 秒 / 5 分钟 / 30 分钟），把结论直接写在告警里。先看页面上那行红字，再对照处理：

| 页面自动诊断结论 | 含义与处理 |
|---|---|
| 统计级别 = 0 / 全为 0 | 性能统计被关闭。按下面第 2 步开启并等待 5–10 分钟。 |
| 三档探测全无有效样本 | vCenter 接口可查、统计级别也开着，但没有返回有效性能数据。先按第 4 步对照 vSphere Client：vSphere 有曲线而本页为 0，请反馈服务端日志；vSphere 也没有曲线，再按第 5 步到 vCenter 侧处理。 |
| 实时档无数据、历史档有数据 | vCenter 实时统计未开启；按第 4 步确认实时曲线。 |
| 未能读取统计级别 / 账号报错 | 多为只读权限或连接问题；按第 3、5 步处理。 |

完整 6 步：

1. 登录 vSphere Client，选中 **vCenter（不是单台 ESXi）** →「配置」→「设置」→「常规」→「统计」→「编辑」。
2. 「统计级别」设为 **1 或更高（推荐 2）**；0 表示关闭性能统计，`QueryPerf` 必然返回空。保存后等待 5–10 分钟。
3. 只读账号授权：「系统管理」→「访问控制」→「角色」，创建角色并勾选 **全局 → System.View**；
   再到「权限」把账号授权到目标数据中心/集群，勾选 **传播到子对象**。
4. 在 vSphere Client 选中同一台虚拟机 →「监控」→「性能」，时间范围切到 **「过去 1 小时」**，
   记录 vSphere Client 里是否有曲线。
5. 若 vSphere Client 有曲线而本页仍为 0：保留页面错误并查看 `journalctl -u qianshu-backend`
   的相关日志，反馈给千枢排查。若 vSphere Client 同样没有曲线：请 vCenter 管理员检查性能统计服务
   （`vpxd`/`perfcharts`）与数据库空间。其它情况回「云账号管理」→ 该账号 →「测试可用性」，
   应返回版本号；若报 SSL 证书错误，开启「跳过 SSL 校验（自签证书）」。
6. 回监控页点 **「采集当前产品」** 重试。该按钮只采集当前页签的产品（VMware 虚拟机或主机），
   不会连带触发 ECS/RDS 等云监控采集；本轮账号错误会显示在页面告警详情中。

其他常见问题：

- **CSV/资产能同步，但没有性能数据**：资产/拓扑同步与性能样本是两条通道，能列虚拟机不代表有性能读权限。
- **只读账号登录失败**：检查账号是否锁定、是否只允许 vCenter 登录、密码是否含特殊字符。
- **缺少 `pyvmomi`**：源码部署执行 `backend/.venv/bin/pip install pyvmomi` 或重跑安装；离线包 vendor 已包含。
- **vCenter 是自签证书**：在云账号中开启跳过 SSL 校验；生产建议导入企业 CA。
- **EC2/ECS 指标有、VMware 没有**：在 VMware 页签点「采集当前产品」，不要用旧版「立即采集全部」。

### 9.4 升级与回滚

| 现象 | 处理 |
|---|---|
| `找不到 pg_dump` | 安装 PostgreSQL 客户端（`dnf install postgresql` / `apt install postgresql-client`），或确保 `qianshu-pg`/`qianshu-postgres` 容器运行 |
| source 每日备份写到 `/opt/qianshu/backups` | `backend/.env` 没有 `BACKUP_DIR`，落到了代码兜底路径 | 设为实际安装目录下的 `backups`（新装由安装器自动写入），重启后端后确认备份文件生成 |
| `数据库备份失败，已中止升级` | 这是保护行为：宁可升级失败也不做无回滚点的升级。按日志修复备份后重试 |
| 升级后健康检查失败 | 脚本会自动回滚并报错；查 `${安装目录}/backups/upgrade-*` 与 `/var/log/qianshu-install-*.log` |
| 自动回滚也失败 | **不要删备份、不要继续操作**。停服务后按 `backups/upgrade-*/meta`、`db.meta` 手工恢复，或联系支持 |
| 发布包没有 `frontend/dist` 且无 npm | 在发布包中加入预构建前端，或在目标机安装 Node 18+ |
| 旧发布包内 `install.sh` 不支持 `--upgrade` | 脚本会保留当前安装器，只升级业务代码；建议重新用新版 `deploy/build_offline.sh` 打包 |
| 回滚后数据少了 | 回滚会把数据库恢复到备份时刻，备份后的业务数据会丢失；VM 指标不受影响 |

### 9.5 密钥与凭证

| 现象 | 处理 |
|---|---|
| 云账号显示「无法解密凭证」 | `MASTER_KEY` 变更或丢失；换回原 `.env`，否则删除账号重新录入（凭证无法恢复） |
| 改了 `MASTER_KEY` 后全部云账号失效 | 不要随意更换；如必须更换，先导出/重录凭证 |
| 口令/Key 出现在 shell 历史中 | 使用 UI 或环境变量输入，清理历史；生产轮换该凭证 |
| 升级后用户都被登出 | `SECRET_KEY` 变化属正常；重新登录即可 |

---

## 10. 安全设计

1. **默认只读**：诊断/巡检工具不执行写操作；处置类动作只产出命令文本，由人确认执行。
2. **写路径人工门**：重启、删除、变更等敏感操作必须在 UI 中二次确认；LLM 生成的 SQL/PromQL/命令不直接执行。
3. **凭证加密**：云 AK/Secret 用 `MASTER_KEY` 加密存储；日志与接口不回显明文。
4. **平台自身通知降噪**：平台自愈事件只写审计，不发站内信/红点；站内通知只承载业务事件。
5. **审计留痕**：登录、配置变更、采集触发、升级/回滚等操作写入审计日志。
6. **最小权限**：建议云账号使用只读 RAM 角色；vCenter 使用只读角色；不要用主账号 AK。

发现安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要开公开 Issue。

---

## 11. 开发、测试与目录结构

```bash
# 准备本地 PostgreSQL（SQLite 已废除；端口避开本机 5432）
docker run -d --name qianshu-devpg -p 127.0.0.1:5433:5432 \
  -e POSTGRES_USER=qianshu -e POSTGRES_PASSWORD=devpass \
  -e POSTGRES_DB=qianshu postgres:16
# backend/.env 设：DATABASE_URL=postgresql://qianshu:devpass@127.0.0.1:5433/qianshu

# 后端（开发）
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8810 --reload

# 前端（开发）
cd frontend
npm ci
npm run dev        # 默认 5273，代理 /api 到 8810

# 前端构建
npm run build      # 产出 frontend/dist

# 后端 mock 测试（不依赖真实云；需一次性 PG，见 docs/active/engineering.md）
# export QIANSHU_TEST_PG_URL=postgresql://qianshutest:qianshutestpw@127.0.0.1:55432/postgres
python3 tests/test_vsphere_collect_mock.py
python3 tests/test_vsphere_host_mock.py

# 质量门禁（详见 docs/active/engineering.md 与 CONTRIBUTING.md）
python3 tests/quality/gate.py
```

目录：

```
backend/          FastAPI（app/api、app/services、app/collector、app/copilotd）
frontend/         Vue 3 + Element Plus + Vite
deploy/           systemd unit、nginx 模板、sysprep、离线包构建
docs/             架构、方案、质量与发布文档
tests/            mock 测试与质量走查
config/           copilotd 配置与 token（运行态）
data/             运行数据（不提交）
backups/          备份与升级回滚点（不提交）
```

版本变更见 [CHANGELOG.md](CHANGELOG.md)；开源发布成熟度与路线见 [docs/active/status.md](docs/active/status.md)；
开发环境、测试与红线见 [CONTRIBUTING.md](CONTRIBUTING.md)；社区准则见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

---

## License

Copyright 2026 QianShu contributors.

Licensed under the [Apache License, Version 2.0](LICENSE). You may not use this project
except in compliance with the License. See [LICENSE](LICENSE) for the full text,
[NOTICE](NOTICE) for attribution, and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
for bundled third-party components and their licenses.
