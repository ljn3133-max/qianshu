# update-data：随版本发布的升级 SQL

这里存放**按产品版本归档的数据库升级脚本**。后端启动时自动执行尚未应用的脚本，
新装、升级、回滚三条路径都走同一套逻辑，不需要现场手工连库执行 SQL。

## 目录约定

```
update-data/
├── README.md
├── _template/                 # 模板，目录名不是版本号，永远不会被执行
│   ├── 001_add_column.sql.example
│   └── 002_backfill.pg.sql.example
├── 0.3.0/                     # 发布 0.3.0 时新建：只放 0.3.0 新增的变更（当前实例）
│   ├── 001_add_xxx.sql
│   └── 002_backfill.pg.sql    # PostgreSQL 专有语法使用
└── 0.3.1/
    └── 001_xxx.sql
```

- 目录名必须是 `主.次.修订`（可带 `-rc.1` 预发布后缀，也可写成 `v0.1.1`）。
  不符合版本格式的目录（如 `_template`）会被忽略。
- 目录内的 `.sql` 文件按**文件名升序**执行，建议用 `001_`、`002_` 前缀。
- 一个版本一个目录；**已发布的版本目录不再修改**（校验和会拦住改动，见下）。
- 需要 PostgreSQL 专有语法时使用方言文件 `002_xxx.pg.sql`；有方言文件时优先，
  通用 `002_xxx.sql` 不会与方言文件同时执行。平台只支持 PostgreSQL，SQLite 已废除。

## 执行时机与顺序

后端启动时在 schema 初始化锁内依次执行：

1. `Base.metadata.create_all()`：确保模型表存在（新库在这里建出当前完整 schema）；
2. legacy `run_migrations()`：历史补列/数据迁移；
3. `_ensure_schema_columns()`：补齐存量库的历史列；
4. **update-data 执行器**：按版本升序应用尚未执行的 SQL（schema 演进的最后一环）；
5. 任务收割、初始管理员、环境变量对账、启动后台调度。

多 worker 启动时由 `.schema.lock` 串行化，只有一个 worker 真正执行；记录表保证
其余 worker 看到“已应用”后直接跳过。

## 新库与老库的基线

- **新库**（启动前还没有 `assets` 表）：`create_all` 已经建出当前版本完整 schema，
  因此包内所有历史版本只登记为 `baseline`，**不执行**。避免“建表已经包含新列，
  再执行 ADD COLUMN 报重复列”。
- **老库**（已有 `assets` 表、还没有 `schema_update_log` 记录）：update-data 是
  从 `LEGACY_BASELINE_VERSION`（当前为 `0.1.0`）之后开始接管，因此 `<= 0.1.0`
  的目录登记为 `baseline`，之后的版本正常执行。这保证把本功能第一次装到存量环境时，
  不会重放 0.1.0 之前已经由 legacy migrate 做过的变更。
- **已接入的库**：逐版本、逐文件比对记录，只执行缺失项。

> **新库不走升级 SQL，因此模型必须同步**：新库的 schema 完全来自 `Base.metadata.create_all()`。
> 改了列/表/普通索引，必须同时改 SQLAlchemy 模型，否则新装环境缺变更。
> 模型无法表达的 DDL（触发器、部分索引、物化视图等）不要只写在 update-data 里，
> 应放到启动初始化阶段做幂等 ensure，否则新库永远不会执行到。

## 记录表 `schema_update_log`

| 字段 | 说明 |
|---|---|
| `version` | 版本目录名，如 `0.1.1` |
| `filename` | SQL 文件名；`__baseline__` 表示整版基线 |
| `checksum` | 文件 SHA-256（baseline 为空） |
| `applied_at` | UTC ISO 时间 |
| `duration_ms` | 执行耗时 |
| `status` | `applied` / `baseline` |
| `error` | 保留字段，失败不写记录（事务回滚） |

查询已执行版本：

```sql
SELECT version, filename, applied_at, duration_ms, status
FROM schema_update_log ORDER BY version, filename;
```

只读查看当前状态（不执行任何 SQL）：

```bash
cd /opt/qianshu/backend
python3 -m app.core.update_data
```

