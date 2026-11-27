-- 0.3.0 数据源监控策略：优先级、提醒去向、重复提醒、采集游标
ALTER TABLE cloud_accounts ADD COLUMN priority VARCHAR(8) NOT NULL DEFAULT 'high';
ALTER TABLE cloud_accounts ADD COLUMN notify_overview BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE cloud_accounts ADD COLUMN notify_inapp BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE cloud_accounts ADD COLUMN notify_push BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE cloud_accounts ADD COLUMN repeat_days INTEGER;
ALTER TABLE cloud_accounts ADD COLUMN last_metrics_at TIMESTAMP;
ALTER TABLE cloud_accounts ADD COLUMN last_alert_eval_at TIMESTAMP;
ALTER TABLE cloud_accounts ADD COLUMN last_host_check_at TIMESTAMP;
ALTER TABLE cloud_accounts ADD COLUMN last_cost_at TIMESTAMP;
