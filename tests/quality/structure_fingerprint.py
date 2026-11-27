#!/usr/bin/env python3
"""页面结构指纹：class 样式覆盖 + 结构完整性检查。

文本量和关键词审查无法发现组件拆分后的样式丢失；指纹要求每页 class 均有样式来源
（组件 scoped + 全局 css），且 script/template/style 三块完整。
"""
import glob
import os
import re

# 仓库根按脚本位置推导（CI checkout 路径任意）；QIANSHU_ROOT 可显式覆盖。
ROOT = os.environ.get("QIANSHU_ROOT") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FE = os.path.join(ROOT, "frontend", "src")


def style_index():
    styled = set()
    for f in glob.glob(f"{FE}/styles/*.css"):
        styled |= set(re.findall(r"\.([\w-]+)", open(f).read()))
    return styled


def main():
    global_styled = style_index()
    problems = []
    for f in sorted(glob.glob(f"{FE}/views/**/*.vue", recursive=True)) + sorted(glob.glob(f"{FE}/layout/*.vue")):
        s = open(f).read()
        has_script = "<script" in s
        has_template = "<template>" in s
        m = re.search(r"<style[\s\S]*</style>", s)
        styled = set(re.findall(r"\.([\w-]+)", m.group(0))) if m else set()
        # 类名引用（排除 :class 表达式——只查静态 class="...")
        used = set()
        for mm in re.finditer(r'(?<!:)class="([^"]+)"', s):
            used |= set(mm.group(1).split())
        # el-* 为 Element Plus 内置类（样式由主题提供），不算缺
        missing = {c for c in used - styled - global_styled
                   if re.fullmatch(r"[a-zA-Z][\w-]*", c) and not c.startswith("el-")}
        if missing:
            problems.append((os.path.basename(f), "缺样式", sorted(missing)[:5]))
        if has_script and has_template and not m and used:
            problems.append((os.path.basename(f), "缺 style 块", ""))
    for p in problems:
        print(f"  ✗ {p[0]}: {p[1]} {p[2]}")
    print(f"结构指纹: {len(problems)} 项问题")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
