-- 0.3.0 到期忽略支持「N 天后恢复提醒」；NULL=永久忽略（保持现语义）
ALTER TABLE expiry_ignores ADD COLUMN until_at TIMESTAMP;
