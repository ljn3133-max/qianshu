"""日志查询统一层。

把「日志查询」接到「已配置日志源」清单上，避免查询能力与 SLS/阿里云凭证绑定，
同时保持既有 SLS 默认源兼容行为：

  源清单 available_sources = LOG_SOURCES（services/datasource）
      + SLS 隐式源（阿里云凭证可用即出现——SLS 现有查询行为向后兼容=默认源语义，
        无凭证且无 LOG_SOURCES 时返回真实空态引导，不是报错）；
  多后端适配 query = sls（collector/sls）/ loki（collector/loki）
      / cls（collector/cls）/ es（collector/es_query）；
  统一形状 = {logs, is_sql, count, has_more}（各适配器同构）。

分工不抢：K8s 容器日志不在此层（k8s_pod_logs 库表，入口「K8s 管理-容器日志」）。
凭证口径：SLS 走阿里云凭证解析、CLS 走腾讯云凭证解析（与采集/探活同一兜底链），
Loki/ES 走数据源自身配置（headers/user/password/token）。
"""
from __future__ import annotations

import logging

from app.collector import credential as cred_mod
from app.services import datasource as ds

logger = logging.getLogger("qianshu.log_query")

# 隐式 SLS 源的名字（未在集成中心登记 SLS、但阿里云凭证可用时合成；
# 登记过 sls 类型日志源则以登记为准，不重复合成）
IMPLICIT_SLS_NAME = "SLS（阿里云凭证）"

# 无任何可查询日志源时的真实空态引导（前端空态引导与本串同义）
GUIDE = "到「集成中心 → 数据源」配置日志源：Loki / Elasticsearch / 腾讯云 CLS / SLS"

_NO_CREDS_ALIYUN = ("未配置阿里云凭证：请到「云账号管理」添加只读账号，"
                    "或配置环境变量 ALIBABA_CLOUD_ACCESS_KEY_ID / ALIBABA_CLOUD_ACCESS_KEY_SECRET")
_NO_CREDS_TENCENT = ("未配置腾讯云凭证：请到「云账号管理」添加只读账号，"
                     "或配置环境变量 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY")


class LogSourceError(RuntimeError):
    """源不可用/参数缺失。status=建议 HTTP 状态码（400 参数、503 缺凭证/无源、502 上游）。"""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


# ---------------- 凭证（与 collector/credential 兜底链同源） ----------------

def _aliyun_creds(db) -> tuple:
    try:
        ak, sk, _src = cred_mod.resolve_sls_credential(db)
    except cred_mod.CredentialError as e:
        raise LogSourceError(_NO_CREDS_ALIYUN, 503) from e
    if not ak or not sk:
        raise LogSourceError(_NO_CREDS_ALIYUN, 503)
    return ak, sk


def _tencent_creds(db) -> tuple:
    try:
        ak, sk, _src = cred_mod.resolve_tencent_credential(db)
    except cred_mod.CredentialError as e:
        raise LogSourceError(_NO_CREDS_TENCENT, 503) from e
    if not ak or not sk:
        raise LogSourceError(_NO_CREDS_TENCENT, 503)
    return ak, sk


def _sls_cred_source(db) -> str:
    """阿里云凭证是否可用（返回来源标签，空串=不可用）。不抛异常。

    查询失败在保存点内回滚：旧凭证表缺失等降级场景不能把调用方事务打成 aborted。
    """
    try:
        with db.begin_nested():
            ak, sk, src = cred_mod.resolve_sls_credential(db)
            return src if (ak and sk) else ""
    except Exception:
        return ""


def _tencent_cred_source(db) -> str:
    try:
        with db.begin_nested():
            ak, sk, src = cred_mod.resolve_tencent_credential(db)
            return src if (ak and sk) else ""
    except Exception:
        return ""


# ---------------- 源清单 ----------------

