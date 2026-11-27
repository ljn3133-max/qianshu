import asyncio
import fcntl
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from sqlalchemy import inspect as sa_inspect

from app.api.v1 import api_router
from app.api.ws import router as ws_router
from app.core.config import settings
from app.core.db import Base, SessionLocal, engine
from app.core.migrate import (migrate_channel_email, run_data_migrations,
                              run_migrations)
from app.core.update_data import apply_pending_updates
from app.core.security import hash_password
from app.models import issue as _issue  # noqa: F401  自愈问题单/计划/事件
from app.models.issue import IssueCase
from app.models import asset as _asset
from app.models import org as _org  # noqa: F401  组织架构与 Namespace 授权模型，供 create_all 发现表
from app.models import permission as _permission  # noqa: F401  create_all 发现表  # noqa: F401  确保 create_all 能发现表
from app.models import object_profile as _object_profile  # noqa: F401  对象全景画像
from app.models import holiday_calendar as _holiday  # noqa: F401
from app.models import behavior_profile as _behavior_profile  # noqa: F401
from app.models import change_event as _change_event  # noqa: F401
from app.models import alert_baseline as _alert_baseline  # noqa: F401
from app.models import recheck_task as _recheck_task  # noqa: F401
from app.models import alert_channel as _alert_channel  # noqa: F401
from app.models import alert_record as _alert_record  # noqa: F401
from app.models import alert_rule as _alert_rule  # noqa: F401
from app.models import cost_daily as _cost_daily  # noqa: F401
from app.models import cost_anomaly as _cost_anomaly  # noqa: F401
from app.models import resource_pack_snapshot as _resource_pack_snapshot  # noqa: F401
from app.models import ai_setting as _ai_setting  # noqa: F401
from app.models import llm_call_log as _llm_call_log  # noqa: F401
from app.models import ai_query_log as _ai_query_log  # noqa: F401
from app.models import in_app_message as _in_app_message  # noqa: F401
from app.models import inspection as _inspection  # noqa: F401
from app.models import inspection_report as _inspection_report  # noqa: F401
from app.models import link_edge as _link_edge  # noqa: F401
from app.models import domain_registry as _domain_registry  # noqa: F401  域名登记表
from app.models import business as _business  # noqa: F401
from app.models import dba_space as _dba_space  # noqa: F401
from app.models import cdn_domain_daily as _cdn_domain_daily  # noqa: F401
from app.models import diagnosis as _diagnosis  # noqa: F401
from app.models import diagnosis_case as _diagnosis_case  # noqa: F401
from app.models import audit as _audit  # noqa: F401
from app.models import credential as _credential  # noqa: F401
from app.models import cloud_account as _cloud_account  # noqa: F401  多云账号表
from app.models import onboarding_run as _onboarding_run  # noqa: F401  跨入口共享接入任务
from app.models import ops_lock as _ops_lock  # noqa: F401
from app.models import saved_query as _saved_query  # noqa: F401
from app.models import sdk_scan as _sdk_scan  # noqa: F401  SDK 治理扫描记录
from app.models import user as _user  # noqa: F401
from app.models import agent_schedule as _agent_schedule  # noqa: F401  工作流定时体检

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("qianshu")


def _report_job_failure(job: str, exc: BaseException) -> None:
    """后台循环单轮失败进问题收件箱；上报失败只记日志，绝不影响下一轮。"""
    try:
        from app.services.self_heal.sources import report_job_failure
        report_job_failure(job, exc)
    except Exception:  # noqa: BLE001
        logger.debug("后台任务失败上报异常", exc_info=True)

# openapi_url 挂到 /api 前缀下：前端 vite/nginx 只代理
# /api/*，schema 放 /api/openapi.json 才能从前端域名直接取到（原 /openapi.json 打不开）
app = FastAPI(
    title="千枢平台 REST API",
    version="0.1.0",
    description=(
        "平台内部 REST 接口目录，包含 Web 页面与自动化管理接口；操作数不代表独立公共能力数。"
        "外部自动化建议在「系统设置 → 访问与审计 → API Token」创建令牌，"
        "通过 Authorization: Bearer <token> 调用。各操作的 x-auth-mode 标明实际鉴权方式。"
    ),
    openapi_url=f"{settings.API_PREFIX}/openapi.json",
    docs_url=f"{settings.API_PREFIX}/docs",
    redoc_url=f"{settings.API_PREFIX}/redoc",
)

if settings.ENV == "dev":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:5273", "http://127.0.0.1:5273"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # CSV 下载：浏览器 JS 要读文件名与截断计数
        expose_headers=["Content-Disposition", "X-Total-Rows", "X-Exported-Rows"],
    )

app.include_router(api_router, prefix=settings.API_PREFIX)
app.include_router(ws_router)   # WebShell（机器 SSH / Pod exec），路径 /ws/*


_5XX_REPORT_LAST: dict = {}
_5XX_REPORT_LOCK = None


def _throttle_5xx_report(key: str, window_s: float = 60.0) -> bool:
    """同一方法+路径每分钟最多上报一次，防止 5xx 风暴把上报线程/数据库打满。"""
    global _5XX_REPORT_LOCK
    if _5XX_REPORT_LOCK is None:
        import threading
        _5XX_REPORT_LOCK = threading.Lock()
    now = time.monotonic()
    with _5XX_REPORT_LOCK:
        if len(_5XX_REPORT_LAST) > 1000:
            _5XX_REPORT_LAST.clear()
        last = _5XX_REPORT_LAST.get(key, 0.0)
        if now - last < window_s:
            return False
        _5XX_REPORT_LAST[key] = now
        return True


@app.middleware("http")
async def _self_heal_5xx_middleware(request, call_next):
    """5xx 进入统一问题漏斗；不改响应、不递归、不在请求线程里做重活。"""
    response = await call_next(request)
    try:
        path = request.url.path
        from app.services.self_heal import policy as self_heal_policy

        if (response.status_code >= 500 and path.startswith("/api/")
                and not path.startswith("/api/issues")
                and path not in ("/api/openapi.json",)
                and self_heal_policy.enabled()
                and _throttle_5xx_report(f"{request.method} {path}")):
            def _report():
                from app.services.self_heal.service import report_issue_safe
                report_issue_safe(
                    source="api_5xx", severity="critical",
                    title=f"接口持续返回 {response.status_code}：{request.method} {path}",
                    reason_code=f"http_{response.status_code}", error=f"HTTP {response.status_code}",
                    target={"target": f"{request.method} {path}"},
                    source_ref={"method": request.method, "path": path,
                                "status": response.status_code},
                    evidence={"method": request.method, "path": path,
                              "status": response.status_code},
                    retryable=False,
                )
            import threading as _threading
            _threading.Thread(target=_report, daemon=True,
                              name="self-heal-5xx").start()
    except Exception:
        logger.warning("5xx 自愈上报失败（响应不变）", exc_info=True)
    return response


# OpenAPI 的 HTTPBearer 来自全局强制改密门，不能据此判断业务接口是否必须登录。
# 目录以真实依赖图为准；函数体内鉴权和独立接入凭证单独登记，避免把公开/匿名口径写反。
_PUBLIC_OPERATIONS = {
    ("GET", "/api/auth/captcha"),
    ("GET", "/api/auth/slider/challenge"),
    ("POST", "/api/auth/slider/verify"),
    ("POST", "/api/auth/login"),
    ("GET", "/api/auth/recovery-hint"),
    ("GET", "/api/bootstrap/status"),
    ("GET", "/api/settings/version"),
    ("GET", "/api/settings/public/version"),
}
_MANUAL_BEARER_PATHS = {"/api/mcp", "/api/synth/screenshots/{fname}"}


def _depends_on_user(dependant, seen=None) -> bool:
    from app.core.deps import get_current_user

    seen = seen if seen is not None else set()
    for child in dependant.dependencies:
        if child.call is get_current_user:
            return True
        if id(child) in seen:
            continue
        seen.add(id(child))
        if _depends_on_user(child, seen):
            return True
    return False


def _route_auth_mode(route: APIRoute, method: str) -> str:
    path = route.path_format
    if (method, path) in _PUBLIC_OPERATIONS:
        return "public"
    if path in _MANUAL_BEARER_PATHS or _depends_on_user(route.dependant):
        return "bearer"
    if path == "/api/bootstrap/probe":
        return "optional"
    if path.startswith("/api/events/webhook"):
        return "webhook_token"
    if path.startswith("/api/agent/lifecycle"):
        return "bridge_token"
    if path == "/api/agent/report":
        return "agent_token"
    return "other"


def _openapi_with_auth_modes():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description,
                         routes=app.routes)
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            op = schema.get("paths", {}).get(route.path_format, {}).get(method.lower())
            if op is not None:
                op["x-auth-mode"] = _route_auth_mode(route, method)
    app.openapi_schema = schema
    return schema


app.openapi = _openapi_with_auth_modes


