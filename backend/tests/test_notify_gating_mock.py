#!/usr/bin/env python3
"""分级提醒通知门禁回归测试。

核心场景：vCenter 不提醒到总览页、不发站内信。
验证：模块开关 × 账号去向 = 最终去向；告警/异动仍落库，只是不打扰；
低优先级只通知一次；总览先过滤再截断。

全部使用一次性 PostgreSQL 库 + monkeypatch 发送函数，绝不触碰真实渠道。

运行：cd /opt/qianshu/backend && python3 tests/test_notify_gating_mock.py
"""
import importlib
import os
import pkgutil
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models as _models_pkg  # noqa: E402
# app/models/__init__.py 只导入了部分模型；create_all 前必须让每个模块都注册到 Base
for _m in pkgutil.iter_modules(_models_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

from app.core.db import Base  # noqa: E402
from app.models.alert_record import AlertRecord  # noqa: E402
from app.models.cloud_account import CloudAccount  # noqa: E402
from app.models.cost_anomaly import CostAnomaly  # noqa: E402
from app.models.in_app_message import InAppMessage  # noqa: E402
from app.models.monitor_policy import MonitorModuleSetting  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import monitor_policy as mp  # noqa: E402
from app.services import notify as nt  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("  ✓ " if cond else "  ✗ ") + name +
          (f"  [{detail}]" if detail and not cond else ""))


def make_db():
    path = os.path.join(tempfile.mkdtemp(prefix="qs-gate-"), "t.db")
    engine = create_engine(test_database_url("notifygate"))
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    both = Note()          # 毒化：任何真实发送都置位
    both.hits = 0

    def fake_send(*_a, **_k):
        both.hits += 1

    nt.send_to_channel = fake_send
    mp.ensure_defaults(db)
    db.add(User(username="admin", password_hash="x", role="admin", is_active=True))
    db.commit()
    return db, both


class Note:
    hits = 0


def account(db, name, **kw):
    acc = CloudAccount(name=name, vendor="aliyun", ak_cipher="x", secret_cipher="y",
                       region="cn-hangzhou", extra_json="{}", enabled=True,
                       status="ok", **kw)
    db.add(acc)
    db.commit()
    return acc.id


def alert(db, account_id, module="alert", notify_count=0, target="i-x"):
    r = AlertRecord(account_id=account_id, module=module, rule_id=1, rule_name="CPU",
                    source="metric", target=target, target_name=target,
                    level="critical", title="t", content="c", status="firing",
                    notify_count=notify_count)
    db.add(r)
    db.commit()
    return r


def set_module(db, module, **kw):
    row = db.get(MonitorModuleSetting, module)
    for k, v in kw.items():
        setattr(row, k, v)
    db.commit()


def msg_count(db):
    return db.query(InAppMessage).count()


