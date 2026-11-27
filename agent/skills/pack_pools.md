# Skill: 查全部资源包抵扣能力池：类型/包数/剩余/剩余比例（告警统计方式是池不是单包）

> 千枢能力 · 场景: cost_analysis · 与 MCP 工具/API 同源生成

## 触发
用户问到：查全部资源包抵扣能力池：类型/包数/剩余/剩余比例（告警统计方式是池不是单包）。可按 account_id 过滤

## 用法
`工具 pack_pools，参数：account_id（云账号ID过滤（可选，缺省=全部账号聚合））`

## 示例
```json
pack_pools({"account_id": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "pack_pools", "params": {...}}
