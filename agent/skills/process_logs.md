# Skill: 查进程对应的应用日志：服务名归一匹配 AppName，看事发时段它在跑什么（日志量形状/级别/内容样本）

> 千枢能力 · 场景: host_res · 与 MCP 工具/API 同源生成

## 触发
用户问到：查进程对应的应用日志：服务名归一匹配 AppName，看事发时段它在跑什么（日志量形状/级别/内容样本）

## 用法
`工具 process_logs，参数：process（进程名，如 nginx / java / xxx.WinService.exe）, around（事发时刻如 2026-10-07 19:04（可选））, window（1h/6h/24h，默认 24h）`

## 示例
```json
process_logs({"process": "...", "around": "...", "window": "..."})
```

## 口径与边界
- 本清单工具仅用于只读取证；LLM 只产出工具名+参数，由代码白名单校验执行
- 本清单不提供外部写操作；平台其他受控写操作必须经过角色权限、明确确认、审计与回滚
- REST 等价: POST /api/ai/tools/execute {"tool": "process_logs", "params": {...}}
