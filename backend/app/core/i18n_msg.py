"""后端下发展示文案的双语助手。

语言协商：前端每个请求带 Accept-Language: zh-CN|en（frontend/src/api/http.js 拦截器）。
  lang_of(request) -> "zh" | "en"（Accept-Language 含 en → en；缺省/服务层无 request → zh）
  pick(d, lang)    -> {"zh": .., "en": ..} 按语言取值；普通字符串原样透传（兼容未双语化字段）

文案载体约定（后端展示文案统一走它）：
  静态展示文案字段一律 {"zh": "中文", "en": "English"}；列表字段逐项双语。
  API 出口按 request 语言 pick 成字符串再返回——响应契约不变（字段仍是 str），兼容现有前端。
  业务逻辑 / 持久化数据 / LLM 语料 一律 pick(x, "zh") 保持原行为（口径不变）。

MSG：高频用户可见错误/提示登记表（key -> 双语），t(key, lang, **kw) 取值并做占位格式化。
"""
from __future__ import annotations


def lang_of(request) -> str:
    """解析请求语言："en" | "zh"。缺省中文；None（定时任务/服务层）→ zh。"""
    if request is None:
        return "zh"
    try:
        al = request.headers.get("accept-language", "") or ""
    except AttributeError:
        return "zh"
    return "en" if "en" in al.lower() else "zh"


def pick(d, lang: str = "zh"):
    """双语载体取值：{"zh":..,"en":..} 按 lang 取，缺项回退 zh；普通值原样返回。"""
    if isinstance(d, dict):
        return d.get(lang) or d.get("zh") or d.get("en") or ""
    return d


# ---------------- 高频用户可见错误/提示登记表（86 条） ----------------
# auth：登录/改密/验证码/找回提示（18）· clouds：连通测试/ack 警示（9）+ sync（2）
# agent：NEED_MODEL/确认门 403（4）+ 变更门 rollback/dismiss/skip（11）
# integrations：状态判定 message（11）+ STATUS_TEXT（4）+ registry detail/last 前缀（9）
# workflows：参数校验（11）· ai：配置/场景/工具/守卫引导（7）