def _lock_path(suffix: str) -> str:
    # 多 worker 共享同一 schema/调度锁；PostgreSQL 形态统一放 /tmp
    return "/tmp/qianshu" + suffix


_schema_lock_fh = None   # 持强引用：GC 掉文件对象会释放 flock
_sched_lock_fh = None


def _refuse_legacy_sqlite_data() -> None:
    """SQLite 已废除且不迁移旧库：目标 PG 为空库时若发现旧 sqlite 文件，中止启动。

    避免升级后静默连上全新空库，让用户误以为历史数据丢失。确认无需保留旧数据后，
    把文件移出数据目录再启动；有数据要保留时先在旧版本导出。
    """
    backend_dir = Path(__file__).resolve().parents[1]
    candidates = [Path("/data/qianshu.db"), backend_dir / "qianshu.db",
                  Path.cwd() / "qianshu.db"]
    found = [str(p) for p in candidates if p.is_file()]
    if found:
        raise RuntimeError(
            "检测到旧 SQLite 数据库文件：" + "、".join(found)
            + "。千枢已改为仅支持 PostgreSQL，不提供 SQLite 数据迁移；"
            "请确认无需保留旧数据后将这些文件移出数据目录，再重新启动")


def _init_schema_serialized() -> None:
    """schema 初始化（建表/补列/补索引/初始管理员）串行化后执行。

    uvicorn --workers N 时每个 worker 都跑 startup()，并发 create_all/ALTER 会互撞
    （table already exists / duplicate column）。阻塞 flock 排队逐个过（每遍幂等，
    非首遍是空跑），锁在初始化完成后立即释放。
    """
    global _schema_lock_fh
    fh = open(_lock_path(".schema.lock"), "w")
    fcntl.flock(fh, fcntl.LOCK_EX)   # 阻塞排队；初始化只需秒级
    _schema_lock_fh = fh
    try:
        # 必须在 create_all 之前判断：新库由 create_all 建当前完整 schema，
        # update-data 历史版本只登记 baseline；老库则要从第一个未应用版本开始补。
        fresh_db = not sa_inspect(engine).has_table("assets")
        if fresh_db:
            _refuse_legacy_sqlite_data()
        Base.metadata.create_all(bind=engine)
        # schema/DDL 先行；ORM 数据迁移必须等 update-data 补完模型新列再跑
        # 旧库缺 cloud_accounts.priority 时，ORM 查询会导致启动失败
        run_migrations(engine, orm_data=False)
        # 先补齐 legacy 迁移漏掉的存量列，再跑版本化 SQL：update-data 是 schema 的最后
        # 一环，脚本可以安全引用"当前基线应有的列"。
        _ensure_schema_columns()
        # 版本化升级 SQL（update-data/<version>/*.sql）：与 legacy migrate 串行，
        # 失败会抛异常中止启动 -> 安装器健康检查失败 -> 自动回滚代码与数据库备份。
        apply_pending_updates(engine, fresh=fresh_db)
        # 0.3.1 邮件渠道列兜底必须放在 update-data 之后：正常升级由 SQL 补列，
        # 此处只兜底"已登记升级但列缺失"的异常库；新库由 create_all 带列，均为幂等空跑。
        migrate_channel_email(engine)
        # 补列完成后再跑 ORM 数据迁移（旧凭证洗入、数据分组镜像）
        run_data_migrations(engine)
        # 模块提醒开关的种子行：新库不走 update-data SQL，必须在 create_all 后补齐，
        # 否则设置页矩阵为空（策略函数对缺行按默认全开处理，但页面需要行可编辑）。
        try:
            from app.core.db import SessionLocal as _SL
            from app.services.monitor_policy import ensure_defaults

            _db = _SL()
            try:
                n = ensure_defaults(_db)
                if n:
                    logger.info("监控模块开关种子补齐 %d 行", n)
            finally:
                _db.close()
        except Exception:
            logger.warning("监控模块开关种子补齐异常（不阻塞启动）", exc_info=True)
        try:
            from app.services.task_engine import reap_stale_tasks

            n = reap_stale_tasks()
            if n:
                logger.info("启动收割: %d 个中断任务置 failed", n)
        except Exception:
            pass
        _ensure_init_admin()
        # env_store 双写对账保证 compose 重建不丢配置：env_kv 入册/回填 .env，
        # 放 schema 串行锁内跑（多 worker 有序、幂等），见 docs/active/deployment.md §5.1
        try:
            from app.services.env_store import sync_with_db

            sync_with_db()
        except Exception:
            logger.warning("env_store 启动对账异常（不阻塞启动）", exc_info=True)
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        _schema_lock_fh = None


def _acquire_scheduler_leader() -> bool:
    """多 worker 只跑一份后台调度。

    24 个采集/检测循环若随每个 worker 各跑一份 = 重复采集、重复告警、重复 LLM 花费。
    文件 flock 选主：抢到锁的 worker 跑全部调度并持锁到进程退出，其余 worker 只服务
    API（锁自动释放后 uvicorn 整组拉起会重新选主）。QIANSHU_SCHEDULER_DISABLED=1
    可强制本进程不跑调度（只起 API 副本时用）。
    """
    global _sched_lock_fh
    if os.environ.get("QIANSHU_SCHEDULER_DISABLED") == "1":
        logger.info("后台调度已按 QIANSHU_SCHEDULER_DISABLED=1 关闭（本进程只服务 API）")
        return False
    try:
        fh = open(_lock_path(".sched.lock"), "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("后台调度已由另一进程持锁（多 worker 单调度），本 worker 只服务 API")
        return False
    fh.write(str(os.getpid()))
    fh.flush()
    _sched_lock_fh = fh
    return True


@app.on_event("startup")
async def startup() -> None:
    _init_schema_serialized()
    if not _acquire_scheduler_leader():
        return
    try:
        from app.services.sls_discovery import reap_interrupted

        n = reap_interrupted()
        if n:
            logger.info("启动收割: %d 个中断 SLS 发现项置 failed（可重新发现）", n)
    except Exception:
        logger.warning("SLS 发现中断项收割异常（不阻塞启动）", exc_info=True)
    try:
        from app.services.onboarding_tasks import reap_interrupted as reap_onboarding

        n = reap_onboarding()
        if n:
            logger.info("启动收割: %d 个中断接入项置 failed/attention（可重试）", n)
    except Exception:
        logger.warning("接入中断项收割异常（不阻塞启动）", exc_info=True)
    try:
        from app.services.self_heal.service import reconcile_self_heal

        out = reconcile_self_heal()
        logger.info("自愈启动收口: %s", out)
    except Exception:
        logger.warning("自愈启动收口异常（不阻塞启动）", exc_info=True)
    _start_self_heal_job()
    _start_metrics_collector()
    _start_alert_evaluator()
    _start_cost_collector()
    _start_inspection()
    _start_report()
    _start_dba_collector()
    _start_expiry_sync()
    _start_cdn_sync()
    _start_link_draft_sync()
    _start_db_maint()
    _start_db_backup()
    _start_engine_retention()
    _start_holiday_sync()
    _start_baseline_job()
    _start_profile_job()
    _start_object_profile_job()
    _start_host_watch()
    _start_change_sync()
    _start_decision_digest()
    _start_topology_scan()
    _start_cloud_asset_sync()
    _start_synth_scheduler()
    _start_oncall_escalation()
    _start_reminder_job()
    _start_knowledge_probe()
    _start_calibration_weekly()
    _start_k8s_monitor()
    _start_sdk_governance()
    _start_workflow_schedule()


_self_heal_task = None


def _start_self_heal_job() -> None:
    """自愈维护：计划过期、诊断中断收口、未处理新问题自动诊断（有限流）。"""
    global _self_heal_task

    def _loop() -> None:
        from app.services.self_heal import executor, planner, policy, service

        while True:
            try:
                if not policy.enabled():
                    # 总开关/熔断关闭：冻结一切自动维护写操作，只保持进程存活。
                    time.sleep(60)
                    continue
                executor.expire_plans()
                executor.retry_due_auto()
                db = SessionLocal()
                try:
                    # 超过 30 分钟仍 diagnosing：进程中断/模型卡死，转人工，保留证据。
                    cutoff = datetime.now() - timedelta(minutes=30)
                    stale = (db.query(IssueCase)
                             .filter(IssueCase.status == "diagnosing",
                                     IssueCase.updated_at < cutoff).limit(20).all())
                    for issue in stale:
                        service.transition(db, issue, "manual_required", kind="escalated",
                                           payload={"reason": "diagnosis_timeout"})
                    if policy.llm_allowed():
                        rows = (db.query(IssueCase)
                                .filter(IssueCase.status == "open",
                                        IssueCase.created_at < datetime.now() - timedelta(minutes=1))
                                .order_by(IssueCase.id.asc()).limit(3).all())
                        for issue in rows:
                            if issue.current_plan_id is None:
                                planner.start_diagnosis(issue.id, user_id=0)
                finally:
                    db.close()
            except Exception:
                logger.warning("自愈维护循环异常（继续下一轮）", exc_info=True)
            time.sleep(60)

    import threading as _threading
    _self_heal_task = _threading.Thread(target=_loop, daemon=True,
                                       name="self-heal-maintain")
    _self_heal_task.start()


_metrics_task = None  # 持强引用，防止后台任务被 GC 回收
_alert_task = None
_cost_task = None
_inspection_task = None
_report_task = None
_dba_task = None
_expiry_task = None
_cdn_task = None
_link_draft_task = None
_topology_task = None
_cloud_sync_task = None
_synth_task = None
_oncall_task = None
_reminder_task = None
_kprobe_task = None
_calib_task = None
_k8s_task = None
_sdk_gov_task = None
_schedule_task = None
_maint_task = None
_engine_ret_task = None
_db_backup_task = None
_holiday_task = None
_baseline_task = None
_profile_task = None
_host_watch_task = None
_change_sync_task = None
_digest_task = None
_object_profile_task = None


def _start_metrics_collector() -> None:
    """指标采集后台循环：周期性把云监控指标写入 VictoriaMetrics。"""
    global _metrics_task
    if not settings.METRICS_COLLECT_ENABLED:
        logger.info("指标采集未启用（METRICS_COLLECT_ENABLED=false）")
        return

    async def loop() -> None:
        await asyncio.sleep(10)  # 等应用完全就绪
        while True:
            try:
                await asyncio.to_thread(_run_collect_once)
            except Exception as exc:
                logger.exception("指标采集轮异常")
                _report_job_failure("metrics_collect", exc)
            await asyncio.sleep(settings.METRICS_COLLECT_INTERVAL)

    _metrics_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "指标采集已启动: 周期 %ds, VM=%s", settings.METRICS_COLLECT_INTERVAL, settings.VM_URL
    )


