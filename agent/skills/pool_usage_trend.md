# Skill: 查某类资源包的每日消耗趋势、平均/最近日消耗、几天耗尽（如 CDN 流量包、日志服务包）

> 千枢能力 · 场景: cost_analysis · 与 MCP 工具/API 同源生成

## 触发
用户问到：查某类资源包的每日消耗趋势、平均/最近日消耗、几天耗尽（如 CDN 流量包、日志服务包）。可按 account_id 过滤

## 用法
`工具 pool_usage_trend，参数：commodity（包类型关键词，如 cdn / dcdn / sls / oss / csk）, days（趋势天数，默认 14）, account_id（云账号ID过滤（可选，缺省=全部账号聚合））`

## 示例
```json
pool_usage_trend({"commodity": "...", "days": "...", "account_id": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "pool_usage_trend", "params": {...}}
