# Skill: 查 CDN/DCDN 按域名的每日流量——CDN 流量包到底被哪个域名烧掉、各占多少

> 千枢能力 · 场景: cost_analysis · 与 MCP 工具/API 同源生成

## 触发
用户问到：查 CDN/DCDN 按域名的每日流量——CDN 流量包到底被哪个域名烧掉、各占多少

## 用法
`工具 cdn_domain_traffic，参数：days（趋势天数，默认 14）, top（返回域名数，默认 5）`

## 示例
```json
cdn_domain_traffic({"days": "...", "top": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "cdn_domain_traffic", "params": {...}}
