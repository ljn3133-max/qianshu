-- 0.3.1 邮件通知渠道：SMTP 配置 + 收件人（密码为密文列，只进不出）
ALTER TABLE alert_channels ADD COLUMN smtp_host VARCHAR(255) DEFAULT '';
ALTER TABLE alert_channels ADD COLUMN smtp_port INTEGER DEFAULT 0;
ALTER TABLE alert_channels ADD COLUMN smtp_user VARCHAR(255) DEFAULT '';
ALTER TABLE alert_channels ADD COLUMN smtp_password_cipher TEXT DEFAULT '';
ALTER TABLE alert_channels ADD COLUMN smtp_starttls BOOLEAN DEFAULT false;
ALTER TABLE alert_channels ADD COLUMN email_from VARCHAR(255) DEFAULT '';
ALTER TABLE alert_channels ADD COLUMN email_to TEXT DEFAULT '[]';
