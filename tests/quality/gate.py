#!/usr/bin/env python3
"""静态质量门（质量自治体系第一环）：改动必过，红灯禁止交付。

七项检查：
  A API 断链    前端调用 vs 后端路由
  B 界面绑定    模板引用 vs script 定义 / 作用域 / v-model / 可选链赋值
  C 模型字段    Model.field 引用 vs 模型定义（"拍脑袋字段名"克星）
  D 编译        vite transform 全前端模块
  E 路由覆盖    每个业务路由必须进 pages.py（走查/视觉基线不漏页）
  F 跳转目标    router.push 的字面量路径必须真有路由
  G 审计动作    audit.record(action=...) 必须在 ACTION_LABELS 登记

用法：python3 tests/quality/gate.py   （后端 8810 / 前端 5273 需在跑）
"""
import ast
import glob
import json
import os
import re
import subprocess
import sys

# 仓库根按脚本位置推导（CI checkout 路径任意）；QIANSHU_ROOT 可显式覆盖。
ROOT = os.environ.get("QIANSHU_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FE = os.path.join(ROOT, "frontend")
BE = os.path.join(ROOT, "backend")
API = "http://127.0.0.1:8810"
VITE = "http://127.0.0.1:5273"
# 端点可被环境变量覆盖（开源/CI 可移植）：GATE_API 保留旧名兼容
API = os.environ.get("GATE_API", os.environ.get("QIANSHU_API", API))
VITE = os.environ.get("QIANSHU_SITE", VITE)
fails = []


def check_api_links():
    """A. 前端调用 vs 后端路由"""
    out = subprocess.run(["curl", "-s", f"{API}/api/openapi.json"], capture_output=True, text=True).stdout
    paths = {p.replace("/api", "", 1) for p in json.loads(out)["paths"].keys()}
    calls = {}
    for f in glob.glob(f"{FE}/src/api/*.js") + glob.glob(f"{FE}/src/views/**/*.vue", recursive=True) + glob.glob(f"{FE}/src/layout/*.vue"):
        s = open(f).read()
        for m in re.finditer(r"http\.(get|post|put|delete)\(\s*[`'\"]([^`'\"]+)", s):
            if m.group(2).startswith("/"):
                calls.setdefault(m.group(2), set()).add(os.path.basename(f))
        for m in re.finditer(r"http\.(get|post|put|delete)\(\s*`([^`]+)`", s):
            calls.setdefault(re.sub(r"\$\{[^}]+\}", "*", m.group(2)), set()).add(os.path.basename(f))
    def matches(call, route):
        cp, rp = call.rstrip("/").split("/"), route.rstrip("/").split("/")
        return len(cp) == len(rp) and all(a == "*" or b.startswith("{") or a == b for a, b in zip(cp, rp))
    missing = {c: s for c, s in calls.items() if not any(matches(c, r) for r in paths)}
    for c, src in missing.items():
        fails.append(f"[A 断链] {c} ← {src}")
    return len(calls), len(missing)


def check_bindings():
    """B. 模板绑定：未定义 / 作用域 / v-model / 可选链赋值"""
    n_files = 0
    for f in glob.glob(f"{FE}/src/views/**/*.vue", recursive=True) + glob.glob(f"{FE}/src/layout/*.vue"):
        s = open(f).read()
        tm = re.search(r"<template>(.*)</template>", s, re.S)
        sm = re.search(r"<script setup>(.*)</script>", s, re.S)
        if not tm or not sm:
            continue
        n_files += 1
        tpl, scr = tm.group(1), sm.group(1)
        # 可选链赋值（非法实验语法）
        for m in re.finditer(r"\w+\?\.(\w+)\s*=(?!=)", tpl):
            fails.append(f"[B 绑定] {os.path.basename(f)}: 可选链赋值 ?.({m.group(1)}=)")
        # v-model 绑 props（非法）
        if re.search(r'v-model[^=]*="props\.', tpl):
            fails.append(f"[B 绑定] {os.path.basename(f)}: v-model 绑 props")
        # v-for 粘连（XinY）
        for m in re.finditer(r'v-for="([^"]*?[a-zA-Z])in(?=[\(\[]|props\.|row\.)', tpl):
            fails.append(f"[B 绑定] {os.path.basename(f)}: v-for 粘连 `{m.group(1)}in…`")
        # 顶层引用未定义
        defined = set()
        for m in re.finditer(r"^\s*(?:async\s+)?function\s+(\w+)", scr, re.M):
            defined.add(m.group(1))
        for m in re.finditer(r"^\s*(?:const|let|var)\s+(\w+)", scr, re.M):
            defined.add(m.group(1))
        for m in re.finditer(r"^\s*import\s+\{([^}]+)\}", scr, re.M):
            defined |= {x.strip().split(" as ")[-1] for x in m.group(1).split(",") if x.strip()}
        for m in re.finditer(r"^\s*import\s+(\w+)", scr, re.M):
            defined.add(m.group(1))
        for m in re.finditer(r'v-for="([^"]+)"', tpl):
            mm = re.search(r'\(([^)]+)\)|(\w+)\s+in\b', m.group(1))
            if mm:
                defined |= {n.strip() for n in (mm.group(1) or mm.group(2)).split(",")}
        for m in re.finditer(r"@\w+(?:\.\w+)*=\"(\w+)\s*\(", tpl):
            if m.group(1) not in defined:
                fails.append(f"[B 绑定] {os.path.basename(f)}: 事件函数未定义 {m.group(1)}")
    return n_files


def check_model_fields():
    """C. Model.field 引用 vs 模型定义（字段名拍脑袋克星）"""
    models = {}
    for f in glob.glob(f"{BE}/app/models/*.py"):
        s = open(f).read()
        fields = set(re.findall(r"^\s+(\w+): Mapped", s, re.M))
        # 经典声明风格 x = Column(...)：agent_session/ai_scenario 等契约模型也使用
        # 此风格，必须与 Mapped 声明一并识别，避免漏检或误报 C 字段。
        fields |= set(re.findall(r"^\s+(\w+)\s*=\s*Column\(", s, re.M))
        for m in re.finditer(r"^class (\w+)\(", s, re.M):
            models[m.group(1)] = fields
    n_refs = 0
    for f in glob.glob(f"{BE}/app/**/*.py", recursive=True):
        s = open(f).read()
        if "/models/" in f:
            continue
        try:
            tree = ast.parse(s)
        except SyntaxError:
            continue
        # 模型类名集合
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                cls = node.value.id
                # 只查首字母大写的类名引用（Model.field 形态）
                if cls and cls[0].isupper() and cls in models:
                    n_refs += 1
                    if node.attr not in models[cls] and not node.attr.startswith("_") and node.attr not in (
                            "query", "metadata", "id"):
                        fails.append(f"[C 字段] {os.path.basename(f)}: {cls}.{node.attr} 不在模型定义")
    return len(models), n_refs


def check_compile():
    """D. vite 编译全前端模块"""
    bad = 0
    files = glob.glob(f"{FE}/src/views/**/*.vue", recursive=True) + \
           glob.glob(f"{FE}/src/layout/*.vue") + glob.glob(f"{FE}/src/api/*.js")
    for f in files:
        rel = f.split("/src/")[1]
        out = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                              f"{VITE}/src/{rel}"], capture_output=True, text=True).stdout
        if out != "200":
            bad += 1
            fails.append(f"[D 编译] src/{rel}: HTTP {out}")
    return len(files), bad


