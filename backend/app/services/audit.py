"""审计日志服务：任何模块记录审计统一走这里，调用方不必感知 session 细节。

ACTION_LABELS 是全站审计动作的中文名唯一清单；humanize() 生成一句话摘要，
列表页直接展示中文动作和业务对象，不依赖用户理解内部动作码。
"""
import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.audit import AuditLog

# 动作码 -> 中文（新动作码必须在此登记；未登记的回落原码+提示）
ACTION_LABELS: dict = {
    "login": "登录系统",
    "logout": "退出系统",
    "user.create": "创建用户",
    "user.update": "修改用户",
    "user.delete": "删除用户",
    "user.password.reset": "重置用户密码",
    "credential.create": "新增云凭证",
    "credential.update": "修改云凭证",
    "credential.delete": "删除云凭证",
    "credential.reveal": "查看凭证明文",
    "alerts.channel.create": "新增通知渠道",
    "alerts.channel.update": "修改通知渠道",
    "alerts.channel.delete": "删除通知渠道",
    "alerts.channel.test": "发送渠道测试消息（真实推送）",
    "alerts.rule.create": "新增告警规则",
    "alerts.rule.update": "修改告警规则",
    "alerts.rule.delete": "删除告警规则",
    "alerts.eval.run": "手动执行告警评估",
    "alerts.rule.auto_create": "自动分析并创建告警规则",
    "alert.record_resolve": "标记告警已恢复",
    "alert.record.ack": "确认/忽略告警（静默提醒）",
    "alert.record.unack": "取消告警静默",
    "alert.baseline_accept": "设为常态（记录机器水位）",
    "policy.module.update": "修改监控模块提醒开关",
    "policy.account.update": "调整数据源监控策略",
    "expiry.ignore.update": "设置/取消到期忽略",
    "asset.sync": "同步云资产",
    "asset.tier": "设置资产关键度",
    "metrics.collect": "手动采集指标",
    "cost.collect": "采集成本账单",
    "cost.daily.export": "导出成本明细 CSV",
    "assets.export": "导出资产台账 CSV",
    "alerts.records.export": "导出告警记录 CSV",
    "cost.detect": "执行成本异动检测",
    "cost.anomaly.ack": "确认/处理成本异动",
    "inspection.run": "执行巡检",
    "inspection.report.generate": "生成巡检日报",
    "inspection.push_settings.update": "修改日报推送设置",
    "inspection.rule.update": "更新巡检规则配置",
    "link.draft.generate": "生成/刷新链路草稿",
    "link.edge.status": "确认链路边状态",
    "business.create": "新建业务线",
    "business.update": "修改业务线",
    "business.delete": "删除业务线",
    "business.draft_generate": "自动生成业务线草稿",
    "business.confirm": "确认业务归属草稿",
    "business.ident.add": "挂接业务标识",
    "business.ident.delete": "移除业务标识",
    "diagnosis.run": "执行故障诊断",
    "diagnosis.case.create": "存入诊断案例",
    "diagnosis.case.accept": "采纳AI建议为经验案例",
    "diagnosis.case.update": "编辑诊断案例",
    "logs.saved_query.create": "保存常用查询",
    "logs.saved_query.delete": "删除常用查询",
    "ai_setting.create": "新增 AI 模型配置",
    "ai_setting.update": "修改 AI 模型配置",
    "ai_setting.delete": "删除 AI 模型配置",
    "ai_setting.test": "测试 AI 模型连通",
    "ai_scenario.update": "修改 AI 场景模型绑定",
    "dba.space_collect": "采集数据库空间快照",
    "profile.accept": "接受行为画像新常态",
    "profile.run": "手动采样行为画像",
    "expiry.domain_register": "登记外部域名到期",
    "expiry.domain_remove": "删除外部域名登记",
    "ecs.start": "开机 ECS 实例",
    "ecs.stop": "关闭 ECS 实例",
    "ecs.reboot": "重启 ECS 实例",
    "vmware.start": "开机 VMware 虚拟机",
    "vmware.stop": "关闭 VMware 虚拟机",
    "vmware.reboot": "重启 VMware 虚拟机",
    "settings.sdk_scan": "手动扫描 SDK 版本",
    "settings.update": "修改系统参数",
    "settings.holiday_sync": "同步节假日日历",
    "settings.components_update": "更新组件识别规则",
    "settings.allowed_hosts": "更新访问域名白名单",
    "settings.product_cards_update": "更新首页产品卡",
    "agent.session.create": "新建智能体会话",
    "agent.session.delete": "删除智能体会话",
    "agent.change.create": "生成智能体变更工单",
    "agent.change.confirm": "确认执行智能体变更",
    "agent.change.execute": "执行智能体变更工单",
    "agent.change.rollback": "回滚智能体变更",
    "agent.change.dismiss": "取消智能体变更工单",
    "agent.change.skip": "跳过智能体变更项",
    "agent.change.unskip": "恢复智能体变更项",
    "agent.schedule.create": "新建工作流定时体检",
    "agent.schedule.update": "修改工作流定时体检",
    "agent.workflow.create": "新建自定义工作流",
    "agent.workflow.update": "修改自定义工作流",
    "agent.workflow.delete": "删除自定义工作流",
    "agent.workflow.save_as": "内置工作流另存为自定义",
    "agent.workflow.restore": "恢复自定义工作流历史版本",
    "agent.workflow.import": "导入自定义工作流",
    "copilotd.detect": "千枢守护：检测到平台异常",
    "copilotd.heal": "千枢守护：定位并修复平台",
    "copilotd.restart": "千枢守护：重启平台服务",
    "copilotd.recovered": "千枢守护：平台已恢复",
    "cloud.account.ack": "云凭证安全警示确认",
    "cloud_account.create": "新增云账号",
    "cloud_account.update": "修改云账号",
    "cloud_account.delete": "删除云账号",
    "cloud_account.test": "云账号连通测试",
    "cloud.sync": "按账号同步云资产",
    "ownership.create": "新增归属登记",
    "ownership.update": "修改归属登记",
    "profile.know": "标记画像已知道",
    "user_api_token.create": "创建 API Token",
    "user_api_token.revoke": "吊销 API Token",
    "code.apply": "应用代码变更（平台自改）",
    "code.rollback": "回滚代码变更（平台自改）",
    "code.restart": "重启生效（代码变更委托重启平台）",
    "issue.plan.approved": "授权平台自愈计划",
    "issue.plan.rejected": "拒绝平台自愈计划",
    "issue.plan.expired": "平台自愈计划过期",
    "issue.repair.started": "开始执行平台自愈计划",
    "issue.repair.rolled_back": "回滚平台自愈代码补丁",
    "issue.repair.restart": "平台自愈委托重启千枢后端",
    "issue.repair.verified": "平台自愈执行后验证通过",
    "issue.repair.failed": "平台自愈执行失败",
    "issue.breaker.reset": "重置平台自愈熔断",
    "department.create": "新建部门",
    "department.update": "修改部门",
    "department.delete": "删除部门",
    "department.reorder": "部门排序",
    "dept_permission.set": "设置部门权限策略",
    "user.department.set": "分配用户部门",
    "namespace_grant.set": "设置 Namespace 授权",
    "namespace_grant.delete": "删除 Namespace 授权",
    # K8s/机器操作复用 humanize 中文摘要，避免列表回落为英文动作码。
    "k8s.cluster_update": "修改 K8s 集群配置",
    "k8s.cluster_delete": "删除 K8s 集群配置",
    "k8s.pod_exec.session": "Pod 终端会话",
    "k8s.secret.reveal": "揭示 Secret 明文",
    "k8s.failures_confirm": "确认 K8s 失败条目",
    "k8s.failures_ignore": "忽略 K8s 失败条目",
    "machine.create": "新增纳管机器",
    "machine.delete": "移除纳管机器",
    "machine.ssh.session": "SSH 终端会话",
    # 动作码须与调用点同步登记；质量门 G 项会拦截新增的未登记动作。
    "asset.tier.batch": "批量设置资产关键度",
    "auth.change_password": "修改登录密码",
    "otp.enable": "启用动态口令（OTP）",
    "permission.set": "设置用户权限策略",
    "bootstrap.ai_init": "安装向导：初始化 AI 配置",
    "bootstrap.apply_config": "安装向导：应用配置",
    "bootstrap.complete": "完成安装向导",
    "cloud.sls_discover": "发现 SLS 完整目录与日志语义",
    "cloud.sls_targets_skip": "暂不配置 SLS 日志目标",
    "change.sync": "同步变更记录",
    "domain.backfill": "回填域名数据",
    "domain.update": "修改域名登记",
    "ownership.delete": "删除归属登记",
    "profile.object_know": "标记对象画像已知道",
    "profile.object_run": "手动采样对象画像",
    "topology.scan": "扫描资产拓扑",
}

