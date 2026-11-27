#!/usr/bin/env python3
"""SLS 完整目录、语义证据、隐私和账号隔离回归；全部云调用 mock。"""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="qianshu-sls-discovery-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("test")
os.environ["MASTER_KEY"] = "ef" * 32
os.environ["SLS_ACCOUNT_ID"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (pathlib.Path(__file__).resolve().parent.parent / "app" / "models").glob("*.py"):
    if _p.stem != "__init__":
        importlib.import_module(f"app.models.{_p.stem}")

from app.collector import sls
from app.core.db import Base, SessionLocal, engine
from app.core.security import encrypt_secret
from app.models.cloud_account import CloudAccount
from fastapi import HTTPException, Request
from app.api.v1 import bootstrap as bootstrap_api
from app.collector import credential
from app.core.config import settings
from app.services import onboarding, sls_discovery

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS " if condition else "  FAIL ") + name +
          ("" if condition else f"  <-- {detail}"))


class FakeProjects:
    def __init__(self, batches, fail_at=-1):
        self.batches = batches
        self.fail_at = fail_at
        self.calls = 0

    def list_project(self, offset, size):
        call = self.calls
        self.calls += 1
        if call == self.fail_at:
            raise RuntimeError("mock project page denied")
        values = self.batches[call] if call < len(self.batches) else []
        return SimpleNamespace(get_projects=lambda: values)


def project_pages():
    old = sls._client
    try:
        batches = [[{"projectName": f"p{i}"} for i in range(100)],
                   [{"projectName": f"p{i}"} for i in range(100, 121)]]
        fake = FakeProjects(batches)
        sls._client = lambda *args, **kwargs: fake
        result = sls.list_projects_detailed("ak", "sk")
        check("121 个 Project 多页完整返回", result["projects"] == sorted(f"p{i}" for i in range(121))
              and result["complete"] and fake.calls == 2, result)

        fake = FakeProjects(batches, fail_at=1)
        sls._client = lambda *args, **kwargs: fake
        result = sls.list_projects_detailed("ak", "sk")
        check("第二页失败保留第一页并显式中止", result["projects"] == sorted(f"p{i}" for i in range(100))
              and not result["complete"] and "denied" in result["error"]
              and result["next_offset"] == 100, result)
        # 兼容接口仍按原行为全量返回，不引入静默截断。
        fake = FakeProjects(batches)
        sls._client = lambda *args, **kwargs: fake
        check("旧 list_projects 接口保留全量分页", sls.list_projects("ak", "sk") == sorted(f"p{i}" for i in range(121)))
    finally:
        sls._client = old


