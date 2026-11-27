# Skill: 空间上涨的表级嫌疑：从 SQL 证据找膨胀源（写语句大扫描）与临时文件源（大扫描+聚合/排序），定位到表

> 千枢能力 · 场景: dba · 与 MCP 工具/API 同源生成

## 触发
用户问到：空间上涨的表级嫌疑：从 SQL 证据找膨胀源（写语句大扫描）与临时文件源（大扫描+聚合/排序），定位到表

## 用法
`工具 space_suspects，参数：instance_id（RDS 实例ID（可先 rds_list））, days（回看天数，默认 3）`

## 示例
```json
space_suspects({"instance_id": "...", "days": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "space_suspects", "params": {...}}