def _run_collect_once() -> None:
    from app.collector.credential import (CredentialError, account_credential_contexts,
                                          active_account_id)
    from app.collector.metrics_job import run_collection, run_vsphere_collection
    from app.services.monitor_policy import collect_due, mark_collected

    db = SessionLocal()
    try:
        # 阿里 CMS 通道（多账号：每个启用 aliyun 账号各采一轮）。
        # 无阿里凭证（腾讯+ESXi 单独部署）不算错误，软跳过，
        # 不挡 vSphere 通道（两通道独立，单通道失败互不影响）。
        for ctx in account_credential_contexts(db):
            # 单账号失败必须隔离：一个账号的凭证/权限问题不能中止其余账号的指标采集。
            try:
                with ctx:
                    acc_id = active_account_id()
                    # 分级监控：medium/low 账号按各自节奏跳过当前周期，
                    # high 账号 collect_due 恒真（升级前后行为一致）；手动采集不走本循环。
                    if not collect_due(db, "metrics", acc_id):
                        continue
                    r = run_collection(db)
                    if not (r or {}).get("skipped"):
                        mark_collected(db, "metrics", acc_id)
                        db.commit()
            except CredentialError as exc:
                db.rollback()
                logger.info("跳过本轮阿里指标采集：%s", exc)
            except Exception:
                db.rollback()
                logger.warning("单个阿里账号指标采集本轮失败（继续其余账号/vSphere 通道）",
                               exc_info=True)
        try:
            run_vsphere_collection(db)   # ESXi/vCenter VM 基础指标（pyvmomi 只读）
        except Exception:
            logger.warning("vSphere 指标采集本轮失败", exc_info=True)
    finally:
        db.close()


def _start_alert_evaluator() -> None:
    """告警评估后台循环：周期性评估告警规则。"""
    global _alert_task
    if not settings.ALERT_EVAL_ENABLED:
        logger.info("告警评估未启用（ALERT_EVAL_ENABLED=false）")
        return

    async def loop() -> None:
        await asyncio.sleep(20)  # 等指标采集/应用完全就绪
        while True:
            try:
                await asyncio.to_thread(_run_alert_eval_once)
            except Exception as exc:
                logger.exception("告警评估轮异常")
                _report_job_failure("alert_eval", exc)
            await asyncio.sleep(settings.ALERT_EVAL_INTERVAL)

    _alert_task = asyncio.get_running_loop().create_task(loop())
    logger.info("告警评估已启动: 周期 %ds", settings.ALERT_EVAL_INTERVAL)


def _run_alert_eval_once() -> None:
    from app.services.alert_eval import run_evaluation

    db = SessionLocal()
    try:
        run_evaluation(db)          # 后台轮：按账号节奏过滤；手动触发传 force=True
    finally:
        db.close()


def _start_cost_collector() -> None:
    """成本采集后台循环：每日 COST_COLLECT_HOUR 点拉 T-1 账单；启动时补采缺口。"""
    global _cost_task
    if not settings.COST_COLLECT_ENABLED:
        logger.info("成本采集未启用（COST_COLLECT_ENABLED=false）")
        return

    async def loop() -> None:
        await asyncio.sleep(30)  # 等应用完全就绪
        while True:
            try:
                await asyncio.to_thread(_run_cost_collect_once)
            except Exception as exc:
                logger.exception("成本采集轮异常")
                _report_job_failure("cost_collect", exc)
            # 30 分钟自愈轮：采集器按缺口驱动，无缺口时只查一次 max(stat_date) 就返回。
            # T-1 账单可能晚于每日定点入库，半小时轮询确保当天补齐；T-1 当日会补账/改行、
            # 不是终态，因此采集窗口固定重拉 T-2..T-1。
            await asyncio.sleep(1800)

    _cost_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "成本采集已启动: T-1 账单缺口自愈每 30 分钟（每日 %02d:%02d 前后必达）",
        settings.COST_COLLECT_HOUR, settings.COST_COLLECT_MINUTE,
    )


def _run_cost_collect_once() -> None:
    from app.collector.credential import account_credential_contexts, active_account_id
    from app.services.cost_job import catch_up_needed, run_collection, run_tencent_collection
    from app.services.monitor_policy import collect_due, mark_collected

    db = SessionLocal()
    try:
        # 阿里账单通道（多账号：每个启用 aliyun 账号各采一轮）；
        # 无阿里凭证软跳过（run_collection 内部收口），不挡腾讯通道
        try:
            ctxs = list(account_credential_contexts(db))   # 多账号：每个启用 aliyun 账号各采一轮
            if not ctxs:
                # 账单断供守卫：无启用账号时通道空转；软跳过与只判全空的就绪度
                # 会掩盖账单持续断供，必须显式检查。
                from app.services.cost_job import guard_bill_staleness
                guard_bill_staleness(db)
            for ctx in ctxs:
                with ctx:
                    acc_id = active_account_id()
                    # 分级监控：medium 2 小时 / low 每天 03:30 一次；high 每轮回原样
                    if not collect_due(db, "cost", acc_id):
                        continue
                    gap = catch_up_needed(db)             # 缺口按账号算（上下文内收口）
                    if gap is not None:
                        run_collection(db, start=gap[0], end=gap[1])
                    mark_collected(db, "cost", acc_id)
                    db.commit()
        except Exception:
            logger.warning("阿里账单采集本轮失败（继续腾讯账单通道）", exc_info=True)
        try:
            run_tencent_collection(db)   # 腾讯账单（tc_bill）
        except Exception:
            logger.warning("腾讯账单采集本轮失败", exc_info=True)
    finally:
        db.close()


async def _daily_with_retry(run_fn, label: str, done_fn=None,
                            retry_s: int = 1800, max_retries: int = 8) -> None:
    """每日任务执行 + 当天失败自愈：只睡到定点且失败不重试会造成当天数据缺失。

    - done_fn=None：跑成功即算完成；抛异常则 retry_s 后当天重试
    - done_fn 给了：以"业务完成"为准（幂等判重），失败/未完成都重试
    """
    for _ in range(max_retries):
        ok = True
        try:
            await asyncio.to_thread(run_fn)
        except Exception as exc:
            ok = False
            logger.exception("%s轮异常，%ds 后当天重试", label, retry_s)
            _report_job_failure(str(label), exc)
        if (done_fn() if done_fn is not None else ok):
            return
        if not ok:
            logger.warning("%s 未完成，%ds 后当天自愈重试", label, retry_s)
        await asyncio.sleep(retry_s)
    logger.warning("%s 当天重试次数用尽（%d 次），等明天调度", label, max_retries)


