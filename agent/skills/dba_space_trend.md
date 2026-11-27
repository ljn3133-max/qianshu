# Skill: 查某 RDS 实例空间近 14 天趋势（数据/日志/备份/磁盘，含 14 天增量）

> 千枢能力 · 场景: dba · 与 MCP 工具/API 同源生成

## 触发
用户问到：查某 RDS 实例空间近 14 天趋势（数据/日志/备份/磁盘，含 14 天增量）

## 用法
`工具 dba_space_trend，参数：instance_id（实例ID或名称（可先 rds_list））`

## 示例
```json
dba_space_trend({"instance_id": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "dba_space_trend", "params": {...}}