MSG: dict = {
    # ---- auth.py：滑块/图形验证码、登录、改密、TOTP ----
    "auth.slider_expired": {
        "zh": "验证已过期，请重新滑动",
        "en": "Verification expired. Please slide the puzzle again.",
    },
    "auth.slider_misaligned": {
        "zh": "拼图未对齐，请重试",
        "en": "Puzzle piece is not aligned. Please try again.",
    },
    "auth.captcha_bad": {
        "zh": "验证码错误或已过期",
        "en": "Incorrect or expired verification code.",
    },
    "auth.user_not_found": {
        "zh": "用户不存在",
        "en": "User does not exist.",
    },
    "auth.otp_bad": {
        "zh": "动态验证码错误（6 位 TOTP）",
        "en": "Incorrect one-time code (6-digit TOTP).",
    },
    "auth.bad_password": {
        "zh": "密码错误",
        "en": "Incorrect password.",
    },
    "auth.disabled": {
        "zh": "账号已禁用",
        "en": "This account has been disabled.",
    },
    # 登录找回兜底提示：仅本机/私网来源且该账号 15 分钟内登录失败 ≥3 次才下发
    "auth.recovery_hint": {
        "zh": "忘记密码？请在服务器千枢目录执行 python3 backend/scripts/qianshu-passwd.py reset {user}（详见部署文档《管理员找回》）",
        "en": "Forgot your password? On the server, run `python3 backend/scripts/qianshu-passwd.py reset {user}` in the qianshu directory (see \"Admin Recovery\" in the deployment doc).",
    },
    "auth.pwd_old_wrong": {
        "zh": "当前密码不正确",
        "en": "Current password is incorrect.",
    },
    "auth.pwd_same": {
        "zh": "新密码不能与当前密码相同",
        "en": "The new password must be different from the current one.",
    },
    "auth.pwd_too_short": {
        "zh": "密码至少 8 位",
        "en": "Password must be at least 8 characters.",
    },
    "auth.pwd_common": {
        "zh": "密码过于常见（弱口令库命中），请换一个",
        "en": "This password is too common (matched the weak-password list). Please choose another one.",
    },
    "auth.pwd_simple": {
        "zh": "密码不能是纯数字或纯字母（请混排）",
        "en": "Password cannot be all digits or all letters. Use a mix of character types.",
    },
    "auth.pwd_changed": {
        "zh": "密码已修改",
        "en": "Password changed.",
    },
    "auth.otp_setup_first": {
        "zh": "请先调用 setup 生成密钥",
        "en": "Run setup first to generate a secret.",
    },
    "auth.otp_code_bad": {
        "zh": "动态码不正确（或等 30 秒重试）",
        "en": "Incorrect one-time code (wait 30 seconds and try again).",
    },
    "auth.otp_disable_need_code": {
        "zh": "关闭需验证当前动态码",
        "en": "Enter your current one-time code to turn this off.",
    },
    "auth.otp_setup_note": {
        "zh": "用验证器 App 扫码/粘贴密钥后，输入 6 位码确认激活",
        "en": "Scan the code (or paste the secret) in your authenticator app, then enter the 6-digit code to activate.",
    },
    # ---- clouds.py：连通测试 / ack 安全警示 ----
    "cloud.ack_required": {
        "zh": "请阅读安全警示并输入「我已知晓」后保存：云账号将用于资产/账单采集与智能体分析、导入等场景，"
              "务必使用只读账号（RAM ReadOnlyAccess）",
        "en": "Please read the security notice and type 「我已知晓」 to save. Cloud accounts are used for "
              "asset and billing collection, copilot analysis, import and similar scenarios. "
              "Be sure to use a read-only account (e.g. RAM ReadOnlyAccess).",
    },
    "cloud.ak_pair": {
        "zh": "AK/SK 需成对填写（至少 8 位）",
        "en": "AK/SK must be provided as a pair (at least 8 characters each).",
    },
    "cloud.onprem_not_here": {
        "zh": "裸机/IDC 不是云账号：请在「机器管理」登记主机，资产会自动进入资产台账",
        "en": "Bare metal / IDC is not a cloud account. Register hosts in Machine Management; they will appear in the asset ledger automatically.",
    },
    "cloud.ak_pair_keep": {
        "zh": "AK/SK 需成对填写（至少 8 位）；留空=不修改",
        "en": "AK/SK must be provided as a pair (at least 8 characters each). Leave both empty to keep the stored credentials.",
    },
    "cloud.name_exists": {
        "zh": "账号名称已存在",
        "en": "An account with this name already exists.",
    },
    "cloud.not_found": {
        "zh": "云账号不存在",
        "en": "Cloud account does not exist.",
    },
    "cloud.decrypt_fail": {
        "zh": "凭证解密失败（MASTER_KEY 变更？）",
        "en": "Failed to decrypt the credentials. (Has the MASTER_KEY changed?)",
    },
    "cloud.test_failed": {
        "zh": "测试失败: {reason}",
        "en": "Test failed: {reason}",
    },
    "cloud.sig_mismatch": {
        "zh": "AK/Secret 不匹配",
        "en": "AK/Secret do not match.",
    },
    "cloud.bad_ak": {
        "zh": "AccessKeyId 不存在或已禁用",
        "en": "AccessKeyId does not exist or has been disabled.",
    },
    # ---- agent_copilot.py / change_gate.py：NEED_MODEL + 确认门 403 ----
    "agent.need_model": {
        "zh": "未配置 AI 模型，请先在 设置 → AI 模型 中配置并启用",
        "en": "No AI model is configured. Go to Settings → AI Models to set one up and enable it.",
    },
    "agent.gate_status": {
        "zh": "工单状态为 {status}，不允许确认执行——变更必须走确认门且不可重放"
              "（已完成/已回滚/已取消的工单不可再执行）",
        "en": "Ticket status is {status}, so it cannot be confirmed for execution. Changes must go through "
              "the confirmation gate and cannot be replayed (completed / rolled back / dismissed tickets cannot run again).",
    },
    "agent.gate_no_items": {
        "zh": "没有可执行的变更项（所选项均已完成/已跳过/已回滚——重放不受理）",
        "en": "No executable change items (all selected items are done / skipped / rolled back — replays are not accepted).",
    },
    "agent.gate_ack": {
        "zh": "该变更含凭证类操作（云账号接入/改凭证），请阅读安全警示并输入「{ack}」后确认"
              "（警示版本 {ver}；确认将记入审计作为免责凭证）",
        "en": "This change includes credential operations (cloud account onboarding / credential update). "
              "Read the security notice and type 「{ack}」 to confirm "
              "(notice version {ver}; the confirmation is recorded in the audit log as a liability waiver).",
    },
    # ---- change_gate.py：rollback / dismiss / skip ----
    "agent.ticket_not_found": {
        "zh": "变更工单不存在",
        "en": "Change ticket does not exist.",
    },
    "agent.items_must_idx": {
        "zh": "items 必须是变更项 idx 数组",
        "en": "items must be an array of change-item idx values.",
    },
    "agent.item_not_found": {
        "zh": "变更项不存在: {idx}",
        "en": "Change item not found: {idx}",
    },
    "agent.rollback_no_done": {
        "zh": "工单没有已执行项，无需回滚",
        "en": "The ticket has no executed items — nothing to roll back.",
    },
    "agent.dismiss_has_done": {
        "zh": "工单已有执行项，须先一键回滚再取消",
        "en": "The ticket already has executed items — roll them back first, then dismiss.",
    },
    "agent.dismiss_bad_status": {
        "zh": "工单状态为 {status}，不允许取消",
        "en": "Ticket status is {status}, so it cannot be dismissed.",
    },
    "agent.adjust_bad_status": {
        "zh": "工单状态为 {status}，不允许调整变更项",
        "en": "Ticket status is {status}, so its change items cannot be adjusted.",
    },
    "agent.skip_bad_idx": {
        "zh": "idx 必须是变更项序号",
        "en": "idx must be the change-item number.",
    },
    "agent.already_skipped": {
        "zh": "该项已是跳过态",
        "en": "This item is already skipped.",
    },
    "agent.skip_only_pending": {
        "zh": "仅待确认（pending）项可跳过，当前 {status}",
        "en": "Only pending items can be skipped (current status: {status}).",
    },
    "agent.restore_only_skipped": {
        "zh": "仅跳过态（skipped）可恢复",
        "en": "Only skipped items can be restored.",
    },
    # ---- integrations.py：链路/渠道状态判定 message ----
    "int.event.token_missing": {
        "zh": "token 未配置——外部事件会被拒收，链路不通（后端 .env 配置 EVENT_WEBHOOK_TOKEN 后重启生效）",
        "en": "Webhook token is not configured — external events will be rejected, so the link is not "
              "working. Set EVENT_WEBHOOK_TOKEN in the backend .env and restart for it to take effect.",
    },
    "int.event.connected": {
        "zh": "已连通 · 近 {days} 天真实收到 {count} 条事件（最近 {last}）",
        "en": "Connected · {count} real events received in the past {days} days (latest {last})",
    },
    "int.event.stale": {
        "zh": "曾收到事件（历史 {total} 条，最近 {last}），但近 {days} 天无新事件——链路配置正确，"
              "无法自证当前仍通；可从 {name} 发一条测试事件后复核",
        "en": "Events were received before ({total} total, latest {last}), but none in the past {days} days "
              "— the configuration is correct, but current connectivity cannot be self-verified. "
              "Send a test event from {name} and re-check.",
    },
    "int.event.none": {
        "zh": "链路配置正确（token 已配置、接入点就绪）但尚无外部事件到达，无法自证端到端连通——"
              "请从 {name} 发一条测试事件，到达后此处显示「已连通」",
        "en": "Configuration looks correct (token set, endpoint ready) but no external events have arrived "
              "yet — end-to-end connectivity cannot be self-verified. Send a test event from {name}; "
              "it will show as “Connected” here once it arrives.",
    },
    "int.channel.disabled": {
        "zh": "渠道已停用——告警不再推送到此渠道；在「编辑」里启用后恢复",
        "en": "The channel is disabled — alerts are no longer pushed here. Re-enable it in Edit to restore "
              "delivery.",
    },
    "int.channel.error": {
        "zh": "配置异常：{problems}——请编辑修复",
        "en": "Configuration error: {problems} — please fix in edit dialog.",
    },
    "int.channel.connected": {
        "zh": "已连通 · 近 {days} 天有真实送达记录（最近 {last}，口径=告警触发时间）",
        "en": "Connected · real delivery records in the past {days} days (latest {last}, based on alert "
              "fire time)",
    },
    "int.channel.stale": {
        "zh": "配置完整（凭证可解密、必填项齐全）；最近一次送达记录 {last}，近 {days} 天无发送——"
              "无法自证当前仍可用（对外发测试消息需用户在告警中心主动触发）",
        "en": "Configuration is complete (credentials decrypt, required fields present); last delivery "
              "record {last}, none in the past {days} days — current availability cannot be "
              "self-verified (sending test messages must be triggered by the user in Alert Center).",
    },
    "int.channel.none": {
        "zh": "配置完整（凭证可解密、必填项齐全）但尚无发送记录，无法自证真实送达"
              "——对外发测试消息需用户在告警中心主动触发",
        "en": "Configuration is complete (credentials decrypt, required fields present) but there are "
              "no delivery records yet — real delivery cannot be self-verified. Sending test messages "
              "must be triggered by the user in Alert Center.",
    },
    "int.problem.bad_webhook": {
        "zh": "webhook 地址不合法",
        "en": "Invalid webhook URL.",
    },
    "int.problem.decrypt_fail": {
        "zh": "webhook 凭证解密失败（MASTER_KEY 变更？）",
        "en": "Failed to decrypt the webhook credentials. (Has the MASTER_KEY changed?)",
    },
    # ---- integrations.py：STATUS_TEXT 状态标签 + registry detail/last_data 前缀 ----
    "int.status.connected": {
        "zh": "已连通",
        "en": "Connected",
    },
    "int.status.unverified": {
        "zh": "未验证",
        "en": "Unverified",
    },
    "int.status.error": {
        "zh": "异常",
        "en": "Error",
    },
    "int.status.disabled": {
        "zh": "已停用",
        "en": "Disabled",
    },
    "int.detail.ds_not_probed": {
        "zh": "尚未探测——点「测试」做真实版本探测",
        "en": "Not probed yet — click Test to run a real version probe.",
    },
    "int.detail.k8s_last_sync": {
        "zh": "最近同步 {ts}",
        "en": "Last sync {ts}",
    },
    "int.detail.k8s_not_probed": {
        "zh": "尚未探测——点「测试」做真实连通测试",
        "en": "Not probed yet — click Test to run a real connectivity test.",
    },
    "int.detail.cloud_last_test": {
        "zh": "最近测试 {ts}",
        "en": "Last tested {ts}",
    },
    "int.detail.cloud_not_tested": {
        "zh": "尚未测试——点「测试可用性」做真实探测",
        "en": "Not tested yet — click Test Availability for a real probe.",
    },
    "int.last.event": {
        "zh": "最近事件",
        "en": "Last event",
    },
    "int.last.send": {
        "zh": "最近发送",
        "en": "Last delivery",
    },
    "int.last.sync": {
        "zh": "最近同步",
        "en": "Last sync",
    },
    "int.last.test": {
        "zh": "最近测试",
        "en": "Last test",
    },
    # ---- agent_workflows / workflows engine：参数校验 ----
    "wf.param.int": {
        "zh": "{field} 必须是整数，收到 {raw!r}",
        "en": "{field} must be an integer, got {raw}",
    },
    "wf.param.num": {
        "zh": "{field} 必须是数字，收到 {raw!r}",
        "en": "{field} must be a number, got {raw}",
    },
    "wf.param.bool": {
        "zh": "{field} 必须是布尔值，收到 {raw!r}",
        "en": "{field} must be a boolean, got {raw}",
    },
    "wf.param.min": {
        "zh": "{field} 不能小于 {min}",
        "en": "{field} must not be less than {min}",
    },
    "wf.param.max": {
        "zh": "{field} 不能大于 {max}",
        "en": "{field} must not be greater than {max}",
    },
    "wf.param.empty": {
        "zh": "{field} 不能为空",
        "en": "{field} must not be empty.",
    },
    "wf.param.not_object": {
        "zh": "params 必须是对象",
        "en": "params must be an object.",
    },
    "wf.param.required": {
        "zh": "{field} 必填（{desc}）",
        "en": "{field} is required ({desc})",
    },
    "wf.param.enum": {
        "zh": "{field} 取值非法 {value!r}，可选: {choices}",
        "en": "{field} has an invalid value {value}; allowed: {choices}",
    },
    "wf.param.invalid": {
        "zh": "参数校验不过：{detail}",
        "en": "Parameter validation failed: {detail}",
    },
    "wf.steps_empty": {
        "zh": "steps 为空（传 key 或 draft steps）",
        "en": "steps is empty (pass key or draft steps).",
    },
    # ---- clouds.py：sync 提示 ----
    "cloud.sync_unsupported": {
        "zh": "暂不支持 {product} 同步（可选: {options}）",
        "en": "Syncing {product} is not supported (options: {options})",
    },
    "cloud.sync_no_accounts": {
        "zh": "没有启用中的云账号——先在云账号管理里添加",
        "en": "No enabled cloud accounts — add one in Cloud Accounts first.",
    },
    # ---- assets.py：旧单产品同步废弃提示（主入口 /clouds/sync） ----
    "assets.sync_deprecated": {
        "zh": "此同步接口已废弃：请改用「运维管理 → 同步云资产」（POST /clouds/sync）；域名/证书同步暂走本接口过渡",
        "en": "This sync API is deprecated: use Ops Management → Sync Cloud Assets (POST /clouds/sync) instead. "
              "Domain/SSL-cert sync still uses this endpoint during the migration window.",
    },
    # ---- ai.py：配置/场景/工具错误 + require_model 引导 ----
    "ai.setting_not_found": {
        "zh": "配置不存在",
        "en": "Configuration does not exist.",
    },
    "ai.scenario_not_found": {
        "zh": "场景不存在",
        "en": "Scenario does not exist.",
    },
    "ai.model_not_found": {
        "zh": "模型连接不存在",
        "en": "Model connection does not exist.",
    },
    "ai.api_key_required": {
        "zh": "请填写 API Key（新建必须填；编辑时留空则用已保存的 Key 测试）",
        "en": "Enter an API Key (required when creating; leave empty while editing to test with the saved key).",
    },
    "ai.tool_exec_failed": {
        "zh": "工具执行失败: {detail}",
        "en": "Tool execution failed: {detail}",
    },
    "ai.tool_not_found": {
        "zh": "工具不存在：{tool}",
        "en": "Tool not found: {tool}",
    },
    "ai.scenario_no_model": {
        "zh": "{label}：场景已停用或未配置可用模型，请在 设置 → AI 模型 中启用场景并配置模型连接",
        "en": "{label}: the scenario is disabled or no usable model is configured. Go to Settings → "
              "AI Models to enable the scenario and set up a model connection.",
    },
}


def t(key: str, lang: str = "zh", **kw) -> str:
    """按 key+语言取错误/提示文案；kw 做占位格式化（str.format）。未登记 key 原样回 key。"""
    s = pick(MSG.get(key, {}), lang) or key
    return s.format(**kw) if kw else s
