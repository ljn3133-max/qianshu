-- 0.3.0 告警归属模块 + 确认/忽略 + 重复提醒状态
ALTER TABLE alert_records ADD COLUMN module VARCHAR(32) NOT NULL DEFAULT 'alert';
ALTER TABLE alert_records ADD COLUMN ack_status VARCHAR(16) NOT NULL DEFAULT 'none';
ALTER TABLE alert_records ADD COLUMN ack_note TEXT NOT NULL DEFAULT '';
ALTER TABLE alert_records ADD COLUMN ack_by VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE alert_records ADD COLUMN ack_by_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alert_records ADD COLUMN ack_at TIMESTAMP;
ALTER TABLE alert_records ADD COLUMN last_notified_at TIMESTAMP;
ALTER TABLE alert_records ADD COLUMN notify_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE alert_records ADD COLUMN next_notify_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_alert_records_next_notify_at ON alert_records (next_notify_at);
