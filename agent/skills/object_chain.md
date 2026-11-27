# Skill: 对象全景因果链：域名/实例近 N 天的多层同窗读数（CDN/SLB/应用/主机/DB/变更/钱）+ 联动变化事件——跨层分析第一入口

> 千枢能力 · 场景: link_fault, host_res, cost_analysis, dba · 与 MCP 工具/API 同源生成

## 触发
用户问到：对象全景因果链：域名/实例近 N 天的多层同窗读数（CDN/SLB/应用/主机/DB/变更/钱）+ 联动变化事件——跨层分析第一入口

## 用法
`工具 object_chain，参数：object（域名或实例（如 cdn.example.com / i-xxx））, days（默认 7，最多 30）`

## 示例
```json
object_chain({"object": "...", "days": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "object_chain", "params": {...}}