def semantic_cases():
    cases = [
        ({"http_host": "x", "upstream_addr": "x", "status": "200", "request_time": "1", "slbid": "lb-x"},
         "slb", 0.9, True),
        ({"domain": "x", "request_uri": "/", "http_code": "200", "hit": "HIT"}, "cdn", 0.9, True),
        ({"domain": "x", "uri": "/", "return_code": "200", "hit_info": "HIT", "response_size": "10"},
         "cdn", 0.9, True),
        ({"matched_host": "x", "request_traceid": "x", "real_client_ip": "x", "upstream_addr": "x",
          "host": "x", "status": "200", "request_time": "1", "upstream_response_time": "1"},
         "waf", 0.9, False),
        ({"proxy_upstream_name": "x", "_namespace_": "x", "upstream_addr": "x", "host": "x",
          "status": "200", "request_time": "1", "upstream_response_time": "1"},
         "ingress", 0.9, False),
        ({"scheduleId": "x", "fingerprint": "x", "drill_down_url": "x", "level": "ERROR", "msg": "secret-message"},
         "system", 0.9, False),
        ({"alert": "x", "error": "x", "level": "ERROR"}, "system", 0.9, False),
        ({"http_host": "x", "upstream_addr": "x", "status": "200", "request_time": "1"},
         "unknown", 0.0, False),
        ({"level": "ERROR", "message": "secret-message", "service": "orders", "trace_id": "secret-trace"},
         "application_json", 0.8, True),
        ({"content": "raw secret line"}, "application_text", 0.5, True),
        ({"foo": "bar", "token": "never-return-this"}, "unknown", 0.0, False),
    ]
    for fields, role, minimum, adoptable in cases:
        raw = dict(fields)
        raw["__time__"] = 1_800_000_000
        project, logstore = (("sls-alert-123-cn-hangzhou", "internal-alert-center-log")
                             if set(fields) == {"alert", "error", "level"} else ("project", "store"))
        result = sls_discovery.classify_sample(project, logstore, [raw])
        check(f"字段指纹角色 {role}", result["role"] == role
              and result["confidence"] >= minimum and result["adoptable"] is adoptable
              and bool(result["evidence"]), result)
        check(f"{role} 只保存派生字段不保存样本值",
              result["latest_at"] == 1_800_000_000
              and all(secret not in json.dumps(result, ensure_ascii=False)
                      for secret in ("secret-message", "secret-trace", "raw secret line",
                                     "never-return-this")), result)

    unknown = sls_discovery.classify_sample("project", "my-slb-store", [{"foo": "x"}])
    check("名称只能作弱证据，不能单独绑定 SLB", unknown["role"] == "unknown"
          and not unknown["adoptable"] and unknown["name_hint"] == "slb", unknown)
    empty = sls_discovery.classify_sample("project", "store", [])
    check("无近期数据与未知角色分开", empty["data_status"] == "no_recent_data"
          and empty["role"] == "unknown" and not empty["adoptable"], empty)


def confirmation_cases():
    app = sls_discovery.classify_sample(
        "app-project", "app-store",
        [{"severity": "ERROR", "msg": "not-persisted", "service": "orders"}])
    slb = sls_discovery.classify_sample("net", "slb", [{
        "http_host": "x", "upstream_addr": "x", "status": "200", "request_time": "1", "slbid": "lb-x"}])
    cdn = sls_discovery.classify_sample("net", "cdn", [{
        "domain": "x", "request_uri": "/", "http_code": "200", "hit": "HIT"}])
    unknown = sls_discovery.classify_sample("other", "unknown", [{"foo": "x"}])
    plan = sls_discovery.confirmation_plan([app, slb, cdn, dict(cdn, logstore="cdn-2"), unknown])
    groups = {x["role"]: x for x in plan["groups"]}
    check("唯一高置信应用可预选，多候选 CDN 不静默取第一条",
          groups["application"]["preselected"][0]["logstore"] == "app-store"
          and groups["cdn"]["preselected"] == [] and len(groups["cdn"]["targets"]) == 2, plan)
    check("SLB 链路位置必须人工确认，未知日志不集中打扰新手",
          groups["slb"]["chain_confirmation_required"] and groups["slb"]["preselected"] == []
          and plan["deferred_count"] == 1, plan)

    mapping = sls_discovery.application_field_mapping(app)
    check("应用字段映射使用真实字段名且语义可消费",
          mapping["semantic_supported"] and mapping["field_mapping"] == {
              "level_field": "severity", "message_field": "msg", "app_field": "service"}, mapping)
    ambiguous = dict(app, fields=["level", "severity", "message"])
    mapping = sls_discovery.application_field_mapping(ambiguous)
    check("多个级别字段不得静默选择一个", not mapping["semantic_supported"]
          and mapping["semantic_reason"] == "field_mapping_ambiguous"
          and mapping["field_mapping"]["level_field"] == "", mapping)
    ambiguous_candidate = sls_discovery.classify_sample("app", "ambiguous", [{
        "level": "ERROR", "severity": "ERROR", "message": "not-persisted"}])
    ambiguous_plan = sls_discovery.confirmation_plan([ambiguous_candidate])
    check("字段歧义候选即使唯一也不预选", ambiguous_plan["groups"][2]["preselected"] == []
          and len(ambiguous_plan["groups"][2]["targets"]) == 1, ambiguous_plan)
    truncated = dict(app, fields_truncated=True)
    mapping = sls_discovery.application_field_mapping(truncated)
    check("字段清单截断停止语义分析", not mapping["semantic_supported"]
          and mapping["semantic_reason"] == "field_inventory_truncated", mapping)
    text = sls_discovery.classify_sample("app", "text", [{"content": "raw"}])
    mapping = sls_discovery.application_field_mapping(text)
    check("文本日志保留原始检索但不猜级别字段", not mapping["semantic_supported"]
          and mapping["semantic_reason"] == "level_field_missing"
          and mapping["field_mapping"]["message_field"] == "content", mapping)


