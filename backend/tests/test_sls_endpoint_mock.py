#!/usr/bin/env python3
"""SLS 注册源 endpoint 传递测试；全部 SDK/凭证均 mock，零外部请求。"""
import json
import os
import sys
import tempfile
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="qianshu-sls-endpoint-")
try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("test")
os.environ["MASTER_KEY"] = "cd" * 32
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aliyun.log import LogClient
from app.collector import sls
from app.core.config import settings
from app.services import datasource, log_query

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail and not condition else ""))


check("HTTPS URL 归一化为 SDK host",
      sls.normalize_endpoint("https://cn-beijing.log.aliyuncs.com/") ==
      "cn-beijing.log.aliyuncs.com")
check("host/path 归一化为 SDK host",
      sls.normalize_endpoint("cn-shanghai.log.aliyuncs.com/ignored") ==
      "cn-shanghai.log.aliyuncs.com")

constructed = []
search_queries = []
sql_queries = []
raw_rows = []
original_log_client = LogClient


class FakeClient:
    def __init__(self, endpoint, ak, sk):
        constructed.append(endpoint)

    def list_project(self, *args):
        return SimpleNamespace(projects=[], get_projects=lambda: [])

    def list_logstores(self, request):
        return SimpleNamespace(get_logstores=lambda: [])

    def get_logs(self, request):
        search_queries.append(request.get_query())
        return SimpleNamespace(get_logs=lambda: list(raw_rows), get_count=lambda: len(raw_rows))

    def execute_logstore_sql(self, project, logstore, start, end, query, power_sql):
        sql_queries.append(query)
        return SimpleNamespace(get_logs=lambda: [], get_count=lambda: 0)


