#!/usr/bin/env python3
"""只读接口自动冒烟。

手写 features.yaml 难以覆盖全部 API 操作；这里从 /openapi.json 自动发现所有
**无路径参数的 GET** 接口，逐个真调用，保证：
  - 不 5xx、不超时、不 404/405；
  - 200 时返回可解析 JSON（或明确允许的文本下载）；
  - 4xx 业务拦截算通过（如"未接入集群"），422=接口需要查询参数（告警不拉红）。

只调用 GET，不执行 HTTP 写方法，不打印响应正文（防凭证泄露）。GET 仍可能实时访问外部云服务，常规本机只读模式必须按上方清单排除。
用法：python3 tests/quality/api_smoke.py [PASSWORD]
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from credentials import require_test_credentials  # noqa: E402

API = os.environ.get("QIANSHU_API", "http://127.0.0.1:8810")
USERNAME, PWD = require_test_credentials()

# 明确跳过：验证码/滑块是交互式挑战，GET 本身没有业务意义
SKIP = {
    "/api/auth/captcha",
    "/api/auth/slider/challenge",
}
# 常规只读总门只能使用本机数据；这些 GET 会直连云 SDK 枚举目录或查询日志，
# 必须留给单独授权的专用云账号验收，不能因“GET 无副作用”自动触发。
if os.environ.get("QIANSHU_QUALITY_READONLY") == "1":
    SKIP.update({
        "/api/bootstrap/sls-catalog",
        "/api/logs/targets",
        "/api/logs/projects",
        "/api/logs/logstores",
        "/api/logs/search",
    })
# 允许 200 非 JSON 的文本/下载型只读接口
TEXT_OK = {
    "/api/bootstrap/agent-script",
    "/api/inspections/report",
    "/api/settings/sdk-report",
    # CSV 下载：200 返回 text/csv 属预期
    "/api/cost/daily/export",
    "/api/assets/export",
    "/api/alerts/records/export",
}
# 已知的"真在做事"只读发现接口：需要逐项目调云 API，不能按普通 GET 的 25 秒判死。
# sls-catalog 即使并发查询也可能超过普通阈值，仍给宽限；
# 超时仍判失败——只是阈值贴合真实工作量，不是放水。
SLOW_TIMEOUT = {
    "/api/bootstrap/sls-catalog": 60,
    # CSV 导出上限 2 万行 + 归属富化，按真实工作量放宽
    "/api/cost/daily/export": 60,
    "/api/assets/export": 60,
    "/api/alerts/records/export": 60,
}


def curl_get(path, token, timeout=25):
    t0 = time.time()
    fd, tmp = tempfile.mkstemp(prefix="qsmoke_")
    os.close(fd)
    try:
        p = subprocess.run(
            ["curl", "-s", "-m", str(timeout), "-o", tmp, "-w", "%{http_code}",
             API + path, "-H", f"Authorization: Bearer {token}"],
            capture_output=True, text=True)
        code = p.stdout.strip()
        raw = b""
        try:
            raw = open(tmp, "rb").read()
        except Exception:
            pass
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    body = raw[:2000].decode("utf-8", "replace")
    # 完整正文单独返回给 JSON 解析：截断的 2000 字节会让所有大响应误判"非 JSON"
    return code, body, round(time.time() - t0, 2), raw


def _check_once(path, token):
    code, body, cost, raw = curl_get(path, token, timeout=SLOW_TIMEOUT.get(path, 25))
    if not code or code == "000":
        return path, "FAIL", "超时/连接失败", cost
    if code.startswith("5"):
        return path, "FAIL", f"HTTP {code} {body[:80]}", cost
    if code in ("401", "404", "405"):
        return path, "FAIL", f"HTTP {code}", cost
    if code == "422":
        return path, "WARN", "需要查询参数", cost
    if code.startswith("4"):
        return path, "OK", f"业务拦截 {code}", cost
    if code == "200":
        if path in TEXT_OK:
            return path, "OK", "文本/下载", cost
        try:
            json.loads(raw)
        except Exception:
            return path, "FAIL", "200 但非 JSON", cost
        return path, "OK", "", cost
    return path, "FAIL", f"HTTP {code}", cost


# 需要查询参数的只读接口：从本系统已有的只读列表接口取一个真实样本，
# 补上参数再请求一次。取不到样本就保留 WARN，不伪造。
SAMPLE_SOURCES = {
    "/api/logs/logstores": ("/api/logs/projects", lambda d: {"project": (d.get("projects") or [""])[0]}),
    "/api/cost/cdn-urls": ("/api/cost/cdn-domains",
                           lambda d: {"domain": ((d.get("domains") or [{}])[0].get("domain") or "")}),
    "/api/dba/slowlogs": ("/api/dba/instances",
                          lambda d: {"instance_id": ((d.get("instances") or [{}])[0].get("instance_id") or "")}),
    "/api/dba/slowlog-records": ("/api/dba/instances",
                                 lambda d: {"instance_id": ((d.get("instances") or [{}])[0].get("instance_id") or "")}),
    # 经典洞察仅 MySQL 支持：挑 MySQL 实例（未开启会返回 400 业务拦截，仍算通过）
    "/api/dba/insight/records": ("/api/dba/instances",
                                 lambda d: {"instance_id": next(
                                     (i.get("instance_id") for i in (d.get("instances") or [])
                                      if str(i.get("engine") or "").lower().startswith("mysql")), "")}),
    "/api/dba/space": ("/api/dba/instances",
                       lambda d: {"instance_id": ((d.get("instances") or [{}])[0].get("instance_id") or "")}),
    "/api/dba/backups": ("/api/dba/instances",
                         lambda d: {"instance_id": ((d.get("instances") or [{}])[0].get("instance_id") or "")}),
    "/api/dba/redis/keys": ("/api/dba/redis/instances",
                            lambda d: {"instance_id": ((d.get("instances") or [{}])[0].get("instance_id") or "")}),
    "/api/profiles/detail": ("/api/profiles",
                             lambda d: {"profile_key": ((d.get("items") or [{}])[0].get("profile_key") or "")}),
    "/api/profiles/objects/chain": ("/api/profiles/objects",
                                    lambda d: {"object": ((d.get("items") or [{}])[0].get("object_key") or "")}),
    "/api/ownership/preview": ("/api/assets?page_size=1",
                               lambda d: {"key_type": "instance",
                                          "key_value": ((d.get("items") or [{}])[0].get("instance_id") or "")}),
    "/api/synth/results": ("/api/synth/checks",
                           lambda d: {"check_id": (d[0].get("id") if isinstance(d, list) and d else "")}),
    # 升级预检只做版本格式与 k8s 只读探测，固定合法版本号即可
    "/api/k8mgmt/upgrade/precheck": ("/api/k8s/clusters",
                                     lambda d: {"target": "1.29",
                                                "cluster_id": (d[0].get("id") if isinstance(d, list) and d else 0)}),
}
_sample_cache = {}


def _get_json(path, token):
    code, body, _cost, raw = curl_get(path, token)
    if code != "200":
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _sample_params(path, token):
    src = SAMPLE_SOURCES.get(path)
    if not src:
        return None
    endpoint, picker = src
    if endpoint not in _sample_cache:
        _sample_cache[endpoint] = _get_json(endpoint, token)
    data = _sample_cache[endpoint]
    if not isinstance(data, (dict, list)):
        return None
    params = picker(data)
    return {k: v for k, v in params.items() if v}


def check(path, token):
    """失败重试一次：后端偶发首请求 404，避免冒烟误报。"""
    r = _check_once(path, token)
    if r[1] == "FAIL":
        time.sleep(0.4)
        r2 = _check_once(path, token)
        if r2[1] != "FAIL":
            return path, "OK", "重试后通过", r2[3]
    if r[1] == "WARN":
        params = _sample_params(path, token)
        if params:
            qs = urllib.parse.urlencode(params)
            r3 = _check_once(f"{path}?{qs}", token)
            if r3[1] == "OK":
                return path, "OK", "带样本参数", r3[3]
            return path, r3[1], r3[2], r3[3]
    return r


def main():
    login = subprocess.run(
        ["curl", "-s", "-X", "POST", f"{API}/api/auth/login",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"username": USERNAME, "password": PWD})],
        capture_output=True, text=True).stdout
    token = json.loads(login).get("token", "")
    if not token:
        print("接口冒烟: 登录失败，无法进行")
        sys.exit(1)

    spec = json.loads(subprocess.run(
        ["curl", "-s", "-m", "20", f"{API}/api/openapi.json"],
        capture_output=True, text=True).stdout)
    all_get = [p for p, ops in spec["paths"].items() if "get" in ops]
    targets = [p for p in all_get if "{" not in p and p not in SKIP]

    results = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(lambda p: check(p, token), targets):
            results.append(r)

    ok = [r for r in results if r[1] == "OK"]
    warn = [r for r in results if r[1] == "WARN"]
    fail = [r for r in results if r[1] == "FAIL"]
    slow = sorted([r for r in results if r[3] > 5], key=lambda x: -x[3])[:5]

    print(f"接口冒烟: {len(ok)}/{len(results)} 通过"
          f"（{len(warn)} 项需查询参数不拉红，{len(fail)} 失败）"
          f"｜只读 GET 覆盖 {len(targets)}/{len(all_get)}")
    for p, _, why, cost in fail:
        print(f"  ✗ {p}: {why} ({cost}s)")
    for p, _, why, cost in warn[:8]:
        print(f"  · {p}: {why}")
    if slow:
        print("  慢接口: " + "，".join(f"{p} {c}s" for p, _, _, c in slow))
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