def legacy_adoption_case(db):
    request = Request({"type": "http", "method": "POST", "path": "/",
                       "headers": [], "client": ("127.0.0.1", 12345)})
    user = SimpleNamespace(id=1, username="mock-admin")
    body = bootstrap_api.ApplyConfigRequest(app_log=["legacy-p", "legacy-s"])
    writes = []
    previous = {key: getattr(settings, key) for key in (
        "SLS_ACCOUNT_ID", "SLS_SLB_TARGETS", "SLS_APP_LOG", "SLS_CDN_TARGET", "SLS_INSPECTION_TARGETS")}

    def query(ak, sk, project, logstore, text, **kwargs):
        return {"logs": [{"cnt": "0"}] if "|" in text else [], "count": 0}

    try:
        with patch.object(bootstrap_api, "_resolve_ak_sk", return_value=("legacy-ak", "legacy-sk")), \
                patch.object(sls_discovery.sls, "query_logs", side_effect=query) as consume, \
                patch("app.services.env_store.set_keys",
                      side_effect=lambda values: writes.append(values) or True):
            result = bootstrap_api.apply_config(body, request, db, user)
        check("无账号旧入口保留两段应用配置和原字段消费",
              result["ok"] and consume.call_count == 2 and len(writes) == 1
              and json.loads(writes[0]["SLS_APP_LOG"]) == ["legacy-p", "legacy-s"]
              and consume.call_args_list[1].args[4] == "LogLevel: ERROR | select count(*) as cnt",
              writes)
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


