# Skill: 查某产品的日成本趋势（按量/包月拆分，T+1 账单统计方式），看费用是否突增

> 千枢能力 · 场景: cost_analysis · 与 MCP 工具/API 同源生成

## 触发
用户问到：查某产品的日成本趋势（按量/包月拆分，T+1 账单统计方式），看费用是否突增。可按 account_id 过滤

## 用法
`工具 product_cost_trend，参数：product（产品关键词，如 cdn / oss / ecs / rds）, days（趋势天数，默认 14）, account_id（云账号ID过滤（可选，缺省=全部账号聚合））`

## 示例
```json
product_cost_trend({"product": "...", "days": "...", "account_id": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "product_cost_trend", "params": {...}}
