#!/usr/bin/env python3
"""C4 事件集成主动化隔离 mock 回归：一次性 PostgreSQL + 采集器打桩，
不打真实云 API、不动 qianshu.db、不碰 8810 运行态。

覆盖（5 组断言 + 附）：
  1) 异动落库即刻驱动画像事件采样：_add（全部检出器唯一落库口）触发
     on_anomaly_landed 主动入线 + 钩内即时短时突变判定——**不等 event_tick 扫表**
  2) profile_drift 自反馈闸不自激（底线：钩子短路 skipped=self_feedback，
     且经 _add 插入路径零入线——文本能命中画像也不回灌）
  3) 同 anomaly:{id} 幂等不重复入线（重复驱动 recorded=0；兜底扫表同键不双记）
  4) 画像漂移在异动事件流可见（profile_drift 条目落 cost_anomalies，
     与 Anomalies 页同一张表）
  5) 双向跳转数据结构（API 函数直调返回结构）：Anomalies 关联画像
     （profile_links：漂移状态/构成变化摘要/drift_events/jump）/
     Profiles 关联事件（related_events：id/anomaly_type/linked）
  附) 配置可配：PROFILE_EVENT_SYNC_ENABLED=False 可回滚（钩子+扫表全停）、
     event_tick 节流间隔取 PROFILE_EVENT_SYNC_INTERVAL_S

与 tests/test_profile_c_smoke.py（C1~C3 底线）互补，不改其一行业务语义。
"""
import os
import sys
import json
from datetime import date, datetime, timedelta

DB_PATH = "/tmp/qianshu_c4_mock.db"
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("probe")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
sys.path.insert(0, "/opt/qianshu/backend")

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.core import migrate as mg  # noqa: E402

# 全部模型注册进 metadata 再建表（真实启动由 main.py 逐模块导入达成同样效果）
import importlib  # noqa: E402
import pkgutil  # noqa: E402
import app.models as _m_pkg  # noqa: E402
for _m in pkgutil.iter_modules(_m_pkg.__path__):
    importlib.import_module(f"app.models.{_m.name}")

FAILED = []