## 部署形态与目录定位

执行器按以下顺序定位 `update-data/`：

1. 环境变量 `UPDATE_DATA_DIR`（容器形态必须显式设置）；
2. PyInstaller 单文件：可执行文件同级的 `update-data/`；
3. 源码形态默认 `<项目根>/update-data`（即 `backend/app/core/update_data.py` 向上三级）。

| 形态 | 目录位置 | 配置 |
|---|---|---|
| 源码 / offline 包 | `<安装目录>/update-data` | 无需配置（默认值） |
| docker-compose | 容器内 `/update-data`（只读挂载） | compose 已内置，`./update-data:/update-data:ro` |
| PyInstaller 单文件 | 可执行文件旁 `update-data/` | 默认自动识别；也可用 `UPDATE_DATA_DIR` 覆盖 |

compose 的 backend 与 copilotd **都要**挂载：看门狗拉起的 backend 进程继承
copilotd 的环境变量与挂载，漏一处会导致自愈重启后看不到升级 SQL。

## 事务与失败处理

- **单个文件 = 一个事务**：SQL 语句与 `schema_update_log` 记录在**同一个事务**里提交。
  中途失败则本文件所有语句和历史记录一起回滚，不会出现“SQL 执行了但没记账”。
- 第一个失败的文件会抛 `UpdateDataError`，**后端启动中止**。`install.sh --upgrade`
  的健康检查失败后自动回滚代码目录与数据库备份；`--rollback` 可手工回滚。
  这是刻意设计：宁可升级失败，也不带着半套 schema 对外服务。
- 重试：修复 SQL 后重新升级即可。已成功的文件有记录会跳过，失败文件从头执行
  （它已被事务回滚，不会半执行）。
- **校验和保护**：已执行文件的 SHA-256 与记录不一致时（有人改了已发布文件），
  执行器记录 ERROR 并拒绝重复执行，不会把新逻辑再跑一遍。修正方式只有发布
  **新版本目录**（如 `0.1.2/002_fix_xxx.sql`），不要改旧版本。
- **禁止**在 SQL 里写 `BEGIN` / `COMMIT` / `ROLLBACK`，事务由执行器管理。
- SQL 必须可重复发布、语义清晰；不要在脚本里写死环境相关路径、账号或密钥。

## 编写检查表

1. 目录名是否是本次发布版本号？旧版本目录是否保持原样？
2. 文件名是否 `NNN_动词_对象.sql`，并能在列表里一眼看出顺序？
3. 是否只做本次发布需要的一件事？大变更拆成多个文件更易定位失败。
4. 新列是否允许 NULL 或带默认值（老数据不回填会怎样）？
5. 索引/表名是否加了业务前缀，避免与平台既有对象冲突？
6. PostgreSQL 语法是否在测试库验证过？数据库专有语法写入 `.pg.sql`。
7. 是否在**测试库**完整跑过“老库升级 + 重复启动 + 回滚重升”？
8. 是否确认没有把 `data/`、`.env`、数据库文件、备份打进发布包？

## 发布流程

1. 复制 `_template` 为本次版本号目录（如 `0.1.1`），删除 `.example` 后缀并按需改写；
2. 在测试库验证；
3. 更新 `CHANGELOG.md`（写明“升级 SQL：...”）；
4. 打包发布包（`install.sh` 的应用包会连同 `update-data/` 一起复制，无需额外配置）；
5. 现场执行 `./install.sh --upgrade --pkg ...`，安装器备份后重启，启动时自动应用。

## 回滚

- `install.sh --upgrade` 健康检查失败会自动回滚代码 + 数据库备份。
- 手工回滚：`./install.sh --rollback`（恢复升级前备份，包括 `schema_update_log`
  与所有已执行 SQL 的 schema 状态）。代码回滚会整体替换 `update-data/`，失败升级
  新增的版本目录不会残留到回滚后的旧代码上。
- SQL 升级本身**不做逆脚本**：回滚依赖升级前的数据库备份。因此涉及数据删除/
  重写的脚本必须先在测试库演练，并在发布说明里标注风险。
