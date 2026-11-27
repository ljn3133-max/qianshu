# Skill: 查主机内存/CPU/磁盘水位时间线：峰值时刻、峰值、当前值（资源类问题先查这个定位事发时刻）

> 千枢能力 · 场景: host_res, link_fault · 与 MCP 工具/API 同源生成

## 触发
用户问到：查主机内存/CPU/磁盘水位时间线：峰值时刻、峰值、当前值（资源类问题先查这个定位事发时刻）

## 用法
`工具 host_metrics，参数：instance（ECS 实例ID或名称）, window（1h/6h/24h，默认 24h）`

## 示例
```json
host_metrics({"instance": "...", "window": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "host_metrics", "params": {...}}
