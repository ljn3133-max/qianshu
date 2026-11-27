#!/usr/bin/env python3
"""证书单一事实冒烟：cert 层统一解析全链路验证。

运行：cd /opt/qianshu/backend && python3 tests/test_cert_fact_mock.py
隔离性：DATABASE_URL/MASTER_KEY 在 import app.* 之前改写为临时值——
不碰真实 qianshu.db、不用真实主密钥、不调任何云 API、不起后台任务、不发通知。

验证项：
  A 归一：序列号归一 / 同证书归一键（序列号优先，域名+签发者兜底）/ 覆盖与 same_cert
  B 实测优先：同证书 TLS 拨测实测（tls_probe）日期压过 CAS 订单（cas_order）
  C 订单兜底：无实测用订单；实测单边只留实测
  D 来源标注：facts/evidence/资产行/到期账全部带 source: tls_probe|cas_order
  E 差异提示：两来源到期日差 >7 天 → source_conflict+并列日期；≤7 天不提示；
    序列号不同=另一张证（serial_mismatch）：域名事实取实测、订单行不冒领
  F 配置消费：synth_checks.cert_warn_days 进到期分级预警档（默认 30=原 30/7 口径不变；
    ≤7 天固定紧急档）——死配置接线语义
  G 回归：expiry（分级/去重/非证书行 30/7 不变）、domain_registry（回填/清单分级）、
    synth（detail 文本解析与采集本体格式对齐 + run_check 落库链路）
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

# ---- 隔离环境必须先于 app 导入设置（env 覆盖 .env）----
_TMPDIR = tempfile.mkdtemp(prefix="qianshu-cert-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import reset_pk_sequences, test_database_url
except ImportError:
    from pg_test_env import reset_pk_sequences, test_database_url

os.environ["DATABASE_URL"] = test_database_url("cert")
os.environ["MASTER_KEY"] = "ce" * 32          # 64 hex 测试主密钥
for _k in ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
           "TENCENT_CLOUD_SECRET_ID", "TENCENT_CLOUD_SECRET_KEY"):
    os.environ.pop(_k, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session  # noqa: E402

from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402,F401  导入全量模型（create_all 同口径）
from app.models.asset import Asset  # noqa: E402
from app.models.synth import SynthCheck, SynthResult  # noqa: E402
from app.services import cert_fact as cf  # noqa: E402
from app.services import domain_registry as dr  # noqa: E402
from app.services import expiry as ex  # noqa: E402

PASS, FAIL = [], []
NOW = datetime.now()
DOMAINS = {"www.a-demo.com", "www.b-demo.com", "only.c-demo.com",
           "www.d-demo.com", "www.e-demo.com", "dup.y-demo.com"}


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  [{'ok' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def day(s: str) -> str:
    return (s or "")[:10]


def seed(db: Session) -> None:
    """六个证书场景 + 一条非证书资产（订单 extra 带 serial/root_brand 照 collector/domain.py 落点）。"""
    # A 实测优先+差异>7 天+配置消费：同证书（序列号 AAAA1111），订单 100 天/实测 20 天
    db.add(Asset(product="ssl_cert", instance_id="cas-a", name="www.a-demo.com", status="ISSUED",
                 expire_time=NOW + timedelta(days=100),
                 extra=json.dumps({"root_brand": "DigiCert", "serial": "AAAA1111"})))
    db.add(SynthCheck(id=1, name="a", check_type="http", target="https://www.a-demo.com",
                      cert_warn_days=14))
    db.add(SynthResult(check_id=1, checked_at=NOW, ok=True, latency_ms=5, status_code=200,
                       detail="HTTP 200｜证书 20 天后到期｜序列号 AAAA1111"))
    # B 订单兜底：无实测
    db.add(Asset(product="ssl_cert", instance_id="cas-b", name="www.b-demo.com", status="ISSUED",
                 expire_time=NOW + timedelta(days=100),
                 extra=json.dumps({"root_brand": "GeoTrust", "serial": "BBBB2222"})))
    # B2 实测单边：无订单，任务级 cert_warn_days=60
    db.add(SynthCheck(id=2, name="c", check_type="http", target="https://only.c-demo.com",
                      cert_warn_days=60))
    db.add(SynthResult(check_id=2, checked_at=NOW, ok=True, latency_ms=5, status_code=200,
                       detail="HTTP 200｜证书 45 天后到期"))
    # C 差异 ≤7 天：不提示来源不一致
    db.add(Asset(product="ssl_cert", instance_id="cas-d", name="www.d-demo.com", status="ISSUED",
                 expire_time=NOW + timedelta(days=30),
                 extra=json.dumps({"root_brand": "DigiCert", "serial": "DDDD4444"})))
    db.add(SynthCheck(id=3, name="d", check_type="http", target="https://www.d-demo.com",
                      cert_warn_days=30))
    db.add(SynthResult(check_id=3, checked_at=NOW, ok=True, latency_ms=5, status_code=200,
                       detail="HTTP 200｜证书 27 天后到期｜序列号 DDDD4444"))
    # E 序列号不匹配：实测出示另一张证（FFFF6666 ≠ 订单 EEEE5555）
    db.add(Asset(product="ssl_cert", instance_id="cas-e", name="www.e-demo.com", status="EXPIRED",
                 expire_time=NOW + timedelta(days=100),
                 extra=json.dumps({"root_brand": "DigiCert", "serial": "EEEE5555"})))
    db.add(SynthCheck(id=4, name="e", check_type="http", target="https://www.e-demo.com",
                      cert_warn_days=30))
    db.add(SynthResult(check_id=4, checked_at=NOW, ok=True, latency_ms=5, status_code=200,
                       detail="HTTP 200｜证书 10 天后到期｜序列号 FFFF6666"))
    # G 回归：同名两单只看最新一张（旧证沉底）+ 非证书资产 30/7 不变
    db.add(Asset(product="ssl_cert", instance_id="cas-dup-old", name="dup.y-demo.com", status="EXPIRED",
                 expire_time=NOW + timedelta(days=5),
                 extra=json.dumps({"root_brand": "Old"})))
    db.add(Asset(product="ssl_cert", instance_id="cas-dup-new", name="dup.y-demo.com", status="ISSUED",
                 expire_time=NOW + timedelta(days=50),
                 extra=json.dumps({"root_brand": "New"})))
    db.add(Asset(product="ecs", instance_id="i-ecs1", name="web-1", status="Running",
                 expire_time=NOW + timedelta(days=10)))
    db.commit()


# ================= A. 归一 =================
def test_normalize() -> None:
    print("A. 归一（序列号 / 同证书判定）")
    check("A1 序列号归一（去 : - 空白、大写）", cf.norm_serial("ab:cd-ef 12") == "ABCDEF12",
          cf.norm_serial("ab:cd-ef 12"))
    check("A2 序列号空/非法 → ''", cf.norm_serial("") == "" and cf.norm_serial("xy") == "")
    check("A3 归一键：序列号优先",
          cf.cert_identity("aa:11", "x.com", "DigiCert") == "serial:AA11",
          cf.cert_identity("aa:11", "x.com", "DigiCert"))
    check("A3b 归一键：无序列号=域名+签发者（小写去尾点）",
          cf.cert_identity("", "X.com.", "digicert") == cf.cert_identity("", "x.com", "DigiCert")
          and cf.cert_identity("", "x.com", "DigiCert") != cf.cert_identity("", "x.com", "GeoTrust"))
    check("A4 覆盖：一级通配/不多级/不盖裸域",
          cf.cert_covers("*.demo.com", "a.demo.com") and not cf.cert_covers("*.demo.com", "demo.com")
          and not cf.cert_covers("*.demo.com", "a.b.demo.com"))
    p = cf.CertEvidence(source="tls_probe", ref="", cert_name="www.a-demo.com", domain="www.a-demo.com",
                        serial="AAAA1111")
    o_same = cf.CertEvidence(source="cas_order", ref="", cert_name="www.a-demo.com", domain="www.a-demo.com",
                             serial="aa-aa-11-11")
    o_other = cf.CertEvidence(source="cas_order", ref="", cert_name="www.a-demo.com", domain="www.a-demo.com",
                              serial="EEEE5555")
    o_wild = cf.CertEvidence(source="cas_order", ref="", cert_name="*.a-demo.com", domain="")
    check("A5 same_cert：序列号归一后相等", cf.same_cert(p, o_same))
    check("A6 same_cert：序列号不同=不同证书", not cf.same_cert(p, o_other))
    check("A7 same_cert：缺序列号按域名覆盖（通配符）", cf.same_cert(p, o_wild))


# ================= B/C. 实测优先 / 订单兜底 =================
def test_resolution(db_rows: dict) -> None:
    print("B/C. 实测优先 / 订单兜底")
    facts = db_rows["domain_facts"]
    fa = facts["www.a-demo.com"]
    check("B1 同证书实测优先（tls_probe 压过 cas_order）",
          fa.source == "tls_probe"
          and abs((fa.not_after.date() - (NOW.date() + timedelta(days=20))).days) <= 1,
          f"{fa.source} {fa.not_after}")
    fb = facts["www.b-demo.com"]
    check("C1 无实测 → 订单兜底（cas_order）",
          fb.source == "cas_order" and fb.order is not None and fb.probe is None
          and abs((fb.not_after.date() - (NOW + timedelta(days=100)).date()).days) <= 1,
          f"{fb.source} {fb.not_after}")
    fc = facts["only.c-demo.com"]
    check("C2 实测单边（无订单）→ 只留实测", fc.source == "tls_probe" and fc.order is None,
          f"{fc.source} order={fc.order}")
    fd = facts["www.d-demo.com"]
    check("C3 两来源同证书合并（证据两条都在）",
          fd.source == "tls_probe" and len(fd.evidence) == 2, f"{fd.source} ev={len(fd.evidence)}")


# ================= D. 来源标注 =================
def test_source_labels(db_rows: dict) -> None:
    print("D. 来源标注（tls_probe / cas_order）")
    facts = db_rows["domain_facts"]
    srcs = {e.source for e in facts["www.a-demo.com"].evidence}
    check("D1 evidence 两来源都标注", srcs == {"tls_probe", "cas_order"}, str(srcs))
    check("D2 来源展示名", cf.SOURCE_LABELS["tls_probe"] == "TLS 拨测实测"
          and cf.SOURCE_LABELS["cas_order"] == "CAS 证书订单")
    with SessionLocal() as db:
        idx = dr.cert_index(db, {"www.a-demo.com", "www.b-demo.com"})
    check("D3 cert_index facts.source 新编码",
          idx["www.a-demo.com"]["facts"]["source"] == "tls_probe"
          and idx["www.b-demo.com"]["facts"]["source"] == "cas_order",
          str({k: v["facts"].get("source") for k, v in idx.items()}))
    item_b = db_rows["expiry_by_id"]["cas-b"]
    check("D4 到期账证书行带 source/label",
          item_b.get("cert_source") == "cas_order" and item_b.get("cert_source_label") == "CAS 证书订单",
          str(item_b.get("cert_source")))
    item_a = db_rows["expiry_by_id"]["cas-a"]
    check("D5 到期账实测行带 source", item_a.get("cert_source") == "tls_probe", str(item_a.get("cert_source")))


# ================= E. 差异提示 =================
def test_conflict(db_rows: dict) -> None:
    print("E. 差异提示（>7 天并列提示 / ≤7 静默 / 序列号不同）")
    facts = db_rows["domain_facts"]
    fa = facts["www.a-demo.com"]
    check("E1 差 80 天 → source_conflict + diff_days",
          fa.source_conflict and fa.diff_days == 80, f"{fa.source_conflict} {fa.diff_days}")
    check("E2 并列日期在场（不静默取一）",
          fa.probe is not None and fa.order is not None
          and fa.probe.not_after != fa.order.not_after)
    d = fa.as_dict()
    check("E3 详情序列化带 probe_not_after/order_not_after",
          d["probe_not_after"] and d["order_not_after"] and d["diff_days"] == 80
          and d["source_conflict"] is True,
          str({k: d.get(k) for k in ("probe_not_after", "order_not_after", "diff_days")}))
    fd = facts["www.d-demo.com"]
    check("E4 差 3 天 → 不提示", (not fd.source_conflict) and fd.diff_days == 3,
          f"{fd.source_conflict} {fd.diff_days}")
    fe = facts["www.e-demo.com"]
    check("E5 序列号不同 → serial_mismatch；域名事实仍取实测",
          fe.serial_mismatch and fe.source == "tls_probe" and fe.source_conflict,
          f"{fe.serial_mismatch} {fe.source}")
    asset_facts = db_rows["asset_facts"]
    fe_row = asset_facts["cas-e"]
    check("E6 订单行不冒领另一张证的实测日期（保留订单 + 标注）",
          fe_row.source == "cas_order" and fe_row.serial_mismatch and fe_row.source_conflict
          and day(fe_row.not_after.isoformat()) == day((NOW + timedelta(days=100)).isoformat()),
          f"{fe_row.source} {fe_row.not_after}")
    item_e = db_rows["expiry_by_id"]["cas-e"]
    check("E7 到期账订单行同口径（订单日期+不一致标注）",
          item_e["cert_source"] == "cas_order" and item_e["serial_mismatch"]
          and item_e["source_conflict"]
          and day(item_e["expire_time"]) == day((NOW + timedelta(days=100)).isoformat()),
          str({k: item_e.get(k) for k in ("cert_source", "serial_mismatch", "expire_time")}))
    item_a = db_rows["expiry_by_id"]["cas-a"]
    check("E8 到期账实测优先且差异标注",
          item_a["source_conflict"] and item_a["diff_days"] == 80
          and day(item_a["expire_time"]) == day((NOW + timedelta(days=20)).isoformat())
          and day(item_a["order_expire_time"]) == day((NOW + timedelta(days=100)).isoformat()),
          str({k: item_a.get(k) for k in ("source_conflict", "diff_days", "expire_time", "order_expire_time")}))
    reg = db_rows["registry_by_domain"]
    check("E9 域名登记清单带差异标注字段",
          reg["www.a-demo.com"]["cert_source_conflict"] is True
          and reg["www.a-demo.com"]["cert_diff_days"] == 80
          and reg["www.d-demo.com"]["cert_source_conflict"] is False,
          str({k: reg["www.a-demo.com"].get(k) for k in ("cert_source_conflict", "cert_diff_days")}))


# ================= F. 配置消费（cert_warn_days） =================
def test_warn_days(db_rows: dict) -> None:
    print("F. 配置消费（cert_warn_days 接进到期分级；默认 30=原 30/7 不变）")
    check("F1 默认口径回归：30/7 未变",
          [cf.cert_level(d) for d in (-1, 0, 7, 8, 30, 31)] ==
          ["expired", "critical", "critical", "warning", "warning", "ok"])
    check("F2 warn_days 生效（20 天：默认=warning，配置 14=ok）",
          cf.cert_level(20) == "warning" and cf.cert_level(20, 14) == "ok")
    check("F3 warn_days 放宽（45 天：默认=ok，配置 60=warning）",
          cf.cert_level(45) == "ok" and cf.cert_level(45, 60) == "warning")
    check("F4 ≤7 天固定紧急档（warn=5 时 6 天仍 critical，8 天 ok）",
          cf.cert_level(6, 5) == "critical" and cf.cert_level(8, 5) == "ok")
    check("F5 非法 warn_days 回默认", cf.cert_level(20, 0) == "warning"
          and cf.cert_level(20, None) == "warning")
    facts = db_rows["domain_facts"]
    check("F6 事实带所在任务 warn_days（14/60 两个任务各归各）",
          facts["www.a-demo.com"].warn_days == 14 and facts["only.c-demo.com"].warn_days == 60,
          f"{facts['www.a-demo.com'].warn_days}/{facts['only.c-demo.com'].warn_days}")
    item_a = db_rows["expiry_by_id"]["cas-a"]
    check("F7 到期账分级消费 warn_days（20 天+配置 14 → ok）",
          item_a["level"] == "ok" and item_a["cert_warn_days"] == 14,
          f"{item_a['level']} {item_a.get('cert_warn_days')}")
    reg = db_rows["registry_by_domain"]
    check("F8 域名登记分级消费 warn_days",
          reg["www.a-demo.com"]["cert_level"] == "ok" and reg["www.a-demo.com"]["cert_warn_days"] == 14
          and reg["only.c-demo.com"]["cert_level"] == "warning",
          f"{reg['www.a-demo.com']['cert_level']}/{reg['only.c-demo.com']['cert_level']}")


# ================= G. 回归 =================
def test_regression(db_rows: dict) -> None:
    print("G. 回归（expiry / domain_registry / synth）")
    by = db_rows["expiry_by_id"]
    check("G1 非证书行 30/7 口径不变（ecs 10 天=warning）",
          by["i-ecs1"]["level"] == "warning" and by["i-ecs1"]["days_left"] == 10
          and "cert_source" not in by["i-ecs1"], str(by["i-ecs1"].get("level")))
    check("G2 同名多单去重只看最新一张（旧证不进账）",
          "cas-dup-new" in by and "cas-dup-old" not in by, str(sorted(by)))
    check("G3 订单兜底行日期/分级原样（100 天=ok）",
          by["cas-b"]["level"] == "ok" and by["cas-b"]["days_left"] == 100, str(by["cas-b"].get("days_left")))
    rows = db_rows["registry_rows"]
    check("G4 回填落新来源编码",
          rows["www.a-demo.com"].cert_source == "tls_probe"
          and rows["www.b-demo.com"].cert_source == "cas_order",
          f"{rows['www.a-demo.com'].cert_source}/{rows['www.b-demo.com'].cert_source}")
    check("G5 回填幂等（第二遍 created=0）",
          db_rows["backfill2"]["created"] == 0 and db_rows["backfill2"]["updated"] >= 6,
          str(db_rows["backfill2"]))
    check("G6 清单在场且带差异标注",
          "www.a-demo.com" in db_rows["registry_by_domain"]
          and db_rows["registry_by_domain"]["www.a-demo.com"]["cert_source_conflict"] is True)
    # synth：detail 文本解析与采集本体输出格式对齐 + run_check 落库链路
    detail = ("HTTP 200｜DNS 1.0ms · 建联 2.0ms · TLS 3.0ms · 首字节 4.0ms · "
              "传输 5.0ms｜12.3KB｜证书 45 天后到期")
    days, serial = cf.parse_probe_cert(detail)
    check("G7 采集 detail 格式解析（含序列号变体）",
          days == 45 and serial == ""
          and cf.parse_probe_cert(detail + "｜序列号 0ABC1234") == (45, "0ABC1234"),
          f"{days}/{serial}")
    with SessionLocal() as db:
        from app.services.synth import run_check

        c = SynthCheck(name="tcp-smoke", check_type="tcp", target="127.0.0.1:1", timeout_sec=2)
        db.add(c)
        db.commit()
        r = run_check(db, c)
        r_ok, r_detail = r.ok, r.detail          # 会话内取值（出 with 再访问会 Detached）
        n_res = db.query(SynthResult).filter(SynthResult.check_id == c.id).count()
        from app.models.alert_record import AlertRecord

        n_alert = db.query(AlertRecord).filter(AlertRecord.rule_name == "拨测连续失败").count()
    check("G8 synth run_check 落库链路不破（失败不误报、结果在场）",
          r_ok is False and n_res == 1 and n_alert == 0 and r_detail != "",
          f"ok={r_ok} res={n_res} alert={n_alert} detail={r_detail[:40]}")


def main() -> None:
    print("== 证书单一事实冒烟==")
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed(db)
        dr.backfill(db)                     # 域名登记回填（清单/落表依赖）
    reset_pk_sequences(engine)              # 显式 id 造数后校准序列
    test_normalize()
    with SessionLocal() as db:
        facts = cf.resolve_for_domain(db, DOMAINS)
        assets = db.query(Asset).filter(Asset.product == "ssl_cert").all()
        asset_facts = cf.resolve_for_assets(db, assets)
    with SessionLocal() as db:
        data = ex.expiry_items(db)
        reg = dr.registry_list(db)
        backfill2 = dr.backfill(db)
        rows = {r.domain: r for r in db.query(dr.DomainRegistry).all()}
    db_rows = {
        "domain_facts": facts,
        "asset_facts": asset_facts,
        "expiry_by_id": {i["instance_id"]: i for i in data["items"]},
        "registry_by_domain": {i["domain"]: i for i in reg["items"]},
        "registry_rows": rows,
        "backfill2": backfill2,
    }
    test_resolution(db_rows)
    test_source_labels(db_rows)
    test_conflict(db_rows)
    test_warn_days(db_rows)
    test_regression(db_rows)
    print(f"\n== 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ==")
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    main()