def discovery_cases(db):
    account = CloudAccount(name="discovery-a", vendor="aliyun", enabled=True, status="ok",
                           ak_cipher=encrypt_secret("account-a-ak"),
                           secret_cipher=encrypt_secret("account-a-sk"),
                           extra_json=json.dumps({"sls_endpoint": "cn-test-a.log.aliyuncs.com"}))
    db.add(account)
    db.commit()
    projects = [f"p{i}" for i in range(21)]
    stores = {p: [f"s{i}" for i in range(35)] for p in projects}
    seen_credentials = []
    seen_endpoints = []

    def fake_query(ak, sk, project, logstore, query, **kwargs):
        seen_credentials.append((ak, sk))
        seen_endpoints.append(kwargs.get("endpoint", ""))
        return {"logs": [{"level": "INFO", "message": f"raw-{project}-{logstore}",
                          "service": "shop", "__time__": 1_800_000_000}],
                "count": 1, "has_more": False, "is_sql": False}

    with patch.object(sls_discovery.sls, "list_projects_detailed",
                      return_value={"projects": projects, "complete": True, "error": "", "pages": 1}), \
            patch.object(sls_discovery.sls, "list_logstores", side_effect=lambda ak, sk, p, endpoint="": stores[p]), \
            patch.object(sls_discovery.sls, "query_logs", side_effect=fake_query):
        result = sls_discovery.discover(account.id, background=False)
    db.expire_all()
    account = db.get(CloudAccount, account.id)
    saved = onboarding.get_run(db, account.id)
    payload = sls_discovery.get_discovery(saved)
    check("21 Project × 35 Logstore 不截断", result["coverage"]["projects"] == 21
          and result["coverage"]["logstores"] == 735 and len(result["logstores"]) == 735,
          result["coverage"])
    check("发现严格使用所属账号凭证", set(seen_credentials) == {("account-a-ak", "account-a-sk")},
          set(seen_credentials))
    check("发现样本使用账号专属 endpoint", set(seen_endpoints) == {"cn-test-a.log.aliyuncs.com"},
          set(seen_endpoints))
    check("Run 持久化发现结果且不含原始日志", "raw-p0-s0" not in saved.items_json
          and "account-a-sk" not in saved.items_json and payload["coverage"]["logstores"] == 735)
    check("列表序列化不携带大目录", "catalog" not in next(
        x for x in onboarding.serialize(saved, account)["items"] if x["key"] == "sls_discovery"))
    check("完整发现后等待集中确认", saved.status == "attention"
          and any(x["key"] == "sls_targets" and x["status"] == "pending"
                  for x in json.loads(saved.items_json)), saved.status)
    logs_capability = next(x for x in onboarding.capability_results(saved, account)
                           if x["capability"] == "logs")
    check("目录完整不等于日志分析就绪", logs_capability["execution"] == "waiting"
          and logs_capability["data_state"] == "available"
          and logs_capability["reason_code"] == "confirmation_required"
          and logs_capability["coverage"]["total_known"], logs_capability)
    check("735 个候选只生成三类集中确认", result["confirmation"]["required"]
          and [x["role"] for x in result["confirmation"]["groups"]] == ["slb", "cdn", "application"]
          and len(result["confirmation"]["groups"][2]["preselected"]) == 0,
          result["confirmation"])

    calls = []

    def partial_list(ak, sk, project, endpoint=""):
        if project == "p3":
            raise RuntimeError("mock logstore permission denied")
        return ["one"]

    with patch.object(sls_discovery.sls, "list_projects_detailed",
                      return_value={"projects": projects, "complete": True, "error": "", "pages": 1}), \
            patch.object(sls_discovery.sls, "list_logstores", side_effect=partial_list), \
            patch.object(sls_discovery.sls, "query_logs", return_value={"logs": [], "count": 0}):
        result = sls_discovery.discover(account.id, background=False)
    failed_project = next(x for x in result["projects"] if x["project"] == "p3")
    check("单 Project 权限失败不伪装为空目录", failed_project["status"] == "failed"
          and "denied" in failed_project["error"] and result["coverage"]["project_failures"] == 1
          and not result["coverage"]["complete"], result["coverage"])
    check("部分目录失败保留其他 Project", len(result["logstores"]) == 20
          and result["status"] == "failed", result["status"])
    calls.clear()

    with patch.object(sls_discovery.sls, "list_projects_detailed",
                      return_value={"projects": ["p0"], "complete": True, "error": "", "pages": 1}), \
            patch.object(sls_discovery.sls, "list_logstores", return_value=["denied-store"]), \
            patch.object(sls_discovery.sls, "query_logs", side_effect=RuntimeError("mock sample denied")):
        result = sls_discovery.discover(account.id, background=False)
    failed_sample = result["logstores"][0]
    check("样本权限失败保留 Logstore 并禁止采纳", failed_sample["logstore"] == "denied-store"
          and failed_sample["query_status"] == "failed" and not failed_sample["adoptable"]
          and result["coverage"]["query_failures"] == 1 and result["status"] == "failed", failed_sample)

    with patch.object(sls_discovery.sls, "query_logs",
                      side_effect=RuntimeError("IndexConfigNotExist: logstore without index config")):
        missing_index = sls_discovery._sample("ak", "sk", "p0", "no-index", "", 0, 1)
    check("未配置索引不误报为权限不足或无数据", missing_index["query_status"] == "failed"
          and missing_index["error_code"] == "index_missing" and not missing_index["adoptable"]
          and "未配置索引" in missing_index["evidence"][0]["detail"]
          and "不是权限不足或无数据" in missing_index["evidence"][0]["detail"], missing_index)

    with patch.object(sls_discovery.sls, "list_projects_detailed",
                      return_value={"projects": [], "complete": True, "error": "", "pages": 1}):
        result = sls_discovery.discover(account.id, background=False)
    check("完整空目录是合法跳过而非失败", result["status"] == "skipped"
          and result["coverage"]["complete"] and result["coverage"]["logstores"] == 0, result)
    db.expire_all()
    empty_logs = next(x for x in onboarding.capability_results(onboarding.get_run(db, account.id), account)
                      if x["capability"] == "logs")
    check("空日志目录同步共享能力证据", empty_logs["execution"] == "skipped"
          and empty_logs["data_state"] == "no_data"
          and empty_logs["reason_code"] == "no_logstores", empty_logs)

    with patch.object(sls_discovery.sls, "list_projects_detailed",
                      side_effect=RuntimeError("mock project listing denied")):
        result = sls_discovery.discover(account.id, background=False)
    db.expire_all()
    target_item = next(x for x in json.loads(onboarding.get_run(db, account.id).items_json)
                       if x["key"] == "sls_targets")
    check("枚举整体失败不等于没有可配置日志", result["status"] == "failed"
          and result["coverage"]["logstores"] == 0 and target_item["status"] == "pending"
          and "尚不能判断" in target_item["detail"], target_item)

    other = CloudAccount(name="discovery-b", vendor="aliyun", enabled=True, status="ok",
                         ak_cipher=encrypt_secret("account-b-ak"),
                         secret_cipher=encrypt_secret("account-b-sk"),
                         extra_json=json.dumps({"endpoint": "cn-test-b.log.aliyuncs.com"}))
    db.add(other)
    db.commit()
    with patch.object(sls_discovery.sls, "query_logs", side_effect=fake_query):
        verified = sls_discovery.verify_target(other, "p", "s")
    check("验证使用指定账号 B 而非默认账号 A", seen_credentials[-1] == ("account-b-ak", "account-b-sk")
          and verified["ok"] and verified["account_id"] == other.id, verified)
    check("采纳验证使用同一账号专属 endpoint", seen_endpoints[-1] == "cn-test-b.log.aliyuncs.com",
          seen_endpoints[-1])
    confirmation = {"role": "application_json", "semantic_supported": True,
                    "field_mapping": {"level_field": "severity"}}
    with patch.object(sls_discovery.sls, "query_logs",
                      return_value={"logs": [{"cnt": "3"}], "count": 1}) as consume:
        consumed = sls_discovery.consume_target(other, "p", "s", confirmation)
    check("采纳后消费复用同账号 endpoint 且只保留派生计数",
          consume.call_args.args[:4] == ("account-b-ak", "account-b-sk", "p", "s")
          and consume.call_args.kwargs["endpoint"] == "cn-test-b.log.aliyuncs.com"
          and consume.call_args.args[4] == "LogLevel: ERROR | select count(*) as cnt"
          and consumed["matched_count"] == 3 and "logs" not in consumed, consumed)


