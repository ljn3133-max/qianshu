"""服务端计划校验：模型输出不是执行许可。

严格白名单：只接受固定顶层字段、固定步骤字段、动作目录内 recipe、与目录一致的
风险、按 recipe schema 校验的参数与上限。R3 代码补丁额外交给 code_mod 校验路径与
行数。任何越权/未知字段/危险关键字都返回明确原因，由调用方记录 plan_rejected。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.self_heal import catalog, fingerprint

MAX_STEPS = 5
MAX_TOTAL_TIMEOUT_S = 900
MAX_TEXT = 4000
_RISK_RANK = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 9}

_PLAN_KEYS = {
    "schema", "summary", "root_cause", "impact", "confidence", "can_auto_fix",
    "need_user", "steps", "verify", "assist_steps", "questions", "risk",
}
_STEP_KEYS = {
    "recipe", "params", "risk", "reason", "expected_effect", "verify",
    "rollback", "timeout_s",
}
_PATCH_KEYS = {"summary", "files"}
_PATCH_FILE_KEYS = {"path", "new", "edits"}
_EDIT_KEYS = {"old", "new"}


class PlanValidationError(ValueError):
    def __init__(self, reason: str, *, code: str = "invalid_plan"):
        super().__init__(reason)
        self.code = code


def _fail(reason: str, code: str = "invalid_plan"):
    raise PlanValidationError(reason, code=code)


def _text(value: Any, *, limit: int, field: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        _fail(f"{field} 必须是字符串")
    value = value.strip()
    if required and not value:
        _fail(f"{field} 不能为空")
    return value[:limit]


def _bool(value: Any, field: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        _fail(f"{field} 必须是布尔值")
    return value


def _scan_forbidden(value: Any, *, field: str = "params", allow_path: bool = False):
    """递归扫描键名和字符串值；R3 的 path 仅允许在 files[].path 结构内。"""
    if isinstance(value, dict):
        for key, item in value.items():
            k = str(key).strip().lower()
            if k in catalog.FORBIDDEN_KEYS and not (allow_path and k == "path"):
                _fail(f"{field} 含禁止字段 `{key}`", code="forbidden_field")
            if k.startswith("_") or "__" in k:
                _fail(f"{field} 含非法字段 `{key}`", code="forbidden_field")
            if allow_path and k == "path":
                _scan_path(item, field=f"{field}.{key}")
                continue
            _scan_forbidden(item, field=f"{field}.{key}", allow_path=allow_path)
    elif isinstance(value, list):
        if len(value) > 50:
            _fail(f"{field} 列表过长", code="too_large")
        for i, item in enumerate(value):
            _scan_forbidden(item, field=f"{field}[{i}]", allow_path=allow_path)
    elif isinstance(value, str):
        low = value.lower()
        if len(value) > 200_000:
            _fail(f"{field} 文本过长", code="too_large")
        for frag in catalog.FORBIDDEN_FRAGMENTS:
            if frag in low:
                _fail(f"{field} 含危险片段 `{frag}`", code="forbidden_fragment")
        if low.startswith("/") or re.match(r"^[a-z]:[\\/]", low):
            _fail(f"{field} 不允许绝对路径", code="forbidden_path")
        for frag in catalog.FORBIDDEN_PATH_FRAGMENTS:
            if frag in low:
                _fail(f"{field} 含路径越界片段", code="forbidden_path")


def _scan_path(value: Any, *, field: str):
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} 必须是文件相对路径")
    _scan_forbidden(value, field=field)


def _validate_schema(value: Any, schema: dict, *, field: str):
    typ = schema.get("type")
    if typ == "object":
        if not isinstance(value, dict):
            _fail(f"{field} 必须是对象")
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                _fail(f"{field} 缺少必填字段 `{key}`")
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                _fail(f"{field} 含未知字段 {sorted(extra)}", code="unknown_field")
        for key, item in value.items():
            if key in props:
                _validate_schema(item, props[key], field=f"{field}.{key}")
    elif typ == "string":
        if not isinstance(value, str):
            _fail(f"{field} 必须是字符串")
        if len(value) > int(schema.get("max_length") or 2000):
            _fail(f"{field} 超过长度上限")
        enum = schema.get("enum")
        if enum and value not in enum:
            _fail(f"{field} 不在允许枚举 {enum}")
        pattern = schema.get("pattern")
        if pattern and not re.match(pattern, value):
            _fail(f"{field} 格式不合法")
    elif typ == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            _fail(f"{field} 必须是整数")
        if "min" in schema and value < schema["min"]:
            _fail(f"{field} 小于最小值")
        if "max" in schema and value > schema["max"]:
            _fail(f"{field} 超过最大值")
    elif typ == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _fail(f"{field} 必须是数字")
    elif typ == "boolean":
        if not isinstance(value, bool):
            _fail(f"{field} 必须是布尔值")
    elif typ == "array":
        if not isinstance(value, list):
            _fail(f"{field} 必须是数组")
        if len(value) > int(schema.get("max_items") or 50):
            _fail(f"{field} 超过条目上限")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                _validate_schema(item, item_schema, field=f"{field}[{i}]")
    else:
        _fail(f"{field} schema 类型未知", code="validator_internal")


def _check_ids(db, recipe: str, params: dict):
    """ID 隶属校验：账号必须存在；run_id 若给出必须属于该账号。"""
    if db is None:
        return
    from app.models.cloud_account import CloudAccount
    from app.models.onboarding_run import OnboardingRun

    account_id = params.get("account_id")
    if account_id:
        account = db.get(CloudAccount, int(account_id))
        if account is None:
            _fail(f"账号 {account_id} 不存在", code="unknown_account")
    run_id = params.get("run_id")
    if run_id:
        run = db.get(OnboardingRun, int(run_id))
        if run is None or (account_id and int(run.account_id) != int(account_id)):
            _fail(f"Run {run_id} 不属于账号 {account_id}", code="unknown_run")


def _normalize_patch(raw: dict, *, field: str) -> dict:
    if not isinstance(raw, dict):
        _fail(f"{field} 必须是对象")
    extra = set(raw) - _PATCH_KEYS
    if extra:
        _fail(f"{field} 含未知字段 {sorted(extra)}", code="unknown_field")
    files = raw.get("files")
    if not isinstance(files, list) or not files:
        _fail(f"{field}.files 必须是非空数组")
    if len(files) > 5:
        _fail(f"{field}.files 最多 5 个文件")
    out = []
    for i, item in enumerate(files):
        if not isinstance(item, dict):
            _fail(f"{field}.files[{i}] 必须是对象")
        extra = set(item) - _PATCH_FILE_KEYS
        if extra:
            _fail(f"{field}.files[{i}] 含未知字段 {sorted(extra)}", code="unknown_field")
        path, new, edits = item.get("path"), item.get("new"), item.get("edits")
        if not isinstance(path, str) or not path.strip():
            _fail(f"{field}.files[{i}].path 必须是相对路径")
        _scan_path(path, field=f"{field}.files[{i}].path")
        entry = {"path": path.strip()}
        if edits is not None:
            if new is not None:
                _fail(f"{field}.files[{i}] 不能同时给 edits 和 new")
            if not isinstance(edits, list) or not edits or len(edits) > 20:
                _fail(f"{field}.files[{i}].edits 必须是 1~20 条数组")
            clean_edits = []
            for j, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    _fail(f"{field}.files[{i}].edits[{j}] 必须是对象")
                if set(edit) - _EDIT_KEYS:
                    _fail(f"{field}.files[{i}].edits[{j}] 含未知字段", code="unknown_field")
                old_text, new_text = edit.get("old"), edit.get("new")
                if not isinstance(old_text, str) or not old_text:
                    _fail(f"{field}.files[{i}].edits[{j}].old 必须是非空字符串")
                if not isinstance(new_text, str):
                    _fail(f"{field}.files[{i}].edits[{j}].new 必须是字符串")
                if old_text == new_text:
                    _fail(f"{field}.files[{i}].edits[{j}] old 与 new 相同")
                clean_edits.append({"old": old_text, "new": new_text})
            entry["edits"] = clean_edits
        else:
            if not isinstance(new, str) or not new:
                _fail(f"{field}.files[{i}].new 必须是非空全文（删除类一律拒绝）")
            entry["new"] = new
        out.append(entry)
    normalized = {"files": out}
    summary = raw.get("summary")
    if summary is not None:
        normalized["summary"] = _text(summary, limit=1000, field=f"{field}.summary")
    # 复用现有 change_tools 的防幻觉/结构/边界校验，再落到 code_mod 最终格式。
    from app.services.copilot import change_tools, code_mod
    errors = change_tools.plan_shape_errors(normalized) + change_tools.plan_olds_errors(normalized)
    built, build_errors = change_tools.build_codeplan(normalized)
    errors += build_errors or []
    if errors:
        _fail("代码补丁结构/现文件校验未过：" + "；".join(errors)[:600],
              code="code_patch_rejected")
    check = code_mod.validate_plan(built)
    if not check.get("ok"):
        _fail("代码补丁未通过 code_mod 校验：" + "；".join(check.get("violations") or []),
              code="code_patch_rejected")
    return normalized


def _step(raw: dict, index: int, db) -> dict:
    field = f"steps[{index}]"
    if not isinstance(raw, dict):
        _fail(f"{field} 必须是对象")
    extra = set(raw) - _STEP_KEYS
    if extra:
        _fail(f"{field} 含未知字段 {sorted(extra)}", code="unknown_field")
    recipe_name = raw.get("recipe")
    if not isinstance(recipe_name, str) or not recipe_name.strip():
        _fail(f"{field}.recipe 不能为空")
    recipe = catalog.get(recipe_name)
    if recipe is None:
        _fail(f"未知动作 `{recipe_name}`，不在固定动作目录内", code="unknown_recipe")
    claimed = raw.get("risk")
    if claimed is not None and claimed != recipe.risk:
        _fail(f"{field}.risk 自报 {claimed} 与目录 {recipe.risk} 不一致",
              code="risk_mismatch")

    params = raw.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        _fail(f"{field}.params 必须是对象")
    if recipe.name == "patch_platform_code":
        if set(params) - {"plan"}:
            _fail(f"{field}.params 只允许 plan", code="unknown_field")
        params = {"plan": _normalize_patch(params.get("plan"), field=f"{field}.params.plan")}
        _check_ids(db, recipe.name, params)
    else:
        _scan_forbidden(params, field=f"{field}.params")
        _validate_schema(params, recipe.params_schema, field=f"{field}.params")
        _check_ids(db, recipe.name, params)

    return catalog.sanitize_plan_step(
        recipe, params,
        reason=_text(raw.get("reason"), limit=1000, field=f"{field}.reason"),
        expected_effect=_text(raw.get("expected_effect"), limit=500,
                              field=f"{field}.expected_effect"),
    )


def normalize_plan(raw: dict, db=None) -> dict:
    """校验并规范化模型 JSON；返回可落库的不可变计划。"""
    if not isinstance(raw, dict):
        _fail("计划必须是 JSON 对象")
    extra = set(raw) - _PLAN_KEYS
    if extra:
        _fail(f"计划含未知字段 {sorted(extra)}", code="unknown_field")

    steps_raw = raw.get("steps") or []
    if not isinstance(steps_raw, list):
        _fail("steps 必须是数组")
    if len(steps_raw) > MAX_STEPS:
        _fail(f"计划最多 {MAX_STEPS} 步，实际 {len(steps_raw)} 步", code="too_many_steps")
    steps = [_step(item, i, db) for i, item in enumerate(steps_raw)]

    risks = [s["risk"] for s in steps]
    risk = max(risks, key=lambda r: _RISK_RANK.get(r, 9)) if risks else "R0"
    if risk == "R4" or any(_RISK_RANK.get(r, 9) >= 9 for r in risks):
        _fail("计划包含 R4/越权动作", code="forbidden_risk")
    if sum(1 for r in risks if r == "R3") > 1:
        _fail("单个计划最多 1 个代码补丁", code="too_many_patches")
    if sum(1 for s in steps if s["recipe"] == "patch_platform_code") > 1:
        _fail("单个计划最多 1 个代码补丁", code="too_many_patches")
    for i, s in enumerate(steps):
        if s["recipe"] == "restart_platform" and i != len(steps) - 1:
            _fail("重启平台必须是最后一步", code="restart_not_last")
        if s["recipe"] == "patch_platform_code" and i != len(steps) - 1:
            _fail("代码补丁必须是最后一步", code="patch_not_last")
    if len({s["recipe"] for s in steps}) != len(steps):
        _fail("同一计划内 recipe 不得重复", code="duplicate_recipe")
    total_timeout = sum(int(s.get("timeout_s") or 0) for s in steps)
    if total_timeout > MAX_TOTAL_TIMEOUT_S:
        _fail(f"计划总超时 {total_timeout}s 超过上限 {MAX_TOTAL_TIMEOUT_S}s",
              code="timeout_too_large")

    need_user = _bool(raw.get("need_user"), "need_user")
    can_auto_fix = _bool(raw.get("can_auto_fix"), "can_auto_fix")
    if not steps and not need_user:
        _fail("计划既没有执行步骤，也没有人工协助步骤", code="empty_plan")
    if need_user and can_auto_fix:
        _fail("need_user 与 can_auto_fix 不能同时为真", code="inconsistent_plan")
    if steps and not can_auto_fix and not need_user:
        _fail("有执行步骤但 can_auto_fix=false", code="inconsistent_plan")

    assist_raw = raw.get("assist_steps") or []
    if not isinstance(assist_raw, list) or len(assist_raw) > 8:
        _fail("assist_steps 必须是最多 8 条的数组")
    assist_steps = []
    for i, item in enumerate(assist_raw):
        if isinstance(item, dict):
            item = item.get("text") or item.get("step") or ""
        assist_steps.append(_text(item, limit=500, field=f"assist_steps[{i}]"))
    assist_steps = [x for x in assist_steps if x]

    questions_raw = raw.get("questions") or []
    if not isinstance(questions_raw, list) or len(questions_raw) > 5:
        _fail("questions 必须是最多 5 条的数组")
    questions = [_text(q, limit=500, field=f"questions[{i}]")
                 for i, q in enumerate(questions_raw)]
    questions = [q for q in questions if q]

    confidence = raw.get("confidence")
    if confidence is None:
        confidence = 0.0
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        _fail("confidence 必须是 0~1 的数字")
    confidence = max(0.0, min(1.0, float(confidence)))

    plan = {
        "schema": "qianshu.self_heal.plan.v1",
        "summary": _text(raw.get("summary") or raw.get("root_cause"), limit=1000,
                         field="summary"),
        "root_cause": _text(raw.get("root_cause"), limit=MAX_TEXT, field="root_cause",
                            required=bool(steps or need_user)),
        "impact": _text(raw.get("impact"), limit=2000, field="impact"),
        "confidence": round(confidence, 4),
        "can_auto_fix": bool(steps) and can_auto_fix,
        "need_user": need_user or (not steps),
        "steps": steps,
        "verify": _text(raw.get("verify"), limit=1000, field="verify"),
        "assist_steps": assist_steps or ([] if steps else ["请人工检查问题单证据后回传处理结果。"]),
        "questions": questions,
        "risk": risk,
    }
    return plan


def plan_hash(plan: dict, *, issue_id: int, version: int) -> str:
    """计划哈希绑定问题单与版本，避免跨问题单相同计划触发唯一索引冲突。"""
    return fingerprint.plan_hash({**plan, "_issue_id": int(issue_id),
                                  "_version": int(version)})


def risk_label(risk: str) -> str:
    return {"R0": "只读/重试", "R1": "平台内低风险", "R2": "重启/配置",
            "R3": "代码修改", "R4": "禁止"}.get(risk, risk)
