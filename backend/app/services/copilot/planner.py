"""工作流匹配：关键词+意图+参数抽取 → 命中走工作流，未命中走工具循环。

契约（loop 调用）：
  plan(db, text, intent) -> {"mode": "workflow"|"tools", "workflow_key": str|None, "reason": str}
  （附加 "params"：从自然语言抽出的工作流参数——契约三键之外的补充字段，调用方可忽略）

匹配原则（"没把握就回 tools，宁可自由工具循环"）：
  - 强短语（完整场景词，如"成本体检"）直接命中；弱关键词（如"成本"）需累计 ≥2 分
  - 意图闸门：smalltalk 一律 tools；接入域工作流要求显式接入动作（强短语已含动词）
  - 多个工作流同分 -> 判为无把握 -> tools
"""
from __future__ import annotations

import re

from app.services.copilot.intent import classify

# 强短语（score 3）/ 弱关键词（score 1）；小写匹配
_MATCHERS: dict = {
    "access_cloud": {
        "strong": ("接入云账号", "接云账号", "添加云账号", "新增云账号", "云账号接入",
                   "接入阿里云", "接阿里云", "阿里云接入", "接入腾讯云", "接腾讯云", "腾讯云接入",
                   "接入火山云", "接火山云", "火山云接入", "接入华为云", "接华为云",
                   "接入百度云", "接入aws", "接入azure", "access_cloud", "云账号接入"),
        "weak": ("云账号", "accesskey", "ak/sk", "ram", "只读账号"),
    },
    "access_datasource": {
        "strong": ("接入数据源", "接数据源", "添加数据源", "新增数据源", "数据源接入",
                   "接入指标源", "接入日志源", "接入日志", "接入指标", "access_datasource"),
        "weak": ("数据源", "指标源", "日志源", "prometheus", "loki", "zabbix"),
    },
    "access_k8s": {
        "strong": ("接入k8s", "接k8s", "k8s接入", "接入kubernetes", "接kubernetes",
                   "添加集群", "新增集群", "接入集群", "接集群", "集群接入", "access_k8s"),
        "weak": ("k8s", "kubernetes", "集群", "kube"),
    },
    "access_virt": {
        "strong": ("接入esxi", "接esxi", "esxi接入", "接入vcenter", "接vcenter", "vcenter接入",
                   "接入kvm", "接kvm", "kvm接入", "接入虚拟化", "虚拟化接入", "access_virt"),
        "weak": ("esxi", "vcenter", "kvm", "虚拟化", "libvirt", "vsphere"),
    },
    "cost_checkup": {
        "strong": ("成本体检", "费用体检", "成本检查", "费用检查", "省钱体检",
                   "成本健康", "cost_checkup", "花多少钱体", "成本体个检"),
        "weak": ("成本", "账单", "费用", "资源包", "省钱", "花了多少"),
    },
    "fault_diagnosis": {
        "strong": ("故障诊断", "故障排查", "排障", "故障定位", "故障分析", "帮我诊断",
                   "诊断一下", "诊断下", "出故障", "出问题了", "打不开了", "fault_diagnosis"),
        "weak": ("诊断", "故障", "报错", "异常", "为什么", "为啥", "咋回事"),
    },
    "capacity_checkup": {
        "strong": ("容量体检", "容量检查", "容量评估", "容量分析", "水位检查",
                   "capacity_checkup", "容量够不够"),
        "weak": ("水位", "容量", "余量", "写满", "磁盘满"),
    },
    "change_impact": {
        "strong": ("变更影响", "变更影响分析", "变更评估", "变更对齐", "这次变更",
                   "change_impact", "变更有没有影响"),
        "weak": ("变更", "影响", "发版", "上线后"),
    },
    "topology_refine": {
        "strong": ("拓扑重画", "拓扑不对", "拓扑画错", "拓扑错了", "改拓扑", "拓扑改进",
                   "拓扑修正", "拓扑归属", "后端不对", "后端错了", "野域名",
                   "归属不对", "归属错了", "改归属", "去噪点", "补链路", "topology_refine"),
        "weak": ("归属", "拓扑", "重画", "噪点", "画错"),
    },
    "alert_tune": {
        "strong": ("告警降噪", "告警调参", "告警太多", "告警太吵", "误报", "降噪", "调参",
                   "告警调一下", "调一下阈值", "改阈值", "alert_tune"),
        "weak": ("阈值", "抑制", "静默", "噪音"),
    },
    "inspection_custom": {
        "strong": ("巡检规则", "巡检定制", "定制巡检", "改巡检", "调巡检", "巡检阈值",
                   "inspection_custom"),
        "weak": ("巡检", "巡检项", "检查规则"),
    },
    "dashboard_custom": {
        "strong": ("仪表盘定制", "定制仪表盘", "改仪表盘", "仪表盘改", "首页卡片",
                   "卡片定制", "换卡片", "定制卡片", "dashboard_custom"),
        "weak": ("仪表盘", "卡片", "产品卡"),
    },
}

# 参数抽取用的触发短语（从 target 剥离）
_TRIGGER_NOISE = (
    "帮我", "请", "麻烦", "一下", "一个", "做个", "做下", "来个", "进行", "分析", "诊断",
    "排查", "体检", "检查", "看看", "查一下", "查下", "看下", "请问", "我想", "需要",
    "接入", "添加", "新增", "登记", "对齐", "评估", "定位",
    "成本", "费用", "容量", "故障", "变更影响", "变更", "水位", "资源包", "账单",
    "了", "吗", "吧", "呢", "的", "呀",
)