def adoption_cases(db):
    first = db.query(CloudAccount).filter_by(name="discovery-a").one()
    second = db.query(CloudAccount).filter_by(name="discovery-b").one()
    candidate = sls_discovery.classify_sample("p0", "s0", [{
        "severity": "ERROR", "msg": "not-persisted", "service": "orders", "__time__": 1_800_000_000}])
    onboarding.record_sls_discovery(db, first, {
        "account_id": first.id, "status": "ready",
        "coverage": {"projects": 1, "logstores": 1, "sampled": 1, "complete": True},
        "projects": [{"project": "p0", "status": "ready"}],
        "logstores": [candidate], "privacy": "derived_fields_only"})
    db.commit()
    request = Request({"type": "http", "method": "POST", "path": "/",
                       "headers": [], "client": ("127.0.0.1", 12345)})
    user = SimpleNamespace(id=1, username="mock-admin")
    body = bootstrap_api.ApplyConfigRequest(account_id=first.id, app_log=["p0", "s0"])
    writes = []
    checked = {"account_id": first.id, "project": "p0", "logstore": "s0",
               "ok": True, "query_status": "ok", "data_status": "no_recent_data"}
    consumed = {"account_id": first.id, "project": "p0", "logstore": "s0", "ok": True,
                "query_status": "ok", "consumption_verified": True,
                "semantic_supported": True, "matched_count": 0,
                "evidence_ref": "sls_consumption:p0/s0:1:2"}
    with patch.object(sls_discovery, "verify_target", return_value=checked) as verify, \
            patch.object(sls_discovery, "consume_target", return_value=consumed) as consume, \
            patch("app.services.env_store.set_keys", side_effect=lambda values: writes.append(values) or True):
        result = bootstrap_api.apply_config(body, request, db, user)
    saved_mapping = json.loads(writes[0]["SLS_APP_LOG"])[2]
    check("采纳先按所属账号验证再完整持久化", verify.call_count == 1 and consume.call_count == 1
          and len(writes) == 1 and writes[0]["SLS_ACCOUNT_ID"] == str(first.id)
          and result["persisted"] and result["verified"][0]["data_status"] == "no_recent_data"
          and result["verified"][0]["consumption_verified"], result)
    check("采纳配置持久化已确认字段映射", saved_mapping == {
        "level_field": "severity", "message_field": "msg", "app_field": "service"}, writes[0])
    run = onboarding.get_run(db, first.id)
    target_item = next(x for x in json.loads(run.items_json) if x["key"] == "sls_targets")
    check("查询成功但无近期数据明确展示，不混为失败", target_item["status"] == "ready"
          and "近 15 分钟无数据" in target_item["detail"], target_item)
    logs_capability = next(x for x in onboarding.capability_results(run, first) if x["capability"] == "logs")
    check("采纳后消费证据进入同一 logs capability", logs_capability["execution"] == "succeeded"
          and logs_capability["data_state"] == "no_data"
          and logs_capability["reason_code"] == "logs_consumption_ready"
          and logs_capability["evidence_refs"] == ["sls_consumption:p0/s0:1:2"], logs_capability)
    ak, sk, _ = credential.resolve_sls_credential(db)
    check("运行时 SLS 默认查询也使用绑定账号", (ak, sk) == ("account-a-ak", "account-a-sk"))
    with patch.object(sls, "list_projects_detailed",
                      return_value={"projects": ["p0"], "complete": True, "error": ""}) as listing, \
            patch.object(sls, "list_logstores", return_value=["s0"]) as stores:
        catalog = bootstrap_api.sls_catalog(account_id=first.id, db=db, user=user)
    check("高级目录与发现使用相同账号 endpoint",
          listing.call_args.kwargs["endpoint"] == "cn-test-a.log.aliyuncs.com"
          and stores.call_args.kwargs["endpoint"] == "cn-test-a.log.aliyuncs.com"
          and catalog["coverage"]["complete"], catalog)
    with credential.account_credential("account-b-ak", "account-b-sk", second.id):
        try:
            credential.resolve_sls_credential(db)
            check("冲突账号上下文 fail closed", False)
        except credential.CredentialError:
            check("冲突账号上下文 fail closed", True)

    previous = settings.SLS_APP_LOG
    with patch.object(sls_discovery, "verify_target", side_effect=RuntimeError("mock verify denied")), \
            patch("app.services.env_store.set_keys") as env_write:
        try:
            bootstrap_api.apply_config(body, request, db, user)
            check("真实查询失败不能采纳", False)
        except HTTPException as exc:
            check("真实查询失败不能采纳", exc.status_code == 502 and not env_write.called
                  and settings.SLS_APP_LOG == previous, exc.detail)

    before_items = onboarding.get_run(db, first.id).items_json
    before_app_log = settings.SLS_APP_LOG
    with patch.object(sls_discovery, "verify_target", return_value=checked), \
            patch("app.services.env_store.set_keys", return_value=False):
        try:
            bootstrap_api.apply_config(body, request, db, user)
            check("持久化失败不能热更新或写成已生效", False)
        except HTTPException as exc:
            check("持久化失败不能热更新或写成已生效", exc.status_code == 500
                  and settings.SLS_APP_LOG == before_app_log
                  and onboarding.get_run(db, first.id).items_json == before_items, exc.detail)

    before_items = onboarding.get_run(db, first.id).items_json
    before_app_log = settings.SLS_APP_LOG
    rollback_writes = []
    with patch.object(sls_discovery, "verify_target", return_value=checked), \
            patch.object(sls_discovery, "consume_target", side_effect=RuntimeError("mock consumer denied")), \
            patch("app.services.env_store.set_keys",
                  side_effect=lambda values: rollback_writes.append(values) or True):
        try:
            bootstrap_api.apply_config(body, request, db, user)
            check("采纳后消费失败完整回滚", False)
        except HTTPException as exc:
            check("采纳后消费失败完整回滚", exc.status_code == 502 and len(rollback_writes) == 2
                  and "已恢复旧配置" in exc.detail and settings.SLS_APP_LOG == before_app_log
                  and onboarding.get_run(db, first.id).items_json == before_items, exc.detail)

    with patch.object(sls_discovery, "verify_target", return_value=checked), \
            patch.object(sls_discovery, "consume_target", side_effect=RuntimeError("mock consumer denied")), \
            patch("app.services.env_store.set_keys", side_effect=[True, False]):
        try:
            bootstrap_api.apply_config(body, request, db, user)
            check("配置回退失败必须显式报警", False)
        except HTTPException as exc:
            check("配置回退失败必须显式报警", exc.status_code == 500
                  and "回退失败" in exc.detail and settings.SLS_APP_LOG == before_app_log, exc.detail)

    unknown = bootstrap_api.ApplyConfigRequest(account_id=first.id, app_log=["p0", "not-discovered"])
    with patch.object(sls_discovery, "verify_target") as verify, \
            patch("app.services.env_store.set_keys") as env_write:
        try:
            bootstrap_api.apply_config(unknown, request, db, user)
            check("未发现的任意目标不能绕过候选确认", False)
        except HTTPException as exc:
            check("未发现的任意目标不能绕过候选确认", exc.status_code == 400
                  and not verify.called and not env_write.called, exc.detail)
    wrong_role = bootstrap_api.ApplyConfigRequest(account_id=first.id, cdn_target=["p0", "s0"])
    try:
        bootstrap_api.apply_config(wrong_role, request, db, user)
        check("候选角色与用途不匹配时拒绝绑定", False)
    except HTTPException as exc:
        check("候选角色与用途不匹配时拒绝绑定", exc.status_code == 400
              and "用途不符" in exc.detail, exc.detail)
    cross_account = bootstrap_api.ApplyConfigRequest(account_id=second.id, app_log=["p0", "s0"])
    try:
        bootstrap_api.apply_config(cross_account, request, db, user)
        check("其他账号候选不能跨账号采纳", False)
    except HTTPException as exc:
        check("其他账号候选不能跨账号采纳", exc.status_code == 400, exc.detail)

    anonymous = bootstrap_api.ApplyConfigRequest(app_log=["p0", "s0"])
    try:
        bootstrap_api.apply_config(anonymous, request, db, user)
        check("多账号省略 account_id 必须拒绝", False)
    except HTTPException as exc:
        check("多账号省略 account_id 必须拒绝", exc.status_code == 400 and "多个阿里云账号" in exc.detail)
    missing_chain = bootstrap_api.ApplyConfigRequest(
        account_id=first.id, slb_targets=[{"project": "p0", "logstore": "slb"}])
    try:
        bootstrap_api.apply_config(missing_chain, request, db, user)
        check("SLB 内外层不能由平台猜测", False)
    except HTTPException as exc:
        check("SLB 内外层不能由平台猜测", exc.status_code == 400 and "外层还是内层" in exc.detail)