def ok(name, cond, detail=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


print("== 0) 自建库 + 迁移幂等 ==")
Base.metadata.create_all(bind=engine)
mg.run_migrations(engine)
mg.run_migrations(engine)   # 幂等

db = SessionLocal()
from app.models.asset import Asset  # noqa: E402
from app.models.behavior_profile import BehaviorProfile  # noqa: E402
from app.models.cost_anomaly import CostAnomaly  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services import profile_engine as pe  # noqa: E402
from app.services import profile_context as pc  # noqa: E402
from app.services.cost_anomaly import _add as anomaly_add  # noqa: E402

# 本组验证事件集成与 drift 生命周期，显式固定为“上下文显著上升”，不访问真实指标源。
pc.for_behavior_profile = lambda db_, p_, now_, latest_: {
    "verdict": pc.DRIFT, "signals": [], "note": "测试上下文：资源显著上升"}

# ---------------- 采集器打桩（不打真实云 API） ----------------
FAKE = {"comp": {"members": [{"name": "aaa.exe", "share_pct": 80.0, "value": 8}], "total": 10},
        "target": "process:i-core001"}


def fake_collect_windowed(db_, p, start, end):
    if p.profile_key != FAKE["target"]:
        return {"members": [], "total": 0}
    return json.loads(json.dumps(FAKE["comp"]))


pe.collect_windowed = fake_collect_windowed
pe._day_covered = lambda entry, now: True   # 生命周期驱动绕开时段覆盖门槛

# ---------------- 种子：一台 core 机器 + learned 画像 ----------------
db.add(Asset(product="ecs", instance_id="i-core001", name="web1", status="Running", tier="core"))
p = BehaviorProfile(
    profile_key="process:i-core001", kind="process", subject="i-core001",
    label="进程构成 · i-core001", learned=True, status="normal",
    baseline=json.dumps({"members": [{"name": "aaa.exe", "freq": 1.0, "share_avg": 60.0,
                                      "share_min": 55.0, "share_max": 65.0}],
                         "top1": "aaa.exe", "top1_freq": 1.0, "n": 5}))
samples = []
for i in range(5, 0, -1):
    d = (date.today() - timedelta(days=i)).isoformat()
    samples.append({"date": d, "sample_kind": "daily",
                    "members": [{"name": "aaa.exe", "share_pct": 60.0, "value": 6.0}], "total": 10.0,
                    "snapshots": [{"ts": f"{d} 12:00:00",
                                   "members": [{"name": "aaa.exe", "share_pct": 60.0, "value": 6.0}],
                                   "total": 10.0}]})
p.samples = json.dumps(samples, ensure_ascii=False)
p.latest = json.dumps({"members": [{"name": "aaa.exe", "share_pct": 60.0, "value": 6.0}], "total": 10.0})
db.add(p)
db.commit()


def ev_samples():
    row = db.query(BehaviorProfile).filter(
        BehaviorProfile.profile_key == "process:i-core001").first()
    return json.loads(row.event_samples or "[]")


# ---------------- 1) 异动落库即刻驱动画像（主动入线，不等 event_tick） ----------------
print("== 1) 异动落库 -> 画像事件采样主动入线（不等扫表）==")
a1 = anomaly_add(db, date.today(), "host_down", "i-core001", "web1",
                 "服务器非运行状态: web1（Stopped）", "x", level="critical")   # host_watch 口径
db.commit()
es = ev_samples()
ok("host_down 落库即刻入线（无 event_tick 参与）：事件样本 source=anomaly、ref=anomaly:{id}",
   len(es) == 1 and es[0].get("source") == "anomaly" and es[0].get("ref_id") == f"anomaly:{a1.id}",
   str(es))

FAKE["comp"] = {"members": [{"name": "bbb.exe", "share_pct": 80.0, "value": 8}], "total": 10}
a2 = anomaly_add(db, date.today(), "growth_accel", "ecs_disk:i-core001", "web1",
                 "磁盘增速突变: web1 +5%/天", "x")   # baseline_job 口径
db.commit()
fast_ev = (db.query(CostAnomaly)
           .filter(CostAnomaly.type == "profile_drift",
                   CostAnomaly.target_key == "process:i-core001#fast",
                   CostAnomaly.ack_status == "open").count())
ok("钩内即时判定：两笔异动构成 top1 换人 -> 短时突变 #fast 事件即刻落异动流",
   fast_ev >= 1 and len(ev_samples()) == 2, f"fast={fast_ev} es={len(ev_samples())}")

# 多检出器同口径（cost / object_profile 经同一落库口）
FAKE["comp"] = {"members": [{"name": "bbb.exe", "share_pct": 80.0, "value": 8}], "total": 10}
anomaly_add(db, date.today(), "payg_spike", "i-core001", "web1", "按量费用突增: web1", "x")
anomaly_add(db, date.today(), "chain_drift", "ecs:i-core001", "web1", "架构异常: web1", "x")
db.commit()
ok("cost_anomaly / object_profile 检出器同口径入线（_add 单点收口）",
   len(ev_samples()) == 4, str([s["ref_id"] for s in ev_samples()]))

# ---------------- 2) 自反馈闸（底线：profile_drift 不回灌，不自激） ----------------
print("== 2) 自反馈闸：profile_drift 自身通知不回灌 ==")
before_n = len(ev_samples())
# 2a) 直调钩子：短路
a_d = CostAnomaly(detect_date=date.today(), type="profile_drift", target_key="i-core001-selfcheck",
                  target_name="进程构成 · i-core001", title="行为画像漂移: 自检用", content="x",
                  level="warning")
db.add(a_d)
db.flush()
r = pe.on_anomaly_landed(db, a_d)
db.commit()
# 2b) 经 _add 插入路径：文本含 i-core001（能命中画像）也零入线
a3 = anomaly_add(db, date.today(), "profile_drift", "i-core001-selfcheck2",
                 "进程构成 · i-core001", "行为画像漂移: 自检2", "x")
db.commit()
ok("钩子短路 skipped=self_feedback 且 _add 路径零入线（防 漂移通知->样本->#fast 自激）",
   r.get("skipped") == "self_feedback" and a3 is not None and len(ev_samples()) == before_n,
   f"{r} before={before_n} after={len(ev_samples())}")

# ---------------- 3) 幂等：同 anomaly:{id} 不重复入线 ----------------
print("== 3) 同 anomaly:{id} 幂等 ==")
r = pe.on_anomaly_landed(db, a2)   # 同一笔异动再次驱动
db.commit()
ok("重复驱动 recorded=0（(source, ref_id) 去重）", r.get("recorded") == 0, str(r))
es_before = len(ev_samples())
rt = pe.sync_event_samples(db)     # 兜底扫表（alert_records/change_events 挂点同函数）
db.commit()
ok("兜底扫表与钩子同键不双记：扫表补录 0 条",
   rt.get("recorded") == 0 and len(ev_samples()) == es_before, f"{rt} {es_before}->{len(ev_samples())}")

# ---------------- 4) 画像漂移 -> 异动事件流可见 ----------------
print("== 4) 画像漂移主动写异动事件流 ==")
FAKE["comp"] = {"members": [{"name": "ccc.exe", "share_pct": 85.0, "value": 8}], "total": 10}
stats = pe.run_profiles(db)
db.commit()
drift_rows = (db.query(CostAnomaly)
              .filter(CostAnomaly.type == "profile_drift",
                      CostAnomaly.target_key == "process:i-core001",
                      CostAnomaly.ack_status.in_(("open", "acknowledged")))
              .order_by(CostAnomaly.id.desc()).all())
ok("漂移检出即刻建 profile_drift 条目（Anomalies 页同表可见）",
   stats.get("drifted", 0) >= 1 and len(drift_rows) >= 1
   and (drift_rows[0].title or "").startswith("行为画像漂移"),
   f"{stats} rows={[(e.target_key, e.title) for e in drift_rows]}")

# ---------------- 5) 双向跳转数据结构（API 函数直调返回结构） ----------------
print("== 5) 双向关联 API 返回结构（Anomalies 关联画像 / Profiles 关联事件）==")
from app.api.v1.cost import anomaly_list  # noqa: E402
from app.api.v1.profiles import profile_detail  # noqa: E402

data = anomaly_list(start="", end="", type="", domain="", level="", status="",
                    anomaly_id=a2.id, account_id=0, page=1, page_size=20, db=db, _user=None)
items = data.get("items") or []
pl = (items[0].get("profile_links") if items else []) or []
link = pl[0] if pl else {}
ok("Anomalies 关联画像：profile_links 带 漂移状态/构成变化摘要/±24h 漂移事件/jump",
   len(items) == 1 and link.get("profile_key") == "process:i-core001"
   and {"status", "status_label", "summary", "drift_events", "jump"} <= set(link)
   and link.get("jump") == {"view": "profiles", "profile_key": "process:i-core001"}
   and bool(link.get("summary")) and len(link.get("drift_events") or []) >= 1,
   f"links={pl}")

det = profile_detail(profile_key="process:i-core001", db=db, _user=None)
tl = det.get("related_events") or []
anom = [x for x in tl if x.get("kind") == "anomaly"]
ok("Profiles 关联事件：条目带 id/anomaly_type/linked（可点跳异动页；±24h 关联标注）",
   bool(anom) and all({"id", "anomaly_type", "linked"} <= set(x) for x in tl)
   and any(x.get("anomaly_type") == "host_down" for x in anom)
   and any(x.get("linked") for x in tl),
   f"tl={[(x['kind'], x.get('anomaly_type'), x.get('linked')) for x in tl][:6]}")
ok("画像侧漂移事件带 id（漂移事件可点回异动页定位）",
   bool(det.get("events")) and all(e.get("id") for e in det["events"]),
   str(det.get("events")))

# ---------------- 附) 配置可配：开关可回滚 + 节流间隔可配 ----------------
print("== 附) PROFILE_EVENT_SYNC_ENABLED / INTERVAL_S ==")
settings.PROFILE_EVENT_SYNC_ENABLED = False
n0 = len(ev_samples())
a4 = anomaly_add(db, date.today(), "pack_spike", "i-core001-pack", "web1 包", "资源包消耗突增", "x")
db.commit()
rt = pe.event_tick(db, force=True)
ok("开关可回滚：False 时钩子与兜底扫表全停（零入线）",
   len(ev_samples()) == n0 and rt.get("skipped") == "disabled", f"{rt} {n0}->{len(ev_samples())}")
settings.PROFILE_EVENT_SYNC_ENABLED = True

settings.PROFILE_EVENT_SYNC_INTERVAL_S = 3600
rt1 = pe.event_tick(db, force=True)   # 开启后兜底扫表补齐关闭期间漏掉的 a4
db.commit()
rt2 = pe.event_tick(db)
ok("兜底扫表补齐关闭期缺口 + 节流间隔取配置（3600s 内二次调用 throttled）",
   rt1.get("recorded") == 1 and rt2.get("skipped") == "throttled", f"{rt1} {rt2}")
settings.PROFILE_EVENT_SYNC_INTERVAL_S = 120

db.close()
print()
if FAILED:
    print(f"C4 冒烟结果: {len(FAILED)} 项失败 -> {FAILED}")
    sys.exit(1)
print("C4 冒烟结果: 全部通过")
