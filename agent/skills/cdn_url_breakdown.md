# Skill: CDN 域名的 URL/文件级流量下钻：哪些文件在烧流量、峰值日较前日谁涨了（流量突增归因）

> 千枢能力 · 场景: cost_analysis, link_fault, host_res · 与 MCP 工具/API 同源生成

## 触发
用户问到：CDN 域名的 URL/文件级流量下钻：哪些文件在烧流量、峰值日较前日谁涨了（流量突增归因）

## 用法
`工具 cdn_url_breakdown，参数：domain（CDN 域名，如 cdn.example.com）, days（默认 7，最多 30）, top（默认 10）`

## 示例
```json
cdn_url_breakdown({"domain": "...", "days": "...", "top": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "cdn_url_breakdown", "params": {...}}
