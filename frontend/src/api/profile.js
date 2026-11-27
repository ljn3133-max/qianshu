import http from '@/api/http'

// 行为画像：构成快照/正常基线/漂移事件/人工确认
export const listProfiles = () => http.get('/profiles')
export const profileDetail = (key) => http.get('/profiles/detail', { params: { profile_key: key } })
export const acceptProfile = (key) => http.post('/profiles/accept', null, { params: { profile_key: key } })
// 「知道了」+分级静默：core 静默 1 天；aux 勾选 silence_3d 才静默 3 天
export const knowProfile = (key, silence3d = false) => http.post('/profiles/know', null, { params: { profile_key: key, silence_3d: silence3d } })
export const knowObject = (object, silence3d = false) => http.post('/profiles/objects/know', null, { params: { object, silence_3d: silence3d } })
export const runProfiles = () => http.post('/profiles/run', {}, { timeout: 300000 })

// 对象全景画像：对象×多层×联动
export const listObjects = () => http.get('/profiles/objects')
export const objectChain = (object, days = 7) => http.get('/profiles/objects/chain', { params: { object, days } })
export const runObjects = (backfill_days = 0) => http.post('/profiles/objects/run', {}, { params: { backfill_days }, timeout: 600000 })
