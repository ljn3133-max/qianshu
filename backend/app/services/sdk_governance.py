"""云 SDK 治理：SDK 清单 + 版本扫描 + 废弃感知。

背景（多云 SDK 维护的三个坑）：
1. 破坏性升级会炸生产——厂商大版本升级常带接口变更，自动升级=事故；
2. 废弃不感知——SDK 被厂商停更/下架后线上还在用，出问题才发现；
3. 多厂商节奏不同——阿里云/腾讯云各自发版，人工盯不过来。

策略：**不自动升级**，锁版本 + 每周扫描报告 + 人工确认升级（SOP 三步：
更新 requirements.txt 固定版本 → 跑冒烟 → 发布）。Provider 抽象层隔离
（app/services/cloud/），SDK 变更只影响 provider 文件，升级影响面可控。

实现：
- SDK_MANIFEST：已知云 SDK 清单（厂商/用途）；build_manifest() 再并入环境里
  已装的相关 SDK（importlib.metadata），清单 = 清单 ∪ 已装；
- scan_sdk_updates()：对每个包请求 PyPI JSON API（10s/个，容错，6 并发）拿
  最新版 + yanked/版本历史，对比当前版本 ->
  [{pkg, current, latest, outdated, deprecated_hint}]（附 vendor/status 等）；
- 废弃感知（诚实边界）：只做 PyPI 侧可验证信号（查无此包/最新版 yanked/
  当前版本被撤回/停更超 2 年）；**厂商废弃公告不做假抓取**，以
  vendor_deprecation_note 字段提醒人工关注官方公告；
- 每周一 09:00 定时扫描（main._start_sdk_governance）：结果落 sdk_scan_records
  表 + 站内信《SDK 治理周报》摘要（只发站内信，绝不推外部渠道）。
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

logger = logging.getLogger("qianshu.sdk_governance")

PYPI_JSON_URL = "https://pypi.org/pypi/{pkg}/json"
FETCH_TIMEOUT = 10.0          # 单包超时（秒），容错：超时/网络错只记"检查失败"
MAX_WORKERS = 6               # 并发拉 PyPI，避免 20+ 包串行把接口拖过前端超时
STALE_DAYS = 730              # 最近发布距今超过 2 年 → 疑似停止维护
KEEP_RECORDS = 12             # 扫描记录保留条数（周扫约 3 个月）

# 厂商废弃公告需人工关注（不做假抓取）——每条结果固定带的提醒
VENDOR_DEPRECATION_NOTE = "厂商废弃公告需人工关注（阿里云/腾讯云官方公告），此处只做 PyPI 侧信号"

# 已知云 SDK 清单（包名 -> 厂商/用途）。未安装的也会跟踪（显示"未安装"+ 最新版）。
SDK_MANIFEST: list[dict] = [
    {"pkg": "aliyun-python-sdk-core", "vendor": "阿里云", "purpose": "旧版 SDK 核心（ECS/KMS 等共用）"},
    {"pkg": "aliyun-python-sdk-ecs", "vendor": "阿里云", "purpose": "旧版 ECS SDK"},
    {"pkg": "aliyun-python-sdk-kms", "vendor": "阿里云", "purpose": "KMS 密钥"},
    {"pkg": "aliyun-log-python-sdk", "vendor": "阿里云", "purpose": "SLS 日志服务"},
    {"pkg": "oss2", "vendor": "阿里云", "purpose": "对象存储 OSS"},
    {"pkg": "alibabacloud-tea-openapi", "vendor": "阿里云", "purpose": "新版 SDK 核心（Darabonba）"},
    {"pkg": "alibabacloud-ecs20140526", "vendor": "阿里云", "purpose": "新版 ECS SDK"},
    {"pkg": "tencentcloud-sdk-python", "vendor": "腾讯云", "purpose": "腾讯云统一 SDK（CVM 等）"},
]

# 自动发现已装云 SDK 的包名前缀/全名（清单 ∪ 已装，覆盖未来新装的 SDK）
_DISCOVER_PREFIXES = ("aliyun-", "alibabacloud", "tencentcloud", "volcengine", "huaweicloud", "bce-", "qcloud")
_DISCOVER_NAMES = {"oss2", "cos-python-sdk-v5"}


def _vendor_of(pkg: str) -> str:
    p = pkg.lower()
    if p.startswith(("aliyun-", "alibabacloud", "oss2")):
        return "阿里云"
    if p.startswith(("tencentcloud", "cos-", "qcloud")):
        return "腾讯云"
    if p.startswith(("volcengine",)):
        return "火山引擎"
    if p.startswith(("huaweicloud",)):
        return "华为云"
    return "其他"


def _installed_map() -> dict[str, str]:
    """环境里已装分布的 {规范化包名: 版本}（importlib.metadata，一次读全量）。"""
    from importlib import metadata

    out: dict[str, str] = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if not name:
            continue
        out[name.lower().replace("_", "-")] = dist.version
    return out


def build_manifest() -> list[dict]:
    """SDK 清单 = 内置清单 ∪ 环境已装的相关 SDK（去重，按厂商/包名排序）。"""
    installed = _installed_map()
    merged: dict[str, dict] = {}
    for item in SDK_MANIFEST:
        merged[item["pkg"].lower().replace("_", "-")] = dict(item)
    for norm, ver in installed.items():
        if norm in merged:
            continue
        if norm.startswith(_DISCOVER_PREFIXES) or norm in _DISCOVER_NAMES:
            merged[norm] = {"pkg": norm, "vendor": _vendor_of(norm), "purpose": "已装云 SDK（自动发现）"}
    out = sorted(merged.values(), key=lambda x: (x["vendor"], x["pkg"]))
    return out


def _current_version(pkg: str, installed: dict[str, str]) -> Optional[str]:
    return installed.get(pkg.lower().replace("_", "-"))


def fetch_pypi(pkg: str) -> dict:
    """PyPI JSON API 单包查询。返回 {latest, yanked_latest, yanked_latest_reason,
    yanked_versions, last_release, versions_count}。

    网络/解析异常不抛出——返回 {"error": ...}，调用方记"检查失败"，单包失败不拖垮整次扫描。
    """
    try:
        resp = httpx.get(PYPI_JSON_URL.format(pkg=pkg), timeout=FETCH_TIMEOUT, follow_redirects=True)
    except Exception as e:  # httpx 超时/网络错/URL 非法——统一容错
        return {"error": f"{type(e).__name__}: {str(e)[:80]}"}
    if resp.status_code == 404:
        return {"error": "pypi_404"}
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}
    try:
        data = resp.json()
        info = data.get("info") or {}
        releases = data.get("releases") or {}
    except (ValueError, AttributeError) as e:
        return {"error": f"响应解析失败: {str(e)[:60]}"}

    latest = info.get("version") or ""
    yanked_latest = bool(info.get("yanked"))
    times = [f.get("upload_time_iso_8601") for fs in releases.values() for f in (fs or [])
             if isinstance(f, dict) and f.get("upload_time_iso_8601")]
    # 版本历史里的撤回（yanked）标记：{版本: 撤回原因}——只记被撤回的，通常很小/为空
    yanked_versions: dict[str, str] = {}
    for ver, fs in releases.items():
        for f in (fs or []):
            if isinstance(f, dict) and f.get("yanked"):
                yanked_versions[ver] = (f.get("yanked_reason") or "")[:120]
                break
    return {
        "latest": latest,
        "yanked_latest": yanked_latest,
        "yanked_latest_reason": (info.get("yanked_reason") or "")[:120],
        "yanked_versions": yanked_versions,
        "last_release": max(times)[:10] if times else "",
        "versions_count": len(releases),
    }


# 状态码与展示文案分离：文案只用于中文兜底，前端按 code 本地化，
# 避免英文界面直接显示「可升级/已废弃」。
_STATUS_CODES = {"最新": "ok", "可升级": "outdated", "已废弃": "deprecated",
                 "未安装": "not_installed", "检查失败": "error"}


def _status_of(current: Optional[str], latest: str, deprecated: bool, error: str) -> str:
    if deprecated:
        return "已废弃"          # 404（查无此包）等强废弃信号优先于"检查失败"
    if error:
        return "检查失败"
    if current is None:
        return "未安装"
    return "最新" if current == latest else "可升级"


def scan_sdk_updates() -> list[dict]:
    """扫描全部清单包：当前版本 vs PyPI 最新版 + 废弃信号。

    输出（spec 字段必含）：[{pkg, current, latest, outdated, deprecated_hint}]
    附 vendor/purpose/status/last_release/deprecated_hint/vendor_deprecation_note。
    """
    manifest = build_manifest()
    installed = _installed_map()

    def _one(item: dict) -> dict:
        pkg = item["pkg"]
        current = _current_version(pkg, installed)
        info = fetch_pypi(pkg)

        if info.get("error"):
            err = info["error"]
            hint = ("PyPI 查无此包（可能已下架或更名）——需人工确认厂商公告"
                    if err == "pypi_404" else f"PyPI 查询失败: {err}")
            deprecated = err == "pypi_404"
            status = _status_of(current, "", deprecated, err)
            return {
                "pkg": pkg, "vendor": item.get("vendor", _vendor_of(pkg)),
                "purpose": item.get("purpose", ""),
                "current": current, "latest": "", "outdated": False,
                "status": status, "status_code": _STATUS_CODES.get(status, "unknown"),
                "deprecated_hint": hint, "last_release": "",
                "vendor_deprecation_note": VENDOR_DEPRECATION_NOTE,
            }

        latest = info.get("latest", "")
        hints: list[str] = []
        deprecated = False
        if info.get("yanked_latest"):
            deprecated = True
            reason = info.get("yanked_latest_reason") or ""
            hints.append(f"最新版 {latest} 已被作者撤回（yanked{'：' + reason if reason else ''}）")
        yanked_map = info.get("yanked_versions") or {}
        if current and current in yanked_map and not info.get("yanked_latest"):
            reason = yanked_map[current] or ""
            hints.append(f"当前版本 {current} 已被作者撤回（yanked{'：' + reason if reason else ''}）——尽快升级")
        last_release = info.get("last_release", "")
        if last_release:
            try:
                # last_release 为 YYYY-MM-DD（fetch_pypi 已截断），按日期算天数
                age_days = (datetime.now(timezone.utc).date() - date.fromisoformat(last_release)).days
                if age_days > STALE_DAYS:
                    deprecated = True
                    hints.append(f"最近发布于 {last_release}（距今约 {age_days // 365} 年），疑似停止维护")
            except ValueError:
                pass
        if current is not None and latest and current != latest:
            hints.append(f"可升级：{current} → {latest}（升级需人工确认，勿自动升级）")

        status = _status_of(current, latest, deprecated, "")
        return {
            "pkg": pkg, "vendor": item.get("vendor", _vendor_of(pkg)),
            "purpose": item.get("purpose", ""),
            "current": current, "latest": latest,
            "outdated": current is not None and latest != "" and current != latest,
            "status": status, "status_code": _STATUS_CODES.get(status, "unknown"),
            "deprecated_hint": "；".join(hints),
            "last_release": last_release,
            "vendor_deprecation_note": VENDOR_DEPRECATION_NOTE,
        }

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(_one, manifest))
    logger.info("SDK 扫描完成: %d 个包，耗时 %.1fs", len(results), time.time() - t0)
    return results


def summarize(items: list[dict]) -> dict:
    return {
        "total": len(items),
        "up_to_date": sum(1 for i in items if i["status"] == "最新"),
        "outdated": sum(1 for i in items if i["outdated"]),
        "deprecated": sum(1 for i in items if i["status"] == "已废弃"),
        "not_installed": sum(1 for i in items if i["status"] == "未安装"),
        "errors": sum(1 for i in items if i["status"] == "检查失败"),
    }


def run_scan(db: Session, trigger: str = "manual", notify: bool = False) -> dict:
    """扫描 + 落库（sdk_scan_records，保留最近 KEEP_RECORDS 条）+ 可选站内信周报。

    notify=True 只发站内信（InAppMessage），绝不推外部渠道（企微/钉钉）——
    周报是给值班人看的摘要，不是告警。
    """
    t0 = time.time()
    items = scan_sdk_updates()
    summary = summarize(items)
    duration_ms = int((time.time() - t0) * 1000)
    scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    from app.models.sdk_scan import SdkScanRecord

    db.add(SdkScanRecord(
        scanned_at=datetime.now(), trigger=trigger,
        total=summary["total"], up_to_date=summary["up_to_date"],
        outdated=summary["outdated"], deprecated=summary["deprecated"],
        not_installed=summary["not_installed"], errors=summary["errors"],
        duration_ms=duration_ms,
        results_json=json.dumps(items, ensure_ascii=False),
    ))
    # 只留最近 KEEP_RECORDS 条（周扫约 3 个月历史）。
    # 会话 autoflush=False：先 flush 让新纪录进查询结果，否则清理会多留 1 条
    db.flush()
    rows = db.query(SdkScanRecord).order_by(SdkScanRecord.id.desc()).all()
    for stale in rows[KEEP_RECORDS:]:
        db.delete(stale)

    if notify:
        _notify_weekly(db, summary, items)

    db.commit()
    return {
        "scanned_at": scanned_at, "trigger": trigger, **summary,
        "duration_ms": duration_ms, "items": items, "note": _NOTE,
    }


_NOTE = ("升级需人工确认三步：① 更新 requirements.txt 固定版本 → ② 跑冒烟（采集/诊断/巡检）→ "
         "③ 发布观察；Provider 抽象层隔离，SDK 变更只影响 provider 文件（app/services/cloud/）。"
         "系统绝不自动升级——破坏性变更会炸生产。")


def _notify_weekly(db: Session, summary: dict, items: list[dict]) -> None:
    """《SDK 治理周报》站内信（不推外部渠道，不打扰真实通知群）。"""
    flagged = [i for i in items if i["status"] in ("可升级", "已废弃", "检查失败")]
    lines = [
        f"- 共扫描 **{summary['total']}** 个包：最新 {summary['up_to_date']} / "
        f"可升级 {summary['outdated']} / 废弃信号 {summary['deprecated']} / "
        f"未安装 {summary['not_installed']} / 检查失败 {summary['errors']}",
    ]
    for i in flagged[:10]:
        # 版本箭头已含升级信息；废弃/失败提示才补一句（避免"可升级"重复两遍）
        extra = ""
        if i["status"] in ("已废弃", "检查失败") and i["deprecated_hint"]:
            extra = f"，{i['deprecated_hint']}"
        lines.append(f"- `{i['pkg']}`（{i['vendor']}）{i['status']}"
                     f"：{i['current'] or '未安装'} → {i['latest'] or '—'}{extra}")
    if len(flagged) > 10:
        lines.append(f"- ……其余 {len(flagged) - 10} 项见「设置 → SDK 治理」")
    lines.append("")
    lines.append("升级**不自动执行**：更新 requirements 固定版本 → 跑冒烟 → 发布（人工确认）。")
    content = "\n".join(lines)
    level = "warning" if (summary["outdated"] or summary["deprecated"]) else "info"
    # 平台系统消息（module=None）：不受监控提醒开关控制
    from app.services.notify import notify_inapp
    notify_inapp(db, "SDK 治理周报", content, level=level, msg_type="system",
                 audience="first_admin")


def get_latest_report(db: Session) -> dict:
    """最近一次扫描结果（供 API 展示）；无记录时返回空态 + 说明。"""
    from app.models.sdk_scan import SdkScanRecord

    row = db.query(SdkScanRecord).order_by(SdkScanRecord.id.desc()).first()
    if row is None:
        return {
            "scanned_at": "", "trigger": "",
            "total": 0, "up_to_date": 0, "outdated": 0, "deprecated": 0,
            "not_installed": 0, "errors": 0, "duration_ms": 0,
            "items": [], "note": _NOTE,
            "hint": "尚未扫描——点「立即扫描」，或等待每周一 09:00 自动扫描",
        }
    try:
        items = json.loads(row.results_json or "[]")
    except ValueError:
        items = []
    return {
        "scanned_at": row.scanned_at.strftime("%Y-%m-%d %H:%M:%S") if row.scanned_at else "",
        "trigger": row.trigger,
        "total": row.total, "up_to_date": row.up_to_date, "outdated": row.outdated,
        "deprecated": row.deprecated, "not_installed": row.not_installed,
        "errors": row.errors, "duration_ms": row.duration_ms,
        "items": items, "note": _NOTE,
    }
