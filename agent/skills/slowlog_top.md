# Skill: 查某 RDS 实例的慢SQL Top（执行次数/耗时/SQL 摘要），定位慢查询

> 千枢能力 · 场景: dba · 与 MCP 工具/API 同源生成

## 触发
用户问到：查某 RDS 实例的慢SQL Top（执行次数/耗时/SQL 摘要），定位慢查询

## 用法
`工具 slowlog_top，参数：instance_id（实例ID或名称（可先 rds_list））, days（1~7，默认 7）`

## 示例
```json
slowlog_top({"instance_id": "...", "days": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "slowlog_top", "params": {...}}
