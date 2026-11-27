# Skill: 账单拆解：目标（实例/产品/场景）近 N 天账单行、逐日趋势、计费场景构成——'这笔钱怎么来的'

> 千枢能力 · 场景: cost_analysis, dba · 与 MCP 工具/API 同源生成

## 触发
用户问到：账单拆解：目标（实例/产品/场景）近 N 天账单行、逐日趋势、计费场景构成——'这笔钱怎么来的'。可按 account_id 过滤

## 用法
`工具 bill_breakdown，参数：target（实例ID/名称/产品名，如 daspost-cn-xxx 或 对象存储）, days（默认 30，最多 90）, account_id（云账号ID过滤（可选，缺省=全部账号聚合））`

## 示例
```json
bill_breakdown({"target": "...", "days": "...", "account_id": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "bill_breakdown", "params": {...}}
