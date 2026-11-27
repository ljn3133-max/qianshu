# Skill: 查下游 RDS/Redis 关键指标（CPU/内存/磁盘/连接/平均RT），含各指标最高实例读数，看是否有下游瓶颈

> 千枢能力 · 场景: link_fault, dba · 与 MCP 工具/API 同源生成

## 触发
用户问到：查下游 RDS/Redis 关键指标（CPU/内存/磁盘/连接/平均RT），含各指标最高实例读数，看是否有下游瓶颈

## 用法
`工具 db_health，参数：window（1h/6h/24h，默认 24h）`

## 示例
```json
db_health({"window": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "db_health", "params": {...}}