def source_entries(db) -> list:
    """全部日志源（含未启用查询的）：LOG_SOURCES + SLS 隐式源（去重）。

    每项：{name, type, type_label, url, queryable, status, detail}。
    status: ready（可查）/ no_credential（缺凭证）/ pending（查询能力未启用）。
    顺序 = SLS 隐式源在前（默认源语义向后兼容）> LOG_SOURCES 配置顺序。
    """
    configured = ds.log_sources()
    entries: list = []
    if not any((s.get("type") or "") == "sls" for s in configured):
        cred_src = _sls_cred_source(db)
        if cred_src:
            entries.append({"name": IMPLICIT_SLS_NAME, "type": "sls", "url": "",
                            "_cred": cred_src})
    entries.extend(dict(s) for s in configured)

    out: list = []
    for e in entries:
        typ = (e.get("type") or "").strip()
        name = (e.get("name") or typ or "?").strip()
        # 原样保留配置字段（headers/user/password/token=查询适配器鉴权，loki/es 用）；
        # 对外输出（available_sources）统一脱敏，凭证绝不出 API
        item = dict(e)
        item.update({"name": name, "type": typ,
                     "type_label": ds.SOURCE_TYPE_LABELS.get(typ, typ),
                     "url": (e.get("url") or "").strip(),
                     "queryable": typ in ds.LOG_QUERY_TYPES})
        if not item["queryable"]:
            item["status"] = "pending"
            item["detail"] = (f"{item['type_label']}：本期支持登记与连通测试；"
                              "查询能力随版本更新自动启用")
        elif typ == "sls":
            cred = e.get("_cred") or _sls_cred_source(db)
            if cred:
                item["status"] = "ready"
                item["detail"] = f"阿里云凭证已配置（{cred}）"
            else:
                item["status"] = "no_credential"
                item["detail"] = _NO_CREDS_ALIYUN
        elif typ == "cls":
            cred = _tencent_cred_source(db)
            if cred:
                item["status"] = "ready"
                item["detail"] = f"腾讯云凭证已配置（{cred}）"
            else:
                item["status"] = "no_credential"
                item["detail"] = _NO_CREDS_TENCENT
        else:  # loki / es：URL 已配即可查（连通看集成中心「测试连通」）
            if item["url"]:
                item["status"] = "ready"
                item["detail"] = item["url"]
            else:
                item["status"] = "pending"
                item["detail"] = (f"{item['type_label']}：缺少服务地址"
                                  "（到 集成中心-数据源 补填 URL）")
        # queryable 必须与 status 同口径：SLS 已登记但无凭证时并不可查；
        # 若仅按类型判定，日志页会缺少空态引导，下拉仍可选并在查询时报错。
        # 只有真正可查（status=ready）的源才可选。
        item["queryable"] = item["status"] == "ready"
        out.append(item)
    return out


def available_sources(db) -> dict:
    """GET /logs/sources 形状：源清单 + 默认源 + 空态引导。

    empty=True（无可查询源）时 guide 给真实引导（到集成中心配置），不报错。
    输出面脱敏：headers/user/password/token/内部键绝不出 API（凭证不给前端）。
    """
    entries = source_entries(db)
    pub = [{k: v for k, v in e.items()
            if k not in ("headers", "user", "password", "token")
            and not k.startswith("_")} for e in entries]
    queryable = [e for e in pub if e["queryable"]]
    default_name = queryable[0]["name"] if queryable else None
    for e in pub:
        e["default"] = (e["name"] == default_name)
    return {"sources": pub, "default": default_name,
            "empty": not queryable,
            "guide": "" if queryable else GUIDE}


def resolve_source(db, name: str = "", project: str = "", logstore: str = "") -> dict:
    """查询用源解析（缺省=第一个可用源；project+logstore 齐=旧 SLS 调用语义）。"""
    all_entries = source_entries(db)
    if name:
        for e in all_entries:
            if e["name"] == name:
                if not e["queryable"]:
                    raise LogSourceError(
                        f"日志源「{name}」当前不可查询："
                        f"{e.get('detail') or e['type_label']}", 400)
                return e
        raise LogSourceError(f"日志源不存在: {name}（先到集成中心配置）", 400)
    queryable = [e for e in all_entries if e["queryable"]]
    # 默认源语义（SLS 现有查询行为向后兼容）：project/logstore 齐的旧调用落 SLS 源
    if project and logstore:
        for e in queryable:
            if e["type"] == "sls":
                return e
        raise LogSourceError("未配置可用的 SLS 日志源：" + GUIDE, 503)
    if not queryable:
        raise LogSourceError("未配置任何可查询的日志源：" + GUIDE, 503)
    return queryable[0]


