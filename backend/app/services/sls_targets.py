"""SLS 目标统一解析（千枢产品态）。

SLB/应用日志/CDN 三类目标一处解析、全站共用（diag_tools / object_profile /
profile_engine / cdn_traffic / host_watch / link_draft / inspection 全走这里）。
换环境只改 SLS_* 配置或走安装向导，不改代码。

产品原则：**默认全空，绝不内置客户环境事实**。未配置时：
- slb_targets() 返回 []、app_log()/cdn_target()/outer_slb() 返回 (None, None)
- 消费方拿空目标走查询会在 collector/sls.py 的统一守卫处收到明确的"未配置"错误，
  各调用点已有 try/except，能力整体优雅降级（说"未配置"而不是崩）

配置键（app/core/config.py，安装向导可代写 .env）：
  SLS_SLB_TARGETS   [[project, logstore, chain], ...]   外层/内层 SLB 访问日志
  SLS_APP_LOG       [project, logstore]                 应用日志库
  SLS_CDN_TARGET    [project, logstore]                 CDN 实时日志库
"""
import json
import logging

from app.core.config import settings

logger = logging.getLogger("qianshu.sls_targets")


def _parse(raw):
    """JSON 配置串解析，空/坏值返回 None（坏值 warn 留痕，视为未配置）。"""
    if not raw or not str(raw).strip():
        return None
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("SLS 目标配置解析失败（视为未配置）: %r", str(raw)[:80])
        return None


def slb_targets() -> list:
    """[(project, logstore, chain), ...]——SLB 访问日志目标（chain=outer/inner）。

    兼容回退：SLS_LINK_TARGETS（老部署迁移期）。
    """
    for key in ("SLS_SLB_TARGETS", "SLS_LINK_TARGETS"):
        raw = _parse(getattr(settings, key, ""))
        if raw:
            return [tuple(x) for x in raw]
    return []


def app_log() -> tuple:
    """(project, logstore)——应用日志库；未配置返回 (None, None)。"""
    for key in ("SLS_APP_LOG", "SLS_LINK_APP_LOG"):
        raw = _parse(getattr(settings, key, ""))
        if raw and len(raw) >= 2:
            return (raw[0], raw[1])
    return (None, None)


def cdn_target() -> tuple:
    """(project, logstore)——CDN 实时日志库；未配置返回 (None, None)。"""
    raw = _parse(getattr(settings, "SLS_CDN_TARGET", ""))
    if raw and len(raw) >= 2:
        return (raw[0], raw[1])
    return (None, None)


def outer_slb() -> tuple:
    """(project, logstore)——外层 SLB（chain=outer；无标签时取第一个目标；未配置 (None, None)）。"""
    targets = slb_targets()
    for project, logstore, chain in targets:
        if chain == "outer":
            return (project, logstore)
    if targets:
        return (targets[0][0], targets[0][1])
    return (None, None)


def slb_pairs() -> list:
    """[(project, logstore), ...]——不带 chain 标签的形态（对象全景等消费方）。"""
    return [(p, l) for p, l, _ in slb_targets()]


def configured() -> bool:
    """任一 SLS 目标已配置（底座体检用：日志面是否就绪的判据之一）。"""
    return bool(slb_targets() or app_log()[0] or cdn_target()[0])