def _start_inspection() -> None:
    """巡检后台循环：每日 INSPECTION_HOUR:MINUTE 跑规则扫描落事实清单
    （08:30 日报消费此清单，故巡检须先于报告完成）。"""
    global _inspection_task
    if not settings.INSPECTION_ENABLED:
        logger.info("巡检未启用（INSPECTION_ENABLED=false）")
        return

    async def loop() -> None:
        await asyncio.sleep(60)  # 等指标采集/应用完全就绪
        # 已过当日巡检时刻且无记录（错过 08:10 或刚部署）才补跑，否则等定时
        from datetime import datetime as _dt

        now = _dt.now()
        scheduled = now.replace(
            hour=settings.INSPECTION_HOUR, minute=settings.INSPECTION_MINUTE,
            second=0, microsecond=0,
        )
        if now >= scheduled and not _has_run_today():
            try:
                await asyncio.to_thread(_run_inspection_once)
            except Exception as exc:
                logger.exception("巡检补跑异常")
                _report_job_failure("inspection", exc)
        while True:
            await asyncio.sleep(_seconds_until(
                settings.INSPECTION_HOUR, settings.INSPECTION_MINUTE))
            # 当日已有成功 run 则跳过；失败/未跑当天自愈重试
            await _daily_with_retry(_run_inspection_once, "巡检", done_fn=_has_run_today)

    _inspection_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "巡检已启动: 每日 %02d:%02d（日报前）",
        settings.INSPECTION_HOUR, settings.INSPECTION_MINUTE,
    )


def _seconds_until(hour: int, minute: int) -> float:
    """距离下一个 hour:minute（本地时区）的秒数。"""
    from datetime import datetime, timedelta as _td

    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        target += _td(days=1)
    return max(1.0, (target - now).total_seconds())


def _run_inspection_once() -> None:
    from app.services.inspection import run_inspection

    db = SessionLocal()
    try:
        run_inspection(db)
    finally:
        db.close()


def _has_run_today() -> bool:
    from datetime import date as _date

    from app.models.inspection import InspectionRun

    db = SessionLocal()
    try:
        return (
            db.query(InspectionRun.id)
            .filter(InspectionRun.run_date == _date.today(), InspectionRun.status == "ok")
            .first()
            is not None
        )
    finally:
        db.close()


def _start_report() -> None:
    """巡检日报后台循环：每日 08:30 把当日事实清单交给 LLM 汇总成日报，
    站内信全员必达 + 按推送开关发机器人。"""
    global _report_task
    if not settings.INSPECTION_REPORT_ENABLED:
        logger.info("巡检日报未启用（INSPECTION_REPORT_ENABLED=false）")
        return

    async def loop() -> None:
        await asyncio.sleep(90)  # 等巡检（启动补跑在 60s）完成
        while True:
            # 已过日报时刻且当日无报告才补跑，否则等定时（_run_report_once 内部再判重）
            from datetime import datetime as _dt

            now = _dt.now()
            scheduled = now.replace(
                hour=settings.INSPECTION_REPORT_HOUR,
                minute=settings.INSPECTION_REPORT_MINUTE,
                second=0, microsecond=0,
            )
            if now >= scheduled:
                await _daily_with_retry(_run_report_once, "巡检日报", done_fn=_report_ok_today)
            await asyncio.sleep(_seconds_until(
                settings.INSPECTION_REPORT_HOUR, settings.INSPECTION_REPORT_MINUTE))

    _report_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "巡检日报已启动: 每日 %02d:%02d（巡检完成之后）",
        settings.INSPECTION_REPORT_HOUR, settings.INSPECTION_REPORT_MINUTE,
    )


def _report_ok_today() -> bool:
    from datetime import date as _date

    from app.models.inspection_report import InspectionReport

    db = SessionLocal()
    try:
        return (db.query(InspectionReport.id)
                .filter(InspectionReport.report_date == _date.today(),
                        InspectionReport.status == "ok").first() is not None)
    finally:
        db.close()


def _run_report_once() -> None:
    """当日报告已存在则跳过；无巡检记录先补跑巡检再生成。"""
    from datetime import date as _date

    from app.models.inspection_report import InspectionReport
    from app.services.inspection import run_inspection
    from app.services.report import generate_report

    db = SessionLocal()
    try:
        today = _date.today()
        # 只认成功日报，status=error 的行不许堵死当天补跑
        exists = db.query(InspectionReport.id).filter(
            InspectionReport.report_date == today,
            InspectionReport.status == "ok").first()
        if exists:
            return
        if not _has_run_today():
            run_inspection(db)
        generate_report(db, notify=True)
    finally:
        db.close()


def _start_dba_collector() -> None:
    """RDS 空间采集后台循环：每日 DBA_COLLECT_HOUR:MINUTE 快照落库。"""
    global _dba_task
    if not settings.DBA_COLLECT_ENABLED:
        logger.info("DBA 空间采集未启用（DBA_COLLECT_ENABLED=false）")
        return

    async def loop() -> None:
        await asyncio.sleep(45)  # 等应用完全就绪
        while True:
            await _daily_with_retry(_run_dba_collect_once, "DBA空间采集", done_fn=_dba_done_today)
            await asyncio.sleep(_seconds_until(
                settings.DBA_COLLECT_HOUR, settings.DBA_COLLECT_MINUTE))

    _dba_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "DBA 空间采集已启动: 每日 %02d:%02d",
        settings.DBA_COLLECT_HOUR, settings.DBA_COLLECT_MINUTE,
    )


def _dba_done_today() -> bool:
    from datetime import date as _date

    from app.models.dba_space import DbaSpaceDaily

    db = SessionLocal()
    try:
        return (db.query(DbaSpaceDaily.id)
                .filter(DbaSpaceDaily.stat_date == _date.today()).first() is not None)
    finally:
        db.close()


def _run_dba_collect_once() -> None:
    from app.collector.credential import account_credential_contexts
    from app.services.dba_job import run_collection

    db = SessionLocal()
    try:
        for ctx in account_credential_contexts(db):   # 多账号：每个启用 aliyun 账号各采一轮
            with ctx:
                run_collection(db)
    finally:
        db.close()


def _start_expiry_sync() -> None:
    """域名/证书采集后台循环：每日 EXPIRY_SYNC 时刻同步；
    启动时若当日未同步过则补跑（首次部署当天就有数据）。"""
    global _expiry_task
    if not settings.EXPIRY_SYNC_ENABLED:
        logger.info("域名/证书采集未启用（EXPIRY_SYNC_ENABLED=false）")
        return

    async def loop() -> None:
        await asyncio.sleep(70)  # 等应用完全就绪
        if not _expiry_synced_today():
            try:
                await asyncio.to_thread(_run_expiry_sync_once)
            except Exception as exc:
                logger.exception("域名/证书采集补跑异常")
                _report_job_failure("domain_cert", exc)
        while True:
            await asyncio.sleep(_seconds_until(
                settings.EXPIRY_SYNC_HOUR, settings.EXPIRY_SYNC_MINUTE))
            await _daily_with_retry(_run_expiry_sync_once, "域名/证书采集",
                                    done_fn=_expiry_synced_today)

    _expiry_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "域名/证书采集已启动: 每日 %02d:%02d（只读列表 API）",
        settings.EXPIRY_SYNC_HOUR, settings.EXPIRY_SYNC_MINUTE,
    )


def _run_expiry_sync_once() -> None:
    from app.collector.credential import account_credential_contexts
    from app.collector.domain import sync_certs, sync_domains
    from app.services.domain_registry import backfill as domain_backfill

    db = SessionLocal()
    try:
        for ctx in account_credential_contexts(db):   # 多账号：每个启用 aliyun 账号各采一轮
            with ctx:
                sync_domains(db)
                sync_certs(db)
        domain_backfill(db)   # 域名登记回填：注册/证书入库后归集六源
    finally:
        db.close()


def _expiry_synced_today() -> bool:
    from datetime import date as _date

    from app.models.asset import Asset

    db = SessionLocal()
    try:
        row = (
            db.query(Asset.synced_at)
            .filter(Asset.product.in_(["domain", "ssl_cert"]))
            .order_by(Asset.synced_at.desc())
            .first()
        )
        if row is None or row[0] is None:
            return False
        return row[0].date() >= _date.today()
    finally:
        db.close()


def _start_cdn_sync() -> None:
    """CDN 按域名每日流量采集：每日 CDN_SYNC 时刻采昨日整日；
    启动时若当日未同步则补跑；表为空（首次部署）先回补 CDN_BACKFILL_DAYS 天。"""
    global _cdn_task
    if not settings.CDN_SYNC_ENABLED:
        logger.info("CDN 域名流量采集未启用（CDN_SYNC_ENABLED=false）")
        return

    async def loop() -> None:
        await asyncio.sleep(80)  # 等应用完全就绪
        # 缺口驱动 + 当天自愈：按 stat_date 补齐缺失日，避免单日失败后留下永久缺口。
        await _daily_with_retry(_run_cdn_sync_once, "CDN域名流量", done_fn=_cdn_gap_empty)
        while True:
            await asyncio.sleep(_seconds_until(
                settings.CDN_SYNC_HOUR, settings.CDN_SYNC_MINUTE))
            await _daily_with_retry(_run_cdn_sync_once, "CDN域名流量", done_fn=_cdn_gap_empty)

    _cdn_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "CDN 域名流量采集已启动: 每日 %02d:%02d 采昨日（只读监控 API）",
        settings.CDN_SYNC_HOUR, settings.CDN_SYNC_MINUTE,
    )


