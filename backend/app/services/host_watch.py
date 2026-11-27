"""关机检测 + aux 外溢检测：辅助机器只关注关机和对 SLB 5xx 的主要贡献。

两个闭环：
1. **关机**（core/aux 都报）：实例实时状态，非 Running → 告警中心（内置规则
   双写 alert_records，告警是告警，不该只躺在成本异动页）+ 异动事件 + 通知；
   恢复 Running 自动关。每 HOST_WATCH_INTERVAL_MIN 分钟一轮。
   厂商通道：阿里 ECS（Running）+ 腾讯 CVM
   （RUNNING）同款语义——非 Running 即报，状态值按各云 API 原样。
2. **aux 5xx 外溢**（独立检测，不搭 5xx 总量告警的车）：aux 后端在 SLB 5xx
   里占大头（≥40% 且 ≥30 条/5min）→ 事件点名"是它外溢"——总量没到 100 条
   阈值也要能发现，这正是 aux 命脉二的本意。
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.models.alert_record import AlertRecord
from app.models.asset import Asset
from app.models.cost_anomaly import CostAnomaly
from app.services.cost_anomaly import _add, _notify_anomalies

logger = logging.getLogger("qianshu.host_watch")

_BUILTIN_RULE = "内置·服务器关机"
_SPILLOVER_RULE = "内置·辅助机器5xx外溢"

# aux 外溢判定：5 分钟窗内该后端 5xx >= 30 条且占全部 5xx >= 40%
_SPILLOVER_MIN_CNT = 30
_SPILLOVER_MIN_SHARE = 0.40

# 厂商通道表：
# 一个通道=（厂商凭证解析, 资产产品码, 运行状态值, 展示名, 实时状态拉取）；
# 新厂商接入只加一行 + 一个 _fetch_*_live。
_CHANNELS: tuple = (
    {"vendor": "aliyun", "product": "ecs", "running": "Running", "label": "ECS"},
    {"vendor": "tencent", "product": "cvm", "running": "RUNNING", "label": "CVM"},
)


def _fetch_ecs_live(ak: str, sk: str, region: str) -> dict:
    """阿里 ECS 实时状态 {instance_id: status}（只读）。"""
    from app.collector.ecs import _build_client, _fetch_instances

    client = _build_client(ak, sk, region)
    return {getattr(i, "instance_id", ""): (getattr(i, "status", "") or "")
            for i in _fetch_instances(client, region)}


def _fetch_cvm_live(ak: str, sk: str, region: str) -> dict:
    """腾讯 CVM 实时状态 {instance_id: InstanceState}（只读，DescribeInstances 分页）。

    状态值=CVM API 原样大写（RUNNING/STOPPED/...），判定见 run_host_check。
    """
    from tencentcloud.cvm.v20170312 import models

    from app.services.cloud.provider import _cvm_client

    client = _cvm_client(ak, sk, region)
    out: dict = {}
    offset = 0
    while True:
        req = models.DescribeInstancesRequest()
        req.Limit = 100
        req.Offset = offset
        resp = client.DescribeInstances(req)
        batch = resp.InstanceSet or []
        for inst in batch:
            out[getattr(inst, "InstanceId", "")] = getattr(inst, "InstanceState", "") or ""
        offset += len(batch)
        if not batch or offset >= (resp.TotalCount or 0):
            break
    return out


def _fetcher(vendor: str):
    """厂商 -> 实时状态拉取函数（调用时取模块属性——mock 测试可替换 _fetch_*_live）。"""
    return {"aliyun": _fetch_ecs_live, "tencent": _fetch_cvm_live}[vendor]


def run_host_check(db: Session, notify: bool = True, vendors: tuple | None = None) -> dict:
    """关机检测 + aux 外溢检测一轮。返回统计。

    vendors 限定本轮厂商通道（("aliyun",) / ("tencent",)——定时任务多账号轮次按
    厂商分开跑，凭证与资产天然对齐）；None=全部通道（单次全量检测）。
    """
    found: list = []
    stats_recovered: list = []
    checked = 0
    for ch in _CHANNELS:
        if vendors is not None and ch["vendor"] not in vendors:
            continue
        c, evs, recs = _check_vendor_hosts(db, ch)
        checked += c
        found += evs
        stats_recovered += recs

    # ---- aux 外溢独立检测（SLS/阿里链路——仅 aliyun 通道轮次跑） ----
    spillover = []
    if vendors is None or "aliyun" in vendors:
        spillover = _check_aux_spillover(db)
    if notify and found:
        _notify_anomalies(db, found)
    db.commit()
    return {"checked": checked, "down": len(found),
            "recovered": len(stats_recovered), "spillover": len(spillover)}


def _check_vendor_hosts(db: Session, ch: dict) -> tuple[int, list, list]:
    """单厂商通道的关机检测一轮。返回 (checked, 告警事件, 恢复实例列表)。"""
    from app.collector.credential import active_account_id

    # 多账号上下文激活时只查本账号资产——否则拿 A 账号凭证
    # 拉不到 B 账号实例的状态，会把 B 实例误判成"非 Running"报关机（误报）。
    q = db.query(Asset).filter(Asset.product == ch["product"], Asset.status != "Deleted")
    acc = active_account_id()
    if acc is not None:
        q = q.filter(Asset.account_id == acc)
    assets = q.all()
    if not assets:
        return 0, [], []
    by_region: dict = defaultdict(list)
    for a in assets:
        by_region[a.region or "cn-hangzhou"].append(a)

    found: list = []
    checked = 0
    stats_recovered: list = []
    fetch = _fetcher(ch["vendor"])
    for region, rows in by_region.items():
        try:
            if ch["vendor"] == "aliyun":
                from app.collector.credential import resolve_aliyun_credential

                ak, sk, _ = resolve_aliyun_credential(db)
            else:
                from app.collector.credential import resolve_tencent_credential

                ak, sk, _ = resolve_tencent_credential(db)
            live_map = fetch(ak, sk, region)
        except Exception as e:
            logger.warning("关机检测 %s/%s 拉取失败: %s", ch["label"], region, str(e)[:120])
            continue
        for a in rows:
            checked += 1
            status = live_map.get(a.instance_id) or a.status
            if str(status).lower() == ch["running"].lower():
                recovered_n = (db.query(CostAnomaly)
                               .filter(CostAnomaly.type == "host_down",
                                       CostAnomaly.target_key == a.instance_id,
                                       CostAnomaly.ack_status.in_(("open", "acknowledged")))
                               .update({CostAnomaly.ack_status: "resolved",
                                        CostAnomaly.ack_note: "实例已恢复 Running，自动关闭"},
                                       synchronize_session=False))
                if recovered_n:
                    stats_recovered.append(a.instance_id)
                    _close_builtin_record(db, a.instance_id, _BUILTIN_RULE)
                continue
            name = a.name or a.instance_id
            ev = _add(
                db, date.today(), "host_down", a.instance_id, name,
                f"服务器非运行状态: {name}（{status}）",
                (f"实例 {name}（{a.instance_id}）当前状态 {status}——关机/异常对业务是硬故障信号\n\n"
                 f"- 实例: {name}\n- 实例ID: {a.instance_id}\n- 状态: {status}\n"
                 f"- 关键度: {getattr(a, 'tier', 'core')}\n"
                 f"- 处理：确认是计划内停机就点确认；否则立即上云控制台开机/排查"),
                value=0, baseline=0, ratio=0, level="critical",
            )
            if ev:
                found.append(ev)
                _upsert_builtin_record(
                    db, rule_name=_BUILTIN_RULE, target=a.instance_id, target_name=name,
                    level="critical",
                    content=f"{ch['label']} 状态 {status}（非 Running）——关机是硬故障信号",
                    account_id=a.account_id or acc or 1,
                )
    return checked, found, stats_recovered


def _upsert_builtin_record(db: Session, rule_name: str, target: str, target_name: str,
                           level: str, content: str, account_id: int = 1,
                           module: str = "host") -> None:
    """内置信号双写告警中心（rule_id=0）：告警是告警，不该只躺在异动页。

    account_id/module 必须由调用方按事件归属传入；缺省落到账号 1 会使多账号环境下
    「关闭 B 账号的主机提醒」因错误归因而失效。
    """
    rec = (db.query(AlertRecord)
           .filter(AlertRecord.rule_id == 0, AlertRecord.rule_name == rule_name,
                   AlertRecord.target == target, AlertRecord.status == "firing")
           .first())
    if rec is None:
        db.add(AlertRecord(
            rule_id=0, rule_name=rule_name, source="builtin",
            target=target, target_name=target_name, level=level,
            title=f"{rule_name} - {target_name}",
            content=content, status="firing", breach_value=0,
            account_id=account_id or 1, module=module,
        ))


def _close_builtin_record(db: Session, target: str, rule_name: str) -> None:
    (db.query(AlertRecord)
     .filter(AlertRecord.rule_id == 0, AlertRecord.rule_name == rule_name,
             AlertRecord.target == target, AlertRecord.status == "firing")
     .update({AlertRecord.status: "recovered", AlertRecord.recovered_at: datetime.now()},
             synchronize_session=False))


# ---------------- 异动 ↔ 同源告警 生命周期联动 ----------------
# 双写产生的两本账：异动页确认/忽略后，内置告警记录仍 firing，下次走查还要再点一遍。
# 收口：异动页的确认/忽略联动关闭同源 firing 内置告警记录（content 留痕来源）。
# 重开方向不联动——告警是否重新触发只认实例真实状态（下轮关机检测自动裁决），
# 人工重开一个已恢复实例的异动不该凭空造出一条 firing 告警。
ANOMALY_BUILTIN_RULES: dict = {
    "host_down": _BUILTIN_RULE,
    "aux_spillover": _SPILLOVER_RULE,
}


def close_builtin_for_anomalies(db: Session, anomalies: list, note: str) -> int:
    """异动确认/忽略 -> 联动关闭同源 firing 内置告警记录。返回关闭条数。

    只处理双写过的两类（host_down/aux_spillover）；其余类型本就只在异动账上。
    不 commit——与调用方的异动状态更新同事务提交，成败一致。
    """
    closed = 0
    for a in anomalies:
        rule = ANOMALY_BUILTIN_RULES.get(getattr(a, "type", ""))
        if not rule:
            continue
        closed += (db.query(AlertRecord)
                   .filter(AlertRecord.rule_id == 0, AlertRecord.rule_name == rule,
                           AlertRecord.target == a.target_key,
                           AlertRecord.status == "firing")
                   .update({AlertRecord.status: "recovered",
                            AlertRecord.recovered_at: datetime.now(),
                            # 联动关闭同时清空确认/重申状态（恢复即 episode 结束）
                            AlertRecord.ack_status: "none",
                            AlertRecord.next_notify_at: None,
                            AlertRecord.content: AlertRecord.content + f" | [{note}]"},
                           synchronize_session=False))
    return closed


def _check_aux_spillover(db: Session) -> list:
    """aux 机器 5xx 外溢独立检测：它贡献 SLB 5xx 大头就点名（哪怕总量没到告警阈值）。"""
    from app.models.link_edge import LinkEdge
    from app.services.tier import aux_instance_ids

    aux = aux_instance_ids(db)
    if not aux:
        return []
    # aux 实例 -> 它的 upstream（链路库映射）
    aux_assets = db.query(Asset).filter(Asset.instance_id.in_(list(aux))).all()
    names = {a.instance_id: (a.name or a.instance_id) for a in aux_assets}
    acct_of = {a.instance_id: (a.account_id or 1) for a in aux_assets}
    name_set = set(names.values())
    ups = {e.upstream_addr: e.ecs_name for e in db.query(LinkEdge).all()
           if (e.ecs_name or "") in name_set}
    if not ups:
        return []
    try:
        from app.collector.credential import resolve_sls_credential
        from app.collector.sls import query_logs
        from app.services.sls_targets import outer_slb
        ak, sk, _ = resolve_sls_credential(db)
        end_s = int(time.time())
        start_s = end_s - 300
        r = query_logs(ak, sk, *outer_slb(),
                       "status >= 500 | select upstream_addr, count(*) as cnt "
                       "group by upstream_addr order by cnt desc limit 10",
                       start=start_s, end=end_s, limit=10)
        rows = r["logs"]
    except Exception as e:
        logger.warning("aux 外溢检测查询失败: %s", e)
        return []
    total = sum(float(x.get("cnt") or 0) for x in rows) or 1
    found: list = []
    for x in rows:
        up = str(x.get("upstream_addr", ""))
        cnt = float(x.get("cnt") or 0)
        ecs = ups.get(up)
        if not ecs:
            continue
        share = cnt / total
        if cnt < _SPILLOVER_MIN_CNT or share < _SPILLOVER_MIN_SHARE:
            continue
        inst_id = next((iid for iid, n in names.items() if n == ecs), up)
        ev = _add(
            db, date.today(), "aux_spillover", inst_id, ecs,
            f"辅助机器 5xx 外溢: {ecs}（占 SLB 5xx 的 {share * 100:.0f}%）",
            (f"辅助机器 {ecs} 近 5 分钟贡献了 SLB 5xx 的大头——这正是该类机器需要被关注的信号\n\n"
             f"- 机器: {ecs}\n- 后端: {up}\n- 5xx: {cnt:.0f} 条（占全部 5xx {share * 100:.0f}%）\n"
             f"- 处理：查这台机器自身状态/日志；它挂了会外溢成全站 5xx"),
            value=cnt, baseline=total, ratio=share, level="critical",
        )
        if ev:
            found.append(ev)
        _upsert_builtin_record(
            db, rule_name=_SPILLOVER_RULE, target=inst_id, target_name=ecs,
            level="critical",
            content=f"近 5 分钟 SLB 5xx 的 {share * 100:.0f}% 来自该辅助机器（{cnt:.0f} 条）",
            account_id=acct_of.get(inst_id, 1),
        )
    # 外溢消失：关闭事件与告警记录
    for iid in names:
        if not any(f.target_key == iid for f in found):
            _close_builtin_record(db, iid, _SPILLOVER_RULE)
            (db.query(CostAnomaly)
             .filter(CostAnomaly.type == "aux_spillover", CostAnomaly.target_key == iid,
                     CostAnomaly.ack_status.in_(("open", "acknowledged")))
             .update({CostAnomaly.ack_status: "resolved",
                      CostAnomaly.ack_note: "外溢已消失，自动关闭"},
                     synchronize_session=False))
    return found
