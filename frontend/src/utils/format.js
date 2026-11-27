// 全局格式化工具：统一时间、数字、百分比和容量显示
import { t } from '@/i18n'

export function fmtTime(iso, withDate = true) {
  if (!iso) return '-'
  const s = iso.replace('T', ' ').slice(0, 19)
  return withDate ? s : s.slice(11, 16)
}

export function fmtNum(v, digits = 0) {
  if (v == null || isNaN(v)) return '-'
  return Number(v).toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

export function fmtPct(v, digits = 1) {
  if (v == null || isNaN(v)) return '-'
  return `${Number(v).toFixed(digits)}%`
}

export function fmtBytes(gb) {
  if (gb == null || isNaN(gb)) return '-'
  return gb >= 1024 ? `${(gb / 1024).toFixed(2)} TB` : `${Number(gb).toFixed(1)} GB`
}

// 云厂商原始状态 → 本地化文案，供 Ops/拓扑状态列共用。
// 未收录的状态保持原值，不吞信息。
const ASSET_STATUS_KEY = {
  running: 'common.asset_status.running',
  active: 'common.asset_status.running',
  normal: 'common.asset_status.normal',
  ok: 'common.asset_status.ok',
  online: 'common.asset_status.online',
  offline: 'common.asset_status.offline',
  stopped: 'common.asset_status.stopped',
  suspended: 'common.asset_status.suspended',
  disconnected: 'common.asset_status.disconnected',
  not_responding: 'common.asset_status.not_responding',
  notresponding: 'common.asset_status.not_responding',
  deleted: 'common.asset_status.deleted',
  creating: 'common.asset_status.creating',
  pending: 'common.asset_status.pending',
  rebooting: 'common.asset_status.rebooting',
  deleting: 'common.asset_status.deleting',
  configuring: 'common.asset_status.configuring',
  initializing: 'common.asset_status.initializing',
  processing: 'common.asset_status.processing',
  locked: 'common.asset_status.locked',
  inactive: 'common.asset_status.inactive',
  isolated: 'common.asset_status.isolated',
  recycled: 'common.asset_status.recycled',
  recycling: 'common.asset_status.recycling',
  rejected: 'common.asset_status.rejected',
  closing: 'common.asset_status.closing',
  destroying: 'common.asset_status.destroying',
  destroyed: 'common.asset_status.destroyed',
  applying: 'common.asset_status.applying',
  init: 'common.asset_status.init',
  initing: 'common.asset_status.initing',
  'limited run': 'common.asset_status.limited_run',
  isolating: 'common.asset_status.isolating',
  disisolating: 'common.asset_status.disisolating',
  'job running': 'common.asset_status.job_running',
  migrating: 'common.asset_status.migrating',
  expanding: 'common.asset_status.expanding',
  waitswitch: 'common.asset_status.waitswitch',
  switching: 'common.asset_status.switching',
  readonly: 'common.asset_status.readonly',
  restarting: 'common.asset_status.restarting',
  'network changing': 'common.asset_status.network_changing',
  upgrading: 'common.asset_status.upgrading',
  'audit-switching': 'common.asset_status.audit_switching',
  'primary-switching': 'common.asset_status.primary_switching',
  offlining: 'common.asset_status.offlining',
  'deployment changing': 'common.asset_status.deployment_changing',
  cloning: 'common.asset_status.cloning',
  'parameter modifying': 'common.asset_status.parameter_modifying',
  'log-switching': 'common.asset_status.log_switching',
  restoring: 'common.asset_status.restoring',
  expired: 'common.asset_status.expired',
  issued: 'common.asset_status.issued',
}

export function assetStatusLabel(status) {
  if (!status) return '-'
  const key = ASSET_STATUS_KEY[String(status).toLowerCase()]
  return key ? t(key) : String(status)
}

// 告警/巡检/画像共用的级别文案（此前各处标签直接渲染 critical/warning/info 英文原值）
const LEVEL_KEY = {
  critical: 'common.level.critical',
  warning: 'common.level.warning',
  info: 'common.level.info',
}

export function levelLabel(level) {
  if (!level) return '-'
  return t(LEVEL_KEY[String(level).toLowerCase()] || 'common.level.info')
}
