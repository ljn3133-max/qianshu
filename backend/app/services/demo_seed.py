"""Demo 沙箱：一键生成演示数据。

造数全走本地库（不碰云、不发通知）；seed/clear 配对，演示完一键清。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

DEMO_TAG = "demo"   # 演示数据标记（clear 只删带标记的）


def seed_demo(db: Session) -> dict:
    from app.models.alert_record import AlertRecord
    from app.models.asset import Asset
    from app.models.cost_anomaly import CostAnomaly
    from app.models.synth import SynthCheck

    counts = {}
    demo_assets = [
        ("ecs", "i-demo-web1", "web-server-1", "Running"),
        ("ecs", "i-demo-web2", "web-server-2", "Running"),
        ("ecs", "i-demo-app1", "app-server-1", "Running"),
        ("rds", "pgm-demo-main", "demo-pgsql", "Running"),
        ("redis", "r-demo-cache", "demo-redis", "Running"),
        ("slb", "lb-demo-gw", "demo-gateway", "active"),
    ]
    for product, iid, name, status in demo_assets:
        if not db.query(Asset).filter(Asset.instance_id == iid).first():
            db.add(Asset(product=product, instance_id=iid, name=name, status=status,
                         region="cn-demo", tier="core", extra=json.dumps({"demo": True}),
                         expire_time=datetime.now() + timedelta(days=120)))
    counts["assets"] = len(demo_assets)

    for i, (level, title) in enumerate([
            ("critical", "【演示】demo-web1 CPU 98%"),
            ("warning", "【演示】demo-pgsql 磁盘 82%"),
            ("warning", "【演示】demo-redis 连接数偏高")]):
        if not db.query(AlertRecord).filter(AlertRecord.title == title).first():
            db.add(AlertRecord(rule_id=0, rule_name=DEMO_TAG, source="metric",
                               target=f"i-demo-{i}", target_name=title[:40],
                               level=level, status="firing", title=title,
                               content="演示数据（seed-demo 生成，clear-demo 可清）",
                               impact_level="medium", impact_desc="演示业务线"))
    counts["alerts"] = 3

    if not db.query(SynthCheck).filter(SynthCheck.name == "【演示】官网拨测").first():
        db.add(SynthCheck(name="【演示】官网拨测", check_type="http",
                          target="https://www.example.com", interval_sec=60))
        counts["synth"] = 1

    db.commit()
    return {"seeded": counts, "message": "演示数据已生成（告警标题带【演示】，可一键清除）"}


def clear_demo(db: Session) -> dict:
    from app.models.alert_record import AlertRecord
    from app.models.asset import Asset
    from app.models.synth import SynthCheck

    n1 = db.query(Asset).filter(Asset.instance_id.like("i-demo-%")
                                | Asset.instance_id.like("pgm-demo-%")
                                | Asset.instance_id.like("r-demo-%")
                                | Asset.instance_id.like("lb-demo-%")).delete(synchronize_session=False)
    n2 = db.query(AlertRecord).filter(AlertRecord.rule_name == DEMO_TAG).delete(synchronize_session=False)
    n3 = db.query(SynthCheck).filter(SynthCheck.name.like("【演示】%")).delete(synchronize_session=False)
    db.commit()
    return {"cleared": {"assets": n1, "alerts": n2, "synth": n3}}