def _run_cdn_sync_once() -> None:
    """缺口驱动采数：补所有缺失日（最新采到 T-1 才算齐），不只采"昨天"。
    每个启用 aliyun 账号各采一轮，缺口按账号算。"""
    from datetime import date as _date, timedelta as _timedelta

    from app.collector.cdn_traffic import sync_cdn_traffic, sync_cdn_traffic_backfill
    from app.collector.credential import account_credential_contexts, active_account_id
    from app.models.cdn_domain_daily import CdnDomainDaily

    db = SessionLocal()
    try:
        for ctx in account_credential_contexts(db):
            with ctx:
                q = db.query(CdnDomainDaily.stat_date)
                acc = active_account_id()
                if acc is not None:
                    q = q.filter(CdnDomainDaily.account_id == acc)   # 缺口按账号算
                row = q.order_by(CdnDomainDaily.stat_date.desc()).first()
                if row is None:
                    n = settings.CDN_BACKFILL_DAYS
                    logger.info("CDN 域名流量表为空，回补最近 %d 天", n)
                    sync_cdn_traffic_backfill(db, n)
                    continue
                latest = row[0]
                yesterday = _date.today() - _timedelta(days=1)
                # 固定重拉最近 2 天（T+1 数据当日可能修订，与成本账单保持同一口径）
                d = min(latest + _timedelta(days=1), yesterday - _timedelta(days=1))
                while d <= yesterday and (yesterday - d).days <= 31:   # 深缺口上限 31 天
                    sync_cdn_traffic(db, d)
                    logger.info("CDN 域名流量采集 %s（缺口或重拉）", d)
                    d += _timedelta(days=1)
    finally:
        db.close()


def _cdn_gap_empty() -> bool:
    from datetime import date as _date, timedelta as _timedelta

    from app.collector.credential import account_credential_contexts, active_account_id
    from app.models.cdn_domain_daily import CdnDomainDaily

    db = SessionLocal()
    try:
        for ctx in account_credential_contexts(db):
            with ctx:
                q = db.query(CdnDomainDaily.stat_date)
                acc = active_account_id()
                if acc is not None:
                    q = q.filter(CdnDomainDaily.account_id == acc)
                row = q.order_by(CdnDomainDaily.stat_date.desc()).first()
                if row is None or row[0] < _date.today() - _timedelta(days=1):
                    return False   # 任一账号有缺口即未完成
        return True
    finally:
        db.close()


def _start_link_draft_sync() -> None:
    """链路草稿每日刷新：每日 LINK_DRAFT 时刻自动重算，无需手动生成才有数据。
    已确认/已忽略的边不会被覆盖（link_draft 只重算 draft 态）。"""
    global _link_draft_task
    if not settings.LINK_DRAFT_ENABLED:
        logger.info("链路草稿自动刷新未启用（LINK_DRAFT_ENABLED=false）")
        return

    async def loop() -> None:
        from app.models.link_edge import LinkEdge
        from app.services.link_draft import generate_link_draft

        def _run() -> None:
            db = SessionLocal()
            try:
                generate_link_draft(db)
                db.commit()
            finally:
                db.close()

        await asyncio.sleep(120)  # 等应用与 SLS 网络就绪；启动时已有数据就不补跑
        db = SessionLocal()
        try:
            empty = db.query(LinkEdge.id).first() is None
        finally:
            db.close()
        if empty:
            try:
                await asyncio.to_thread(_run)
                logger.info("链路草稿：启动补跑完成（原为空）")
            except Exception as exc:
                logger.exception("链路草稿启动补跑异常")
                _report_job_failure("link_draft", exc)
        while True:
            await asyncio.sleep(_seconds_until(settings.LINK_DRAFT_HOUR, settings.LINK_DRAFT_MINUTE))
            try:
                await asyncio.to_thread(_run)
            except Exception as exc:
                logger.exception("链路草稿每日刷新异常")
                _report_job_failure("link_draft", exc)

    _link_draft_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "链路草稿自动刷新已启动: 每日 %02d:%02d（SLS 只读聚合）",
        settings.LINK_DRAFT_HOUR, settings.LINK_DRAFT_MINUTE,
    )


def _start_db_maint() -> None:
    """每日数据保留清理：无界增长表滚动删除。"""
    global _maint_task
    if not settings.MAINT_ENABLED:
        logger.info("数据保留清理未启用（MAINT_ENABLED=false）")
        return

    async def loop() -> None:
        from app.services.db_maint import prune_old_data

        while True:
            await asyncio.sleep(_seconds_until(settings.MAINT_HOUR, settings.MAINT_MINUTE))
            try:
                def _run() -> dict:
                    db = SessionLocal()
                    try:
                        deleted = prune_old_data(db)
                        db.commit()
                        return deleted
                    finally:
                        db.close()

                deleted = await asyncio.to_thread(_run)
                if any(deleted.values()):
                    logger.info("数据保留清理完成: %s", deleted)
            except Exception as exc:
                logger.exception("数据保留清理异常")
                _report_job_failure("data_retention", exc)

    _maint_task = asyncio.get_running_loop().create_task(loop())
    from app.services import retention as _ret
    logger.info(
        "数据保留清理已启动: 每日 %02d:%02d（llm %dd / audit %dd / 消息 %dd）",
        settings.MAINT_HOUR, settings.MAINT_MINUTE,
        _ret.llm_days(), _ret.audit_days(), _ret.msg_days(),
    )


def _start_db_backup() -> None:
    """每日数据库备份。

    pg_dump 统一入口（services/db_backup.backup_db），落 BACKUP_DIR。
    默认 03:37（错开 03:17 清理与 pg-backup.sh cron）；pg 侧当日已有备份自动跳过
    （与 cron/手工双跑不重打），保留口径=最新 14 份（docs/active/deployment.md §10.6）。"""
    global _db_backup_task
    if not settings.BACKUP_ENABLED:
        logger.info("数据库备份未启用（BACKUP_ENABLED=false）")
        return

    async def loop() -> None:
        from app.services.db_backup import backup_db

        while True:
            await asyncio.sleep(_seconds_until(settings.BACKUP_HOUR, settings.BACKUP_MINUTE))
            try:
                res = await asyncio.to_thread(backup_db, settings.BACKUP_KEEP_DAYS)
                logger.info("每日数据库备份: %s", res)
            except Exception as exc:
                logger.exception("每日数据库备份异常")
                _report_job_failure("db_backup", exc)

    _db_backup_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "每日数据库备份已启动: %02d:%02d（保留 %d 份，目录 %s）",
        settings.BACKUP_HOUR, settings.BACKUP_MINUTE,
        settings.BACKUP_KEEP_DAYS, getattr(settings, "BACKUP_DIR", "/opt/qianshu/backups"),
    )


def _start_engine_retention() -> None:
    """存储引擎超期数据渐进清理。

    VM 死序列清理 + VL 删除任务（能力探活），逐次动态取值热生效，
    docs/active/deployment.md。保存设置时也会即时跑一轮（PUT /settings/retention）。"""
    global _engine_ret_task
    if not settings.MAINT_ENABLED:
        logger.info("引擎保留清理未启用（MAINT_ENABLED=false）")
        return

    async def loop() -> None:
        from app.services.engine_retention import clean_all

        await asyncio.sleep(90)   # 启动避峰：先让采集/建表稳定
        while True:
            try:
                res = await asyncio.to_thread(clean_all)
                vm_r, vl_r = res.get("vm", {}), res.get("vl", {})
                if vm_r.get("deleted_series") or not vm_r.get("skipped") or vl_r.get("task_id"):
                    logger.info("引擎保留清理: vm=%s vl=%s", vm_r, vl_r)
            except Exception as exc:
                logger.exception("引擎保留清理异常")
                _report_job_failure("engine_retention", exc)
            await asyncio.sleep(max(60, int(settings.ENGINE_RETENTION_INTERVAL_MIN)) * 60)

    _engine_ret_task = asyncio.get_running_loop().create_task(loop())
    logger.info("引擎保留清理已启动: 每 %d 分钟（VM 死序列 / VL 删除任务探活）",
                settings.ENGINE_RETENTION_INTERVAL_MIN)


