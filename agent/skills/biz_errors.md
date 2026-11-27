# Skill: 查业务报错：应用 ERROR 与 SLB 5xx 的小时分布——资源异常时段业务是否受损

> 千枢能力 · 场景: host_res, link_fault · 与 MCP 工具/API 同源生成

## 触发
用户问到：查业务报错：应用 ERROR 与 SLB 5xx 的小时分布——资源异常时段业务是否受损

## 用法
`工具 biz_errors，参数：window（1h/6h/24h，默认 24h）, around（聚焦时刻（可选））, app_name（应用名过滤（可选））, host（域名过滤（可选））`

## 示例
```json
biz_errors({"window": "...", "around": "...", "app_name": "...", "host": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "biz_errors", "params": {...}}
