#!/usr/bin/env python3
"""功能回归 Agent（第二环）：features.yaml 声明式清单 → 内容级断言。

用法：python3 tests/quality/features_test.py
"""
import json
import os
import re
import subprocess
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from credentials import require_test_credentials  # noqa: E402

API = os.environ.get("QIANSHU_API", "http://127.0.0.1:8810")
USERNAME, PASSWORD = require_test_credentials()
os.environ["PASSWORD"] = PASSWORD
READONLY = os.environ.get("QIANSHU_QUALITY_READONLY", "0") == "1"
results = []
skipped = []


def dig(obj, path):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            return None
        cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is None:
            return None
    return cur


def evaluate(data, exp):
    """只执行白名单断言；未知写法必须失败，不能退化为非空即通过。"""
    length_match = re.fullmatch(r"(.+?)\s+长度\s*(=|>)\s*(\d+)", exp)
    if length_match:
        key, operator, expected = length_match.groups()
        value = dig(data, key.strip())
        actual = len(value) if isinstance(value, (list, dict, str)) else None
        if actual is None:
            return False, f"{key.strip()} 不存在或不是可计长度字段"
        ok = actual == int(expected) if operator == "=" else actual > int(expected)
        return ok, f"{key.strip()} 长度 {actual} {operator} {expected}?"

    if exp.endswith(" 存在"):
        key = exp[:-3].strip()
        value = dig(data, key)
        return value is not None, f"{key} {'在' if value is not None else '不在'}"

    compare_match = re.fullmatch(r"(.+?)\s*>\s*(-?\d+(?:\.\d+)?)", exp)
    if compare_match:
        key, expected = compare_match.groups()
        key = key.strip()
        value = dig(data, key)
        value = len(value) if isinstance(value, (list, dict)) else value
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > float(expected)
        return ok, f"{key}={value} > {expected}?"

    if " 含 " in exp:
        key, frag = [x.strip() for x in exp.split(" 含 ", 1)]
        value = dig(data, key)
        ok = value is not None and frag in str(value)
        return ok, f"{key} 含 {frag}: {value is not None and frag in str(value)}"

    if exp == "列表":
        ok = isinstance(data, list)
        return ok, f"返回{'是' if ok else '不是'}列表（空列表也算过）"

    if exp == "存在":
        return bool(data), "整体响应非空"

    if exp == "ok":
        ok = isinstance(data, dict) and data.get("ok") is True
        return ok, "ok 字段必须为 true"

    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", exp):
        value = dig(data, exp)
        return value is not None, f"{exp} {'在' if value is not None else '不在'}"

    return False, f"未知断言: {exp}"


def decode_curl_response(completed):
    if completed.returncode != 0:
        raise ValueError(f"curl 退出码 {completed.returncode}")
    body, separator, status_text = completed.stdout.rpartition("\n")
    if not separator:
        raise ValueError("响应缺少 HTTP 状态")
    try:
        status = int(status_text.strip())
        data = json.loads(body)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"响应非 JSON 或 HTTP 状态无效: {completed.stdout[:60]}") from exc
    return status, data


def run_case(token, case):
    spec = case["api"]
    if "|" in spec:
        path, body = spec.split("|", 1)
    else:
        path, body = spec, None
    method, path = path.split(" ", 1)
    if READONLY and method != "GET":
        skipped.append((case["name"], "只读模式，未执行非 GET 用例"))
        return
    cmd = ["curl", "-s", "-m", "30", "-X", method, f"{API}/api{path}",
           "-H", f"Authorization: Bearer {token}", "-w", "\n%{http_code}"]
    if body:
        body = body.replace("{PASSWORD}", os.environ["PASSWORD"])
        body = body.replace("{USERNAME}", USERNAME)
        if "__EXISTING" in body:
            # 重复接入用例：用库中已存在的集群名/地址
            existing_cmd = ["curl", "-s", f"{API}/api/k8s/clusters",
                            "-H", f"Authorization: Bearer {token}", "-w", "\n%{http_code}"]
            try:
                status, existing = decode_curl_response(subprocess.run(
                    existing_cmd, capture_output=True, text=True))
                if not 200 <= status < 300:
                    raise ValueError(f"读取既有集群 HTTP {status}")
                ex = existing[0]
                body = body.replace("__EXISTING__", ex["name"]).replace("__EXISTING_URL__", ex["api_url"])
            except (ValueError, KeyError, IndexError, TypeError):
                results.append((False, case["name"], "无法读取重复接入用例所需的既有集群"))
                return
        cmd += ["-H", "Content-Type: application/json", "-d", body]
    try:
        status, data = decode_curl_response(subprocess.run(cmd, capture_output=True, text=True))
    except (ValueError, subprocess.SubprocessError) as exc:
        results.append((False, case["name"], str(exc)))
        return

    if 200 <= status < 300 and isinstance(data, dict) and "未接入" in str(data.get("detail", "")):
        skipped.append((case["name"], "未接入集群，不计通过"))
        return
    # "拦截"断言必须同时满足 4xx 和可读 detail。
    if case.get("expect") == "拦截":
        ok = (400 <= status < 500 and isinstance(data, dict)
              and len(str(data.get("detail", ""))) > 5)
        why = (f"HTTP {status}，拦截提示: {str(data.get('detail', ''))[:40]}"
               if ok else f"未按契约拦截（HTTP {status}）")
        results.append((ok, case["name"], why))
        return
    if not 200 <= status < 300:
        detail = data.get("detail", "") if isinstance(data, dict) else ""
        results.append((False, case["name"], f"HTTP {status}: {str(detail)[:60]}"))
        return

    try:
        ok, why = evaluate(data, case["expect"])
    except Exception as exc:
        ok, why = False, f"断言异常: {exc}"
    results.append((ok, case["name"], why))


def main():
    login_cmd = ["curl", "-s", "-X", "POST", f"{API}/api/auth/login",
                 "-H", "Content-Type: application/json", "-w", "\n%{http_code}",
                 "-d", json.dumps({"username": USERNAME,
                                   "password": os.environ["PASSWORD"]})]
    try:
        login_status, login_data = decode_curl_response(subprocess.run(
            login_cmd, capture_output=True, text=True))
    except (ValueError, subprocess.SubprocessError) as exc:
        print(f"✗ 登录请求失败: {exc}")
        return 1
    token = login_data.get("token", "") if 200 <= login_status < 300 and isinstance(login_data, dict) else ""
    if not token:
        print(f"✗ 登录失败（HTTP {login_status}），功能回归无法进行")
        return 1
    import glob
    domains = []
    for filename in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "features*.yaml"))):
        with open(filename, encoding="utf-8") as stream:
            domains += yaml.safe_load(stream)["domains"]
    for domain in domains:
        for case in domain["cases"]:
            run_case(token, case)
    passed = sum(1 for ok, _, _ in results if ok)
    print(f"== 功能回归: {passed}/{len(results)} 通过；跳过 {len(skipped)} 项（不计通过） ==")
    for ok, name, why in results:
        print(f"  {'✓' if ok else '✗'} {name}  {why}")
    for name, reason in skipped:
        print(f"  - {name}  SKIP（{reason}）")
    return 0 if results and passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
