# QianShu AIOps Platform

[简体中文](README.md) | English

> **A private AI ops team for your cloud — it patrols and diagnoses; you make the decisions. Non-cloud machines in your datacenter are covered too.**
>
> QianShu is built for small and mid-sized ops teams whose primary estate is one cloud (Alibaba Cloud first)
> plus a few non-cloud hosts. It turns asset onboarding, metrics/logs/billing/change collection, anomaly
> detection, diagnosis and audit into one self-hosted delivery. Read-only by default; write actions require
> human confirmation.

License: [Apache License 2.0](LICENSE). Security reports: [SECURITY.md](SECURITY.md). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md).

See the **[documentation index](docs/active/README.md)** for product goals, current status, verified capability boundaries, and active work, or browse the **[Repo Wiki (Chinese)](docs/active/README.md)** by task. Historical proposals are not current capability claims.

---

## Contents

- [1. Capabilities](#1-capabilities)
- [2. Deployment options](#2-deployment-options)
- [3. Requirements](#3-requirements)
- [4. Installation](#4-installation)
  - [4.1 Docker / Podman Compose](#41-docker--podman-compose)
  - [4.2 Source deployment (systemd + nginx)](#42-source-deployment-systemd--nginx)
  - [4.3 Offline package](#43-offline-package)
  - [4.4 First login and setup wizard](#44-first-login-and-setup-wizard)
- [5. Ports and data directories](#5-ports-and-data-directories)
- [6. One-command upgrade and rollback](#6-one-command-upgrade-and-rollback)
- [7. Backup, restore and migration](#7-backup-restore-and-migration)
- [8. Configuration](#8-configuration)
- [9. FAQ and error handling](#9-faq-and-error-handling)
- [10. Security model](#10-security-model)
- [11. Development, testing and layout](#11-development-testing-and-layout)

---

## 1. Capabilities

| Area | What it does |
|---|---|
| Asset onboarding | Read-only Alibaba Cloud AK probing (ECS/RDS/Redis/SLB/CDN/OSS…), VMware vCenter/ESXi, Tencent Cloud, host agents |
| Monitoring | Cloud metrics collected into local VictoriaMetrics; instance charts, host resources, VMware VMs/hosts |
| Alerting | Metric alerts, noise reduction, maintenance windows, automatic rule analysis suggestions, one-click diagnosis |
| Inspection | Scheduled inspection, daily/weekly reports, expiring assets (domains/certificates/subscriptions), readiness checks |
| Cost | Billing sync, product/region/account analysis, CDN domain traffic, cost anomalies |
| Logs and tracing | SLS/CLS/Loki/Elasticsearch queries, topology, slow SQL, log anomalies and 5xx drill-down; VictoriaLogs is currently a deployment placeholder without write/query integration |
| Anomalies and profiles | Same-day-pattern baselines (holidays/shift days), behavior profiles (processes/CDN domains/billing/SLB URIs/log apps) |
| Agent | QianShu agent with diagnosis scenario routing, read-only tool orchestration, shared diagnosis. AI output is advisory text only |
| Governance | Audit log, business notifications, users/roles, retention policies, system settings |

> Platform self-healing writes audit records only — no in-app message and no red dot. In-app notifications
> are reserved for business events.

---

## 2. Deployment options

| Mode | Best for | Dependencies | Frontend entry | Upgrade |
|---|---|---|---|---|
| **Compose all-in-one** | First run, single-host delivery | Docker 20.10+ / Compose v2, or Podman + podman-compose | `http://<IP>:8080` | `./install.sh --upgrade` |
| **Source (systemd + nginx)** | Production with systemd and nginx | Python 3.9+, Node 18+ (frontend build), nginx, systemd | `http://<IP>` (80) | `./install.sh --upgrade` |
| **Self-contained offline package** | Air-gapped hosts | Target needs only Python 3.9+; source mode also nginx | Same as above | `./install.sh --upgrade --pkg qianshu-offline.tar.gz` |

All modes share the same data model, backend API and features. See
[docs/active/deployment.md](docs/active/deployment.md) for architecture, retention, caching and HA roadmap (Chinese).

---

## 3. Requirements

### 3.1 Hardware (single host)

| Item | Minimum | Recommended |
|---|---|---|
| CPU | 2 cores | 4+ cores |
| RAM | 4 GB (OOM-prone) | 8 GB+ |
| Disk | 20 GB free | 100 GB+ SSD (metrics and logs grow) |
| Network | Access to target cloud APIs; outbound access to the configured LLM if AI is used | Same |

> Compose mode can put data volumes on a dedicated disk with `./install.sh --data-dir /data/qianshu`.
> Source mode uses `DATABASE_URL`, `BACKUP_DIR` and the copilotd config in `backend/.env` instead (see §5, §7.4).
> Run `./deploy/sysprep.sh` for a read-only preflight, or `./install.sh --sysprep` for idempotent tuning.

### 3.2 Software

**Compose mode**

- Linux x86_64 (RHEL/CentOS/Rocky 9, Ubuntu 20.04+, etc.)
- Docker Engine 20.10+ with Compose v2, or Podman 4+ with podman-compose
- `curl`, `tar`
- The first image build needs access to base image registries, PyPI and npm; use `PIP_INDEX` / `NPM_REGISTRY` in `.env` for mirrors

**Source mode**

- Python 3.9+ (`python3 --version`)
- Node.js 18+ and npm (frontend build only; skip if the release ships `frontend/dist`)
- nginx (production) and systemd
- Upgrade/rollback requires root (for `systemctl`)

### 3.3 Running costs and external dependencies

- The platform itself is provided under the [Apache License 2.0](LICENSE). No software fee is charged, and no cloud resources, model quota or SMS/voice quota are included.
- Cloud APIs read only the data inside accounts you authorize. API request volume, log storage, model usage and SMS/voice delivery are billed by the respective providers.
- The LLM is optional: without a model connection, deterministic collection, inspection, alerting and read-only queries on configured sources still run. Diagnosis/explanation features point to Settings → AI Models instead of inventing a conclusion.
- The platform never opens paid products, buys resource packages or starts procurement on its own; any billable external action must be performed by you in the provider console.

---

## 4. Installation

All installers tee their log to `/var/log/qianshu-install-<timestamp>.log` (fallback: `<install-dir>/logs/`).
The script prints the path on failure; check it first when troubleshooting.

### 4.1 Docker / Podman Compose

```bash
# Run inside the complete tree: install.sh reads docker-compose.yml, backend/, frontend/ and deploy/
# from the same directory. Download the release archive, or git clone the repo, then cd into it.
# Downloading install.sh alone and piping it to bash is not supported.
cd <install-dir>
./install.sh                              # compose (default)
./install.sh --port 9810                  # change host port for the backend (default 8810)
./install.sh --data-dir /data/qianshu     # put data volumes on a dedicated disk (compose only)
```

The script checks the container runtime and Compose, generates `.env` with random `SECRET_KEY`,
`MASTER_KEY` and initial admin password, builds and starts the stack, waits for health, then prints
the URL and initial password.

Services: `backend` (8810), `frontend` (8080→80), `vm`, `victorialogs`, `otel-collector`, `copilotd`.
VictoriaMetrics/VictoriaLogs are not published to the host.

Only frontend port `8080` is exposed on all interfaces by default. Backend `8810`, copilotd `18810`
and OTLP `4317/4318` bind to `127.0.0.1`. To expose the backend API directly, set
`BACKEND_BIND_HOST` in `.env` and restrict sources at the firewall.

```bash
docker compose ps
docker compose logs -f backend
docker compose restart backend
docker compose down        # data stays in ./data or QIANSHU_DATA_DIR
```

### 4.2 Source deployment (systemd + nginx)

```bash
# Production: venv + frontend build + nginx reverse proxy + systemd
sudo ./install.sh --mode source

# Development host: also install the Vite frontend unit; no nginx
sudo ./install.sh --mode source --dev

# Non-default backend port (nginx still listens on 80)
sudo ./install.sh --mode source --port 9810
```

What it installs:

- `backend/.venv` with `backend/requirements.txt` (offline from `vendor/` when present)
- `npm ci && npm run build` → `frontend/dist`
- `/etc/nginx/conf.d/qianshu.conf`: serves `dist`, proxies `/api/` to `127.0.0.1:8810`
- Enables `qianshu-backend` and `qianshu-copilotd`; `--dev` also enables `qianshu-frontend` (Vite 5273)
- Health check: `http://127.0.0.1:8810/api/bootstrap/status`

The install directory does not have to be `/opt/qianshu`; paths and ports in units, nginx and copilotd
config are rewritten to the actual location. The database is always PostgreSQL: the installer installs
local PG15, creates the DB/role and writes `DATABASE_URL` into `backend/.env`, then writes
`BACKUP_DIR=<install-dir>/backups` for daily backups. **Do not install under a 700-mode directory such as `/root`** —
the nginx worker will not be able to read `frontend/dist` (see FAQ).

### 4.3 Offline package

On a connected build host:

```bash
./deploy/build_offline.sh                # targets with python3.9+: source mode
./deploy/build_offline.sh --with-images  # targets without python3 (CentOS 7): compose + images
# Produces dist/qianshu-offline.tar.gz: source + vendor wheels + prebuilt frontend/dist
# + installer; --with-images also ships images/qianshu-images-<version>.tar
```

On an air-gapped target with python3.9+:

```bash
tar xzf qianshu-offline.tar.gz
cd qianshu-offline        # use the actual extracted directory name
sudo ./install.sh --mode source
```

On an air-gapped target **without python3** (CentOS 7 etc., Docker + compose v2 required):

```bash
tar xzf qianshu-offline.tar.gz
cd qianshu-offline
sudo ./install.sh         # detects images/*.tar, loads them and starts without building
```

> Images are produced by `deploy/build_offline_images.sh` from the package's own vendor wheels
> (backend) and prebuilt dist (frontend), then checked for leaked `.env`/DB/log files.

### 4.4 First login and setup wizard

1. Open the URL (compose `8080`, source production `80`).
2. User `admin`; the initial password is printed once and stored in `INIT_ADMIN_PASSWORD` in `.env`
   (if already changed, use the current password).
3. **The first login forces a password change**; the platform is locked until it is completed.
4. Follow the setup wizard: enter a read-only cloud AK → automated five-surface probe → readiness report →
   add missing data sources.
5. Cloud credentials are encrypted by the UI. Never put them in scripts, shell history or tickets.

Without a cloud account, model connection or data, the platform degrades to the actual state and does
not produce definitive conclusions:

- **No cloud account**: login, install status and settings work; onboarding shows "not connected" and no asset, cost or health verdict is generated.
- **No model connection**: collection, inspection, alerting and read-only queries on configured sources still work; diagnosis/explanation entry points point to Settings → AI Models instead of returning a fabricated AI answer.
- **No data or no permission**: pages and the agent show missing data surfaces, failure reasons and permissions to add. A successful probe does not mean metrics, logs or bills exist.
- **No prices or samples**: cost and log semantics show the evidence gap only; no definitive savings, amounts or business ownership are claimed.

---

## 5. Ports and data directories

| Mode | Purpose | Default |
|---|---|---|
| compose | Frontend (public) | `8080` (`FRONTEND_PORT`) |
| compose / source | Backend API | `8810` (`BACKEND_PORT` / `--port` / `QIANSHU_PORT`) |
| source production | nginx frontend | `80` |
| source development | Vite frontend | `5273` |
| both | copilotd lifecycle channel | `127.0.0.1:18810` (localhost only) |
| compose | OTLP gRPC / HTTP | `4317` / `4318` |
| compose | PostgreSQL (started with the stack by default) | Container network only |

Data paths:

| Path | Contents |
|---|---|
| `.env` (compose) or `backend/.env` (source) | `SECRET_KEY`, `MASTER_KEY`, DB URL, integrations. **Back this up** |
| Compose data root `./data` (`QIANSHU_DATA_DIR` overrides) | PostgreSQL data `postgres/`, VictoriaMetrics, VictoriaLogs, copilotd action log; `db/` is only a legacy SQLite detection point |
| Source database | Installer installs PG15, creates the DB/role and writes `DATABASE_URL` to `backend/.env`; an explicit `DATABASE_URL` selects external PostgreSQL |
| Source daily backups | `<install-dir>/backups` by default (installer writes `BACKUP_DIR`; `/opt/qianshu/backups` is only the code fallback) |
| Source copilotd action log | `<install-dir>/data/copilotd/actions.jsonl` (see `actions_path` in `config/qianshu-copilotd.json`) |
| `<install-dir>/backups/` | Upgrade code/DB rollback points; source daily backups also default here |
| `${QIANSHU_DATA_DIR:-./data}/backups/` | Compose daily DB backups (mounted as `/backups` in the container) |
| `config/qianshu-copilotd.token` | Local copilotd bridge token (0600; do not share) |
| `secrets/` | Runtime signing/authorization keys (never commit or distribute) |

> `QIANSHU_DATA_DIR` is read by Compose only. Source mode determines data locations from
> `DATABASE_URL`, `BACKUP_DIR` and the copilotd config.

---

## 6. One-command upgrade and rollback

> The upgrade entry point is `install.sh --upgrade` (the old `upgrade.sh` is a compatibility wrapper).
> It backs up **code and database** first and automatically rolls back if the health check fails.
> Backups go to `<install-dir>/backups/upgrade-<timestamp>/`.

### 6.1 Three ways to upgrade

```bash
# 1) Release package (recommended): keeps .env / data / backups / secrets / copilotd token
sudo ./install.sh --upgrade --pkg /path/to/qianshu-offline.tar.gz

# 2) In-place: after git pull or manually replacing the code
sudo ./install.sh --upgrade

# 3) Preflight only — no file changes, no restart
sudo ./install.sh --upgrade --dry-run
sudo ./install.sh --upgrade --pkg /path/to/qianshu-offline.tar.gz --dry-run
```

The mode (compose/source) is auto-detected; override with `--mode compose` or `--mode source`.
Source mode requires root; compose mode requires permission to run compose.

### 6.2 What the upgrade does

1. Detect mode, backend port, data root and `DATABASE_URL`.
2. Back up code (excluding `.venv`, `node_modules`, `data`, `backups`, `secrets`, `.env`) to
   `backups/upgrade-<timestamp>/code.tar.gz`.
3. Back up the database to `database.dump` (PostgreSQL):
   - Uses host `pg_dump`; if the DB runs in a container and no host client exists, it uses
     `pg_dump` inside the container;
   - `pg_dump -Fc` runs online, so no downtime is needed.
4. Extract the release and overwrite application code. `.env`, data, secrets and tokens are never overwritten.
5. Source: update Python deps; use the release's `frontend/dist` when present, otherwise
   `npm ci && npm run build`; restart `qianshu-backend`, `qianshu-copilotd` and `qianshu-frontend` if present.
   Compose: `up -d --build`.
6. The backend runs idempotent migrations on startup.
7. Health check `http://127.0.0.1:<port>/api/bootstrap/status`; if it does not pass in 90 seconds,
   code and database are rolled back automatically and the upgrade fails.

After a successful upgrade:

- hard-refresh the browser (**Ctrl+Shift+R**) to bypass cached frontend assets;
- check `/api/bootstrap/status`, login, monitoring charts and the anomalies page;
- delete old backups only after the new version is verified.

### 6.3 Rollback

```bash
sudo ./install.sh --rollback backups/upgrade-20261111-210000
# or ./upgrade.sh --rollback backups/upgrade-20261111-210000
```

Rollback stops services, restores the database snapshot, restores code, reinstalls dependencies/starts
services and runs the health check. **Database changes made after the snapshot are lost** (VictoriaMetrics
metrics are outside the database snapshot and are unaffected). Treat rollback as an emergency operation.

### 6.4 Pre-upgrade checklist

- [ ] Free disk > database size × 2 + 1 GB
- [ ] PostgreSQL: host `pg_dump`/`pg_restore` available, or the `qianshu-pg`/`qianshu-postgres` container is running
- [ ] Source mode: npm/Node 18+ available, or the release ships `frontend/dist`
- [ ] Read the release `CHANGELOG.md` for breaking changes
- [ ] Run during a low-traffic window; run `--dry-run` first

---

## 7. Backup, restore and migration

### 7.1 What must be backed up

Priority: `.env` (keys) > database (business data) > `backups/` > VictoriaMetrics/VictoriaLogs volumes.

- **Losing `MASTER_KEY` makes stored cloud credentials undecryptable**; they must be re-entered.
- Losing `SECRET_KEY` invalidates all login tokens (users must sign in again).
- The database holds users, assets, settings, alert and anomaly history; back it up regularly.

### 7.2 Built-in daily backups

The platform backs up daily at 03:37 by default (offset from the 03:17 cleanup). It always uses
PostgreSQL `pg_dump -Fc`, writes to `BACKUP_DIR`, and rotates after
`BACKUP_KEEP_DAYS` (14 days). See `.env.example` or system settings.

### 7.3 Manual backup and restore

> Restoring overwrites the current database. Before proceeding, verify the backup is readable, confirm
> the path matches the actual `DATABASE_URL`, and obtain explicit authorization. If unsure, validate on
> an isolated copy first. The built-in daily backup in §7.2 is usually preferable.

PostgreSQL is the only database; use `pg_dump -Fc` / `pg_restore` for both modes.

```bash
cd <install-dir>
mkdir -p backups

# source: use PGPASSWORD/.pgpass instead of putting the password in shell history
pg_dump -h 127.0.0.1 -p 5432 -U qianshu -Fc -f backups/qianshu-manual.dump qianshu

# compose: the DB is container-network only, so run the client inside the container
docker compose exec -T postgres pg_dump -U qianshu -Fc qianshu \
  > backups/qianshu-manual.dump

# source restore (stop the backend first to avoid active connections)
sudo systemctl stop qianshu-backend
pg_restore --clean --if-exists --no-owner --no-privileges \
  -h 127.0.0.1 -U qianshu -d qianshu backups/qianshu-manual.dump
sudo systemctl start qianshu-backend

# compose restore
docker compose stop backend
docker compose exec -T postgres pg_restore \
  --clean --if-exists --no-owner --no-privileges -U qianshu -d qianshu \
  < backups/qianshu-manual.dump
docker compose start backend
```

> Legacy SQLite files are no longer read and there is no migration path: every new install or
> upgrade connects to PostgreSQL. An old `qianshu.db` is only a startup detection point; when the
> target PG database is empty, the backend refuses to start instead of silently creating a blank DB.

### 7.4 Moving data to a new disk

**compose**

```bash
docker compose down
sudo mkdir -p /data/qianshu
sudo cp -a data/. /data/qianshu/
# set QIANSHU_DATA_DIR=/data/qianshu in .env
docker compose up -d
```

**source**

Source mode does not read `QIANSHU_DATA_DIR`. Stop the services and update `backend/.env`:

```dotenv
DATABASE_URL=postgresql://qianshu:PASSWORD@127.0.0.1:5432/qianshu
BACKUP_DIR=/data/qianshu/backups
```

The database itself is managed by PostgreSQL and its path cannot be changed in `.env`.
For a new disk, take a logical backup and restore it:

```bash
sudo systemctl stop qianshu-backend qianshu-copilotd
pg_dump -h 127.0.0.1 -U qianshu -Fc -f /tmp/qianshu-before-move.dump qianshu
sudo mkdir -p /data/qianshu/backups
sudo cp -a backups/. /data/qianshu/backups/
# after PostgreSQL is ready at the new location (target DB empty or already backed up)
pg_restore --clean --if-exists --no-owner --no-privileges \
  -h 127.0.0.1 -U qianshu -d qianshu /tmp/qianshu-before-move.dump
# adjust ownership for the actual runtime user, then start
sudo systemctl start qianshu-backend qianshu-copilotd
```

The copilotd action log is controlled by `actions_path` in `config/qianshu-copilotd.json` (install
directory by default); edit that field and restart `qianshu-copilotd` if it should move too.
VictoriaMetrics/VictoriaLogs exist only in compose mode.

> Compose stores PostgreSQL data under `data/postgres`; move it together with `QIANSHU_DATA_DIR`.
> If unsure, stop everything and take a cold backup before moving. Source-mode PostgreSQL data is
> managed by the system service; use logical backup/restore for that move.

### 7.5 Stopping and data retention (no uninstall script)

There is **no automatic uninstall script**. Stopping services does not delete data; deleting anything
requires a verified backup and explicit authorization.

```bash
# compose: stop and remove containers, keep bind-mounted data
docker compose down

# source: stop and disable autostart
sudo systemctl disable --now qianshu-backend qianshu-copilotd
# if the development frontend was installed
sudo systemctl disable --now qianshu-frontend
```

Keep: `.env` (compose) or `backend/.env` (source), the data root, `backups/`,
`config/qianshu-copilotd.token` and `secrets/`. Deleting the code directory without backing up keys and
data permanently loses encrypted cloud credentials and all business history.

Full cleanup is a manual destructive operation and must be confirmed step by step:

1. Stop services and take a restorable backup;
2. Delete the install and data directories (compose: also remove unused images/volumes);
3. Remove `/etc/systemd/system/qianshu-*.service` and run `systemctl daemon-reload`;
4. For source production, remove `/etc/nginx/conf.d/qianshu.conf` and reload nginx as needed.

---

## 8. Configuration

- **The UI (setup wizard / system settings) is the source of truth for runtime settings**: changes are
  written to the database and survive restarts.
- `.env` carries bootstrap values and secrets: `SECRET_KEY`, `MASTER_KEY`, `DATABASE_URL`,
  `INIT_ADMIN_PASSWORD`, ports, data root, SLS/product cards, etc.
- Business settings edited directly in `.env` must be registered to persist:
  - source: `cd backend && python3 -m app.services.env_store`
  - compose: `docker compose exec backend python3 -m app.services.env_store`
- Environment adaptation without code changes: inject `SLS_ENDPOINT`, `SLS_SLB_TARGETS`,
  `SLS_APP_LOG`, `SLS_CDN_TARGET`, `PRODUCT_CARDS` via `.env`/UI. Unconfigured capabilities degrade
  gracefully to "not configured".

Common settings:

```dotenv
# Ports and data directory (compose)
BACKEND_PORT=8810
FRONTEND_PORT=8080
QIANSHU_DATA_DIR=/data/qianshu

# Database (PostgreSQL only)
# compose: the in-stack postgres starts by default; install.sh generates the password,
# and the URL is assembled from POSTGRES_*
POSTGRES_DB=qianshu
POSTGRES_USER=qianshu
# POSTGRES_PASSWORD is generated by the installer and written to .env; do not overwrite it for an existing DB

# source: the installer installs local PG15 and writes the URL; set it explicitly for external PG
# DATABASE_URL=postgresql://qianshu:PASSWORD@127.0.0.1:5432/qianshu
# Source-mode backup directory (compose uses the BACKUP_DIR=/backups volume)
# BACKUP_DIR=/data/qianshu/backups

# Package mirrors for restricted networks
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
NPM_REGISTRY=https://registry.npmmirror.com

# Retention (days; 0 = forever; VM/VL upper bounds are engine startup parameters)
VM_RETENTION_DAYS=180
VL_RETENTION_DAYS=30
RETENTION_AUDIT_DAYS=180
RETENTION_MSG_DAYS=90
```

---

## 9. FAQ and error handling

### 9.1 Installation

| Symptom | Cause | Fix |
|---|---|---|
| `有容器运行时但缺 compose 编排` | Docker installed without Compose | `dnf install docker-compose-plugin` or install podman-compose |
| `Docker 安装失败` | Repository/network unavailable | Install Docker/Podman manually; use source mode offline |
| `python3 版本过低（需 3.9+）` | Python too old | Upgrade Python or use a supported OS |
| `缺 npm` / `node 版本过低` | Source mode needs a frontend build | Install Node 18+, or use a release with `frontend/dist` |
| `缺 nginx` | Source production requires nginx | `dnf install nginx` / `apt install nginx` |
| Port 8810/8080 already in use | Another service owns it | `--port 9810`; compose also supports `FRONTEND_PORT` |
| `install.sh` fails when downloaded alone | The installer needs the sibling compose/backend/frontend/deploy files | Extract the full release or clone the full repo and run it inside; `curl \| bash` is not supported |
| `--data-dir` has no effect in source mode | The flag only manages compose volumes | Set `DATABASE_URL` and `BACKUP_DIR` in `backend/.env` (see §7.4) |
| `PostgreSQL 连接失败` | Wrong URL/network/credentials | Check `DATABASE_URL`, pg health, grants; see `docs/active/deployment.md` |
| No initial password in output | Old install or password already changed | Check `INIT_ADMIN_PASSWORD`; if changed, use the current password. **Never reset the production admin** |
| Where is the install log? | The installer always logs | `tail -100 /var/log/qianshu-install-*.log` |

### 9.2 Startup and access

| Symptom | Check |
|---|---|
| 403/500 from nginx, `Permission denied` in logs | Install dir under a 700 path such as `/root`; nginx cannot read `frontend/dist`. Install under `/opt/qianshu` or relax path permissions |
| Blank page or old UI | Hard refresh `Ctrl+Shift+R`; compose: rebuild `frontend`; source: ensure `frontend/dist` is new |
| Backend not ready in 90 s | compose: `docker compose logs backend`; source: `journalctl -u qianshu-backend -n 100` |
| `502 Bad Gateway` | Backend down or port mismatch; check the unit/log and nginx upstream |
| Login redirects back to login | `SECRET_KEY` changed; sign in again. All workers must share the same `.env` |
| Admin appears reset after migration | Connected to a different (empty) PostgreSQL DB or the backup was not restored; check `DATABASE_URL`/`POSTGRES_DB` and the restore target |
| Data lost after compose restart | Data not mounted under `QIANSHU_DATA_DIR`; inspect `.env` and `docker compose config` |

### 9.3 Monitoring and VMware

**No VMware VM/host charts; warning: "vCenter is connected, but no VMware performance samples were
returned this round."**

The collection button is not broken. QianShu **reads your vCenter statistics level automatically and,
when samples are zero, probes all intervals** (20s / 5min / 30min), writing the conclusion directly
into the warning. Read the red conclusion first, then act:

| Automatic diagnosis on the page | Meaning and action |
|---|---|
| Statistics level = 0 / all 0 | Performance statistics are disabled. Enable them per step 2 and wait 5–10 minutes. |
| All three interval probes have no valid samples | The vCenter API works and statistics are enabled, but no valid performance data is returned. Compare with vSphere Client per step 4: if vSphere shows charts while this page stays at 0, report the server logs; if vSphere is also empty, handle it on the vCenter side per step 5. |
| Real-time empty, historical has data | vCenter real-time statistics are disabled; confirm the real-time chart per step 4. |
| Statistics level unreadable / account errors | Usually permissions or connectivity; follow steps 3 and 5. |

Full six steps:

1. Sign in to vSphere Client and select **vCenter (not an individual ESXi host)** → Configure → Settings →
   General → Statistics → Edit.
2. Set **Statistics level to 1 or higher (2 recommended)**. 0 disables performance statistics and
   `QueryPerf` always returns empty. Wait 5–10 minutes after saving.
3. Grant the read-only account: Administration → Access Control → Roles, create a role with
   **Global → System.View**; then assign it on the target datacenter/cluster under Permissions and enable
   **Propagate to children**.
4. In vSphere Client select the same VM → Monitor → Performance and set the time range to **Past 1 hour**.
   Note whether vSphere Client itself shows a chart.
5. If vSphere Client shows charts but this page stays at 0: keep the page error and check
   `journalctl -u qianshu-backend`, then report it for investigation. If vSphere Client is also empty:
   ask the vCenter admin to check the performance statistics service (`vpxd`/`perfcharts`) and database
   space. Otherwise open Cloud Accounts → the account → **Test availability**, which should return a
   version. If it reports an SSL certificate error, enable **Skip SSL verification (self-signed
   certificate)**.
6. Return to Monitoring and click **"Collect this product"**. The button only collects the current tab's
   product (VMware VMs or hosts); it does not trigger ECS/RDS or other cloud collections. Per-account
   errors for the round are shown in the page warning.

More:

- **Assets/topology sync but no performance data**: asset sync and performance samples are separate
  channels; listing VMs does not imply performance-read access.
- **Read-only login fails**: check account lockout, vCenter-only login restrictions and special characters.
- **`pyvmomi` missing**: `backend/.venv/bin/pip install pyvmomi`, or reinstall; offline packages include it.
- **Self-signed vCenter certificate**: enable skip-SSL verification; use a corporate CA in production.
- **ECS metrics exist but VMware does not**: click "Collect this product" on the VMware tab, not the old
  "collect everything" button.

### 9.4 Upgrade and rollback

| Symptom | Fix |
|---|---|
| `找不到 pg_dump` | Install PostgreSQL client tools, or keep `qianshu-pg`/`qianshu-postgres` running for container-side backup |
| Source daily backups land in `/opt/qianshu/backups` | `backend/.env` has no `BACKUP_DIR`, so the code fallback is used | Point it at `<install-dir>/backups` (new installs write this automatically), restart the backend and confirm the backup file appears |
| `数据库备份失败，已中止升级` | Protection: no upgrade without a rollback point. Fix the backup and retry |
| Health check fails after upgrade | The script rolls back automatically; inspect `backups/upgrade-*` and `/var/log/qianshu-install-*.log` |
| Automatic rollback also fails | **Do not delete backups or continue.** Stop services and restore manually using `backups/upgrade-*/meta` and `db.meta`, or contact support |
| Release has no `frontend/dist` and no npm | Add a prebuilt frontend to the package, or install Node 18+ on the target |
| Release's `install.sh` doesn't support `--upgrade` | The script keeps the current installer and upgrades application code only; rebuild with the current `deploy/build_offline.sh` |
| Data changed after rollback | Rollback restores the DB snapshot; business data written after the backup is lost. VM metrics are unaffected |

### 9.5 Keys and credentials

| Symptom | Fix |
|---|---|
| Cloud account shows "cannot decrypt credentials" | `MASTER_KEY` changed or lost. Restore the original `.env`; otherwise delete and re-enter the account |
| All cloud accounts broke after changing `MASTER_KEY` | Never rotate it casually; export/re-enter credentials first |
| Secrets in shell history | Enter via UI/environment; clear history and rotate the credential |
| All users logged out after upgrade | Expected when `SECRET_KEY` changes; sign in again |

---

## 10. Security model

1. **Read-only by default**: diagnosis/inspection tools do not write; remediation produces command text
   for a human to execute.
2. **Human gate on writes**: restart/delete/change actions require explicit UI confirmation. SQL/PromQL/commands
   generated by the LLM are never executed directly.
3. **Encrypted credentials**: cloud AK/Secret are encrypted with `MASTER_KEY`; logs and APIs never echo plaintext.
4. **Notification hygiene**: platform self-healing writes audit records only; in-app messages carry business events.
5. **Audit trail**: login, settings changes, collection triggers and upgrades/rollbacks are audited.
6. **Least privilege**: use read-only RAM roles for cloud accounts and read-only vCenter roles; never use a
   primary account AK.

Report security issues privately per [SECURITY.md](SECURITY.md); do not open a public issue.

---

## 11. Development, testing and layout

```bash
# Local PostgreSQL (SQLite is gone; port avoids host 5432)
docker run -d --name qianshu-devpg -p 127.0.0.1:5433:5432 \
  -e POSTGRES_USER=qianshu -e POSTGRES_PASSWORD=devpass \
  -e POSTGRES_DB=qianshu postgres:16
# backend/.env: DATABASE_URL=postgresql://qianshu:devpass@127.0.0.1:5433/qianshu

# Backend (development)
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8810 --reload

# Frontend (development)
cd frontend
npm ci
npm run dev        # 5273, proxies /api to 8810

npm run build      # outputs frontend/dist

# Backend mock tests (no real cloud; needs disposable PG, see docs/active/engineering.md)
# export QIANSHU_TEST_PG_URL=postgresql://qianshutest:qianshutestpw@127.0.0.1:55432/postgres
python3 tests/test_vsphere_collect_mock.py
python3 tests/test_vsphere_host_mock.py

# Quality gate (some cases need local services/Chrome; see tests/quality)
python3 tests/quality/gate.py
```

Layout:

```
backend/          FastAPI (app/api, app/services, app/collector, app/copilotd)
frontend/         Vue 3 + Element Plus + Vite
deploy/           systemd units, nginx template, sysprep, offline package builder
docs/             architecture, designs, quality and release docs (Chinese)
tests/            mock tests and quality walkthroughs
config/           copilotd config and token (runtime)
data/             runtime data (not committed)
backups/          backups and upgrade rollback points (not committed)
```

See [CHANGELOG.md](CHANGELOG.md) for version history, [docs/active/status.md](docs/active/status.md) for
open-source readiness (Chinese), [CONTRIBUTING.md](CONTRIBUTING.md) for development rules, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community guidelines.

---

## License

Copyright 2026 QianShu contributors.

Licensed under the [Apache License, Version 2.0](LICENSE). You may not use this project except in
compliance with the License. See [LICENSE](LICENSE) for the full text, [NOTICE](NOTICE) for
attribution, and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for bundled third-party
components and their licenses.
