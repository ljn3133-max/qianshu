# Skill: 计费相关配置实况：SQL 审计/日志的开通状态、冷热存储时长与收费统计方式——'是不是开着、哪块在收钱'

> 千枢能力 · 场景: cost_analysis, dba · 与 MCP 工具/API 同源生成

## 触发
用户问到：计费相关配置实况：SQL 审计/日志的开通状态、冷热存储时长与收费统计方式——'是不是开着、哪块在收钱'

## 用法
`工具 bill_config，参数：instance（RDS 实例ID或名称，如 rds-example）`

## 示例
```json
bill_config({"instance": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "bill_config", "params": {...}}