def main():
    db, note = make_db()
    vid = account(db, "vcenter", priority="low",
                  notify_overview=False, notify_inapp=False, notify_push=False)
    aid = account(db, "aliyun-prod", priority="high")

    # ---- 1) vCenter 三去向全关：0 站内信、0 外推 ----
    db.query(InAppMessage).delete()
    db.commit()
    n = nt.notify_inapp(db, "t", "c", audience="active", module="alert",
                        account_id=vid)
    db.commit()
    check("vCenter 关站内信后不写消息", n == 0 and msg_count(db) == 0, str(n))
    r = nt.notify_all_channels(db, "t", "c", level="critical", module="alert",
                               account_id=vid)
    check("vCenter 关外推后不发渠道", note.hits == 0 and "push" in r["blocked"], str(r))

    # ---- 2) 同模块高优先级账号不受影响 ----
    n2 = nt.notify_inapp(db, "t", "c", audience="active", module="alert",
                         account_id=aid)
    db.commit()
    check("同模块其他账号照常收信", n2 == 1 and msg_count(db) == 1, str(n2))

    # ---- 3) 模块开关：关站内信只影响站内信 ----
    set_module(db, "alert", inapp=False)
    n3 = nt.notify_inapp(db, "t", "c", audience="active", module="alert",
                         account_id=aid)
    db.commit()
    check("模块关站内信 → 不写消息", n3 == 0 and msg_count(db) == 1, str(n3))
    check("模块关站内信仍允许外推", mp.can_push(db, "alert", aid))
    set_module(db, "alert", inapp=True)

    # ---- 4) 模块不支持的去向绝不误发 ----
    n4 = nt.notify_inapp(db, "t", "c", audience="active", module="expiry",
                         account_id=aid)
    db.commit()
    check("到期提醒不支持站内信 → 0 条", n4 == 0 and msg_count(db) == 1, str(n4))

    # ---- 5) 平台系统消息（module=None）不受业务开关控制 ----
    set_module(db, "alert", enabled=False)
    n5 = nt.notify_inapp(db, "系统消息", "c", audience="first_admin")
    db.commit()
    check("平台系统消息不受监控开关控制", n5 == 1 and msg_count(db) == 2, str(n5))
    set_module(db, "alert", enabled=True)

    # ---- 6) 成本异动按 (模块, 账号) 分组门禁 ----
    db.query(InAppMessage).delete()
    db.commit()
    note.hits = 0
    low_anom = CostAnomaly(detect_date=date.today(), last_seen=date.today(),
                           type="payg_spike", level="critical", target_key="i-low",
                           target_name="低优机器", title="按量突增", content="c",
                           value=100, baseline=10, ratio=10, account_id=vid)
    high_anom = CostAnomaly(detect_date=date.today(), last_seen=date.today(),
                            type="payg_spike", level="critical", target_key="i-hi",
                            target_name="核心机器", title="按量突增2", content="c",
                            value=200, baseline=10, ratio=20, account_id=aid)
    db.add_all([low_anom, high_anom])
    db.commit()
    from app.services.cost_anomaly import _notify_anomalies
    _notify_anomalies(db, [low_anom, high_anom])
    db.commit()
    bodies = [m.content_md for m in db.query(InAppMessage).all()]
    check("异动只给放行账号发一条", len(bodies) == 1 and "按量突增2" in bodies[0],
          str(bodies))
    check("低优账号异动计数不增加", (low_anom.notify_count or 0) == 0,
          str(low_anom.notify_count))
    check("放行账号异动计数+1", (high_anom.notify_count or 0) == 1,
          str(high_anom.notify_count))

    # ---- 7) 总览：先按去向/确认过滤，再截断 ----
    db.query(AlertRecord).delete()
    db.query(InAppMessage).delete()
    db.commit()
    for i in range(8):
        alert(db, vid, target=f"i-vc-{i}")          # 被静音，不该占位
    alert(db, aid, target="i-hi-1")
    acked = alert(db, aid, target="i-hi-2")
    acked.ack_status = "ignored"
    db.commit()
    from app.services.dashboard import summary
    s = summary(db)
    titles = [t["title"] for t in s["todos"]]
    check("总览不含被静音账号的告警", not any("i-vc-" in (t.get("detail") or "")
                                              and "实例" in (t.get("detail") or "")
                                              for t in s["todos"]), str(titles))
    check("总览不含已忽略告警", len([t for t in s["todos"] if t["cat"] == "告警"]) == 1,
          str(titles))
    check("总览待办不超过 8 条", len(s["todos"]) <= 8, str(len(s["todos"])))

    # ---- 8) 已通知一次不影响总览可见性（只控制主动打扰） ----
    db.query(AlertRecord).delete()
    db.query(InAppMessage).delete()
    db.commit()
    low_acc = db.get(CloudAccount, vid)
    low_acc.notify_overview = True
    db.commit()
    alert(db, vid, notify_count=1, target="i-low-seen")
    db.commit()
    s2 = summary(db)
    check("已通知一次的低优先级告警仍可在总览看到",
          len([t for t in s2["todos"] if t["cat"] == "告警"]) == 1,
          str([t["title"] for t in s2["todos"]]))

    # ---- 9) 出站全关时 notify_all_channels 不写也不发 ----
    note.hits = 0
    db.query(InAppMessage).delete()
    db.commit()
    low_acc.notify_inapp = False
    low_acc.notify_push = False
    db.commit()
    r9 = nt.notify_all_channels(db, "t", "c", level="critical", module="alert",
                                account_id=vid)
    check("三去向全关：无站内信/无外推",
          r9["sent"] == [] and note.hits == 0 and msg_count(db) == 0, str(r9))

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    if bad:
        print("失败：" + "; ".join(bad))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
