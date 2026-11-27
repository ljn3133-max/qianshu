# Skill: 云上变更时间线：谁在什么时候改了什么（重启/改配置/扩容等）

> 千枢能力 · 场景: link_fault, cost_analysis, dba, host_res · 与 MCP 工具/API 同源生成

## 触发
用户问到：云上变更时间线：谁在什么时候改了什么（重启/改配置/扩容等）。排障第一问——先查这个再查指标

## 用法
`工具 recent_changes，参数：hours（回看小时数，默认 48）, keyword（按资源/操作/人过滤，可选）, account_id（云账号ID，缺省/0=全部账号）`

## 示例
```json
recent_changes({"hours": "...", "keyword": "...", "account_id": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "recent_changes", "params": {...}}
