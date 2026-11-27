-- 0.3.1 拓扑扫描：保存当次完整架构快照，供历史查看与诊断取证
ALTER TABLE topology_scans ADD COLUMN arch_json TEXT DEFAULT '';
