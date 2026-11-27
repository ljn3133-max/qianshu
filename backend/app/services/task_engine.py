"""K8s 管理任务引擎：任务化运维动作的执行底座。

设计：任务=状态机+日志流+步骤表（升级等复杂流程复用步骤状态机）；
执行=线程+SSH/命令模板（命令由代码模板生成——不执行任何 LLM/用户粘贴的命令原文）。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.k8s_task import K8sTask, K8sTaskLog

logger = logging.getLogger("qianshu.task_engine")


def log_line(db: Session, task_id: int, line: str) -> None:
    db.add(K8sTaskLog(task_id=task_id, line=str(line)[:2000]))
    db.commit()


def reap_stale_tasks() -> int:
    """启动收割：服务重启会带走执行线程，running 任务永不落地=悬挂。
    启动时全部置 failed（注明中断原因），不能遗留永远 running 的假状态。"""
    from app.core.db import SessionLocal

    sdb = SessionLocal()
    try:
        rows = (sdb.query(K8sTask)
                .filter(K8sTask.status.in_(("running", "pending")))
                .all())
        for t in rows:
            t.status = "failed"
            t.error = "服务重启，任务执行被中断（可重试）"
            t.finished_at = datetime.now()
            sdb.add(K8sTaskLog(task_id=t.id, line="【任务中断】服务重启，执行线程被终止"))
        sdb.commit()
        return len(rows)
    finally:
        sdb.close()


def submit(db: Session, task_type: str, title: str, meta: dict, steps: list,
           runner, submitted_by: str = "", submitted_by_id: int = 0) -> K8sTask:
    """提交任务（异步执行 runner(task_id, log, set_steps) -> (ok, err)）。

    集群 (cluster_id, cluster_name)、提交人
    (submitted_by_id, submitted_by) 双字段——id 连实体、name 展示快照。
    """
    from app.services.refs import resolve_cluster_ref

    cid, cname = resolve_cluster_ref(db, meta.get("cluster_id") or 0, meta.get("cluster_name", ""))
    task = K8sTask(
        task_type=task_type, title=title,
        cluster_name=cname or meta.get("cluster_name", ""), cluster_id=cid,
        status="pending", meta_json=json.dumps(meta, ensure_ascii=False),
        steps_json=json.dumps(steps, ensure_ascii=False),
        submitted_by=submitted_by, submitted_by_id=submitted_by_id or 0,
    )
    db.add(task)
    db.commit()
    task_id = task.id

    def _run():
        from app.core.db import SessionLocal

        sdb = SessionLocal()
        try:
            t = sdb.get(K8sTask, task_id)
            t.status = "running"
            t.started_at = datetime.now()
            sdb.commit()

            def log(msg):
                ldb = SessionLocal()
                try:
                    log_line(ldb, task_id, msg)
                finally:
                    ldb.close()

            def set_steps(steps_):
                sdb2 = SessionLocal()
                try:
                    tt = sdb2.get(K8sTask, task_id)
                    tt.steps_json = json.dumps(steps_, ensure_ascii=False)
                    sdb2.commit()
                finally:
                    sdb2.close()

            ok, err = runner(task_id, log, set_steps)
            t = sdb.get(K8sTask, task_id)
            sdb.refresh(t)
            if t.status == "aborted":
                log("【任务已终止】")
                return
            # 步骤失败必须传导到任务结果，避免 init 失败却显示任务成功——
            # runner 的 ok 不作数，以步骤表事实为准（任一 failed → 任务 failed）
            steps_final = json.loads(t.steps_json or "[]")
            failed_steps = [s for s in steps_final if s.get("status") == "failed"]
            if ok and failed_steps:
                names = "、".join(s.get("label", s.get("key", "")) for s in failed_steps)
                ok, err = False, (err or "") + f"（步骤失败：{names}）"
            t.status = "succeeded" if ok else "failed"
            t.error = (err or "")[:2000]
            t.finished_at = datetime.now()
            sdb.commit()
        except Exception as e:
            logger.exception("任务执行异常 %s", task_id)
            t = sdb.get(K8sTask, task_id)
            if t and t.status != "aborted":
                t.status = "failed"
                t.error = str(e)[:2000]
                t.finished_at = datetime.now()
                sdb.commit()
        finally:
            sdb.close()

    threading.Thread(target=_run, daemon=True).start()
    return task


def task_out(t: K8sTask) -> dict:
    duration = None
    if t.started_at:
        end = t.finished_at or datetime.now()
        duration = int((end - t.started_at).total_seconds())
    return {
        "id": t.id, "task_type": t.task_type, "title": t.title,
        "cluster_name": t.cluster_name,
        "cluster_id": getattr(t, "cluster_id", 0) or 0,   # 统一集群引用
        "status": t.status,
        "meta": json.loads(t.meta_json or "{}"),
        "steps": json.loads(t.steps_json or "[]"),
        "error": t.error, "submitted_by": t.submitted_by,
        "submitted_by_id": getattr(t, "submitted_by_id", 0) or 0,
        "created_at": t.created_at.isoformat(timespec="seconds"),
        "started_at": t.started_at.isoformat(timespec="seconds") if t.started_at else None,
        "finished_at": t.finished_at.isoformat(timespec="seconds") if t.finished_at else None,
        "duration_s": duration, "retries": t.retries,
    }
