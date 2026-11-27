"""轻量 schema 迁移（PostgreSQL，幂等，启动时执行）。

背景：Base.metadata.create_all 只建新表不改旧表，加列必须自己 ALTER。
每次迁移先用 SQLAlchemy inspector 检查列是否存在，存在即跳过——新环境建表时
列已在 CREATE TABLE 里，同样安全通过。ALTER 语句按方言转换类型与默认值
（DATETIME→TIMESTAMP、布尔默认 0/1→false/true）。

cost_anomalies 生命周期列与历史数据归并：
- pack_exhausting 旧实例级记录全部 resolved，后续由抵扣能力池检测；
- 其余类型按 (type, target_key) 分组，每组只留最新一条 open，更早的 resolved。

旧云账号凭证由 migrate_cloud_accounts_from_legacy 幂等迁入统一台账。
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import inspect as sa_inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("qianshu.migrate")


def _cols(engine: Engine, table: str) -> list:
    """列名（建表顺序），跨库；表不存在返回空列表。"""
    insp = sa_inspect(engine)
    if not insp.has_table(table):
        return []
    return [c["name"] for c in insp.get_columns(table)]


def _has_table(engine: Engine, table: str) -> bool:
    return sa_inspect(engine).has_table(table)


def _ddl_fragment(engine: Engine, ddl_type: str, default: str) -> tuple:
    """ALTER 用的 (类型, 默认值) 按方言转换。

    - pg 无 DATETIME 类型 -> TIMESTAMP；
    - pg 布尔列的 DEFAULT 0/1 是 integer 字面量（报错）-> false/true。
    """
    if engine.url.get_backend_name() != "postgresql":
        return ddl_type, default
    dtype = re.sub(r"\bDATETIME\b", "TIMESTAMP", ddl_type, flags=re.I)
    if "BOOLEAN" in ddl_type.upper() and default in ("0", "1"):
        default = "true" if default == "1" else "false"
    return dtype, default


def _add_column(engine: Engine, table: str, column: str, ddl_type: str,
                default: str = "") -> bool:
    """列不存在则 ALTER ADD（带 DEFAULT 时旧行自动填充默认值）。返回是否新增。"""
    if not _has_table(engine, table) or column in _cols(engine, table):
        return False
    dtype, dval = _ddl_fragment(engine, ddl_type, default)
    d = f" DEFAULT {dval}" if default else ""
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {dtype}{d}"))
    logger.info("迁移: %s 增加 %s %s", table, column, ddl_type)
    return True


def migrate_cost_anomalies(engine: Engine) -> None:
    """cost_anomalies 生命周期列 + 历史归并（只在首次加列成功时做数据修正）。"""
    if not _has_table(engine, "cost_anomalies"):
        return  # 表还没建（全新环境，create_all 会带上新列）
    added = _add_column(engine, "cost_anomalies", "last_seen", "DATE")
    _add_column(engine, "cost_anomalies", "ack_status", "VARCHAR(16)", default="'open'")
    _add_column(engine, "cost_anomalies", "ack_note", "TEXT", default="''")
    _add_column(engine, "cost_anomalies", "ack_by", "VARCHAR(64)", default="''")
    _add_column(engine, "cost_anomalies", "ack_at", "DATETIME")
    if not added:
        return  # 已迁移过，数据修正不重复执行
    with engine.begin() as conn:
        # 旧行的 last_seen = detect_date
        conn.execute(text("UPDATE cost_anomalies SET last_seen = detect_date WHERE last_seen IS NULL"))
        # 旧实例级耗尽预测全部关闭（改由池级检测，target_key 语义已变）
        conn.execute(text(
            "UPDATE cost_anomalies SET ack_status='resolved',"
            " ack_note='池化改造：实例级耗尽预测由池级检测替代'"
            " WHERE type='pack_exhausting'"))
        # 其余类型：每组 (type,target_key) 只保留最新一条 open
        conn.execute(text(
            "UPDATE cost_anomalies SET ack_status='resolved',"
            " ack_note='生命周期上线：历史重复记录归并'"
            " WHERE id NOT IN ("
            "   SELECT max(id) FROM cost_anomalies WHERE ack_status='open'"
            "   GROUP BY type, target_key)"
            " AND ack_status='open'"))
    logger.info("迁移: cost_anomalies 历史记录已按生命周期归并")


def migrate_diagnosis_sessions(engine: Engine) -> None:
    """diagnosis_sessions 加 scenario 列（2026-09-24 意图分诊；旧行默认 link_fault）。"""
    if not _has_table(engine, "diagnosis_sessions"):
        return
    _add_column(engine, "diagnosis_sessions", "scenario", "VARCHAR(32)",
                default="'link_fault'")


# 补索引清单：覆盖收件箱、用量统计、告警评估、成本下钻与审计查询路径。
# - in_app_messages (user_id, id)：收件箱按用户+倒序分页（原全表扫）
# - llm_call_logs (purpose, id)：用量统计/明细（原全表扫）
# - alert_records (rule_id, target, status)：评估引擎每 60s 的 firing 查找
# - alert_records (status, id)：告警记录页按状态+倒序
# - cost_daily (instance_id, stat_date)：实例下钻 / 包消耗速率历史
# - audit_logs (action, created_at)：审计按操作类型+时间倒序
# （与 migrate_indexes 共用；scripts/export_ddl.py 导出 pg DDL 时同源引用）
EXTRA_INDEXES = [
    ("ix_in_app_messages_user", "in_app_messages", "(user_id, id)"),
    ("ix_llm_call_logs_purpose_id", "llm_call_logs", "(purpose, id)"),
    ("ix_alert_records_rule_target_status", "alert_records", "(rule_id, target, status)"),
    ("ix_alert_records_status_id", "alert_records", "(status, id)"),
    ("ix_cost_daily_instance_date", "cost_daily", "(instance_id, stat_date)"),
    ("ix_audit_logs_action_created", "audit_logs", "(action, created_at)"),
    ("ix_issue_cases_fingerprint_status", "issue_cases", "(fingerprint, status)"),
    ("ix_issue_cases_source_created", "issue_cases", "(source, created_at)"),
    ("ix_issue_cases_status_last", "issue_cases", "(status, last_seen)"),
    ("ix_issue_plans_issue_status", "issue_plans", "(issue_id, status)"),
    ("ix_issue_events_issue_created", "issue_events", "(issue_id, created_at)"),
]

# 同一指纹的活动问题单唯一；已恢复/已忽略后再次发生可新建并 linked recurrence_of_id。
_ISSUE_ACTIVE_STATUSES = (
    "open", "diagnosing", "awaiting_approval", "awaiting_user",
    "repairing", "verifying", "recurring", "manual_required",
)


def migrate_indexes(engine: Engine) -> None:
    """补查询路径缺失的索引（幂等；CREATE INDEX IF NOT EXISTS）。"""
    with engine.begin() as conn:
        for name, table, cols in EXTRA_INDEXES:
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table}{cols}"))
    logger.info("迁移: 查询索引已就绪（%d 个）", len(EXTRA_INDEXES))


def migrate_self_heal_indexes(engine: Engine) -> None:
    """自愈问题单并发去重索引（幂等，仅 PostgreSQL）。

    create_all 只能建普通索引/约束；部分唯一索引必须显式建，否则两个请求会同时
    插入同指纹问题单。服务层另有 SELECT ... FOR UPDATE + IntegrityError 合并双保险。
    """
    if not _has_table(engine, "issue_cases"):
        return
    statuses = ", ".join("'" + s + "'" for s in _ISSUE_ACTIVE_STATUSES)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_issue_cases_active_fingerprint"
            f" ON issue_cases (fingerprint) WHERE status IN ({statuses})"))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_issue_plans_plan_hash"
            " ON issue_plans (plan_hash) WHERE plan_hash <> ''"))
    logger.info("迁移: 自愈问题单活动指纹唯一索引已就绪")


def migrate_asset_tier(engine: Engine) -> None:
    """assets 加关键度列：core=业务核心，aux=辅助机器。

    aux 机器豁免指标告警与画像，只监测关机和 5xx 外溢，使监测强度与业务关键度对齐。
    """
    if not _has_table(engine, "assets"):
        return
    _add_column(engine, "assets", "tier", "VARCHAR(16)", default="'core'")


def migrate_vmware_tier_aux(engine: Engine) -> None:
    """VMware 资产一次性回填为辅助机器，默认 tier=aux。

    背景：虚拟化平台里模板/中间件/备份机大量存在，默认 core 会让指标告警与
    行为画像全量噪声化。新建行由 collector/base.upsert_asset 直接落 aux；本迁移
    只把接入早期已按 core 入库的存量行改一次，之后用户手动改 core 不会被覆盖。
    幂等：env_kv 落版本标记，只执行一次；标记表不存在则跳过（不冒险每次启动重刷）。
    """
    if not _has_table(engine, "assets") or not _has_table(engine, "env_kv"):
        return
    marker = "migrate.vmware_tier_aux_v1"
    with engine.begin() as conn:
        if conn.execute(text("SELECT 1 FROM env_kv WHERE key = :k"), {"k": marker}).first():
            return
        r = conn.execute(text(
            "UPDATE assets SET tier = 'aux'"
            " WHERE product IN ('vmware_vm', 'vmware_host') AND tier = 'core'"))
        changed = r.rowcount or 0
        # 先删再插（一次性迁移，无并发）；updated_at 用 CURRENT_TIMESTAMP 兼容两库
        conn.execute(text("DELETE FROM env_kv WHERE key = :k"), {"k": marker})
        conn.execute(text(
            "INSERT INTO env_kv (key, value, updated_at)"
            " VALUES (:k, :v, CURRENT_TIMESTAMP)"), {"k": marker, "v": str(changed)})
    logger.info("迁移: VMware 资产默认辅助机器（存量回填 %d 项，仅执行一次）", changed)


def migrate_alert_silence_reason(engine: Engine) -> None:
    """alert_records 加 silence_reason（2026-09-28 通知分诊）。

    必须区分两种静默，否则再触发抑制期会被当成静默时段，在结束后错误补发：
    window=规则静默时段（结束可补发）/ resuppress=再触发抑制期（永不补发）。
    """
    if not _has_table(engine, "alert_records"):
        return
    _add_column(engine, "alert_records", "silence_reason", "VARCHAR(16)", default="''")


def migrate_case_source(engine: Engine) -> None:
    """diagnosis_cases 加 source（2026-09-28 经验回流）：auto=诊断自动沉淀。"""
    if not _has_table(engine, "diagnosis_cases"):
        return
    _add_column(engine, "diagnosis_cases", "source", "VARCHAR(16)", default="'manual'")


def migrate_diag_trigger(engine: Engine) -> None:
    """diagnosis_sessions 加 trigger（2026-09-29）：auto=事件自动触发 / manual=人工发起。
    该字段用于在历史记录中区分系统自动诊断与人工或测试发起的诊断。"""
    if not _has_table(engine, "diagnosis_sessions"):
        return
    _add_column(engine, "diagnosis_sessions", "trigger", "VARCHAR(16)", default="'manual'")


def migrate_case_status(engine: Engine) -> None:
    """diagnosis_cases 加 status：自动诊断只产生待确认候选，人工采纳后才进经验库。

    存量降级 SQL 只能在首次加列时执行一次；重复执行会把已丢弃的自动候选冲回 pending，
    破坏人工处置结果与迁移幂等性。
    """
    if not _has_table(engine, "diagnosis_cases"):
        return
    added = _add_column(engine, "diagnosis_cases", "status", "VARCHAR(16)", default="'accepted'")
    if not added:
        return
    # 存量人工案例保持已确认；自动生成的候选统一降级为待确认。
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE diagnosis_cases SET status='pending' WHERE source='auto'"))


def migrate_agent_p2(engine: Engine) -> None:
    """agent_messages 补工具完整结果列；早期版本只保存 4KB 截断版。
    变更工单、快照和工作流运行三表由 create_all 自动建，不在此迁移。"""
    if _has_table(engine, "agent_messages"):
        _add_column(engine, "agent_messages", "tool_results_json", "TEXT", "'[]'")


def migrate_agent_review(engine: Engine) -> None:
    """千枢智能体两轮交付制：消息审查 verdict 列（2026-11-06，幂等补列）。

    review_json：{pass, issues:[{kind,detail,suggestion}], assertions, revised, rounds}
    ——审查结论与逐条断言标注随消息留档（回溯"它凭什么这么说/审出了什么"）。"""
    if _has_table(engine, "agent_messages"):
        _add_column(engine, "agent_messages", "review_json", "TEXT", "''")


def migrate_user_otp(engine: Engine) -> None:
    """用户 OTP 二步验证字段（2026-10-31 登录安全矩阵）。"""
    if not _has_table(engine, "users"):
        return
    _add_column(engine, "users", "otp_secret", "VARCHAR(64)", "''")
    _add_column(engine, "users", "otp_enabled", "BOOLEAN", "0")


def migrate_users_department(engine: Engine) -> None:
    """users 加 department_id（2026-11-09 组织架构）：挂部门（NULL=未分配，存量零变化）。

    departments / dept_permissions / namespace_grants 为新表（create_all 自动建）。
    """
    if not _has_table(engine, "users"):
        return
    _add_column(engine, "users", "department_id", "INTEGER")


def migrate_verify_switches(engine: Engine) -> None:
    """自签证书 verify 开关三列（2026-11-03）：
    渠道/AI 模型默认校验（1），拨测 accept_self_signed 默认严格（0）。"""
    if _has_table(engine, "alert_channels"):
        _add_column(engine, "alert_channels", "verify_ssl", "BOOLEAN", "1")
    if _has_table(engine, "ai_settings"):
        _add_column(engine, "ai_settings", "verify_ssl", "BOOLEAN", "1")
    if _has_table(engine, "synth_checks"):
        _add_column(engine, "synth_checks", "accept_self_signed", "BOOLEAN", "0")


def migrate_profile_known_state(engine: Engine) -> None:
    """画像「已知道」+分级静默 + 快层事件样本列（2026-11-04，幂等补列）。

    - known_at / silence_until / notify_state：drift 被确认'知道'后转 known，
      核心机器静默 1 天、辅助机器勾选才静默 3 天，到期漂移仍在则恢复提醒；
    - behavior_profiles.event_samples：快层事件样本（分钟级，sample_kind='event'）。
    """
    if _has_table(engine, "behavior_profiles"):
        _add_column(engine, "behavior_profiles", "known_at", "DATETIME")
        _add_column(engine, "behavior_profiles", "silence_until", "DATETIME")
        _add_column(engine, "behavior_profiles", "notify_state", "VARCHAR(16)", default="''")
        _add_column(engine, "behavior_profiles", "event_samples", "TEXT", default="'[]'")
    if _has_table(engine, "object_profiles"):
        _add_column(engine, "object_profiles", "known_at", "DATETIME")
        _add_column(engine, "object_profiles", "silence_until", "DATETIME")
        _add_column(engine, "object_profiles", "notify_state", "VARCHAR(16)", default="''")


def migrate_k8s_cluster_admin(engine: Engine) -> None:
    """k8s_clusters 管理面字段（2026-10-29 管理平台）：SSH 登记 + read_only 安全默认。"""
    if not _has_table(engine, "k8s_clusters"):
        return
    _add_column(engine, "k8s_clusters", "ssh_host", "VARCHAR(64)", "''")
    _add_column(engine, "k8s_clusters", "ssh_user", "VARCHAR(64)", "'root'")
    # 安全默认只读：旧行（生产集群）自动只读，测试集群显式开写
    _add_column(engine, "k8s_clusters", "read_only", "BOOLEAN", "1")
    _add_column(engine, "k8s_clusters", "rotated_at", "DATETIME", "")


def migrate_link_edges_rule_dim(engine: Engine) -> bool:
    """link_edges 规则感知改造（2026-11-04）：加 path_pattern/rule_name/rule_source
    三维列 + 唯一约束换 (chain, host, path_pattern, upstream_addr)。

    sqlite 不能 ALTER 约束，pg 重建约束也要整表重写，统一走整表重建（新表 DDL
    直接取自 ORM 模型元数据并按当前方言编译，与全新建库 create_all 的 schema
    零漂移）：
    旧行全部保留，新列填 ''（path_pattern='' 语义=「全部路径」旧粒度行），
    状态/备注/确认人不动；旧唯一约束 (chain,host,upstream) 行在新约束下仍唯一
    （path_pattern 恒 ''），不存在冲突。幂等：列已存在即跳过（新库 create_all
    直接带新列）。
    """
    if not _has_table(engine, "link_edges"):
        return False   # 表还没建（全新环境，create_all 会带上新列新约束）
    if "path_pattern" in _cols(engine, "link_edges"):
        return False   # 已迁移过
    from sqlalchemy.schema import CreateTable

    from app.models.link_edge import LinkEdge

    with engine.begin() as conn:
        old_cols = _cols(engine, "link_edges")
        # 1) 旧表改名（索引随表走，暂占着 ix_link_edges_* 名字）
        conn.execute(text("ALTER TABLE link_edges RENAME TO link_edges_legacy_mig"))
        # pg 的命名约束是库级对象：旧约束 uq_link_edge 随改名仍占名字，新建表同名约束
        # 会撞 DuplicateTable——先摘掉旧约束（sqlite 约束名随表作用域且不支持 DROP
        # CONSTRAINT，不走此步）
        if engine.url.get_backend_name() == "postgresql":
            conn.execute(text(
                "ALTER TABLE link_edges_legacy_mig DROP CONSTRAINT IF EXISTS uq_link_edge"))
        # 2) 用模型元数据建新表（新列 + 新唯一约束），DDL 按当前方言编译
        conn.execute(text(str(CreateTable(LinkEdge.__table__).compile(dialect=engine.dialect))))
        # 3) 旧行整表洗入，新三维列填默认（''/''/'log'）。
        #    模型后续新增的列（如身份引用 confirmed_by_id）在旧表中可能不存在；
        #    统一补 0/''，否则 INSERT 列集缺列会触发 NOT NULL 约束错误。
        cols_sql = ", ".join(old_cols)
        known_new = ("path_pattern", "rule_name", "rule_source")
        extra_cols = [c.name for c in LinkEdge.__table__.columns
                      if c.name not in old_cols and c.name not in known_new]
        extra_sql = "".join(f", {c}" for c in extra_cols)
        extra_sel = "".join(", 0" if c.endswith("_id") else ", ''" for c in extra_cols)
        conn.execute(text(
            f"INSERT INTO link_edges ({cols_sql}, path_pattern, rule_name, rule_source{extra_sql}) "
            f"SELECT {cols_sql}, '', '', 'log'{extra_sel} FROM link_edges_legacy_mig"))
        # 4) 删旧表（连带其索引释放名字），再补新表索引
        conn.execute(text("DROP TABLE link_edges_legacy_mig"))
        for name, cols in (("ix_link_edges_chain", "(chain)"),
                           ("ix_link_edges_host", "(host)"),
                           ("ix_link_edges_status", "(status)")):
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON link_edges {cols}"))
        # 5) pg 的 SERIAL 序列不随显式 id 灌入推进，校准防新插入撞主键（sqlite 自增无此问题）
        if engine.url.get_backend_name() == "postgresql":
            conn.execute(text(
                "SELECT setval(pg_get_serial_sequence('link_edges','id'),"
                " COALESCE((SELECT MAX(id) FROM link_edges), 0) + 1, false)"))
    logger.info("迁移: link_edges 规则维度重建完成（旧行保留，path_pattern=''=全路径旧粒度）")
    return True


def migrate_cloud_accounts_from_legacy(engine: Engine) -> dict:
    """旧凭证表 cloud_credentials -> 云账号表 cloud_accounts 一次性洗入（幂等）。

    2026-11-02 多云收口：定时任务凭证主通道改为 CloudAccount（resolve 新通道优先）。
    **旧表保留只读过渡**——旧行不删不改，resolve 兜底链仍可读；过渡期经旧入口新增的
    凭证会在下次启动补洗入（本函数幂等，可重复执行）。

    迁移口径（每行 -> 一行 CloudAccount）：
    - name=旧名；vendor=旧行 vendor（默认 aliyun）；enabled 保留；
    - ak/secret 统一为 encrypt_secret 密文：旧值可能是明文（AK）或既有密文（secret_cipher，
      同套 AES-256-GCM 格式）——先试解密，解开=密文转明文再加密、解不开=按明文加密，
      保证 ak_cipher/secret_cipher 均为同一套可逆密文（兼容"AK 明文/密文"两种历史形态）；
    - 幂等：extra_json 记 legacy_credential_id 映射 + 按 name 去重（name 有唯一约束），
      重复启动不产生重复行、不覆盖人工改过的行；
    - 洗入顺序 is_default DESC, id ASC：旧"默认凭证"落最小 id，resolve 默认取它，
      单账号部署凭证来源连续不漂移。

    2026-11-09 凭证体系收口：云账号管理=唯一凭证入口，本迁移为旧表数据的唯一入台账通道；
    旧表写入口（/settings/credentials 增删改/揭示/测试）已 410 弃用，旧表此后只读冻结。
    备份说明：动手前备份 cloud_credentials 与 cloud_accounts 两表
    （pg_dump -t cloud_credentials -t cloud_accounts）。
    回滚预案：删除洗入行即可——旧表旧行不删不改，回滚零丢失：
      DELETE FROM cloud_accounts WHERE extra_json LIKE '%"migrated_from": "cloud_credentials"%';
    （人工改过的洗入行如需保留，按 extra_json.legacy_credential_id 逐行甄别后只删纯镜像行。）

    返回 {"migrated": n, "skipped": n, "errors": n}。
    """
    from sqlalchemy import inspect as _sa_inspect
    from sqlalchemy.orm import Session

    insp = _sa_inspect(engine)
    if not (insp.has_table("cloud_credentials") and insp.has_table("cloud_accounts")):
        return {"migrated": 0, "skipped": 0, "errors": 0}   # 新环境：无旧行可洗

    from app.core.security import decrypt_secret, encrypt_secret
    from app.models.cloud_account import CloudAccount
    from app.models.credential import CloudCredential

    def _as_cipher(value: str) -> str:
        """明文或既有密文 -> 统一 encrypt_secret 密文（兼容旧表 AK 明文/密文两种形态）。"""
        if not value:
            return ""
        try:
            plain = decrypt_secret(value)   # 能解开=已是密文，转明文
        except Exception:
            plain = value                   # 解不开=明文
        return encrypt_secret(plain)

    migrated = skipped = errors = 0
    with Session(engine) as db:
        rows = (
            db.query(CloudCredential)
            .order_by(CloudCredential.is_default.desc(), CloudCredential.id)
            .all()
        )
        existing = db.query(CloudAccount).all()
        names = {a.name for a in existing}
        legacy_ids: set = set()
        for a in existing:   # extra_json 里的旧 id 映射（重命名后仍幂等）
            try:
                extra = json.loads(a.extra_json or "{}")
                if extra.get("legacy_credential_id") is not None:
                    legacy_ids.add(int(extra["legacy_credential_id"]))
            except Exception:
                pass

        for r in rows:
            if r.id in legacy_ids or r.name in names:
                skipped += 1
                continue
            if not (r.access_key_id or "").strip():
                errors += 1
                logger.warning("凭证 %s 无 AK，跳过洗入", r.name)
                continue
            try:
                ak_cipher = _as_cipher(r.access_key_id)
                secret_cipher = _as_cipher(r.secret_cipher or "")
            except Exception as e:   # MASTER_KEY 缺失等：单行失败不阻断其余
                errors += 1
                logger.error("凭证 %s 洗入失败，跳过: %s", r.name, e)
                continue
            extra = {
                "legacy_credential_id": r.id,
                "migrated_from": "cloud_credentials",
                "is_default": bool(r.is_default),
                "remark": r.remark or "",
            }
            if getattr(r, "account_id", None):
                extra["legacy_account_id"] = r.account_id
            db.add(CloudAccount(
                name=r.name, vendor=r.vendor or "aliyun",
                ak_cipher=ak_cipher, secret_cipher=secret_cipher,
                extra_json=json.dumps(extra, ensure_ascii=False),
                enabled=bool(r.enabled), status="untested",
            ))
            names.add(r.name)
            legacy_ids.add(r.id)
            migrated += 1
        db.commit()

    if migrated or skipped or errors:
        logger.info(
            "云账号洗入: 新迁 %d / 跳过 %d（已迁或重名）/ 失败 %d（旧凭证表保留只读过渡）",
            migrated, skipped, errors,
        )
    return {"migrated": migrated, "skipped": skipped, "errors": errors}


def migrate_alert_rule_account(engine: Engine) -> None:
    """alert_rules 补 account_id 维度（2026-11-09，幂等补列）。

    0=全部账号（存量规则默认，行为不变）；>0=绑定账号（cloud_accounts.id），
    评估只覆盖该账号资产、记录按目标账号打标。
    """
    if not _has_table(engine, "alert_rules"):
        return
    _add_column(engine, "alert_rules", "account_id", "INTEGER", default="0")
    with engine.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_alert_rules_account ON alert_rules (account_id)"))


def migrate_channel_email(engine: Engine) -> None:
    """alert_channels 邮件渠道列（2026-11-12 0.3.1）。

    与 update-data/0.3.1/001_alert_channel_email.* 双路径互补：SQL 覆盖发布包升级，
    本函数覆盖启动自建/旧库（create_all 只建新表，不给旧表补列）。
    """
    if not _has_table(engine, "alert_channels"):
        return
    _add_column(engine, "alert_channels", "smtp_host", "VARCHAR(255)", default="''")
    _add_column(engine, "alert_channels", "smtp_port", "INTEGER", default="0")
    _add_column(engine, "alert_channels", "smtp_user", "VARCHAR(255)", default="''")
    _add_column(engine, "alert_channels", "smtp_password_cipher", "TEXT", default="''")
    _add_column(engine, "alert_channels", "smtp_starttls", "BOOLEAN", default="0")
    _add_column(engine, "alert_channels", "email_from", "VARCHAR(255)", default="''")
    _add_column(engine, "alert_channels", "email_to", "TEXT", default="'[]'")


def migrate_cloud_auto_sync(engine: Engine) -> None:
    """接入即用（2026-11-11）：规则来源标识 + 云账号自动播种/同步时间（幂等补列）。

    alert_rules.origin: manual（存量默认）/ auto（接入账号后自动播种，可编辑可删除）。
    cloud_accounts.alert_seeded_at: 一次性播种标记（删除规则后不复活）。
    cloud_accounts.last_sync_at: 最近同步时间（总览页展示）。
    """
    if _has_table(engine, "alert_rules"):
        _add_column(engine, "alert_rules", "origin", "VARCHAR(8)", default="'manual'")
    if _has_table(engine, "cloud_accounts"):
        _add_column(engine, "cloud_accounts", "alert_seeded_at", "DATETIME")
        _add_column(engine, "cloud_accounts", "last_sync_at", "DATETIME")


# 身份引用统一，消除用户引用 int/string 混用：
# 旧字符串列（*_by，username 展示快照）各补一个 *_by_id int 列（users.id 引用），
# 模板=audit_logs 的 (user_id, username) 双字段模式。写入处双写，读取两列都给。
# 备份说明：迁移只加列+按 users.username 回填，不改不删旧列；
# 回滚=忽略新增列即可（旧行为仅用 *_by 字符串，完全兼容）。
IDENTITY_REF_COLUMNS = [
    ("alert_acks", "ack_by"),
    ("cost_anomalies", "ack_by"),
    ("behavior_profiles", "confirmed_by"),
    ("alert_target_baselines", "confirmed_by"),
    ("calibration_items", "confirmed_by"),
    ("k8s_failures", "confirmed_by"),
    ("link_edges", "confirmed_by"),
    ("ownership_rules", "created_by"),
    ("expiry_ignores", "created_by"),
    ("biz_identifiers", "created_by"),
    ("k8s_tasks", "submitted_by"),
    ("workflow_defs", "created_by"),
    ("agent_workflow_schedules", "created_by"),
    ("workflow_def_versions", "created_by"),
    ("agent_change_tickets", "confirmed_by"),
]


def migrate_identity_refs(engine: Engine) -> None:
    """身份引用补 *_by_id（2026-11-09，幂等）：加列 + 按 users.username 回填。

    只在**首次加列**时回填一次，避免每次启动都执行数据修正。
    回填两级匹配：精确 username 先行，大小写不敏感兜底（users.username 唯一，无歧义）；
    对不上用户的旧行留 0（历史系统账号/已删用户——如实留空，不造引用）。
    """
    if not _has_table(engine, "users"):
        return
    for table, col in IDENTITY_REF_COLUMNS:
        if not _has_table(engine, table):
            continue
        added = _add_column(engine, table, f"{col}_id", "INTEGER", default="0")
        if not added:
            continue
        with engine.begin() as conn:
            # COALESCE 兜底 0：对不上用户的行保持 0，NULL 会破坏"0=未知"语义。
            conn.execute(text(
                f"UPDATE {table} SET {col}_id = COALESCE(("
                f" SELECT u.id FROM users u WHERE u.username = {table}.{col}), 0)"
                f" WHERE {col} <> '' AND {col}_id = 0"))
            conn.execute(text(
                f"UPDATE {table} SET {col}_id = COALESCE(("
                f" SELECT u.id FROM users u WHERE lower(u.username) = lower({table}.{col})), 0)"
                f" WHERE {col} <> '' AND {col}_id = 0"))
    logger.info("迁移: 身份引用 *_by_id 就绪（%d 表）", len(IDENTITY_REF_COLUMNS))


def migrate_k8s_task_cluster_ref(engine: Engine) -> None:
    """k8s_tasks 补 cluster_id（2026-11-09，幂等补列+回填）。

    旧表只存 cluster_name（字符串软引用，集群改名即断链）；统一为
    (cluster_id, cluster_name) 双字段——id 连实体、name 作展示快照。
    回填按 k8s_clusters.name 精确匹配；对不上的留 0（集群已删/未登记，不造引用）。
    """
    if not _has_table(engine, "k8s_tasks"):
        return
    added = _add_column(engine, "k8s_tasks", "cluster_id", "INTEGER", default="0")
    if not added:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE k8s_tasks SET cluster_id = COALESCE(("
            " SELECT c.id FROM k8s_clusters c WHERE c.name = k8s_tasks.cluster_name), 0)"
            " WHERE cluster_id = 0 AND cluster_name <> ''"))
    logger.info("迁移: k8s_tasks.cluster_id 就绪（按集群名回填）")


def migrate_account_concept_unify(engine: Engine) -> dict:
    """account_id 双概念唯一化（2026-11-09）：assets.account_id 显式指向 cloud_accounts。

    背景：数据表 account_id 历史上混用两套语义——accounts（数据分组）与
    cloud_accounts（云账号台账），展示层靠 tenant.account_index 合并兜底（接缝）。
    本迁移把**旧数据分组洗入云账号台账（保 id）**：每个 accounts 行在 cloud_accounts
    建同 id 镜像行（extra_json.migrated_from='accounts_p22' 记来源），
    存量数据行 account_id 零改写即显式指向 cloud_accounts；accounts 表退为纯
    「数据分组」标签（展示面已改名避混淆），不再占用 account_id 语义。

    口径（红线：不猜）：镜像行 vendor 留空（厂商未登记不猜）、enabled=0
    （无密钥不参与采集轮次），只作归属解析/账号维度参照。

    幂等：cloud_accounts 已有同 id 行即跳过；镜像行带标记可辨识。
    备份说明：动手前备份 cloud_accounts 与 accounts 两表
    （pg_dump -t accounts -t cloud_accounts）。
    回滚预案：删除镜像行即可——
      DELETE FROM cloud_accounts WHERE extra_json LIKE '%"migrated_from": "accounts_p22"%';
    数据行未改写，回滚零丢失。
    """
    from sqlalchemy.orm import Session

    if not (_has_table(engine, "accounts") and _has_table(engine, "cloud_accounts")):
        return {"bridged": 0, "skipped": 0}
    from app.models.account import Account
    from app.services.tenant import bridge_group_to_cloud

    bridged = skipped = 0
    with Session(engine) as db:
        for g in db.query(Account).order_by(Account.id).all():
            _row, created = bridge_group_to_cloud(db, g)
            if created:
                bridged += 1
            else:
                skipped += 1
        db.commit()

    if bridged:
        logger.info("迁移: 数据分组洗入云账号台账 %d 行（保 id，标记 accounts_p22，"
                    "回滚=删镜像行，数据零改写）", bridged)
    return {"bridged": bridged, "skipped": skipped}


def migrate_self_heal_messages_read(engine: Engine) -> None:
    """平台自愈存量站内信一次性标记已读（2026-11-11）。

    自愈事件不再写站内信，但升级前已经写进去的未读消息还会点亮铃铛红点。
    不删除（保留历史可查），只把标题为「千枢自愈…」的未读消息标记已读；
    env_kv 落一次性标记，之后不再扫描（升级后新消息也不会再产生）。
    """
    if not _has_table(engine, "in_app_messages") or not _has_table(engine, "env_kv"):
        return
    marker = "migrate.self_heal_messages_read_v1"
    with engine.begin() as conn:
        if conn.execute(text("SELECT 1 FROM env_kv WHERE key = :k"), {"k": marker}).first():
            return
        r = conn.execute(text(
            "UPDATE in_app_messages SET read_at = CURRENT_TIMESTAMP"
            " WHERE read_at IS NULL AND title LIKE '千枢自愈%'"))
        changed = r.rowcount or 0
        conn.execute(text("DELETE FROM env_kv WHERE key = :k"), {"k": marker})
        conn.execute(text(
            "INSERT INTO env_kv (key, value, updated_at)"
            " VALUES (:k, :v, CURRENT_TIMESTAMP)"), {"k": marker, "v": str(changed)})
    logger.info("迁移: 平台自愈站内信存量标记已读 %d 条（不再产生新消息）", changed)


def run_data_migrations(engine: Engine) -> None:
    """ORM 数据迁移（必须在 update-data 之后执行）。

    2026-11-12 启动顺序修复：ORM 查询会 SELECT 模型全部列，而模型新增列由 update-data
    补齐；若在补列前跑，旧库启动直接 Fatal（cloud_accounts.priority does not exist）。
    因此 main 先跑 schema 迁移 + update-data，再调用本函数。
    """
    migrate_cloud_accounts_from_legacy(engine)
    migrate_account_concept_unify(engine)   # account_id 双概念唯一化，保 id 洗入


def migrate_business_status(engine: Engine) -> None:
    """业务草稿确认门（2026-11-16）：旧人工映射保留生效，旧自动草稿退回待确认。"""
    if not _has_table(engine, "businesses"):
        return
    added = _add_column(engine, "businesses", "status", "VARCHAR(16)",
                        default="'confirmed'")
    if not added:
        return
    with engine.begin() as conn:
        result = conn.execute(text(
            "UPDATE businesses SET status='draft' WHERE description LIKE '自动草稿%'"))
    logger.info("迁移: 业务归属草稿确认门就绪，旧自动草稿 %s 条转为待确认",
                getattr(result, "rowcount", 0))


def migrate_change_sources(engine: Engine) -> None:
    """变更来源/原生 ID 补列；旧 webhook 按既有 event_id 前缀回填，其余为 ActionTrail。"""
    if not _has_table(engine, "change_events"):
        return
    _add_column(engine, "change_events", "source_type", "VARCHAR(32)", default="''")
    _add_column(engine, "change_events", "external_id", "VARCHAR(255)", default="''")
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE change_events SET external_id=event_id"
            " WHERE external_id='' OR external_id IS NULL"))
        conn.execute(text(
            "UPDATE change_events SET source_type=CASE"
            " WHEN event_id LIKE 'github:%' THEN 'github'"
            " WHEN event_id LIKE 'gitlab:%' THEN 'gitlab'"
            " WHEN event_id LIKE 'gitea:%' THEN 'gitea'"
            " WHEN event_id LIKE 'jenkins:%' THEN 'jenkins'"
            " WHEN event_id LIKE 'skywalking:%' THEN 'skywalking'"
            " WHEN event_id LIKE 'zipkin:%' THEN 'zipkin'"
            " ELSE 'actiontrail' END"
            " WHERE source_type='' OR source_type IS NULL"))


def run_migrations(engine: Engine, *, orm_data: bool = True) -> None:
    """全部迁移入口（startup 时在 create_all 之后调用）。

    orm_data=False 时只跑 schema 补列/DDL，ORM 数据迁移由调用方在 update-data
    之后用 run_data_migrations() 补跑（main._init_schema_serialized 即此用法）。
    """
    # schema 补列必须先于 ORM 数据迁移（2026-11-11 旧版 PostgreSQL 兼容约束：
    # migrate_cloud_accounts_from_legacy 用 ORM 查 CloudAccount，模型新增列在旧库
    # 未补时启动直接 Fatal；列补完再跑数据迁移才安全）
    if engine.url.get_backend_name() == "postgresql":
        migrate_cloud_auto_sync(engine)
    if orm_data:
        run_data_migrations(engine)
    if engine.url.get_backend_name() != "postgresql":
        return  # 非 PostgreSQL 由 DBA 管理 schema（脚本自建库除外）
    migrate_change_sources(engine)
    migrate_cost_anomalies(engine)
    migrate_diagnosis_sessions(engine)
    migrate_indexes(engine)
    migrate_self_heal_indexes(engine)
    migrate_asset_tier(engine)
    migrate_vmware_tier_aux(engine)   # 2026-11-11 VMware 默认辅助机器（一次性回填）
    migrate_alert_silence_reason(engine)
    migrate_case_source(engine)
    migrate_diag_trigger(engine)
    migrate_case_status(engine)
    migrate_k8s_cluster_admin(engine)
    migrate_user_otp(engine)
    migrate_users_department(engine)   # 2026-11-09 组织架构：users 挂部门（幂等补列）
    migrate_alert_rule_account(engine)   # 2026-11-09 规则账号维度
    # migrate_cloud_auto_sync 已前置到 ORM 数据迁移之前执行（见函数开头），此处不再重复
    migrate_identity_refs(engine)   # 2026-11-09 身份引用 *_by_id
    migrate_k8s_task_cluster_ref(engine)   # 2026-11-09 集群引用统一
    migrate_business_status(engine)   # 2026-11-16 业务草稿确认后才参与归属
    migrate_agent_p2(engine)   # 2026-11-05 智能体工具完整结果列
    migrate_agent_review(engine)   # 2026-11-06 智能体两轮交付制审查 verdict 列
    migrate_verify_switches(engine)   # 2026-11-03 C2 自签 verify 开关
    migrate_profile_known_state(engine)   # 2026-11-04 C1/C2 画像已知道+静默+事件样本列
    migrate_link_edges_rule_dim(engine)   # 2026-11-04 链路边规则维度（重建表，旧行保留）
    migrate_self_heal_messages_read(engine)   # 2026-11-11 自愈站内信停发 + 存量已读
    # migrate_channel_email 不在此处调用（2026-11-12 启动顺序约束）：
    # main 的启动顺序是 run_migrations -> update-data -> migrate_channel_email。
    # 由 update-data 先补列、startup 迁移只作兜底（见 main._init_schema_serialized），
    # 避免同一列在升级 SQL 后续步骤中重复添加。
    # permission_policies 为新表（create_all 自动建）
