-- 0.3.0 存量成本异动按 target_key（实例 ID）反查资产账号；查不到归 0=全局
UPDATE cost_anomalies
SET account_id = COALESCE(
    (SELECT MIN(a.account_id)
       FROM assets a
      WHERE a.instance_id = cost_anomalies.target_key
        AND a.account_id IS NOT NULL
        AND a.account_id > 0),
    0)
WHERE account_id = 0;
