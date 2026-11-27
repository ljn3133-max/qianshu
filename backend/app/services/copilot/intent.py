"""意图四分类：query / analyze / change / smalltalk。

默认使用规则/关键词分类（确定性、零成本、可测试）；classify_query() 是可替换的
分类接口，换成 LLM/小模型分类时无需修改调用方。
"""
from __future__ import annotations

INTENTS = ("query", "analyze", "change", "smalltalk")

INTENT_LABELS = {
    "query": "查询",
    "analyze": "分析",
    "change": "变更",
    "smalltalk": "闲聊",
}

# 变更类动词（命中后进入变更计划路径）；"改成/改为/换成/重命名"等句式必须覆盖，
# 否则"把X改成Y"这类自然表达会误落查询路径
_CHANGE_KW = (
    "修改", "更改", "删除", "移除", "重启", "关机", "停机", "开机", "创建", "新增",
    "添加", "接入", "配置", "执行", "下发", "回滚", "升级", "部署", "扩容", "缩容",
    "调参", "调阈值", "打开", "开启", "关闭", "停用", "禁用", "批量", "替我", "帮我改",
    "帮我删", "帮我重", "帮我配", "画一下拓扑", "改一下",
    "改成", "改为", "换成", "重命名", "改名", "调成", "设为", "设置成",
)

_ANALYZE_KW = (
    "为什么", "为啥", "原因", "分析", "诊断", "排查", "定位", "归因", "异常", "报错",
    "故障", "对比", "趋势", "涨了", "跌了", "突增", "突降", "变慢", "超了", "耗尽",
    "体检", "怎么看", "咋回事", "什么问题", "帮我看看", "问题出在",
)

_QUERY_KW = (
    "多少", "哪些", "什么", "怎么", "查询", "查一下", "查下", "看看", "列表", "清单",
    "现状", "统计", "有没有", "是否", "现在", "目前", "最近", "概况", "总览", "概览",
    "有哪些", "有几个", "是谁", "什么时候", "哪些要到期", "top", "TOP",
)

_SMALLTALK_KW = (
    "你好", "您好", "在吗", "在么", "hello", "hi", "hey", "谢谢", "感谢", "再见",
    "你是谁", "你能做什么", "你能干什么", "自我介绍", "help", "帮助", "怎么用",
)


def classify(text: str) -> str:
    """关键词规则分类。优先级：闲聊 > 变更 > 分析 > 查询；默认查询。

    规则刻意保守：宁可多给工具（只读零风险），不可漏判变更类（变更必须收口）。
    """
    t = (text or "").strip()
    if not t:
        return "smalltalk"
    low = t.lower()
    # 短句问候先判闲聊（避免"你好，帮我查成本"被闲聊误吞——超过 12 字不判闲聊）
    if len(t) <= 12 and any(k in low for k in _SMALLTALK_KW):
        return "smalltalk"
    if any(k in t for k in _CHANGE_KW) or any(k in low for k in ("delete", "restart", "reboot")):
        return "change"
    if any(k in t for k in _ANALYZE_KW) or any(k in low for k in ("why", "root cause")):
        return "analyze"
    if any(k in t for k in _QUERY_KW):
        return "query"
    return "query"


def classify_query(db, text: str) -> str:
    """可替换的分类入口：换 LLM/小模型分类时只改本函数，签名保持兼容。

    当前实现为纯规则；db 参数预留（分类可记 ai_query_logs 等留痕）。
    """
    return classify(text)
