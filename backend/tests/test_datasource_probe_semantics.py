#!/usr/bin/env python3
"""探测证据语义回归：鉴权失败/404/无系统信息不得算已连通；零网络。"""
import json
import os
import sys

try:  # 独立脚本（tests/ 在 sys.path[0]）与 pytest 包导入两种方式
    from tests.pg_test_env import test_database_url
except ImportError:
    from pg_test_env import test_database_url
os.environ["DATABASE_URL"] = test_database_url("memory")
os.environ["MASTER_KEY"] = "cd" * 32
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import datasource as ds

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"  [{'ok' if condition else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail and not condition else ""))


class Response:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode()
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


original_get = ds._get
try:
    responses = []
    ds._get = lambda *args, **kwargs: responses.pop(0)

    responses.append(Response(401, "missing authorization header",
                              {"WWW-Authenticate": "Basic"}))
    r = ds._probe_graylog({"name": "g", "url": "http://graylog.invalid"})
    check("Graylog 401=未验证而非已连通", r["ok"] is None and "鉴权未通过" in r["detail"], str(r))

    responses.append(Response(401, ""))
    r = ds._probe_graylog({"name": "g", "url": "http://graylog.invalid"})
    check("Graylog 401 无响应体=未验证", r["ok"] is None, str(r))

    responses.append(Response(200, {"cluster_id": "c1", "tagline": "Manage your logs"}))
    r = ds._probe_graylog({"name": "g", "url": "http://graylog.invalid"})
    check("Graylog 真实系统信息=已连通", r["ok"] is True, str(r))

    for status in (401, 403, 404, 500):
        responses.append(Response(status, {"version": "1.2.3"}))
        r = ds._probe_http_custom({"name": "h", "url": "http://custom.invalid"})
        check(f"自定义 HTTP {status}=失败（响应版本字段不掩盖失败）",
              r["ok"] is False and str(status) in r["detail"], str(r))

    responses.append(Response(200, {"message": "ok"}))
    r = ds._probe_http_custom({"name": "h", "url": "http://custom.invalid"})
    check("自定义 HTTP 200 无系统信息=未验证", r["ok"] is None, str(r))

    responses.append(Response(200, {"version": "1.2.3"}))
    r = ds._probe_http_custom({"name": "h", "url": "http://custom.invalid"})
    check("自定义 HTTP 200 有版本=已连通", r["ok"] is True and "1.2.3" in r["detail"], str(r))

    responses.extend(Response(200, {"message": "ok"}) for _ in range(3))
    r = ds._probe_filebeat({"name": "f", "url": "http://filebeat.invalid"})
    check("Filebeat 任意 JSON=未验证", r["ok"] is None and "产品标识" in r["detail"], str(r))

    responses.extend(Response(200, {"host": {"os": {"version": "1.2.3"}}}) for _ in range(3))
    r = ds._probe_filebeat({"name": "f", "url": "http://filebeat.invalid"})
    check("Filebeat 主机 OS 嵌套版本=未验证", r["ok"] is None, str(r))

    responses.append(Response(200, {"beat": "filebeat", "hostname": "lab", "version": "8.15.0"}))
    r = ds._probe_filebeat({"name": "f", "url": "http://filebeat.invalid"})
    check("Filebeat 产品标识=已连通", r["ok"] is True and "8.15.0" in r["detail"], str(r))

    responses.extend([Response(404, ""), Response(404, ""), Response(200, {"message": "ok"})])
    r = ds._probe_influx({"name": "i", "url": "http://influx.invalid"})
    check("InfluxDB 任意 health JSON=未验证", r["ok"] is None and "产品标识" in r["detail"], str(r))

    responses.extend([Response(404, ""), Response(404, ""),
                      Response(200, {"name": "influxdb", "status": "pass", "version": "2.7.10"})])
    r = ds._probe_influx({"name": "i", "url": "http://influx.invalid"})
    check("InfluxDB 产品标识=已连通", r["ok"] is True and "2.7.10" in r["detail"], str(r))
finally:
    ds._get = original_get

print(f"\n数据源探测语义测试: {len(PASS)}/{len(PASS) + len(FAIL)} 通过")
if FAIL:
    raise SystemExit(1)
