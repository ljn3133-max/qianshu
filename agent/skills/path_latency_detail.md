# Skill: 查域名下各接口路径的响应时间（平均/p95）与 5xx：定位到底哪个 URI 慢

> 千枢能力 · 场景: link_fault · 与 MCP 工具/API 同源生成

## 触发
用户问到：查域名下各接口路径的响应时间（平均/p95）与 5xx：定位到底哪个 URI 慢。用户点名 /api/xxx 慢时先查这个

## 用法
`工具 path_latency_detail，参数：host（完整域名（先 find_domain 拿到））, path（路径或前缀，如 /api/order（可选，缺省=全部 URI））, window（1h/6h/24h，默认 24h）, top（返回条数，默认 10）`

## 示例
```json
path_latency_detail({"host": "...", "path": "...", "window": "...", "top": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "path_latency_detail", "params": {...}}
