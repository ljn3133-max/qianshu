# Skill: 查 RDS 实例清单（引擎/状态/到期），下钻慢SQL或空间前先看这个

> 千枢能力 · 场景: dba · 与 MCP 工具/API 同源生成

## 触发
用户问到：查 RDS 实例清单（引擎/状态/到期），下钻慢SQL或空间前先看这个。可按 account_id 过滤

## 用法
`工具 rds_list，参数：account_id（云账号ID过滤（可选，缺省=全部账号聚合））`

## 示例
```json
rds_list({"account_id": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "rds_list", "params": {...}}