def _start_holiday_sync() -> None:
    """节假日日历自动同步。

    每日 HOLIDAY_SYNC 时刻检查本年/次年数据是否齐，缺则拉（holiday-cn 镜像
    国务院安排，含调休班次）；启动后 80 秒先查一次（新环境即刻可用）。
    """
    global _holiday_task
    if not settings.HOLIDAY_SYNC_ENABLED:
        logger.info("节假日日历自动同步未启用（HOLIDAY_SYNC_ENABLED=false）")
        return

    async def loop() -> None:
        from app.services.holiday import need_sync, sync_holidays

        def _run() -> dict:
            db = SessionLocal()
            try:
                if not need_sync(db):
                    return {}
                r = sync_holidays(db)
                return r
            finally:
                db.close()

        await asyncio.sleep(80)
        try:
            r = await asyncio.to_thread(_run)
            if r:
                logger.info("节假日日历启动同步: %s", r)
        except Exception as exc:
            logger.exception("节假日日历启动同步异常")
            _report_job_failure("holiday_calendar", exc)
        while True:
            await asyncio.sleep(_seconds_until(settings.HOLIDAY_SYNC_HOUR, settings.HOLIDAY_SYNC_MINUTE))
            try:
                r = await asyncio.to_thread(_run)
                if r:
                    logger.info("节假日日历每日同步: %s", r)
            except Exception as exc:
                logger.exception("节假日日历同步异常")
                _report_job_failure("holiday_calendar", exc)
            # 缺口未平（need_sync）当天 30 分钟自愈——节假日表缺失会让同日型基线整天失真
            from app.services.holiday import need_sync as _need_sync
            for _ in range(8):
                try:
                    if not _need_sync():
                        break
                except Exception:
                    break
                await asyncio.sleep(1800)
                try:
                    await asyncio.to_thread(_run)
                except Exception as exc:
                    logger.exception("节假日日历补同步异常")
                    _report_job_failure("holiday_calendar", exc)

    _holiday_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "节假日日历自动同步已启动: 每日 %02d:%02d（含调休班次，源 %s）",
        settings.HOLIDAY_SYNC_HOUR, settings.HOLIDAY_SYNC_MINUTE,
        settings.HOLIDAY_URL_TEMPLATE,
    )


def _start_baseline_job() -> None:
    """周期基线检测：每日 BASELINE_JOB 时刻跑
    CDN 流量突增 + 磁盘/空间增速突变（成本 payg_spike 随 10:00 成本检测内跑）。"""
    global _baseline_task
    if not settings.BASELINE_JOB_ENABLED:
        logger.info("基线检测未启用（BASELINE_JOB_ENABLED=false）")
        return

    async def loop() -> None:
        from app.services.baseline_job import run_baseline_checks

        def _run() -> dict:
            db = SessionLocal()
            try:
                r = run_baseline_checks(db)
                db.commit()
                return r
            finally:
                db.close()

        while True:
            await asyncio.sleep(_seconds_until(settings.BASELINE_JOB_HOUR, settings.BASELINE_JOB_MINUTE))
            try:
                await asyncio.to_thread(_run)
            except Exception as exc:
                logger.exception("基线检测异常")
                _report_job_failure("baseline", exc)

    _baseline_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "周期基线检测已启动: 每日 %02d:%02d（CDN 流量 + 磁盘/空间增速；成本随 10:00 检测）",
        settings.BASELINE_JOB_HOUR, settings.BASELINE_JOB_MINUTE,
    )


def _start_profile_job() -> None:
    """行为画像每日采样+漂移检测。"""
    global _profile_task
    if not settings.PROFILE_JOB_ENABLED:
        logger.info("行为画像未启用（PROFILE_JOB_ENABLED=false）")
        return

    async def loop() -> None:
        # 慢层多时刻采样：按 PROFILE_SAMPLE_HOURS 采样（默认 2/8/14/20），
        # 时刻点逻辑在 profile_engine.sample_hours（可配置），这里只管调度到下一个时刻
        from app.services.profile_engine import run_profiles, seconds_until_next_sample

        def _run() -> dict:
            db = SessionLocal()
            try:
                r = run_profiles(db)
                return r
            finally:
                db.close()

        while True:
            await asyncio.sleep(max(60, seconds_until_next_sample()))
            try:
                r = await asyncio.to_thread(_run)
                logger.info("行为画像采样: %s", r)
            except Exception as exc:
                logger.exception("行为画像采样异常")
                _report_job_failure("behavior_profile", exc)

    _profile_task = asyncio.get_running_loop().create_task(loop())
    from app.services.profile_engine import sample_hours
    logger.info(
        "行为画像已启动: 每日 %s 采样（当日多快照聚合日构成+漂移检测+事件样本短时突变；"
        "PROFILE_SAMPLE_HOURS 可配置）",
        "/".join(f"{h:02d}" for h in sample_hours()),
    )


def _start_object_profile_job() -> None:
    """对象全景画像：对象×多层×联动变化。

    可回滚：OBJECT_PROFILE_V2=false 时不启动（旧行为画像照常）。"""
    if not settings.OBJECT_PROFILE_V2:
        logger.info("对象全景画像未启用（OBJECT_PROFILE_V2=false，回滚态：旧画像引擎独跑）")
        return

    async def loop():
        while True:
            await asyncio.sleep(_seconds_until(settings.OBJECT_PROFILE_JOB_HOUR,
                                               settings.OBJECT_PROFILE_JOB_MINUTE))
            try:
                def _run():
                    from app.core.db import SessionLocal
                    from app.services.object_profile import run_object_profiles
                    db = SessionLocal()
                    try:
                        r = run_object_profiles(db)
                        db.commit()
                        return r
                    finally:
                        db.close()
                await _daily_with_retry(_run, "对象全景画像", done_fn=None)
            except Exception as exc:
                logger.exception("对象全景画像跑批异常")
                _report_job_failure("object_profile", exc)

    global _object_profile_task
    _object_profile_task = asyncio.get_running_loop().create_task(loop())   # 强引用防 GC
    logger.info(
        "对象全景画像已启动: 每日 %02d:%02d（对象多层快照+联动基线+因果链事件；"
        "回滚开关 OBJECT_PROFILE_V2）",
        settings.OBJECT_PROFILE_JOB_HOUR, settings.OBJECT_PROFILE_JOB_MINUTE,
    )


def _start_host_watch() -> None:
    """关机检测：每 HOST_WATCH_INTERVAL_MIN 分钟查一次实例实时状态。
    关机对 core/aux 都是硬信号；aux 机器的指标告警已豁免，这是它两条命脉之一。
    阿里 ECS 与腾讯 CVM 各按账号轮次检测，凭证与资产按厂商对齐。"""
    global _host_watch_task
    if not settings.HOST_WATCH_ENABLED:
        logger.info("关机检测未启用（HOST_WATCH_ENABLED=false）")
        return

    async def loop() -> None:
        from app.collector.credential import account_credential_contexts, active_account_id
        from app.services.host_watch import run_host_check
        from app.services.monitor_policy import collect_due, mark_collected

        def _run() -> dict:
            db = SessionLocal()
            try:
                merged: dict = {"checked": 0, "down": 0, "recovered": 0, "spillover": 0}
                # 多账号：每账号各查一轮；厂商通道分开跑，
                # 凭证与资产按厂商对齐，不跨厂商借凭证。
                for vendor, keys in (("aliyun", ("aliyun",)), ("tencent", ("tencent",))):
                    for ctx in account_credential_contexts(db, vendor=vendor):
                        with ctx:
                            acc_id = active_account_id()
                            # 分级监控：medium 30 分钟 / low 每天 03:20 一次
                            if not collect_due(db, "host", acc_id):
                                continue
                            r = run_host_check(db, vendors=keys)
                            mark_collected(db, "host", acc_id)
                            db.commit()
                            for k in merged:
                                merged[k] += r.get(k, 0) or 0
                return merged
            finally:
                db.close()

        await asyncio.sleep(90)   # 等应用/凭证就绪
        while True:
            try:
                r = await asyncio.to_thread(_run)
                if r.get("down"):
                    logger.warning("关机检测: %s 台非 Running", r["down"])
            except Exception as exc:
                logger.exception("关机检测异常")
                _report_job_failure("host_watch", exc)
            await asyncio.sleep(settings.HOST_WATCH_INTERVAL_MIN * 60)

    _host_watch_task = asyncio.get_running_loop().create_task(loop())
    logger.info("关机检测已启动: 每 %d 分钟（ECS/CVM 实时状态）", settings.HOST_WATCH_INTERVAL_MIN)


def _start_change_sync() -> None:
    """变更源独立调度：沿用云资产周期，不受今日决策清单开关影响。"""
    global _change_sync_task
    if settings.CLOUD_SYNC_INTERVAL_S <= 0:
        logger.info("变更源定时同步未启用（CLOUD_SYNC_INTERVAL_S<=0）")
        return

    async def loop() -> None:
        from app.services.change_source import sync_changes

        def _run() -> dict:
            db = SessionLocal()
            try:
                return sync_changes(db, days=2)
            finally:
                db.close()

        await asyncio.sleep(90)
        while True:
            try:
                r = await asyncio.to_thread(_run)
                if r.get("errors") or r.get("truncated"):
                    logger.warning("变更源本轮不完整: errors=%s truncated=%s",
                                   r.get("errors"), r.get("truncated"))
                else:
                    logger.info("变更源同步: 新增 %d / 拉取 %d",
                                r.get("created", 0), r.get("fetched", 0))
            except Exception as exc:
                logger.exception("变更源同步异常")
                _report_job_failure("change_sync", exc)
            await asyncio.sleep(settings.CLOUD_SYNC_INTERVAL_S)

    _change_sync_task = asyncio.get_running_loop().create_task(loop())
    logger.info("变更源定时同步已启动: 每 %ds（独立于决策清单）", settings.CLOUD_SYNC_INTERVAL_S)


