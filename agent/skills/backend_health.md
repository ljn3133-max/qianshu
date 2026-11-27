# Skill: 查域名的后端 ECS：运行状态、CPU/内存/磁盘 24h 峰值、每台的流量/5xx/RT

> 千枢能力 · 场景: link_fault · 与 MCP 工具/API 同源生成

## 触发
用户问到：查域名的后端 ECS：运行状态、CPU/内存/磁盘 24h 峰值、每台的流量/5xx/RT

## 用法
`工具 backend_health，参数：host（完整域名）`

## 示例
```json
backend_health({"host": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "backend_health", "params": {...}}