_VENDOR_MAP = {
    "阿里云": "aliyun", "腾讯云": "tencent", "火山云": "volc", "火山": "volc",
    "华为云": "huawei", "百度云": "baidu", "亚马逊": "aws", "aws": "aws",
    "azure": "azure", "微软云": "azure", "自建": "onprem", "idc": "onprem",
}

_WINDOW_PAT = (
    (r"近\s*1\s*(?:小时|h)", "1h"), (r"一小时", "1h"), (r"1h", "1h"),
    (r"近\s*6\s*(?:小时|h)", "6h"), (r"6h", "6h"),
    (r"近\s*24\s*(?:小时|h)", "24h"), (r"24h", "24h"), (r"一天", "24h"), (r"当天", "24h"),
)

# 并列连词：一句话点名多个工作流（"成本和容量体检"）= 没把握 -> tools
_COORD = ("和", "与", "以及", "还有", "然后", "顺便", "都", "、", "+")


def _score(text: str, spec: dict) -> tuple:
    """(总分, 命中证据列表)。强短语 3 分，弱关键词 1 分。"""
    score = 0
    hits = []
    for p in spec.get("strong", ()):  # noqa: B007
        if p in text:
            score += 3
            hits.append(p)
    for p in spec.get("weak", ()):
        if p in text:
            score += 1
            hits.append(p)
    return score, hits


def _extract_params(text: str, key: str) -> dict:
    """从自然语言抽取工作流参数（尽力而为，抽不出就留给表单/默认值）。"""
    low = (text or "").lower()
    params: dict = {}
    m = re.search(r"(\d+)\s*天", text)
    if m:
        params["days"] = int(m.group(1))
    m = re.search(r"(\d+)\s*(?:小时|h(?![a-z]))", low)
    if m:
        params["hours"] = int(m.group(1))
    for pat, win in _WINDOW_PAT:
        if re.search(pat, low):
            params["window"] = win
            break
    for zh, code in _VENDOR_MAP.items():
        if zh in text or zh in low:
            params["vendor"] = code
            break
    m = re.search(r"账号\s*(?:id\s*[:=]?)?(\d+)", text)
    if m:
        params["account_id"] = int(m.group(1))

    # target：剥触发短语与标点，剩下的短词当目标（域名/实例/应用名）
    if key in ("fault_diagnosis", "change_impact", "topology_refine"):
        t = text
        for p in _MATCHERS.get(key, {}).get("strong", ()):
            t = t.replace(p, " ")
        for w in _TRIGGER_NOISE:
            t = t.replace(w, " ")
        t = re.sub(r"[\s，。！？、：:；;（）()【】\[\]\"'‘’“”?？!！]+", "", t)
        if 1 <= len(t) <= 40:
            params["target"] = t
    # 改进域：反馈/需求/偏好原文透传（⑨⑪⑫ 的步骤要全文解析，参数抽不全就靠原文）
    if key == "topology_refine":
        params["feedback"] = text
    elif key == "inspection_custom":
        params["requirement"] = text
    elif key == "dashboard_custom":
        params["preference"] = text
    return params


def plan(db, text: str, intent: str = None) -> dict:
    """工作流匹配主入口。返回 {mode, workflow_key, reason}（+ params 补充字段）。

    没把握就回 tools：宁可自由工具循环，不硬套工作流（契约硬要求）。
    """
    text = (text or "").strip()
    it = intent or classify(text)
    if it == "smalltalk":
        return {"mode": "tools", "workflow_key": None, "workflow": None,
                "reason": "闲聊/能力咨询不走工作流，交给自由工具循环直答", "params": {}}
    if not text:
        return {"mode": "tools", "workflow_key": None, "workflow": None, "reason": "空输入", "params": {}}

    low = text.lower()
    scores: list = []
    for key, spec in _MATCHERS.items():
        s, hits = _score(low, spec)
        if s > 0:
            scores.append((s, key, hits))
    if not scores:
        return {"mode": "tools", "workflow_key": None, "workflow": None,
                "reason": "无工作流关键词命中，走自由工具循环", "params": {}}

    scores.sort(key=lambda x: -x[0])
    top_s, top_key, top_hits = scores[0]
    # 阈值：强短语命中（≥3）或弱信号累计 ≥2；否则没把握
    if top_s < 2:
        return {"mode": "tools", "workflow_key": None, "workflow": None,
                "reason": f"弱信号不足（{'、'.join(top_hits)}，得分 {top_s} < 2），宁可回自由工具循环",
                "params": {}}
    # 并列第一 -> 没把握
    if len(scores) > 1 and scores[1][0] == top_s:
        tied = "、".join(k for s2, k, _ in scores if s2 == top_s)
        return {"mode": "tools", "workflow_key": None, "workflow": None,
                "reason": f"多个工作流同分命中（{tied}），无把握不硬套，回自由工具循环",
                "params": {}}
    # 并列连词点名多个工作流（"成本和容量体检"）-> 没把握
    if (len(scores) > 1 and any(c in text for c in _COORD)
            and any(s2 >= 1 and k != top_key for s2, k, _ in scores)):
        others = "、".join(k for s2, k, _ in scores if k != top_key and s2 >= 1)
        return {"mode": "tools", "workflow_key": None, "workflow": None,
                "reason": f"一句话点名多个工作流（{top_key} 与 {others}），无把握不硬套，回自由工具循环",
                "params": {}}

    reason = f"命中工作流 {top_key}（关键词：{'、'.join(dict.fromkeys(top_hits))}，得分 {top_s}）"
    params = _extract_params(text, top_key)
    return {
        "mode": "workflow",
        "workflow_key": top_key,
        "reason": reason,
        "params": params,
        # loop 兼容别名（loop._try_workflow 按 plan["workflow"] 取 key）
        "workflow": top_key,
    }
