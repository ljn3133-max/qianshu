# 贡献指南

感谢参与千枢。提交代码前请先读本文件；涉及安全问题的请按 [`SECURITY.md`](SECURITY.md) 私下报告，不要开公开 Issue。

## 环境要求

| 组件 | 版本 | 用途 |
|---|---|---|
| Python | 3.9+ | 后端（FastAPI） |
| Node.js | 18+ | 前端（Vue 3 + Vite） |
| Chrome / Chromium | 最新 | 浏览器走查与视觉基线（仅本地/CI E2E 任务） |
| Docker / Podman | 可选 | compose 部署、PostgreSQL / VictoriaMetrics |

## 本地启动

```bash
# 后端
cd backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env          # 填 SECRET_KEY / MASTER_KEY（或运行 ../install.sh 生成）
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8810

# 前端（另开终端）
cd frontend
npm ci
npm run dev                       # http://127.0.0.1:5273
```

> 生产/交付形态请使用 `install.sh`（compose 或 `--mode source`），不要把开发服务器直接暴露到公网。
> 首次登录后必须修改初始密码；`must_change_password` 未清除前，除白名单接口外全部拒绝。

## 测试（提交前必跑）

质量脚本只接受**一次性非 admin 测试用户**：在管理页新建独立用户，跑完删除；不要使用或重置 `admin` 及任何既有账号。身份通过环境变量传入，不写进脚本、命令行历史或文件：

```bash
export QIANSHU_TEST_USERNAME='ci-once-<随机后缀>'
export PASSWORD='<一次性合成口令>'

# 1) 全量一页验收（需后端 8810 + 前端 5273 已启动）
python3 tests/quality/run_all.py

# 2) 仅静态质量门（A 断链 / B 绑定 / C 模型字段 / D 编译 / E 路由覆盖 / F 跳转 / G 审计动作）
python3 tests/quality/gate.py

# 3) 后端隔离 mock 测试（一次性 PostgreSQL；不碰真实库、不调网络、不发通知）
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

SQLite 已废除；未设置 `QIANSHU_TEST_PG_URL` 时测试直接失败，不会回退到本地文件库。

变更卡浏览器用例 `tests/test_agent_card_cdp_mock.py` 额外需要本机 Chrome 与前端 `5273`（`npm run dev`）已启动；它用页面内 mock 拦截全部 `/api`，不依赖后端。CI 的 mock 任务不装 Chrome，跳过这一类；另有独立 `browser` job 用 `vite preview` 构建产物 + 预装 Chrome 跑该用例。

端点可移植：`QIANSHU_API`、`QIANSHU_SITE` 环境变量可覆盖默认的 `127.0.0.1:8810` / `127.0.0.1:5273`；`gate.py` 兼容旧变量 `GATE_API`。

### 测试红线

- 只走**只读/无副作用**路径；危险写操作（重启、删除、变更、改密）没有明确授权不得测试。
- 绝不向真实企微/钉钉/通知渠道发送测试消息。
- 生产库只允许操作**本次测试自建的库**；隔离测试使用临时 SQLite 与专属假口令。
- LLM 只产出文本/参数；测试不得执行 LLM 生成的 SQL / PromQL / Shell 原文。

## 代码约定

1. **写路径永远人工门**：诊断/巡检工具默认只读；处置动作只产出命令文本。
2. **可观测、可审计**：新增写操作必须调用 `audit.record(action=...)`，并在 `app/services/audit.py` 的 `ACTION_LABELS` 登记中文动作名（`gate.py` G 项会拦截漏登记）。
3. **双语**：后端下发给用户的文案走 `app/core/i18n_msg.py`（zh/en 双全）；前端新增文案同时补 `zh-CN` 与 `en-US` locale。
4. **前端新增页面**：必须加入 `tests/quality/pages.py` 的 `PAGES`，否则质量门 E 项红灯（走查/视觉基线会漏页）。
5. **不硬编码环境事实**：客户域名、IP、AK、账单数据不得进入代码/测试/文档；用 `.env` 或 UI 配置。
6. **不引入不必要的依赖**：能用标准库解决的不要加包；新增依赖需说明用途并更新 `requirements.txt`。
7. **复用优先**：先看现有服务/组件怎么做的，遵循既有模式；改动只做一件事。

## 许可与素材

- 本项目按 [Apache License 2.0](LICENSE) 授权。提交即表示你同意以同一许可提供该贡献（inbound = outbound），无需签署额外 CLA。
- 只提交你拥有权利或已获授权的代码、图片、字体、图标、示例数据和文档。请勿粘贴 GPL/AGPL 等与 Apache-2.0 不兼容的代码，也不要复制来源不明的截图或客户素材。
- 新增或升级第三方依赖时，必须确认其许可证可再分发，并在 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 中更新清单；离线包中的 wheel 与 npm 包自带的 LICENSE/NOTICE 不得删除。
- 项目名称、Logo 和商标不在 Apache-2.0 授权范围内；再分发时不要暗示官方背书。

## 提交与 PR

- 一个 PR 只做一件事；描述里写清"改了什么 / 怎么验证 / 没验证到什么"。
- 不要提交 `.env`、`secrets/`、`data/`、`backups/`、数据库文件、构建产物、客户截图。
- 提交前执行 `git status` 复核，确认没有凭证或客户信息进入暂存区。
- 安全问题不要放在公开 PR 描述里。

## 行为准则

参与本项目即表示同意遵守 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