def _start_decision_digest() -> None:
    """今日决策清单：每天一条替代碎片轰炸；渠道推送受 push_settings 总闸控制。"""
    global _digest_task
    if not settings.DECISION_DIGEST_ENABLED:
        logger.info("今日决策清单未启用（DECISION_DIGEST_ENABLED=false）")
        return

    async def loop() -> None:
        from app.services.decision_digest import run_digest

        def _run() -> dict:
            from app.services.recheck import run_rechecks
            db = SessionLocal()
            try:
                rechecks = run_rechecks(db)       # 先复查（决策闭环），结论进今日视野
                r = run_digest(db, push=True)
                return {**r, "rechecks": rechecks}
            finally:
                db.close()

        while True:
            await asyncio.sleep(_seconds_until(
                settings.DECISION_DIGEST_HOUR, settings.DECISION_DIGEST_MINUTE))
            try:
                r = await asyncio.to_thread(_run)
                logger.info("今日决策清单已生成: %s 类事项, 渠道 %s", r.get("sections"), r.get("pushed_channels"))
            except Exception as exc:
                logger.exception("今日决策清单生成异常，30 分钟后当天重试")
                _report_job_failure("decision_digest", exc)
            # 当天失败自愈（判重在 run_digest 内，重试不会重复推送）
            for _ in range(8):
                if _digest_sent_today():
                    break
                await asyncio.sleep(1800)
                try:
                    await asyncio.to_thread(_run)
                except Exception as exc:
                    logger.exception("今日决策清单补跑异常")
                    _report_job_failure("decision_digest", exc)

    _digest_task = asyncio.get_running_loop().create_task(loop())
    logger.info(
        "今日决策清单已启动: 每日 %02d:%02d（渠道推送跟随日报总闸）",
        settings.DECISION_DIGEST_HOUR, settings.DECISION_DIGEST_MINUTE,
    )


def _digest_sent_today() -> bool:
    from datetime import date as _date

    from app.models.in_app_message import InAppMessage

    db = SessionLocal()
    try:
        return (db.query(InAppMessage.id)
                .filter(InAppMessage.msg_type == "digest",
                        InAppMessage.title.like(f"[今日决策] {_date.today().isoformat()}%"))
                .first() is not None)
    finally:
        db.close()


def _ensure_schema_columns() -> None:
    """幂等补列（存量库升级）：users.must_change_password 等。

    与 app.core.migrate 同一套补列助手：统一走 SQLAlchemy inspector（PostgreSQL）。
    """
    from app.core.migrate import _add_column

    try:
        if _add_column(engine, "users", "must_change_password", "BOOLEAN NOT NULL",
                       default="0"):
            logger.info("schema 升级：users 补列 must_change_password")
        if _add_column(engine, "synth_results", "timing", "TEXT", default="''"):
            logger.info("schema 升级：synth_results 补列 timing")
        if _add_column(engine, "alert_records", "impact_level", "VARCHAR(16)", default="''"):
            _add_column(engine, "alert_records", "impact_desc", "TEXT", default="''")
            logger.info("schema 升级：alert_records 补列 impact_level/impact_desc")
        # 常用查询多源：source=日志源名（空=旧行 SLS）、
        # target=CLS 主题/ES 索引；旧行两列空串即原语义
        if _add_column(engine, "saved_queries", "source", "VARCHAR(64)", default="''"):
            logger.info("schema 升级：saved_queries 补列 source")
        if _add_column(engine, "saved_queries", "target", "VARCHAR(256)", default="''"):
            logger.info("schema 升级：saved_queries 补列 target")
        _add_column(engine, "change_events", "source_type", "VARCHAR(32)", default="''")
        _add_column(engine, "change_events", "external_id", "VARCHAR(255)", default="''")
        # 多账号打标：数据面主表带 account_id（默认 1=默认账号）
        # resource_pack_snapshots 是真实表名；旧名会导致补列静默失败，
        # 使 ResourcePackSnapshot 一直没有 account_id。
        for table in ("assets", "cost_daily", "alert_records", "change_events",
                      "cdn_domain_daily", "resource_pack_snapshots", "cloud_credentials"):
            try:
                if _add_column(engine, table, "account_id", "INTEGER NOT NULL", default="1"):
                    logger.info("schema 升级：%s 补列 account_id", table)
            except Exception as e:
                logger.warning("%s account_id 补列跳过: %s", table, str(e)[:60])
    except Exception as e:
        logger.warning("schema 补列跳过（列已存在或库不可写）: %s", str(e)[:80])


def _ensure_init_admin() -> None:
    """首次启动且无任何用户时，创建初始管理员。"""
    from app.models.user import User

    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            import secrets
            pwd = settings.INIT_ADMIN_PASSWORD or secrets.token_urlsafe(12)
            db.add(
                User(
                    username=settings.INIT_ADMIN_USERNAME,
                    password_hash=hash_password(pwd),
                    display_name="管理员",
                    role="admin",
                    must_change_password=True,   # 默认密码必须换掉才能用系统
                )
            )
            db.commit()
            logger.info("已创建初始管理员 %s 初始密码: %s （首次登录后必须修改密码）",
                        settings.INIT_ADMIN_USERNAME,
                        pwd)
    finally:
        db.close()


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


def _start_topology_scan() -> None:
    """拓扑定时轻扫描：每 30 分钟重建图+diff 留痕（秒级，不调云 API）。
    全量同步（资产+链路）走手动"扫描"按钮——用户要知道图是何时扫的、变了什么。"""
    global _topology_task

    async def loop() -> None:
        await asyncio.sleep(60)   # 等应用就绪
        while True:
            try:
                def _light():
                    from app.core.db import SessionLocal
                    from app.services.topology import run_scan
                    db = SessionLocal()
                    try:
                        return run_scan(db, trigger="timer", full=False)
                    finally:
                        db.close()
                r = await asyncio.to_thread(_light)
                if (r.get("diff") or {}).get("added") or (r.get("diff") or {}).get("removed"):
                    logger.info("拓扑定时扫描: 组件 +%d/-%d（变化见扫描历史）",
                                len(r["diff"]["added"]), len(r["diff"]["removed"]))
            except Exception as exc:
                logger.exception("拓扑定时扫描异常")
                _report_job_failure("topology", exc)
            await asyncio.sleep(1800)

    _topology_task = asyncio.get_running_loop().create_task(loop())
    logger.info("拓扑定时扫描已启动: 每 30 分钟轻扫描（全量扫描走页面按钮）")


def _start_cloud_asset_sync() -> None:
    """云资产定时同步：每 CLOUD_SYNC_INTERVAL_S 秒同步全部启用账号。

    接入账号时已后台同步一次（services/cloud/sync_job.bootstrap_account）；本循环负责
    兜底刷新（云上资源增删不需要用户回运维管理点同步）。首轮失败/未播种账号在下轮补播。
    拓扑不需额外动作：build_graph 实时查库，资产变化自动反映。
    """
    global _cloud_sync_task
    if settings.CLOUD_SYNC_INTERVAL_S <= 0:
        logger.info("云资产定时同步未启用（CLOUD_SYNC_INTERVAL_S<=0）")
        return

    async def loop() -> None:
        await asyncio.sleep(180)   # 等应用就绪（首轮接入同步通常已完成）
        while True:
            try:
                def _run():
                    from app.services.cloud.sync_job import auto_sync_all
                    return auto_sync_all()
                r = await asyncio.to_thread(_run)
                if r.get("skipped"):
                    logger.info("云资产定时同步跳过：%s", r["skipped"])
                else:
                    s = r.get("summary") or {}
                    logger.info(
                        "云资产定时同步完成: %d 账号, 新增 %d / 更新 %d / 标失联 %d, 错误 %d",
                        s.get("accounts", 0), s.get("created", 0), s.get("updated", 0),
                        s.get("missing_marked", 0), len(s.get("errors") or []),
                    )
            except Exception as exc:
                logger.exception("云资产定时同步异常")
                _report_job_failure("cloud_assets", exc)
            await asyncio.sleep(settings.CLOUD_SYNC_INTERVAL_S)

    _cloud_sync_task = asyncio.get_running_loop().create_task(loop())
    logger.info("云资产定时同步已启动: 每 %ds 同步全部启用账号", settings.CLOUD_SYNC_INTERVAL_S)


def _start_synth_scheduler() -> None:
    """拨测调度：每 60 秒跑到期任务（各任务自带 interval）。"""
    global _synth_task

    async def loop() -> None:
        await asyncio.sleep(45)
        while True:
            try:
                def _run():
                    from app.core.db import SessionLocal
                    from app.services.synth import run_due_checks
                    db = SessionLocal()
                    try:
                        return run_due_checks(db)
                    finally:
                        db.close()
                await asyncio.to_thread(_run)
            except Exception as exc:
                logger.exception("拨测调度异常")
                _report_job_failure("synth_probe", exc)
            await asyncio.sleep(60)

    _synth_task = asyncio.get_running_loop().create_task(loop())
    logger.info("拨测调度已启动: 每 60 秒执行到期任务（HTTP/TCP/DNS）")