def discovery_orchestration_cases(db):
    from app.services.cloud import sync_job

    account = db.query(CloudAccount).filter_by(name="discovery-a").one()
    provider = SimpleNamespace(test_account=lambda *_args: {"ok": True, "message": "mock ok"})
    sync_result = {"summary": {"failed_accounts": [], "accounts": 1}, "results": []}
    with patch.object(sync_job, "get_provider", return_value=provider), \
            patch.object(sync_job, "sync_accounts", return_value=sync_result), \
            patch.object(sync_job, "seed_rules_for_account", return_value=[]), \
            patch.object(sls_discovery, "discover", return_value={"status": "ready"}) as discover, \
            patch.object(sync_job.threading, "Thread") as thread:
        sync_job.bootstrap_account(account.id)
    check("新账号后台接入完成后自动发现 SLS",
          discover.call_count == 1 and discover.call_args.args == (account.id,)
          and discover.call_args.kwargs == {"background": True}, discover.call_args)
    check("新账号独立启动账单、指标和变更准备且测试不访问云",
          [call.kwargs.get("kwargs") for call in thread.call_args_list] == [
              {"account_id": account.id, "capabilities": ["cost"]},
              {"account_id": account.id, "capabilities": ["metrics"]},
              {"account_id": account.id, "capabilities": ["changes"]}]
          and thread.return_value.start.call_count == 3, thread.call_args_list)

    with patch.object(sync_job, "sync_accounts",
                      return_value={"summary": {"failed_accounts": []}, "results": []}), \
            patch.object(sync_job, "seed_rules_for_account", return_value=[]), \
            patch.object(sls_discovery, "discover") as discover:
        sync_job.auto_sync_all()
    check("定时同步不主动扫描旧账号 SLS", not discover.called)