def check_page_coverage():
    """E. 路由覆盖：每个业务路由必须进页面清单。

    走查和视觉基线共用 pages.py；新加路由若未进清单，质量门直接红灯。
    """
    from pages import PAGES, PUBLIC_PAGES, ROUTE_EXEMPT

    router = open(f"{FE}/src/router/index.js").read()
    routes = set(re.findall(r"\{\s*path:\s*'([a-z0-9][a-z0-9/-]*)'", router))
    exempt = set(ROUTE_EXEMPT) | set(PUBLIC_PAGES)
    missing = sorted(routes - set(PAGES) - exempt)
    stale = sorted(set(PAGES) - routes)
    for p in missing:
        fails.append(f"[E 路由覆盖] /{p} 未纳入 pages.py 页面清单（走查/视觉基线会漏）")
    for p in stale:
        fails.append(f"[E 路由覆盖] pages.py 的 /{p} 已不存在于路由")
    return len(routes), len(missing) + len(stale)


def check_route_targets():
    """F. 页面跳转目标存在：按钮/跳转写的路径必须真有路由。

    功能变多后"点了没反应/掉 404"最常见来源是改名后漏改跳转字符串；
    静态扫 router.push / router.replace / go('...') 的字面量路径。
    """
    router = open(f"{FE}/src/router/index.js").read()
    routes = {"/" + p for p in re.findall(r"\{\s*path:\s*'([a-z0-9][a-z0-9/-]*)'", router)}
    routes |= {"/", "/login", "/change-password"}
    targets = set()
    for f in glob.glob(f"{FE}/src/views/**/*.vue", recursive=True) + \
             glob.glob(f"{FE}/src/layout/*.vue") + glob.glob(f"{FE}/src/components/*.vue"):
        s = open(f).read()
        for m in re.finditer(
                r"""(?:router\.(?:push|replace)\s*\(\s*(?:path:\s*)?|\bgo\s*\(\s*)['"]([^'"]+)['"]""", s):
            p = m.group(1).split("?")[0].split("#")[0]
            if p.startswith("/") and re.fullmatch(r"/[a-z0-9/-]*", p):
                targets.add(p)
    missing = sorted(targets - routes)
    for p in missing:
        fails.append(f"[F 跳转] {p} 无对应路由（改名漏改或拼写错误）")
    return len(targets), len(missing)


