# 工程、质量与贡献

> 更新：2026-11-17。本文是当前工程约束与质量入口；文档目录规则见 [README.md](README.md)。

## 开始工作前

1. 按 [README.md](README.md) 的顺序读取 `active/` 中本任务需要的具体文件。
2. 按“安装 → 配置 → 首次获得价值 → 日常使用 → 故障定位”评估用户任务。
3. 先用工具确认问题真实存在，再读现有实现并复用模式。
4. 一个改动只做一件事，不顺手扩大范围。
5. 代码与文档不一致时同步更新 `active/`；旧版移入 `archive/`，不保留平行真相。
6. 完成必须用对应用户任务的真实数据端到端验证，接口 200 或文档存在不算完成。

## 任务契约与可信判定

- 实施 `FIRST-USE`、`CONCISE-INTERACTION`、`COST-INSIGHTS` 前，从 [README.md](README.md) 进入唯一任务契约，核对允许文件和反例；不得另建平行需求或完成状态表。
- 任务契约固定包含任务 ID/状态/日期、起点与结果、范围与不做、自动步骤与人工点、数据与失败契约、允许文件与测试、正反例、验证等级与发布条件。
- 新增本地链接、任务必填章节、关联测试路径和任务 ID 唯一性必须进入自动检查；内容正确性仍由对应功能测试证明。
- 断言必须检查目标字段的真实内容、类型和长度；未知断言、缺失字段、非预期 HTTP 状态及子进程非零退出必须失败。
- 跳过、未执行和视觉待复核不计通过；摘要不得承诺“无需人工验收”。
- 新测试必须接入 `.github/workflows/ci.yml` 实际执行的入口，不能只新增一个无人调用的脚本。

## 代码地图

```text
backend/app/api/          REST / WebSocket 路由
backend/app/core/         配置、数据库、迁移、安全
backend/app/models/       SQLAlchemy 模型
backend/app/services/     业务编排、诊断、成本、智能体
backend/app/collector/    云与数据源采集 / 查询适配
frontend/src/views/       页面与业务页签
frontend/src/router/      路由与首次接入守卫
frontend/src/api/         API 客户端
agent/skills/             运行时 Skill 资源，不是文档真相源
agent/mcp/                MCP 工具导出快照
agent/knowledge/          平台自改经验运行留痕
deploy/                   部署模板
update-data/              版本化数据更新 SQL
tests/quality/            工程质量门与正式视觉基线
docs/active/              唯一文档真相源
docs/archive/             只读历史，默认不读
docs/tmp/                 可弃运行产物，默认不读
```

## 代码约定

- 写路径保留人工确认、审计和失败恢复；诊断工具默认只读。
- LLM 只产出文本或结构化参数，不直接执行生成的 SQL、PromQL 或 Shell。
- 新增审计动作同步登记中文动作名。
- 用户文案同时维护 `zh-CN` 与 `en-US`。
- 新增前端页面必须加入 `tests/quality/pages.py`。
- 客户域名、IP、密钥、账单和原始日志不得硬编码进源码、测试或文档。
- 不引入不必要依赖；SDK 锁定版本、按既有周期扫描，不自动升级外部组件。
- 平台自改管道只允许既有代码域和 `docs/active/`、`docs/tmp/`；不得修改 `docs/archive/` 或在 `docs/` 新建其他目录。
- 贡献流程与安全披露分别遵循根目录 `CONTRIBUTING.md`、`SECURITY.md`。

## 五层质量门

| 层 | 入口 | 内容 |
|---|---|---|
| 静态质量门 | `tests/quality/gate.py` | API 断链、模板绑定、模型字段、Vite 模块转换、路由覆盖、跳转目标、审计动作 |
| 功能回归 | `features.yaml` + `features_test.py` | 内容级断言，HTTP 200 不算通过 |
| 接口冒烟 | `api_smoke.py` | 自动发现无路径参数的只读 GET |
| 页面渲染 | `browser_sweep.py` | 真实 Chrome、页面质量契约和逐页签检查 |
| 视觉基线 | `visual_baseline.py` | 与 `tests/quality/baseline/` 像素容差对比，人工核对后才固化 |

`run_all.py` 还运行结构指纹并生成一页临时摘要。摘要和视觉差异图统一写 `docs/tmp/`，不得再写 `docs/quality/` 或 `active/`。

## 常用命令

```bash
# 静态门：需要本机后端 8810 与 Vite 5273
python3 tests/quality/gate.py

# 只读总门：使用本机一次性验收账号（身份只走环境变量）
QIANSHU_TEST_USERNAME="<一次性验收用户>" \
PASSWORD="<该用户口令>" \
QIANSHU_QUALITY_READONLY=1 \
python3 tests/quality/run_all.py

# 前端生产构建
npm --prefix frontend run build

# 后端隔离 mock：需要一次性 PostgreSQL 管理连接（SQLite 已废除）
# 每个测试自建唯一临时库并在退出时清理；未设置变量会直接失败
docker run --rm -d --name qianshu-testpg -p 127.0.0.1:55432:5432 \
  -e POSTGRES_USER=qianshutest -e POSTGRES_PASSWORD=qianshutestpw \
  -e POSTGRES_DB=postgres postgres:16
export QIANSHU_TEST_PG_URL=postgresql://qianshutest:qianshutestpw@127.0.0.1:55432/postgres
cd backend
for f in tests/test_*_mock.py; do
  case "$f" in *cdp*) continue;; esac
  python3 "$f" || exit 1
done
docker rm -f qianshu-testpg
```

默认地址可用 `QIANSHU_API`、`QIANSHU_SITE` 覆盖。静态门的 Vite 模块转换检查不替代生产构建。

GitHub Actions（`.github/workflows/ci.yml`）只跑隔离项：后端字节编译、结构指纹、不依赖后端/网络的 mock、前端生产构建，以及用页面内 `/api` mock 的变更卡 CDP 用例（构建产物 + `vite preview` + 预装 Chrome，不连真实后端）。需要真实后端、一次性身份、外部只读查询的用例只在具备授权的环境中手动运行。

## 测试红线

- `QIANSHU_QUALITY_READONLY` 默认不是开启状态，只读总门必须显式设为 `1`。
- 非 GET、真实云写入、外部集群写入和真实通知不得为了全绿执行；跳过项不计通过。
- GET 也可能实时访问云，只读总门按现有清单排除实时 SLS 范围。
- 总门会写 `docs/tmp/`；既有浏览器脚本还会结束现有 Chrome 并重建临时目录，运行前确认本机没有需保留的 Chrome 会话。
- Chrome 统一通过 `tests/quality/chrome_run.py` 启动：root 下自动降权 nobody（规避 Chrome 146 的 root 网络吊死），普通用户/CI runner 保持当前身份。
- 验收只用一次性账号并清理，不读取或重置既有 admin 密码与强制改密标志。
- 隔离测试使用临时库和专用假凭证；破坏性逻辑必须先备份并在副本验证。
- 不读取 `docs/tmp/` 摘要作为当前事实；当前验收结论由负责人核对后写回 `status.md`。

## 交付前自评审

1. 重读全部改动，核对功能预期、边界、失败路径和同类挂载点。
2. 新增本地链接必须可达；新文档必须从 `active/README.md` 或对应任务文档可达。
3. 报告固定说明“改了什么 / 验证了什么 / 还剩什么不改及理由”。
4. 明确真实、隔离、代码支持和未执行范围，不把历史证据当当前复验。
5. 文档重构或回滚同样要验证运行时引用，不能只检查文件是否存在。
