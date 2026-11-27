"""AI 场景×模型矩阵解析。
场景键：ask（AI 分析）/ inspection（巡检）/ diagnosis（诊断）/ copilot（千枢智能体）。
解析顺序：场景显式停用 → None（开关真断电，不兜底——消费方按未配置引导）；
场景绑定的模型连接 → 兜底第一个启用的连接（开箱可用）→ None（前端引导跳设置）。
凭证解密只在本函数内发生，返回给调用方直接可用；展示层一律 _mask_ak 口径不回显。"""
from __future__ import annotations

from typing import Optional

# 场景展示名使用双语载体 {"zh","en"}：
# API 出口按 Accept-Language 选文（app.core.i18n_msg.pick）；审计/服务层固定取 zh 保持原行为。
SCENARIOS = {
    "ask": {"zh": "AI 分析", "en": "AI Analysis"},
    "inspection": {"zh": "巡检中心", "en": "Inspection Center"},
    "diagnosis": {"zh": "故障诊断", "en": "Fault Diagnosis"},
    "copilot": {"zh": "千枢智能体", "en": "QianShu Copilot"},
}


def resolve_model(db, scenario: str) -> Optional[dict]:
    """返回 {setting_id, label, base_url, api_key, model, temperature, max_tokens, verify_ssl} 或 None。"""
    from app.models.ai_scenario import AiScenarioModel
    from app.models.ai_setting import AiSetting

    row = db.query(AiSetting).filter(AiSetting.enabled == True).order_by(AiSetting.id).all()  # noqa: E712
    if not row:
        return None
    chosen = row[0]   # 兜底：第一个启用连接（场景未绑定也开箱可用）
    try:
        s = db.get(AiScenarioModel, scenario)
        if s is not None and s.enabled is False:
            # 场景显式停用（enabled=False）：开关真断电，直接 None——不再兜底默认连接
            # 消费方已有未配置/NEED_MODEL 引导，显式停用不能回退到默认连接
            return None
        if s and s.enabled:
            bound = db.get(AiSetting, s.setting_id)
            if bound and bound.enabled:
                chosen = bound
    except Exception:
        pass   # 场景表缺失/未迁移：兜底不受影响
    try:
        from app.core.security import decrypt_secret
        api_key = decrypt_secret(chosen.api_key_cipher)
    except Exception:
        api_key = ""
    return {
        "setting_id": chosen.id,
        "label": chosen.provider_label,
        "base_url": chosen.base_url,
        "api_key": api_key,
        "model": chosen.model,
        "temperature": chosen.temperature or 0.2,
        "max_tokens": chosen.max_tokens or 8192,
        "verify_ssl": bool(chosen.verify_ssl),
    }


def require_model(db, scenario: str, lang: str = "zh"):
    """消费面守卫：resolve_model 为 None（场景显式停用 / 无可用模型连接）时
    抛 LLMError 引导文案，可用时返回 resolve_model 的 dict。

    copilot loop 不走本守卫（其契约是 SSE error code=NEED_MODEL，前端有引导卡）；
    ask/inspection/diagnosis 走本守卫，异常由各 API 层按 LLMError 现有路径转 502 展示。
    lang：请求界面语言（zh/en）——引导文案按 lang 出文（MSG: ai.scenario_no_model）；
    服务层缺省 zh 保持原行为。
    """
    cfg = resolve_model(db, scenario)
    if cfg is None:
        from app.core.i18n_msg import pick, t
        from app.services.llm import LLMError

        label = pick(SCENARIOS.get(scenario), lang) or scenario
        raise LLMError(t("ai.scenario_no_model", lang, label=label))
    return cfg
