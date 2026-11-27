# Skill: 查近 14 天成本异动记录（池将耗尽/包刚耗尽/按量突增/新增费用项，含确认状态）

> 千枢能力 · 场景: cost_analysis · 与 MCP 工具/API 同源生成

## 触发
用户问到：查近 14 天成本异动记录（池将耗尽/包刚耗尽/按量突增/新增费用项，含确认状态）

## 用法
`工具 cost_anomaly_list，无参数`

## 示例
```json
cost_anomaly_list()
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "cost_anomaly_list", "params": {...}}