def check_audit_actions():
    """G. 审计动作码登记：audit.record(action=...) 的字面量必须在
    ACTION_LABELS 登记，否则审计页直接显示英文动作码（运维看不懂做了什么）。

    新增动作未登记时质量门必须失败，不能等到页面暴露英文码后再补。
    """
    src = open(f"{BE}/app/services/audit.py").read()
    labels = {}
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == "ACTION_LABELS"):
            labels = ast.literal_eval(node.value)
    n_calls = 0
    missing = {}
    for f in glob.glob(f"{BE}/app/**/*.py", recursive=True):
        try:
            tree = ast.parse(open(f).read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if name != "record":
                continue
            for kw in node.keywords:
                if kw.arg == "action" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    n_calls += 1
                    a = kw.value.value
                    if a not in labels:
                        missing.setdefault(a, os.path.basename(f))
    for a, srcfile in sorted(missing.items()):
        fails.append(f"[G 审计] action={a} ← {srcfile} 未在 ACTION_LABELS 登记")
    return n_calls, len(missing)


def main():
    print("== 静态质量门 ==")
    a = check_api_links()
    b = check_bindings()
    c = check_model_fields()
    d = check_compile()
    e = check_page_coverage()
    f = check_route_targets()
    g = check_audit_actions()
    print(f"  A 断链: {a[0]} 调用 / {a[1]} 断链")
    print(f"  B 绑定: {b} 文件检查")
    print(f"  C 字段: {c[0]} 模型 / {c[1]} 引用")
    print(f"  D 编译: {d[0]} 文件 / {d[1]} 失败")
    print(f"  E 路由覆盖: {e[0]} 路由 / {e[1]} 未覆盖")
    print(f"  F 跳转目标: {f[0]} 目标 / {f[1]} 无路由")
    print(f"  G 审计动作: {g[0]} 调用 / {g[1]} 未登记")
    if fails:
        print(f"\n✗ 质量门未过（{len(fails)} 项）:")
        for f in fails[:30]:
            print("  ", f)
        sys.exit(1)
    print("\n✓ 质量门全绿")


if __name__ == "__main__":
    main()