def _start_oncall_escalation() -> None:
    """值班升级链扫描：每 60 秒查超时未认领的 critical 告警，逐级升级。"""
    global _oncall_task

    async def loop() -> None:
        await asyncio.sleep(75)
        while True:
            try:
                def _run():
                    from app.core.db import SessionLocal
                    from app.services.oncall import escalate_due
                    db = SessionLocal()
                    try:
                        return escalate_due(db)
                    finally:
                        db.close()
                r = await asyncio.to_thread(_run)
                if r.get("escalated"):
                    logger.info("值班升级: %d 条告警升级通知", len(r["escalated"]))
            except Exception as exc:
                logger.exception("值班升级扫描异常")
                _report_job_failure("oncall_escalation", exc)
            await asyncio.sleep(60)

    _oncall_task = asyncio.get_running_loop().create_task(loop())
    logger.info("值班升级链已启动: 每 60 秒扫描（critical %d 分钟未认领逐级升级）",
                settings.ALERT_ESCALATE_MINUTES)


def _start_reminder_job() -> None:
    """重复提醒扫描：每 30 分钟重申到期问题。

    确认/忽略后的重申间隔由数据源优先级决定（medium 7 天 / low 30 天 /
    high 不重申 / 账号 repeat_days 可覆盖、0=永不）。到期项重置为未处理，
    聚合发一条提醒并重新进入总览与决策清单。
    """
    global _reminder_task

    async def loop() -> None:
        await asyncio.sleep(120)
        while True:
            try:
                def _run():
                    from app.core.db import SessionLocal
                    from app.services.reminder_job import run_reminders
                    db = SessionLocal()
                    try:
                        return run_reminders(db)
                    finally:
                        db.close()
                r = await asyncio.to_thread(_run)
                if r.get("groups"):
                    logger.info("重复提醒: 告警 %d / 异动 %d（聚合 %d 组）",
                                r["alerts"], r["anomalies"], r["groups"])
            except Exception as exc:
                logger.exception("重复提醒扫描异常")
                _report_job_failure("reminder", exc)
            await asyncio.sleep(1800)

    _reminder_task = asyncio.get_running_loop().create_task(loop())
    logger.info("重复提醒扫描已启动: 每 30 分钟检查已确认/忽略的到期重申项")


def _start_knowledge_probe() -> None:
    """知识探针自检：每天 06:17 跑一遍口径探针，漂移即告警。"""
    global _kprobe_task

    async def loop() -> None:
        await asyncio.sleep(120)
        while True:
            try:
                now = datetime.now()
                if now.hour == 6 and now.minute == 17:
                    def _run():
                        from app.core.db import SessionLocal
                        from app.services.knowledge_probe import run_probes
                        db = SessionLocal()
                        try:
                            return run_probes(db)
                        finally:
                            db.close()
                    r = await asyncio.to_thread(_run)
                    if r.get("drifted"):
                        logger.warning("知识探针发现漂移: %s", r["drifted"])
                    await asyncio.sleep(120)   # 跳过本分钟
            except Exception as exc:
                logger.exception("知识探针异常")
                _report_job_failure("knowledge_probe", exc)
            await asyncio.sleep(60)

    _kprobe_task = asyncio.get_running_loop().create_task(loop())
    logger.info("知识探针已启动: 每天 06:17 自检（漂移即站内信告警）")


def _start_calibration_weekly() -> None:
    """校准周报：每周一 09:37 生成"我学到的"确认清单（幂等）。"""
    global _calib_task

    async def loop() -> None:
        await asyncio.sleep(150)
        while True:
            try:
                now = datetime.now()
                if now.weekday() == 0 and now.hour == 9 and now.minute == 37:
                    def _run():
                        from app.core.db import SessionLocal
                        from app.services.calibration import generate_weekly
                        db = SessionLocal()
                        try:
                            return generate_weekly(db)
                        finally:
                            db.close()
                    r = await asyncio.to_thread(_run)
                    if not r.get("skipped"):
                        logger.info("校准周报: %s", r)
                    await asyncio.sleep(120)
            except Exception as exc:
                logger.exception("校准周报生成异常")
                _report_job_failure("calibration_report", exc)
            await asyncio.sleep(60)

    _calib_task = asyncio.get_running_loop().create_task(loop())
    logger.info("校准周报已启动: 每周一 09:37（我学到的+请拍板清单）")


def _start_k8s_monitor() -> None:
    """K8s 容器监控调度：资源同步+失败扫描每 10 分钟，日志采集每 2 分钟，
    日志保留清理每日 3:23（配置变更另走 API 立即清）。"""
    global _k8s_task

    async def loop() -> None:
        await asyncio.sleep(90)
        last_full = 0
        last_clean = None
        while True:
            try:
                now = time.time()
                full = now - last_full >= 600
                def _run(full=full):
                    from app.core.db import SessionLocal
                    from app.models.k8s import K8sCluster
                    from app.services.k8s_monitor import (
                        cleanup_logs, collect_logs, log_retention_days, scan_failures, sync_cluster)
                    db = SessionLocal()
                    try:
                        out = {"synced": 0, "logs": 0, "failures": 0}
                        for c in db.query(K8sCluster).all():
                            if full:
                                sync_cluster(db, c)
                                out["synced"] += 1
                                out["failures"] += len(scan_failures(db, c))
                            out["logs"] += collect_logs(db, c)
                        now2 = datetime.now()
                        if last_clean is None or (now2.hour == 3 and now2.minute == 23
                                                  and last_clean != now2.date()):
                            cleanup_logs(db)
                            out["cleaned"] = True
                        return out
                    finally:
                        db.close()
                r = await asyncio.to_thread(_run)
                if full:
                    last_full = now
                if r.get("logs") or r.get("failures"):
                    logger.info("K8s 监控: %s", r)
            except Exception as exc:
                logger.exception("K8s 监控调度异常")
                _report_job_failure("k8s_monitor", exc)
            await asyncio.sleep(120)

    _k8s_task = asyncio.get_running_loop().create_task(loop())
    logger.info("K8s 容器监控已启动: 资源同步 10 分钟 / 日志采集 2 分钟 / 保留清理每日 3:23")


def _start_workflow_schedule() -> None:
    """工作流例行体检调度：小时级检查，到点即跑（仅分析域）。

    后台循环只做"到点检查"，执行直调 services/agent_schedule.run_due；该函数可独立
    直调，无需启动服务。
    产物落 agent_workflow_runs；有异动才发站内信（msg_type=inspection），无异动静默。
    """
    global _schedule_task

    async def loop() -> None:
        await asyncio.sleep(150)
        last_checked = None
        while True:
            try:
                now = datetime.now()
                if last_checked != now.hour:
                    last_checked = now.hour

                    def _run():
                        from app.core.db import SessionLocal
                        from app.services.agent_schedule import run_due
                        db = SessionLocal()
                        try:
                            return run_due(db)
                        finally:
                            db.close()

                    r = await asyncio.to_thread(_run)
                    if r.get("ran"):
                        logger.info("工作流例行体检: %s", r)
            except Exception as exc:
                logger.exception("工作流例行体检调度异常")
                _report_job_failure("workflow_health", exc)
            await asyncio.sleep(60)

    _schedule_task = asyncio.get_running_loop().create_task(loop())
    logger.info("工作流例行体检已启动: 小时级检查（到点即跑，有异动才站内信）")


def _start_sdk_governance() -> None:
    """SDK 治理周扫描：每周一 09:00 扫云 SDK 版本（锁版本+人工升级，绝不自动升级）。

    结果落 sdk_scan_records 表 + 站内信《SDK 治理周报》摘要（只发站内信，不推外部渠道）。
    当天幂等（last_run 日期防重），09:xx 分钟漂移也会补跑。
    """
    global _sdk_gov_task

    async def loop() -> None:
        await asyncio.sleep(150)
        last_run = None
        while True:
            try:
                now = datetime.now()
                if now.weekday() == 0 and now.hour == 9 and last_run != now.date():
                    def _run():
                        from app.core.db import SessionLocal
                        from app.services.sdk_governance import run_scan
                        db = SessionLocal()
                        try:
                            return run_scan(db, trigger="timer", notify=True)
                        finally:
                            db.close()
                    r = await asyncio.to_thread(_run)
                    last_run = now.date()
                    logger.info("SDK 治理周扫描: 共 %d 包（可升级 %d / 废弃信号 %d）",
                                r.get("total", 0), r.get("outdated", 0), r.get("deprecated", 0))
            except Exception as exc:
                logger.exception("SDK 治理扫描异常")
                _report_job_failure("sdk_scan", exc)
            await asyncio.sleep(60)

    _sdk_gov_task = asyncio.get_running_loop().create_task(loop())
    logger.info("SDK 治理已启动: 每周一 09:00 扫描（结果落库+站内信周报，不自动升级）")