# ---------------- 目标清单（SLS project / CLS 主题 / ES 索引） ----------------

def targets(db, source_name: str = "") -> dict:
    """GET /logs/targets 形状：{kind, source, type, items:[{id,name,detail}]}。

    sls -> projects（二层 logstore 走既有 /logs/logstores）；
    cls -> 日志主题；es -> 索引（隐藏索引剔除）；loki -> 无目标（LogQL 自带流选择）。
    """
    src = resolve_source(db, name=source_name)
    typ = src["type"]
    base = {"kind": "none", "source": src["name"], "type": typ, "items": []}
    try:
        if typ == "sls":
            from app.collector import sls as sls_mod

            ak, sk = _aliyun_creds(db)
            items = [{"id": p, "name": p, "detail": ""}
                     for p in sls_mod.list_projects(ak, sk, endpoint=src.get("url") or "")]
            return dict(base, kind="projects", items=items)
        if typ == "cls":
            from app.collector import cls as cls_mod

            ak, sk = _tencent_creds(db)
            items = [{"id": t["topic_id"],
                      "name": t["topic_name"] or t["topic_id"],
                      "detail": f"日志集 {t['logset_id']}" if t.get("logset_id") else ""}
                     for t in cls_mod.list_topics(ak, sk, url=src.get("url") or "")]
            return dict(base, kind="topics", items=items)
        if typ == "es":
            from app.collector import es_query as es_mod

            items = [{"id": x["index"], "name": x["index"],
                      "detail": f"docs {x['docs']}" if x.get("docs") else ""}
                     for x in es_mod.list_indices(
                         src.get("url") or "",
                         headers=es_mod.auth_headers(src),
                         verify_ssl=bool(src.get("verify_ssl")))]
            return dict(base, kind="indices", items=items)
        return base
    except LogSourceError:
        raise
    except Exception as e:
        logger.exception("日志源目标清单失败（source=%s）", src["name"])
        raise LogSourceError(f"目标清单失败: {str(e)[:200]}", 502) from e


# ---------------- 统一查询 ----------------

def query(db, src: dict, query_str: str, start: int, end: int,
          limit: int = 20, offset: int = 0,
          project: str = "", logstore: str = "", target: str = "",
          reverse: bool = True) -> dict:
    """按源类型分发查询（只读）。返回统一形状 {logs, is_sql, count, has_more}。"""
    typ = src.get("type") or ""
    if typ == "sls":
        from app.collector import sls as sls_mod

        if not project or not logstore:
            raise LogSourceError("SLS 查询需要 project 与 logstore", 400)
        ak, sk = _aliyun_creds(db)
        return sls_mod.query_logs(ak, sk, project, logstore, query_str,
                                  start=start, end=end, limit=limit,
                                  offset=offset, reverse=reverse,
                                  endpoint=src.get("url") or "")
    if typ == "cls":
        from app.collector import cls as cls_mod

        if not target:
            raise LogSourceError("CLS 查询需要日志主题（target=TopicId）", 400)
        ak, sk = _tencent_creds(db)
        return cls_mod.query_cls(ak, sk, target, query_str,
                                 start=start, end=end, limit=limit,
                                 url=src.get("url") or "")
    if typ == "es":
        from app.collector import es_query as es_mod

        if not target:
            raise LogSourceError(
                "ES 查询需要索引（target=索引名，支持通配如 logs-*）", 400)
        return es_mod.query_es(src.get("url") or "", target, query_str,
                               start=start, end=end, limit=limit, offset=offset,
                               headers=es_mod.auth_headers(src),
                               verify_ssl=bool(src.get("verify_ssl")))
    if typ == "loki":
        from app.collector import loki as loki_mod

        q = (query_str or "").strip()
        if not q:
            raise LogSourceError(
                'Loki 查询需要 LogQL（至少一个流选择器，如 {app="nginx"}）', 400)
        return loki_mod.query_loki(src.get("url") or "", q, start, end,
                                   limit=limit,
                                   headers=src.get("headers") or None,
                                   verify_ssl=bool(src.get("verify_ssl")))
    raise LogSourceError(f"日志源类型 {typ} 查询能力未启用", 400)
