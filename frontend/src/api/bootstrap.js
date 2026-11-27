import http from '@/api/http'

// 安装向导（千枢核心流程）：接入方式探测 + 底座体检报告
export const bootstrapStatus = () => http.get('/bootstrap/status')
export const bootstrapProbe = (data) => http.post('/bootstrap/probe', data)
export const bootstrapReport = () => http.get('/bootstrap/report')

// 补数据闭环：SLS 目录 + 配置写回
export const bootstrapSlsCatalog = (accountId = 0) =>
  http.get('/bootstrap/sls-catalog', { params: { account_id: accountId || undefined } })
export const applySlsConfig = (data) => http.post('/bootstrap/apply-config', data)

// 非云机器与已有监控的接入命令和监控源清单
export const bootstrapAgentCommand = () => http.get('/bootstrap/agent-command')
export const bootstrapMonitorSources = () => http.get('/bootstrap/monitor-sources')
// Agent 单文件脚本内容（下载落盘用；后端带 Content-Disposition attachment）
export const bootstrapAgentScript = () => http.get('/bootstrap/agent-script', { responseType: 'text' })
