# Skill: 查域名在 SLB 层的健康：状态码分布、5xx率、平均RT、按后端分组

> 千枢能力 · 场景: link_fault · 与 MCP 工具/API 同源生成

## 触发
用户问到：查域名在 SLB 层的健康：状态码分布、5xx率、平均RT、按后端分组。窗口默认 24h

## 用法
`工具 slb_overview，参数：host（完整域名（先 find_domain 拿到））, window（1h/6h/24h，默认 24h）`

## 示例
```json
slb_overview({"host": "...", "window": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "slb_overview", "params": {...}}
