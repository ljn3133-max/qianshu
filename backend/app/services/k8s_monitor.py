"""K8s 容器监控服务：同步/日志采集/失败分析/保留清理。

失败分析口径（"结合实际情况"）：诊断要素=退出码+重启次数+OOM 记录+资源限额+事件流+日志尾部，
结论模板=是什么+为什么+怎么办（见 docs/active/capabilities.md 第五节）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.collector import k8s as k8s_api
from app.models.k8s import K8sCluster, K8sFailure, K8sPodLog, K8sResource

logger = logging.getLogger("qianshu.k8s_monitor")


def cluster_creds(db: Session, cluster: K8sCluster) -> tuple:
    """集群凭证（守卫版）：空/坏密文优雅降级为无 token，避免单个集群的
    凭证问题令整个扫描返回 500。"""
    from app.core.security import decrypt_secret

    cipher = (cluster.token_cipher or "").strip()
    if not cipher:
        return cluster.api_url, "", cluster.verify_tls
    try:
        token = decrypt_secret(cipher)
    except Exception:
        logger.warning("集群 %s token 解密失败（MASTER_KEY 变更或密文损坏）——按无 token 处理", cluster.name)
        return cluster.api_url, "", cluster.verify_tls
    return cluster.api_url, token, cluster.verify_tls


# ---------------------------------------------------------------- 资源同步

def sync_cluster(db: Session, cluster: K8sCluster,
                 kinds=("Pod", "Service", "Ingress", "Deployment", "Node",
                        "Namespace", "PersistentVolumeClaim", "ConfigMap", "Secret")) -> dict:
    """资源清单同步：全量快照 + is_active 标记（消失=已销毁）。"""
    api_url, token, verify = cluster_creds(db, cluster)
    ns_scope = [x for x in (cluster.ns_scope or "").split(",") if x.strip()]
    total = 0
    all_rows: dict = {}   # kind -> rows（关联解析用）
    for kind in kinds:
        try:
            rows = k8s_api.list_resources(api_url, token, kind, ns_scope, verify)
            all_rows[kind] = rows
        except Exception as e:
            logger.warning("资源同步失败 %s/%s: %s", cluster.name, kind, str(e)[:80])
            continue
        seen = set()
        for r in rows:
            key = (kind, r["namespace"], r["name"])
            seen.add(key)
            row = (db.query(K8sResource)
                   .filter(K8sResource.cluster_id == cluster.id, K8sResource.kind == kind,
                           K8sResource.namespace == r["namespace"], K8sResource.name == r["name"])
                   .first())
            payload = {"labels": r.get("labels"), "status": r.get("status"),
                       "restarts": r.get("restarts"), "ready": r.get("ready"), "containers_detail": r.get("containers"),
                       "node": r.get("node"), "pod_ip": r.get("pod_ip", ""), "containers": r.get("containers"),
                       "hosts": r.get("hosts"), "cluster_ip": r.get("cluster_ip"),
                       "kubelet": r.get("kubelet"), "created": r.get("created"),
                       "internal_ip": r.get("internal_ip", ""),
                       "selector": r.get("selector"), "backends": r.get("backends")}
            if row is None:
                db.add(K8sResource(cluster_id=cluster.id, kind=kind, namespace=r["namespace"],
                                   name=r["name"], is_active=True,
                                   labels_json=json.dumps(r.get("labels") or {}, ensure_ascii=False),
                                   status_json=json.dumps(payload, ensure_ascii=False)))
            else:
                row.status_json = json.dumps(payload, ensure_ascii=False)
                row.is_active = True
                row.observed_at = datetime.now()
            total += 1
        # 消失的标记 is_active=0（已销毁/下线）
        for row in (db.query(K8sResource)
                    .filter(K8sResource.cluster_id == cluster.id, K8sResource.kind == kind,
                            K8sResource.is_active == True).all()):  # noqa: E712
            if (kind, row.namespace, row.name) not in seen:
                row.is_active = False
    # 关联解析（Ingress→Service→Pod 链）：写回 status_json
    pods_rows = all_rows.get("Pod") or []
    svc_rows = all_rows.get("Service") or []
    svc_pods = {}
    for svc in svc_rows:
        sel = svc.get("selector") or {}
        matched = [p["name"] for p in pods_rows
                   if sel and all((p.get("labels") or {}).get(k) == v for k, v in sel.items())]
        svc_pods[(svc["namespace"], svc["name"])] = matched
    for r in svc_rows:
        row = (db.query(K8sResource)
               .filter(K8sResource.cluster_id == cluster.id, K8sResource.kind == "Service",
                       K8sResource.namespace == r["namespace"], K8sResource.name == r["name"])
               .first())
        if row:
            st = json.loads(row.status_json or "{}")
            st["matched_pods"] = svc_pods.get((r["namespace"], r["name"]), [])
            row.status_json = json.dumps(st, ensure_ascii=False)
    for r in all_rows.get("Ingress") or []:
        row = (db.query(K8sResource)
               .filter(K8sResource.cluster_id == cluster.id, K8sResource.kind == "Ingress",
                       K8sResource.namespace == r["namespace"], K8sResource.name == r["name"])
               .first())
        if row:
            st = json.loads(row.status_json or "{}")
            st["chain"] = [{"service": b, "pods": svc_pods.get((r["namespace"], b), [])}
                           for b in (r.get("backends") or [])]
            row.status_json = json.dumps(st, ensure_ascii=False)
    cluster.synced_at = datetime.now()
    cluster.status = "ok"
    db.commit()
    return {"cluster": cluster.name, "resources": total}


# ---------------------------------------------------------------- 日志采集

def collect_logs(db: Session, cluster: K8sCluster, tail: int = 100) -> int:
    """活跃 pod 日志采样入库（销毁前覆盖——已销毁 pod 的日志靠此前采集）。"""
    api_url, token, verify = cluster_creds(db, cluster)
    pods = (db.query(K8sResource)
            .filter(K8sResource.cluster_id == cluster.id, K8sResource.kind == "Pod",
                    K8sResource.is_active == True).all())  # noqa: E712
    n = 0
    # 时区统一：K8s 时间戳是 UTC，cutoff 必须用 utcnow（本地时间比较=8 小时偏差全过滤）
    from datetime import timezone

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=150)
    for p in pods:
        try:
            status = json.loads(p.status_json or "{}")
            containers = status.get("containers") or [{"name": ""}]
            for cs in containers[:3]:
                lines = k8s_api.pod_logs(api_url, token, p.namespace, p.name,
                                         container=cs.get("name") or "", tail_lines=tail,
                                         since_seconds=180, verify_tls=verify)
                for ln in lines:
                    try:
                        ts = (datetime.fromisoformat(ln["ts"].replace("Z", "+00:00"))
                              .astimezone(timezone.utc).replace(tzinfo=None))
                    except Exception:
                        ts = datetime.now(timezone.utc).replace(tzinfo=None)
                    if ts < cutoff:
                        continue
                    db.add(K8sPodLog(cluster_id=cluster.id, namespace=p.namespace,
                                     pod=p.name, container=cs.get("name") or "",
                                     ts=ts, stream=ln.get("stream", "stdout"),
                                     line=ln.get("line", "")[:2000]))
                    n += 1
        except Exception as e:
            logger.debug("pod 日志采集跳过 %s/%s: %s", p.namespace, p.name, str(e)[:60])
    if n:
        db.commit()
    return n


# ---------------------------------------------------------------- 失败分析

FAILURE_TYPES = ("OOMKilled", "CrashLoopBackOff", "ImagePullBackOff", "Pending")


def scan_failures(db: Session, cluster: K8sCluster) -> list:
    """容器失败检测 + 诊断生成（结合实际情况：退出码/重启/OOM/限额/事件）。"""
    api_url, token, verify = cluster_creds(db, cluster)
    pods = (db.query(K8sResource)
            .filter(K8sResource.cluster_id == cluster.id, K8sResource.kind == "Pod").all())
    found = []
    for p in pods:
        try:
            status = json.loads(p.status_json or "{}")
        except Exception:
            continue
        containers = status.get("containers") or []
        for cs in containers:
            ftype = _classify(cs, status)
            if not ftype:
                continue
            # 幂等：同 pod+container+类型 已有 pending/ignored/confirmed 则跳过
            dup = (db.query(K8sFailure)
                   .filter(K8sFailure.cluster_id == cluster.id,
                           K8sFailure.namespace == p.namespace, K8sFailure.pod == p.name,
                           K8sFailure.container == (cs.get("name") or ""),
                           K8sFailure.failure_type == ftype).first())
            if dup:
                continue
            diagnosis = _diagnose(db, api_url, token, verify, p, cs, ftype, status)
            db.add(K8sFailure(cluster_id=cluster.id, namespace=p.namespace, pod=p.name,
                              container=cs.get("name") or "", failure_type=ftype,
                              diagnosis=diagnosis))
            found.append(ftype)
    if found:
        db.commit()
    return found


def _classify(cs: dict, pod_status: dict) -> str:
    reason = cs.get("last_reason") or ""
    if reason == "OOMKilled":
        return "OOMKilled"
    if reason == "ImagePullBackOff" or reason == "ErrImagePull":
        return "ImagePullBackOff"
    if (cs.get("restarts") or 0) >= 3 and (cs.get("exit_code") not in (None, 0)):
        return "CrashLoopBackOff"
    if pod_status.get("status") == "Pending":
        return "Pending"
    return ""


def _diagnose(db, api_url, token, verify, pod_row, cs: dict, ftype: str, pod_status: dict) -> str:
    """诊断=是什么+为什么+怎么办（结合退出码/OOM/限额/事件/日志尾部）。"""
    limits = ""
    try:
        res = cs.get("resources") or {}
        if res:
            limits = f"requests={res.get('requests', {})} limits={res.get('limits', {})}"
        else:
            raw = json.loads(pod_row.status_json or "{}").get("raw") or {}
            for c in raw.get("spec", {}).get("containers") or []:
                if c.get("name") == cs.get("name"):
                    res = c.get("resources") or {}
                    limits = f"requests={res.get('requests', {})} limits={res.get('limits', {})}"
    except Exception:
        pass
    events = []
    try:
        events = k8s_api.pod_events(api_url, token, pod_row.namespace, pod_row.name, verify)[:5]
    except Exception:
        pass
    tail_log = ""
    try:
        lines = k8s_api.pod_logs(api_url, token, pod_row.namespace, pod_row.name,
                                 container=cs.get("name") or "", tail_lines=20, verify_tls=verify)
        tail_log = " | ".join(l["line"][:80] for l in lines[-3:])
    except Exception:
        pass

    parts = []
    if ftype == "OOMKilled":
        parts.append(f"【是什么】容器内存超限被杀（exitCode=137, OOMKilled，重启 {cs.get('restarts')} 次）")
        parts.append(f"【为什么】{limits or '未见资源限额配置'}")
        parts.append("【怎么办】把 memory limit 提到历史峰值的 1.5 倍，或排查内存泄漏（日志尾部线索："
                     + (tail_log[:120] or "无") + "）")
    elif ftype == "CrashLoopBackOff":
        parts.append(f"【是什么】容器反复崩溃（exitCode={cs.get('exit_code')}，重启 {cs.get('restarts')} 次）")
        parts.append("【为什么】应用启动即退出；日志尾部：" + (tail_log[:160] or "无日志"))
        parts.append("【怎么办】按退出码与日志修复启动问题（1=应用错误、127=命令不存在、139=段错误）")
    elif ftype == "ImagePullBackOff":
        parts.append(f"【是什么】镜像拉取失败（{cs.get('last_reason') or 'ImagePullBackOff'}）")
        parts.append("【为什么】事件：" + ("；".join(e["message"][:60] for e in events) or "无事件"))
        parts.append("【怎么办】三查：镜像名与 tag 是否正确、仓库凭证（imagePullSecrets）、节点到仓库网络")
    elif ftype == "Pending":
        parts.append("【是什么】Pod 长时间 Pending 未调度")
        parts.append("【为什么】事件：" + ("；".join(e["message"][:80] for e in events) or "无事件"))
        parts.append("【怎么办】按事件处置：节点资源不足→扩容/驱逐低优先级；污点容忍→补 toleration")
    return "\n".join(parts)


# ---------------------------------------------------------------- 保留清理

def log_retention_days() -> int:
    """动态取保留天数：保存后热生效；0=永久。"""
    from app.services import retention

    return retention.k8s_days()


def cleanup_logs(db: Session) -> int:
    """按保留天数清存量日志（配置变更后立即执行一次——'7 天改 3 天现有数据按规则清理'）。
    0=永久保留：不删任何行。"""
    days = log_retention_days()
    if days <= 0:
        logger.info("K8s 日志保留清理跳过（0=永久保留）")
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    n = (db.query(K8sPodLog).filter(K8sPodLog.ts < cutoff)
         .delete(synchronize_session=False))
    db.commit()
    logger.info("K8s 日志保留清理: %d 行（保留 %d 天）", n, days)
    return n