try:
    previous_app_log = settings.SLS_APP_LOG
    # aliyun.log.LogClient 是函数内导入，必须 patch 源模块而非本地引用。
    import aliyun.log
    aliyun.log.LogClient = FakeClient
    sls._client("test-ak", "test-sk", "https://cn-beijing.log.aliyuncs.com/")
    sls.list_projects("test-ak", "test-sk", endpoint="cn-shanghai.log.aliyuncs.com")
    sls.list_logstores("test-ak", "test-sk", "project", endpoint="cn-qingdao.log.aliyuncs.com")
    sls.query_logs("test-ak", "test-sk", "project", "store", "*",
                   start=1, end=2, endpoint="cn-shenzhen.log.aliyuncs.com")
    check("客户端/项目/Logstore/查询均使用各自 endpoint", constructed == [
        "cn-beijing.log.aliyuncs.com", "cn-shanghai.log.aliyuncs.com",
        "cn-qingdao.log.aliyuncs.com", "cn-shenzhen.log.aliyuncs.com",
    ], str(constructed))

    settings.SLS_APP_LOG = json.dumps(["project", "store", {
        "level_field": "severity", "app_field": "service", "message_field": "msg"}])
    raw_rows.append(SimpleNamespace(
        get_contents=lambda: {"severity": "ERROR", "service": "orders", "msg": "raw-not-persisted"},
        get_time=lambda: 123))
    result = sls.query_logs("test-ak", "test-sk", "project", "store",
                            'AppName: "LogLevel" and LogLevel: ERROR', start=1, end=2)
    check("应用检索使用真实字段且不替换引号内值",
          search_queries[-1] == 'service: "LogLevel" and severity: ERROR', search_queries[-1])
    check("原始检索结果补齐旧消费字段别名",
          result["logs"][0]["LogLevel"] == "ERROR" and result["logs"][0]["AppName"] == "orders"
          and result["logs"][0]["Content"] == "raw-not-persisted", result)
    discovered = sls.query_logs("test-ak", "test-sk", "project", "store", "*",
                                start=1, end=2, apply_field_mapping=False)
    check("重复发现读取未映射原样本，避免真实字段与别名歧义",
          "LogLevel" not in discovered["logs"][0] and "AppName" not in discovered["logs"][0]
          and search_queries[-1] == "*", discovered)
    raw_rows.clear()
    sls.query_logs("test-ak", "test-sk", "project", "store",
                   'AppName: "orders" and LogLevel: ERROR | select AppName, LogLevel, count(*) as cnt '
                   'group by AppName, LogLevel order by LogLevel limit 10', start=1, end=2)
    check("SQL 使用真实字段并保留 LogLevel/AppName 输出别名",
          sql_queries[-1] == 'service: "orders" and severity: ERROR | '
                             'select service as AppName, severity as LogLevel, count(*) as cnt '
                             'group by service, severity order by severity limit 10', sql_queries[-1])
    settings.SLS_APP_LOG = json.dumps(["project", "store", {
        "level_field": "severity", "app_field": "", "message_field": "msg"}])
    try:
        sls.query_logs("test-ak", "test-sk", "project", "store", "AppName: orders", start=1, end=2)
        check("缺少应用字段时明确停止语义分析", False)
    except sls.SLSQueryError as exc:
        check("缺少应用字段时明确停止语义分析", "AppName" in str(exc) and "原始检索" in str(exc), str(exc))
    sls.query_logs("test-ak", "test-sk", "project", "store", "*", start=1, end=2)
    check("缺少语义字段不阻断原始检索", search_queries[-1] == "*", search_queries[-1])
    sls.query_logs("test-ak", "test-sk", "project", "store", '"AppName"', start=1, end=2)
    check("引号内字段名只是原始文本，不触发语义拒绝",
          search_queries[-1] == '"AppName"', search_queries[-1])
    settings.SLS_APP_LOG = json.dumps(["project", "store"])
    sls.query_logs("test-ak", "test-sk", "project", "store",
                   "LogLevel: ERROR | select LogLevel, count(*) as cnt group by LogLevel limit 10",
                   start=1, end=2)
    check("旧两段配置保持原字段查询行为",
          sql_queries[-1] == "LogLevel: ERROR | select LogLevel, count(*) as cnt "
                             "group by LogLevel limit 10", sql_queries[-1])

    import app.collector.credential as credential
    original_resolve = credential.resolve_aliyun_credential
    original_sls_resolve = credential.resolve_sls_credential
    credential.resolve_aliyun_credential = lambda db: ("test-ak", "test-sk", "mock")
    credential.resolve_sls_credential = lambda db: ("test-ak", "test-sk", "mock")
    constructed.clear()
    result = datasource._probe_sls(
        {"name": "registered", "url": "https://cn-chengdu.log.aliyuncs.com"}, db=object())
    check("注册源探活使用注册地址而非杭州硬编码",
          constructed == ["cn-chengdu.log.aliyuncs.com"] and result["ok"] is True,
          str(result))

    calls = []
    original_query = sls.query_logs
    original_projects = sls.list_projects
    sls.query_logs = lambda *args, **kwargs: calls.append(("query", kwargs.get("endpoint"))) or {
        "logs": [], "is_sql": False, "count": 0, "has_more": False}
    sls.list_projects = lambda *args, **kwargs: calls.append(("targets", kwargs.get("endpoint"))) or []
    original_creds = log_query._aliyun_creds
    log_query._aliyun_creds = lambda db: ("test-ak", "test-sk")
    src = {"name": "registered", "type": "sls", "url": "https://cn-zhangjiakou.log.aliyuncs.com"}
    log_query.query(object(), src, "*", 1, 2, project="p", logstore="s")
    original_resolve_source = log_query.resolve_source
    log_query.resolve_source = lambda db, name="": src
    log_query.targets(object(), "registered")
    check("统一目标清单和日志查询均传递注册地址",
          calls == [("query", "https://cn-zhangjiakou.log.aliyuncs.com"),
                    ("targets", "https://cn-zhangjiakou.log.aliyuncs.com")], str(calls))
finally:
    aliyun.log.LogClient = original_log_client
    settings.SLS_APP_LOG = previous_app_log
    if "original_resolve" in locals():
        credential.resolve_aliyun_credential = original_resolve
        credential.resolve_sls_credential = original_sls_resolve
    if "original_query" in locals():
        sls.query_logs = original_query
        sls.list_projects = original_projects
        log_query._aliyun_creds = original_creds
        log_query.resolve_source = original_resolve_source

print(f"\nSLS endpoint mock 测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    raise SystemExit(1)
