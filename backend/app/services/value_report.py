"""价值量化账单：每月汇总平台实际完成的运维动作。

产品的留存与销售生命线：降噪比、真问题、自动诊断、成本发现、巡检覆盖——
全部从已有数据统计（不新增采集面）。口径诚实：只报做过的，不折算"省的钱"。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

logger = logging.getLogger("qianshu.value_report")


def monthly_report(db: Session, year: int, month: int) -> dict:
    from app.models.alert_record import AlertRecord
    from app.models.cost_anomaly import CostAnomaly
    from app.models.diagnosis import DiagnosisSession
    from app.models.in_app_message import InAppMessage
    from app.models.inspection import InspectionRun

    lo = datetime(year, month, 1)
    hi = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)

    def _rng(col):
        return col >= lo, col < hi

    alerts = (db.query(AlertRecord)
              .filter(*_rng(AlertRecord.fired_at)).all())
    firing = sum(1 for a in alerts if a.status == "firing")
    recovered = sum(1 for a in alerts if a.status == "recovered")
    critical = sum(1 for a in alerts if a.level == "critical")

    diags = (db.query(DiagnosisSession)
             .filter(*_rng(DiagnosisSession.created_at)).count())
    anomalies = (db.query(CostAnomaly)
                 .filter(*_rng(CostAnomaly.created_at)).all())
    cost_found = len(anomalies)   # 口径=发现的异动条数（模型无状态字段，不按状态过滤）
    try:
        inspections = (db.query(InspectionRun)
                       .filter(*_rng(InspectionRun.created_at)).count())
    except Exception:
        inspections = 0

    return {
        "period": f"{year}-{month:02d}",
        "alerts": {"total": len(alerts), "critical": critical,
                   "recovered": recovered, "open": firing},
        "auto_diagnosis": diags,
        "cost_findings": cost_found,
        "inspection_runs": inspections,
        "highlights": [
            f"处理告警 {len(alerts)} 条（critical {critical}，已恢复 {recovered}）" if alerts else "本月无告警打扰",
            f"自动诊断 {diags} 次（战时替人排障）" if diags else "",
            f"发现成本异动/风险 {cost_found} 条" if cost_found else "",
            f"巡检执行 {inspections} 轮" if inspections else "",
        ],
        "note": "统计说明=平台真实动作留痕（告警/诊断/异动/巡检），未折算金额——"
                "省钱结论需结合处置结果，不虚报",
    }
