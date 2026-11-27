-- 0.3.0 模块级提醒开关：默认全开 = 升级零行为变化
CREATE TABLE IF NOT EXISTS monitor_module_settings (
    module      VARCHAR(32) PRIMARY KEY,
    enabled     BOOLEAN NOT NULL DEFAULT true,
    overview    BOOLEAN NOT NULL DEFAULT true,
    inapp       BOOLEAN NOT NULL DEFAULT true,
    push        BOOLEAN NOT NULL DEFAULT true,
    updated_at  TIMESTAMP NOT NULL
);
INSERT INTO monitor_module_settings
    (module, enabled, overview, inapp, push, updated_at) VALUES
    ('alert',          true, true, true, true, CURRENT_TIMESTAMP),
    ('host',           true, true, true, true, CURRENT_TIMESTAMP),
    ('cost',           true, true, true, true, CURRENT_TIMESTAMP),
    ('cdn',            true, true, true, true, CURRENT_TIMESTAMP),
    ('profile',        true, true, true, true, CURRENT_TIMESTAMP),
    ('expiry',         true, true, true, true, CURRENT_TIMESTAMP),
    ('inspection',     true, true, true, true, CURRENT_TIMESTAMP),
    ('digest',         true, true, true, true, CURRENT_TIMESTAMP),
    ('synth',          true, true, true, true, CURRENT_TIMESTAMP),
    ('oncall',         true, true, true, true, CURRENT_TIMESTAMP),
    ('object_profile', true, true, true, true, CURRENT_TIMESTAMP),
    ('calibration',    true, true, true, true, CURRENT_TIMESTAMP)
ON CONFLICT (module) DO NOTHING;
