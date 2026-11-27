#!/usr/bin/env python3
"""诊断证据补强隔离冒烟：全 mock，不碰真实 SLS/VM/生产库。

覆盖：
  A path_latency_detail：URI 级 avg/p95/5xx + 路径过滤 + 按 p95 排序 + 慢接口标注；
  B p95 不受支持时降级为 avg（p95 留空、note 明示）；
  C SLS 窗口无日志时回退链路库 LinkEdge（路径前缀粒度）；
  D 完全无数据时给出可操作的 hint；
  E db_health：Redis 平均RT 已纳入指标；summary 带各指标最高实例读数（未超阈值也能引用）；
  F 预取判定：Redis/缓存/OOM/慢 命中；打不开不命中；_MAX_STEPS=8；工具场景白名单正确。

用法：python3 tests/test_diag_evidence_mock.py   （退出码 0=全过）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

# ---- 隔离环境必须先于 app 导入设置 ----
TMPDIR = tempfile.mkdtemp(prefix="qianshu-diag-evidence-mock-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("test")
os.environ.setdefault("MASTER_KEY", "0123456789abcdef0123456789abcdef")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm import Session  # noqa: E402

import app.collector.credential as cred  # noqa: E402
from app.core.db import Base, engine  # noqa: E402
from app.models.asset import Asset  # noqa: E402
from app.models.link_edge import LinkEdge  # noqa: E402
from app.models.topology_scan import TopologyScan  # noqa: E402
from app.services import diag_tools as DT  # noqa: E402
from app.services import diagnosis as DG  # noqa: E402
import app.main  # noqa: E402,F401  全量注册模型（业务标识/变更/成本等）后再建表

Base.metadata.create_all(engine)
db = Session(engine)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ ") + name + ("" if cond else f"  -> {detail}"))


# ---- 统一桩：凭据 + SLS 目标（不触网） ----
cred.resolve_aliyun_credential = lambda _db: ("ak", "sk", None)
cred.resolve_sls_credential = lambda _db: ("ak", "sk", None)
DT.slb_targets = lambda: [("proj", "logstore", "outer")]

_orig_sls = DT.sls_query_logs


def _use_sls(fn):
    DT.sls_query_logs = fn


def _rows(*rows):
    return {"logs": list(rows)}


ROWS = [
    {"request_uri": "/api/order/list", "cnt": "100", "rt": "0.4", "p95": "1.2", "c5xx": "2"},
    {"request_uri": "/api/user/info", "cnt": "50", "rt": "0.05", "p95": "0.08", "c5xx": "0"},
    {"request_uri": "/static/a.js", "cnt": "900", "rt": "0.01", "p95": "0.02", "c5xx": "0"},
]

# ===== A URI 级下钻 + 路径过滤 =====
print("A path_latency_detail：URI 级 avg/p95/5xx + 路径过滤")
_seen_queries = []


def _sls_a(ak, sk, project, logstore, q, start=None, end=None, limit=None):
    _seen_queries.append(q)
    return _rows(*ROWS)


_use_sls(_sls_a)
out = DT.path_latency_detail(db, {"host": "www.demo.com", "path": "/api/order", "window": "24h"})
items = out.get("items") or []
check("A 路径过滤只留 /api/order/list",
      len(items) == 1 and items[0]["uri"] == "/api/order/list", str(items))
check("A 平均RT 秒→毫秒换算正确", items and items[0]["avg_rt_ms"] == 400.0, str(items))
check("A p95 换算正确", items and items[0]["p95_rt_ms"] == 1200.0, str(items))
check("A 5xx 计数透出", items and items[0]["5xx"] == 2, str(items))
check("A note 标注疑似慢接口 1 个", "1 个" in (out.get("note") or ""), out.get("note"))
check("A 查询含 request_uri + approx_percentile",
      any("request_uri" in q and "approx_percentile" in q for q in _seen_queries),
      str(_seen_queries)[:200])
check("A 按路径归并（regexp_extract 去查询串）",
      any("regexp_extract" in q and "group by regexp_extract" in q for q in _seen_queries),
      str(_seen_queries)[:200])

# 无 path：按 p95 降序（order 1.2s > user 0.08s > static 0.02s）
out = DT.path_latency_detail(db, {"host": "www.demo.com", "window": "24h"})
uris = [it["uri"] for it in out.get("items") or []]
check("A 无 path 时按 p95 降序", uris == ["/api/order/list", "/api/user/info", "/static/a.js"], str(uris))

# ===== B p95 不支持时降级 =====
print("B p95 不受支持 → 自动降级 avg")


def _sls_b(ak, sk, project, logstore, q, start=None, end=None, limit=None):
    if "approx_percentile" in q:
        raise RuntimeError("unsupported function approx_percentile")
    return _rows({"request_uri": "/api/slow", "cnt": "10", "rt": "2.5", "c5xx": "0"})


_use_sls(_sls_b)
out = DT.path_latency_detail(db, {"host": "www.demo.com", "window": "24h"})
it = (out.get("items") or [{}])[0]
check("B 降级后仍有平均RT 2500ms", it.get("avg_rt_ms") == 2500.0, str(it))
check("B 降级后 p95 为空并注明", it.get("p95_rt_ms") is None and "不支持 p95" in (out.get("note") or ""),
      str(out.get("note")))

# ===== B2 regexp_extract 不被支持 → 原文 URI 粒度 =====
print("B2 regexp_extract 不受支持 → 降级原始 URI")


def _sls_b2(ak, sk, project, logstore, q, start=None, end=None, limit=None):
    if "regexp_extract" in q:
        raise RuntimeError("unsupported function regexp_extract")
    return _rows({"request_uri": "/api/slow?ts=1", "cnt": "5", "rt": "1.5", "p95": "2.0", "c5xx": "0"})


_use_sls(_sls_b2)
out = DT.path_latency_detail(db, {"host": "www.demo.com", "window": "24h"})
it = (out.get("items") or [{}])[0]
check("B2 降级后仍拿到原始 URI 数据", it.get("uri") == "/api/slow?ts=1", str(it))
check("B2 note 注明未按路径归并", "原始 URI" in (out.get("note") or ""), out.get("note"))

# ===== B3 查询成功但无日志：不重复换写法 =====
print("B3 查询成功但窗口无日志 → 只查一次")
_calls = []


def _sls_b3(ak, sk, project, logstore, q, start=None, end=None, limit=None):
    _calls.append(q)
    return _rows()


_use_sls(_sls_b3)
DT.path_latency_detail(db, {"host": "none.demo.com", "window": "24h"})
check("B3 无数据不做 4 次降级重试", len(_calls) == 1, f"{len(_calls)} 次")

# ===== C 回退链路库 =====
print("C SLS 无日志 → 回退链路库 LinkEdge")
_use_sls(lambda *a, **k: _rows())
db.add(LinkEdge(chain="outer", host="www.demo.com", upstream_addr="10.0.0.8:80",
                path_pattern="/api", req_count=5000, cnt_5xx=3, rt_avg_ms=88.0))
db.add(LinkEdge(chain="outer", host="www.demo.com", upstream_addr="10.0.0.8:80",
                path_pattern="/static", req_count=9000, cnt_5xx=0, rt_avg_ms=4.0))
db.commit()
out = DT.path_latency_detail(db, {"host": "www.demo.com", "path": "/api", "window": "24h"})
items = out.get("items") or []
check("C 回退命中链路库 /api 且带 RT",
      len(items) == 1 and items[0]["uri"] == "/api" and items[0]["avg_rt_ms"] == 88.0, str(items))
check("C 标明来源为链路库聚合", items and items[0]["source"] == "链路库(24h聚合)", str(items))
check("C note 说明 p95 不可得", "链路库" in (out.get("note") or ""), out.get("note"))

# ===== D 完全无数据 → hint =====
print("D 无 SLS、无链路库 → hint")
out = DT.path_latency_detail(db, {"host": "unknown.demo.com", "path": "/api/x", "window": "24h"})
check("D 返回空并给出排查提示",
      not out.get("items") and "没有 SLB 访问日志" in (out.get("hint") or ""), str(out))

# ===== E db_health：Redis 平均RT + 峰值读数 =====
print("E db_health：Redis 平均RT 纳入 + summary 正向读数")
db.add(Asset(account_id=1, product="redis", instance_id="r-1", name="cache-a"))
db.add(Asset(account_id=1, product="rds", instance_id="rm-1", name="db-a"))
db.commit()


def _vm(q, start, end, step):
    if "aliyun_redis_shardingavgrt" in q:
        return [{"metric": {"instance_id": "r-1"}, "values": [[1, "0.8"]]}]
    if "aliyun_redis_shardingmemoryusage" in q:
        return [{"metric": {"instance_id": "r-1"}, "values": [[1, "42.5"]]}]
    if "aliyun_redis_shardingconnectionusage" in q:
        return [{"metric": {"instance_id": "r-1"}, "values": [[1, "12.0"]]}]
    if "aliyun_redis_shardingcpuusage" in q:
        return [{"metric": {"instance_id": "r-1"}, "values": [[1, "3.0"]]}]
    if "aliyun_rds_cpuusage" in q:
        return [{"metric": {"instance_id": "rm-1"}, "values": [[1, "91.2"]]}]
    if "aliyun_rds_memoryusage" in q:
        return [{"metric": {"instance_id": "rm-1"}, "values": [[1, "70.0"]]}]
    return []


DT.vm_query_range = _vm
out = DT.db_health(db, {"window": "24h"})
items = out.get("items") or []
summary = out.get("summary") or []
rt_sum = next((s for s in summary if s["metric"] == "平均响应时间ms"), None)
mem_sum = next((s for s in summary if s["metric"] == "内存使用率%" and s["product"] == "REDIS"), None)
check("E Redis 平均RT 进入指标口径", rt_sum is not None and rt_sum["peak"] == 0.8, str(rt_sum))
check("E Redis 正常值也出现在 summary（可被结论引用）",
      mem_sum is not None and mem_sum["peak"] == 42.5 and mem_sum["instance"] == "cache-a", str(mem_sum))
check("E 超阈值 RDS CPU 进入 items 且点名实例",
      any(i["product"] == "RDS" and i["peak"] == 91.2 and i["name"] == "db-a" for i in items), str(items))
check("E 部分指标零样本时状态为 partial",
      out.get("status") == "partial" and out["coverage"]["metrics_with_samples"] == 6,
      str(out.get("coverage")))

# ===== E2 无样本 / 查询失败 / 部分失败不得误判正常 =====
print("E2 db_health：无样本、失败与部分覆盖")
DT.vm_query_range = lambda *a: []
empty = DT.db_health(db, {"window": "24h"})
check("E2 全部零样本状态 no_data",
      empty.get("status") == "no_data" and not empty.get("summary"), str(empty))
check("E2 零样本不再宣称均在阈值内",
      "无法判断" in empty.get("note") and "均在阈值内" not in empty.get("note"), empty.get("note"))


def _vm_all_error(*a):
    raise RuntimeError("mock VM unavailable")


DT.vm_query_range = _vm_all_error
failed = DT.db_health(db, {"window": "24h"})
check("E2 全部查询失败状态 error",
      failed.get("status") == "error" and len(failed.get("failed_metrics")) == len(DT._DB_METRICS),
      str(failed.get("coverage")))


def _vm_partial(q, start, end, step):
    if "aliyun_redis_shardingavgrt" in q:
        return [{"metric": {"instance_id": "r-1"}, "values": [[1, "1.5"]]}]
    if "aliyun_rds_cpuusage" in q:
        raise RuntimeError("mock timeout")
    return []


DT.vm_query_range = _vm_partial
partial = DT.db_health(db, {"window": "24h"})
check("E2 单指标有样本且有失败时状态 partial",
      partial.get("status") == "partial" and partial["coverage"]["metrics_with_samples"] == 1
      and partial["coverage"]["queries_failed"] == 1, str(partial.get("coverage")))
check("E2 部分证据明确禁止整体正常背书", "不能据此宣称" in partial.get("note"), partial.get("note"))


def _vm_all_ok(q, start, end, step):
    product = "r-1" if "redis" in q else "rm-1"
    return [{"metric": {"instance_id": product}, "values": [[1, "10.0"]]}]


DT.vm_query_range = _vm_all_ok
normal = DT.db_health(db, {"window": "24h"})
check("E2 全指标有样本且未越限才允许 ok",
      normal.get("status") == "ok" and not normal.get("items")
      and "8 项指标均有样本" in normal.get("note"), str(normal))
DT.vm_query_range = _vm

# ===== G 链路数据陈旧必须显式告警 =====
print("G 链路数据超过 24h 未刷新 → stale 标记")
db.add(LinkEdge(chain="outer", host="old.demo.com", upstream_addr="10.0.0.9:80",
                req_count=100, cnt_5xx=0, rt_avg_ms=50.0,
                generated_at=datetime.now() - timedelta(days=15)))
db.commit()
fd = DT.find_domain(db, {"keyword": "old.demo.com"})
bh = DT.backend_health(db, {"host": "old.demo.com"})
check("G find_domain 标 stale 并给出数据时间",
      fd.get("stale") is True and bool(fd.get("data_as_of")) and "未刷新" in (fd.get("note") or ""),
      str(fd)[:200])
check("G backend_health 标 stale 且注明流量非实时",
      bh.get("stale") is True and "不是实时值" in (bh.get("note") or ""), str(bh)[:200])
fd2 = DT.find_domain(db, {"keyword": "www.demo.com"})
check("G 新数据不误标 stale", not fd2.get("stale"), str(fd2)[:120])

db.add(Asset(account_id=2, product="cvm", instance_id="ins-tc-1", name="tc-backend",
             status="running"))
db.add(LinkEdge(chain="outer", host="tc.demo.com", upstream_addr="10.3.0.8:80",
                ecs_name="tc-backend", req_count=10, status="confirmed"))
db.commit()
tc_backend = DT.backend_health(db, {"host": "tc.demo.com"})
check("G 腾讯 CVM 后端不按 ECS 过滤掉",
      tc_backend["items"] and tc_backend["items"][0].get("asset_product") == "cvm"
      and tc_backend["items"][0].get("ecs_status") == "running", str(tc_backend))

# ===== F 预取判定 / 预算 / 场景白名单 =====
print("F 预取判定与工具注册")
check("F Redis/OOM 问题触发预取", DG._needs_db_prefetch("是 Redis 缓存爆了还是应用 OOM"))
check("F 接口慢触发预取", DG._needs_db_prefetch("www.tita.com/api/xxx 接口慢"))
check("F 打不开不触发预取", not DG._needs_db_prefetch("www.demo.com 打不开"))
check("F 最大步数=8", DG._MAX_STEPS == 8, str(DG._MAX_STEPS))
lf = DT.tools_for_scenario("link_fault")
check("F path_latency_detail 注册到 link_fault", "path_latency_detail" in lf)
check("F path_latency_detail 不进入成本场景",
      "path_latency_detail" not in DT.tools_for_scenario("cost_analysis"))

# ===== H 诊断读取真实架构快照，不套固定厂商链路 =====
print("H 架构快照进入诊断上下文")
no_snapshot = DG._architecture_evidence(db)
check("H 无快照时明确证据缺口", "无可用完整快照" in no_snapshot
      and "不得假设" in no_snapshot, no_snapshot)
arch = {
    "groups": [
        {"id": "g:tc:app", "role": "app", "label": "订单 CVM", "count": 1,
         "source_label": "腾讯云", "account_label": "腾讯生产",
         "members": [{"label": "order-cvm", "meta": {"product": "cvm"}}]},
        {"id": "g:tc:db", "role": "db", "label": "MongoDB", "count": 1,
         "source_label": "腾讯云", "account_label": "腾讯生产",
         "members": [{"label": "mongo-a", "meta": {"product": "tc_mongodb"}}]},
    ],
    "links": [{"source": "g:tc:app", "target": "g:tc:db",
               "types": ["net"], "weight": 1.0}],
}
db.add(TopologyScan(scanned_at=datetime(2026, 11, 13, 12, 0), trigger="test",
                    groups=2, links=1, members=2, diff_json="{}", synced_json="{}",
                    arch_json=json.dumps(arch, ensure_ascii=False)))
db.commit()
evidence = DG._architecture_evidence(db)
check("H 证据带扫描时间和真实腾讯组件",
      "2026-11-13T12:00:00" in evidence and "腾讯生产" in evidence
      and "订单 CVM" in evidence and "MongoDB" in evidence, evidence)
check("H 证据带真实依赖连线", "订单 CVM -> MongoDB" in evidence, evidence)
context = DG._scenario_context("link_fault", db)
check("H 链路场景使用快照而非固定阿里链路",
      evidence in context and "用户 -> 外层SLB" not in context, context[:500])
check("H 系统提示不再声称只排查阿里云", "排查阿里云上" not in DG._DIAG_SYSTEM)

print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
if FAIL:
    print("失败项：\n- " + "\n- ".join(FAIL))
sys.exit(0 if not FAIL else 1)