_RESOURCE_LABELS = {
    "user": "用户", "credential": "云凭证", "alert_channel": "通知渠道",
    "alert_rule": "告警规则", "cost": "成本", "inspection": "巡检",
    "link_edge": "链路边", "business": "业务线", "diagnosis": "诊断",
    "ai_setting": "AI 模型", "ai_scenario": "AI 场景", "dba": "数据库", "domain": "域名",
    "asset": "资产", "metrics": "指标", "ecs": "ECS 实例", "vmware": "VMware 虚拟机",
    "audit": "审计",
    "user_api_token": "API Token", "code_mod": "代码变更",
    "department": "部门", "namespace_grant": "Namespace 授权",
}

# 摘要里值得带出来的 detail 键（按优先级取前几个，避免整段 JSON 糊在列表上）
_SUMMARY_KEYS = ("name", "username", "domain", "title", "expire_time",
                 "status", "threshold", "metric", "action", "type", "note")


def humanize(action: str, resource_type: str = "", resource_id: str = "",
             detail: str = "", result: str = "ok") -> dict:
    """一条审计记录的中文视图：{action_label, summary}。"""
    label = ACTION_LABELS.get(action, action)
    parts: dict = {}
    if detail:
        try:
            parts = json.loads(detail)
            if not isinstance(parts, dict):
                parts = {}
        except ValueError:
            parts = {}
    bits: list = []
    for k in _SUMMARY_KEYS:
        v = parts.get(k)
        if v not in (None, "", [], {}):
            bits.append(f"{k}={v}" if k in ("threshold", "metric") else str(v))
        if len(bits) >= 3:
            break
    seg: list = [label]
    if action.startswith(("ecs.", "vmware.")):
        seg.append(str(parts.get("instance_id") or resource_id or ""))
    elif resource_id and action not in ("login", "logout", "metrics.collect",
                                       "cost.collect", "cost.detect", "inspection.run",
                                       "alerts.eval.run", "dba.space_collect",
                                       "link.draft.generate", "business.draft_generate",
                                       "diagnosis.run", "inspection.report.generate"):
        seg.append(f"「{resource_id}」")
    if bits:
        seg.append("（" + "，".join(bits) + "）")
    summary = "".join(seg)
    if result and result != "ok":
        summary += f" · 结果:{result}"
    return {"action_label": label, "summary": summary}


def record(
    db: Session,
    *,
    action: str,
    user_id: Optional[int] = None,
    username: str = "",
    resource_type: str = "",
    resource_id: str = "",
    detail: Any = None,
    result: str = "ok",
    client_ip: str = "",
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            username=username,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            detail=json.dumps(detail, ensure_ascii=False, default=str) if detail else "",
            result=result,
            client_ip=client_ip,
        )
    )
    # 审计写入随当前事务提交；调用方若异常回滚，则审计与业务一致地不落库
