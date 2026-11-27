# Skill: 查应用 ERROR 日志：数量 + 最新样本内容（应用名 = 链路库 app_name 或域名）

> 千枢能力 · 场景: link_fault, host_res · 与 MCP 工具/API 同源生成

## 触发
用户问到：查应用 ERROR 日志：数量 + 最新样本内容（应用名 = 链路库 app_name 或域名）

## 用法
`工具 app_errors，参数：app_name（应用名，如 www.example.com）, window（1h/6h/24h，默认 1h）`

## 示例
```json
app_errors({"app_name": "...", "window": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "app_errors", "params": {...}}
