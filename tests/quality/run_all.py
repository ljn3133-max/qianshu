#!/usr/bin/env python3
"""质量自治体系 · 一键验收：生成一页临时验收摘要。

用法：设置 QIANSHU_TEST_USERNAME 和 PASSWORD 环境变量后执行
      python3 tests/quality/run_all.py
"""
import os
import re
import subprocess
import sys
import time

Q = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(Q))
OUT = os.path.join(ROOT, "docs", "tmp")
sys.path.insert(0, Q)
from credentials import require_test_credentials  # noqa: E402

TEST_USERNAME, PWD = require_test_credentials()
os.environ["QIANSHU_TEST_USERNAME"] = TEST_USERNAME
os.environ["PASSWORD"] = PWD  # 子脚本统一继承一次性身份
READONLY = os.environ.get("QIANSHU_QUALITY_READONLY", "0") == "1"
stamp = time.strftime("%Y%m%d-%H%M")


def sh(cmd, timeout=300):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def main():
    os.makedirs(OUT, exist_ok=True)
    lines = []

    # ===== 1. 静态质量门 =====
    gate_result = sh(f"cd {ROOT} && python3 tests/quality/gate.py")
    fp_result = sh(f"cd {ROOT} && python3 tests/quality/structure_fingerprint.py")
    gate = gate_result.stdout
    fp = fp_result.stdout
    fp_ok = fp_result.returncode == 0 and "0 项问题" in fp
    gate_ok = gate_result.returncode == 0 and "全绿" in gate and fp_ok
    if not fp_ok:
        gate += "\n[结构指纹]\n" + fp
    lines.append(f"## 静态质量门：{'✓ 全绿' if gate_ok else '✗ 未过'}")
    if not gate_ok:
        lines += ["```", gate.strip(), "```"]

    # ===== 2. 功能回归 =====
    env = os.environ.copy()
    env["PASSWORD"] = PWD
    feat_result = subprocess.run(["python3", f"{Q}/features_test.py"], capture_output=True,
                                 text=True, env=env, timeout=300)
    feat = feat_result.stdout
    m = re.search(r"功能回归: (\d+)/(\d+) 通过", feat)
    mskip = re.search(r"跳过 (\d+) 项（不计通过）", feat)
    feat_ok = bool(feat_result.returncode == 0 and m and m.group(1) == m.group(2)
                   and int(m.group(2)) > 0 and mskip)
    lines.append(f"## 功能回归：{m.group(0) if m else '未跑通'}" + (" ✓" if feat_ok else " ⚠"))
    if READONLY:
        skipped_cases = [line.strip() for line in feat.splitlines()
                         if line.strip().startswith("- ") and "SKIP（只读模式" in line]
        skip_total = int(mskip.group(1)) if mskip else len(skipped_cases)
        lines.append(f"只读范围：仅执行 GET 用例；跳过共 {skip_total} 项，其中 {len(skipped_cases)} 项非 GET 写用例未执行，均不计为通过。")
        lines += skipped_cases
    if not feat_ok:
        fails = [line for line in feat.splitlines() if line.strip().startswith("✗")]
        detail = fails or ["```", feat[-500:], "```"]
        if feat_result.returncode != 0:
            detail = [f"功能回归子进程退出码：{feat_result.returncode}"] + detail
        lines += detail

    # ===== 2.5 只读接口自动冒烟 =====
    smoke_result = sh(f"cd {ROOT} && python3 tests/quality/api_smoke.py")
    smoke = smoke_result.stdout
    ms = re.search(r"接口冒烟: (\d+)/(\d+) 通过", smoke)
    smoke_ok = bool(smoke_result.returncode == 0 and ms and ms.group(1) == ms.group(2))
    lines.append(f"## 接口冒烟：{ms.group(0) if ms else '未跑通'}" + (" ✓" if smoke_ok else " ⚠"))
    if not smoke_ok:
        fails = [line for line in smoke.splitlines() if line.strip().startswith("✗")]
        lines += fails or ["```", smoke[-500:], "```"]

    # ===== 3. 浏览器全页面渲染 + 页面质量契约 =====
    br_result = sh(f"cd /tmp && python3 {Q}/browser_sweep.py", timeout=900)
    br = br_result.stdout
    m = re.search(r"页面渲染: (\d+)/(\d+) 正常", br)
    mq = re.search(r"页面质量: (\d+) 项问题", br)
    js_err = re.search(r"JS 错误: (.+)", br)
    br_ok = bool(br_result.returncode == 0 and m and m.group(1) == m.group(2)
                 and mq and mq.group(1) == "0" and js_err and "零" in js_err.group(1))
    lines.append(f"## 页面渲染：{m.group(0) if m else '未跑通'}｜"
                 f"{mq.group(0) if mq else '质量未跑通'}｜{js_err.group(0) if js_err else ''}"
                 + (" ✓" if br_ok else " ⚠"))
    if not br_ok:
        bad = [line for line in br.splitlines() if "[质量]" in line or line.strip().startswith("✗")]
        lines += bad or ["```", br[-800:], "```"]

    # ===== 3.5 视觉基线对比：独立待复核，不冒充通过 =====
    vis_result = sh(f"cd {ROOT} && python3 tests/quality/visual_baseline.py", timeout=900)
    vis = vis_result.stdout
    m = re.search(r"视觉基线对比: (.+)", vis)
    vis_changed = [line for line in vis.splitlines() if line.strip().startswith("·")]
    vis_ok = bool(vis_result.returncode == 0 and m and "0 页有变化" in m.group(1))
    lines.append(f"## 视觉基线：{m.group(1) if m else '未跑通'}"
                 + (" ✓" if vis_ok else " ⚠ 待人工复核"))
    if READONLY:
        lines.append("实时 SLS 排除：logs 页面及 targets/projects/logstores/search/catalog GET 未执行；独立 mock 专项另行验收。")
    if vis_changed:
        lines.append("视觉变化页（像素变化超容差；新旧图在 docs/tmp/visual/，请人工确认）：")
        lines += vis_changed
        lines.append("确认无碍后可执行：python3 tests/quality/visual_baseline.py <口令> --solidify 重新固化")

    # ===== 汇总：任何子进程失败或视觉待复核都不能给出全绿结论 =====
    all_ok = gate_ok and feat_ok and smoke_ok and br_ok and vis_ok
    if all_ok:
        conclusion = "✅ 已执行检查通过——不替代产品任务与人工验收"
        if READONLY:
            conclusion += "；非 GET 写用例未执行"
    else:
        conclusion = "⚠ 有异常或待复核项——请看下方展开项"
    summary = [
        f"# 千枢验收摘要 · {stamp}",
        "",
        f"## 结论：{conclusion}",
        "",
        f"验收范围：{'本机只读；真实云同步、外部集群及其他写操作不在本轮验收范围。' if READONLY else '完整功能回归（按 features.yaml 执行）；仍不替代产品任务与人工验收。'}",
        "",
    ]
    report = "\n".join(summary + lines) + "\n"
    path = os.path.join(OUT, f"验收摘要-{stamp}.md")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(report)
    print(report)
    print(f"[摘要已存] {path}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
