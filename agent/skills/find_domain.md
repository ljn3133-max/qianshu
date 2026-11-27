# Skill: 按关键词查找已知链路域名（把『xxx.com打不开』对准到具体域名），返回后端/流量/健康概要

> 千枢能力 · 场景: link_fault · 与 MCP 工具/API 同源生成

## 触发
用户问到：按关键词查找已知链路域名（把『xxx.com打不开』对准到具体域名），返回后端/流量/健康概要

## 用法
`工具 find_domain，参数：keyword（域名或关键词，如 example.com 或 www.example.com）`

## 示例
```json
find_domain({"keyword": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "find_domain", "params": {...}}
