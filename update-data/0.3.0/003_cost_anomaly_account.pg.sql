-- 0.3.0 成本异动归因账号 + 重复提醒状态
ALTER TABLE cost_anomalies ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cost_anomalies ADD COLUMN last_notified_at TIMESTAMP;
ALTER TABLE cost_anomalies ADD COLUMN notify_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cost_anomalies ADD COLUMN next_notify_at TIMESTAMP;
CREATE INDEX IF NOT EXISTS ix_cost_anomalies_account_id ON cost_anomalies (account_id);
CREATE INDEX IF NOT EXISTS ix_cost_anomalies_next_notify_at ON cost_anomalies (next_notify_at);