def recovery_cases(db):
    account = CloudAccount(name="discovery-restart", vendor="aliyun", enabled=True, status="ok",
                           ak_cipher=encrypt_secret("restart-ak"),
                           secret_cipher=encrypt_secret("restart-sk"))
    db.add(account)
    db.commit()
    onboarding.record_sls_started(db, account)
    db.commit()
    db.expire_all()
    sls_discovery._ACTIVE.clear()  # 模拟新进程：持久 running 仍在，旧执行线程已经消失

    with patch.object(sls_discovery.sls, "list_projects_detailed") as cloud_listing:
        recovered = sls_discovery.reap_interrupted()
    run = onboarding.get_run(db, account.id)
    payload = sls_discovery.get_discovery(run)
    target_item = next(x for x in json.loads(run.items_json) if x["key"] == "sls_targets")
    check("重启收割中断发现且绝不自动访问云", recovered == 1 and not cloud_listing.called
          and payload["status"] == "failed" and "请重新发现" in payload["detail"], payload)
    check("中断后目标保持待确认而非伪装空目录", target_item["status"] == "pending"
          and "尚不能判断" in target_item["detail"] and run.status == "attention", target_item)
    recovered_logs = next(x for x in onboarding.capability_results(run, account) if x["capability"] == "logs")
    check("中断发现不会遗留 logs running", recovered_logs["execution"] == "failed"
          and recovered_logs["reason_code"] == "logs_discovery_incomplete", recovered_logs)
    with patch.object(sls_discovery.sls, "list_projects_detailed") as cloud_listing:
        check("中断收割幂等", sls_discovery.reap_interrupted() == 0 and not cloud_listing.called)

    with patch.object(sls_discovery.sls, "list_projects_detailed",
                      return_value={"projects": [], "complete": True, "error": "", "pages": 1}):
        result = sls_discovery.discover(account.id, background=False)
    db.expire_all()
    check("中断后可显式重新发现", result["status"] == "skipped"
          and sls_discovery.get_discovery(onboarding.get_run(db, account.id))["status"] == "skipped",
          result)


def main():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        project_pages()
        semantic_cases()
        confirmation_cases()
        legacy_adoption_case(db)
        discovery_cases(db)
        adoption_cases(db)
        discovery_orchestration_cases(db)
        recovery_cases(db)
    finally:
        db.close()
    print(f"\nSLS discovery mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
