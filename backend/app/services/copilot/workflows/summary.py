"""工作流结构化详情渲染：内置/自定义统一出步骤视图。

只读消费 WORKFLOWS spec 与 workflow_defs 定义——**不动内置本体（access/analysis/
improve/onboard.py）与引擎**，也不动 loop/change_tools：
  - builtin_step_views(spec)        内置步骤序列视图：idx/label/kind/tools/失败策略
  - builtin_detail(key, spec, lang) 内置只读详情（params_schema 带敏感标记）
  - derive_custom_steps(spec)       内置 -> 自定义步骤模板（「另存为自定义」用）
  - custom_detail(def_out)          自定义详情（params_template 敏感值掩码）

工具识别启发式（步骤视图的"工具"列）：内置步骤是 (label, fn) 二元组，fn 为内建
函数——从函数源码里提取引用的白名单工具名（_tool/run_tool 调用实参、变更清单
"tool": "..." 字面量）；识别不到 = 内建步骤（kind=internal，不迁移进自定义副本）。
敏感参数掩码：params_template 里敏感键（password/token/ak/sk 类）的值一律 ****，
凭证不出详情接口（与 change_tools/preview 同一口径）。
"""
from __future__ import annotations

import functools
import inspect
import re

from app.core.i18n_msg import pick
from app.services.copilot.workflows import custom as wf_custom

# 步骤函数源码里的工具名引用（三种写法：只读工具收口 / run_tool / 变更清单 tool 字面量）
_TOOL_PATTERNS = (
    re.compile(r"""_tool\(\s*db\s*,\s*["']([a-z][a-z0-9_]*)["']"""),
    re.compile(r"""run_tool\(\s*db\s*,\s*["']([a-z][a-z0-9_]*)["']"""),
    re.compile(r""""tool"\s*:\s*["']([a-z][a-z0-9_]*)["']"""),
)


@functools.lru_cache(maxsize=None)
def _step_view_of(fn) -> tuple:
    """步骤函数 -> (tools, kind)。按函数缓存（inspect.getsource 有成本，清单接口反复用）。"""
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):  # pragma: no cover  内置步骤都是真实函数，防御而已
        return (), "internal"
    tools: list = []
    for pat in _TOOL_PATTERNS:
        for m in pat.finditer(src):
            name = m.group(1)
            # 白名单外的工具名不认（识别噪音不算工具步骤）
            if wf_custom.tool_kind(name) and name not in tools:
                tools.append(name)
    if any(wf_custom.tool_kind(t) == "change" for t in tools) or "change_step" in src:
        kind = "change"
    elif tools:
        kind = "read"
    else:
        kind = "internal"
    return tuple(tools), kind


def builtin_step_views(spec: dict) -> list:
    """内置步骤序列视图：idx/label/kind/tools/失败策略。
    失败策略：引擎逐步执行、单步失败不中止后续（依赖前置结果的步骤自行跳过），
    故内置步骤统一 continue_on_fail=True。"""
    out = []
    for i, (label, fn) in enumerate(spec.get("steps") or [], 1):
        tools, kind = _step_view_of(fn)
        out.append({
            "idx": i,
            "label": str(label),
            "kind": kind,
            "tools": list(tools),
            "tool": tools[0] if tools else "",
            "continue_on_fail": True,
            "will_ticket": kind == "change",
            "params_template": {},
        })
    return out


def builtin_detail(key: str, spec: dict, lang: str = "zh") -> dict:
    """内置工作流只读详情（GET /workflows/{key}/detail）：步骤序列/工具/失败策略 + 参数表。
    敏感参数（ak/sk/token 类）打 sensitive 标记；有默认值也不回显敏感默认值。"""
    schema: dict = {}
    for name, fs in (spec.get("params_schema") or {}).items():
        f = dict(fs or {})
        sensitive = wf_custom.is_sensitive_key(name)
        f["sensitive"] = sensitive
        if sensitive and "default" in f:
            f["default"] = "****"
        schema[name] = f
    steps = builtin_step_views(spec)
    return {
        "key": key,
        "custom": False,
        "builtin": True,
        "name": pick(spec.get("name", key), lang),
        "domain": pick(spec.get("domain", ""), lang),
        "description": pick(spec.get("description", ""), lang),
        "params_schema": schema,
        "params_hint": [],
        "steps": steps,
        "enabled": True,
        "version": None,
        "has_change_steps": any(s["will_ticket"] for s in steps),
    }


