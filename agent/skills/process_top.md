# Skill: 查进程级 top（内存或 CPU）：各进程峰值/日常水位/突变时刻——谁在吃资源

> 千枢能力 · 场景: host_res · 与 MCP 工具/API 同源生成

## 触发
用户问到：查进程级 top（内存或 CPU）：各进程峰值/日常水位/突变时刻——谁在吃资源

## 用法
`工具 process_top，参数：instance（ECS 实例ID或名称）, metric（memory/cpu，默认 memory）, window（1h/6h/24h，默认 24h）, around（聚焦时刻如 2026-10-07 19:05（可选））`

## 示例
```json
process_top({"instance": "...", "metric": "...", "window": "...", "around": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "process_top", "params": {...}}