def steps_summary(spec: dict) -> list:
    """内置步骤名清单（合并清单接口的"步骤摘要"列；只取 label，不做源码识别）。"""
    return [str(label) for label, _fn in (spec.get("steps") or [])]


# ---------------- 另存为自定义：内置 spec -> 自定义步骤模板 ----------------

def derive_custom_steps(spec: dict) -> tuple:
    """内置 spec -> 自定义步骤模板（另存为副本用）。返回 (steps, omitted_labels)。

    只迁移白名单工具调用步骤（一个工具一步；多工具步骤按工具拆步）：
      - 参数模板 = 工具参数里与内置入参同名的 {{占位}} + 变更工具必填参数 {{占位}}
        （运行参数名与内置 params_schema 对齐，原入参直接可用）
      - continue_on_fail=True：与内置引擎语义一致（单步失败不中止后续）
    内建逻辑步骤（校验/指引/合成——没有白名单工具可表达）如实剔除进 omitted_labels，
    不硬塞假工具（另存为只做诚实可迁移的骨架，绝不放宽自定义步骤白名单）。
    """
    catalog = {t["name"]: t for t in wf_custom.tool_catalog()}
    builtin_fields = set((spec.get("params_schema") or {}).keys())
    steps: list = []
    omitted: list = []
    for label, fn in spec.get("steps") or []:
        tools, _kind = _step_view_of(fn)
        tools = [t for t in tools if t in catalog]
        if not tools:
            omitted.append(str(label))
            continue
        for tool in tools:
            tmpl: dict = {}
            for p in (catalog.get(tool) or {}).get("params") or []:
                name = str(p.get("name") or "")
                if name and (p.get("required") or name in builtin_fields):
                    tmpl[name] = "{{" + name + "}}"
            step_label = str(label) if len(tools) == 1 else f"{label}（{tool}）"
            steps.append({
                "tool": tool,
                "label": step_label,
                "params_template": tmpl,
                "continue_on_fail": True,
            })
    return steps, omitted


# ---------------- 自定义详情（params_template 敏感值掩码） ----------------

def _mask_template(tmpl) -> dict:
    out: dict = {}
    for k, v in (tmpl or {}).items():
        if wf_custom.is_sensitive_key(k):
            out[k] = "****"
        elif isinstance(v, dict):
            out[k] = _mask_template(v)
        elif isinstance(v, list):
            out[k] = [_mask_template(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


def custom_detail(d: dict) -> dict:
    """自定义工作流详情（def_out dict -> 与内置同构的只读详情）。敏感参数掩码。"""
    steps = []
    for i, s in enumerate(d.get("steps") or [], 1):
        tool = str((s or {}).get("tool") or "")
        kind = wf_custom.tool_kind(tool) or "read"
        steps.append({
            "idx": i,
            "label": str((s or {}).get("label") or tool),
            "kind": kind,
            "tools": [tool] if tool else [],
            "tool": tool,
            "continue_on_fail": bool((s or {}).get("continue_on_fail")),
            "params_template": _mask_template((s or {}).get("params_template") or {}),
            "will_ticket": kind == "change",
        })
    return {
        "key": d.get("key", ""),
        "custom": True,
        "builtin": False,
        "name": d.get("name", ""),
        "domain": d.get("domain", ""),
        "description": d.get("description", ""),
        "params_schema": {},
        "params_hint": d.get("params_hint") or [],
        "steps": steps,
        "enabled": bool(d.get("enabled", True)),
        "version": d.get("version"),
        "has_change_steps": bool(d.get("has_change_steps", False)),
        "created_by": d.get("created_by", ""),
        "updated_at": d.get("updated_at"),
    }
