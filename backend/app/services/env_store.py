""".env 原子读写 + env_kv 库双写。

规矩：所有写运行时可写配置的代码只走这里——读改写原子（临时文件+os.replace）、
写前备份、写后校验（行数不减），任何失败保留原文件。

双写（docs/active/deployment.md §5.1）：set_keys 同时落 .env 与 env_kv 表
（settings 表先例：配置入库存库）。读取**库优先**（get_key：库值覆盖 .env 值）：
compose 形态容器重建 .env 即失的缺口由库存量兜住——启动时 sync_with_db 把库值
回填 .env（供 pydantic settings / copilotd 桥等 .env 消费方），并把存量 .env 非
密钥键一次入册。密钥（SECRET_KEY/MASTER_KEY/DATABASE_URL/口令/token）由拒写名单
挡在库外：只存 .env，密钥作用域不变。

口径：运行时配置**唯一入口 = 系统设置/安装向导 UI**（set_keys
双写即生效）；.env 只作 bootstrap 初始值与密钥载体。手改 .env **不会静默失效**——
sync_with_db 对账按“谁后改谁生效”裁决冲突（.env mtime vs 该键库 updated_at）：
文件较新 → 手改值入册（手改不丢）；库较新/文件缺失 → 库值回填（同样留痕提示被覆盖）。
环境变量注入面（compose 根 .env 手改/装机引导）：非空注入未入册键收编入册、已入册
异值留痕提示双轨；空串注入视为未注入（${VAR:-} 惯用法，不会盖掉库值）。
全部裁决方向都留痕（日志 + 站内信，绝不走外发渠道），不静默。
手动对账/入册：cd backend && python3 -m app.services.env_store（重启后端亦自动对账）。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
from datetime import datetime

logger = logging.getLogger("qianshu.env_store")

_LOCK = threading.Lock()
_DB_LOCK = threading.Lock()
ENV_PATH = os.path.join(os.getcwd(), ".env")

# 读缓存（mtime 失效）：多 worker 下别的进程写入后，本进程下次读即见新值（热生效）
_CACHE: dict = {"mtime": None, "data": {}}
# env_kv 库覆盖层缓存（短 TTL）：写方 set_keys 即刷；跨进程靠 TTL 兜（秒级收敛）
_DB_CACHE: dict = {"ts": 0.0, "data": {}}
_DB_TTL_S = 3.0

# 密钥拒写名单：永不入 env_kv（密钥作用域=只在 .env，见模块 docstring）。
# KEY 类只拒 _KEY/_KEYS/_KEY_ID 结尾（MASTER_KEY/SECRET_KEY/ACCESS_KEY_ID），
# 中段含 KEY 的普通配置键（如 AAA_KEY_OK）不受影响。
_DB_DENY_EXACT = {"SECRET_KEY", "MASTER_KEY", "DATABASE_URL", "INIT_ADMIN_PASSWORD"}
_DB_DENY_SUBSTR = ("SECRET", "PASSWORD", "PASSWD", "TOKEN", "PRIVATE")
_DB_DENY_SUFFIX = ("_KEY", "_KEYS", "_KEY_ID")


def _db_allowed(key: str) -> bool:
    if key in _DB_DENY_EXACT:
        return False
    up = key.upper()
    if any(s in up for s in _DB_DENY_SUBSTR):
        return False
    return not up.endswith(_DB_DENY_SUFFIX)


def _parsed() -> dict:
    try:
        mt = os.path.getmtime(ENV_PATH)
    except OSError:
        mt = None
    if _CACHE["mtime"] == mt:
        return _CACHE["data"]
    data: dict = {}
    try:
        for ln in open(ENV_PATH).read().splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, _, v = ln.partition("=")
                data[k.strip()] = v.strip()
    except OSError:
        data = {}
    _CACHE["mtime"], _CACHE["data"] = mt, data
    return data


def _db_data(force: bool = False) -> dict:
    """env_kv 全量键值（短 TTL 缓存）。库不可用返回 {}（不抛，.env 路径照常工作）。"""
    import time

    with _DB_LOCK:
        if not force and time.time() - _DB_CACHE["ts"] < _DB_TTL_S:
            return _DB_CACHE["data"]
    try:
        from app.core.db import SessionLocal
        from app.models.env_kv import EnvKv

        db = SessionLocal()
        try:
            rows = db.query(EnvKv.key, EnvKv.value).all()
        finally:
            db.close()
        data = {k: (v if v is not None else "") for k, v in rows}
    except Exception as e:
        logger.debug("env_kv 读取失败（回退 .env）: %s", str(e)[:120])
        return _DB_CACHE["data"]   # 库不可用时沿用旧缓存（好过回退到更旧的 .env）
    with _DB_LOCK:
        _DB_CACHE["ts"] = time.time()
        _DB_CACHE["data"] = data
    return data


def _db_put(values: dict) -> bool:
    """upsert 到 env_kv（拒写名单外）。成功 True。"""
    items = {k: str(v).strip() for k, v in values.items() if _db_allowed(k)}
    if not items:
        return True
    try:
        from app.core.db import SessionLocal
        from app.models.env_kv import EnvKv

        db = SessionLocal()
        try:
            for k, v in items.items():
                row = db.get(EnvKv, k)
                if row is None:
                    db.add(EnvKv(key=k, value=v))
                elif row.value != v:
                    row.value = v
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.error("env_kv 写入失败（.env 已写，下次启动 sync 补库）: %s", str(e)[:120])
        return False
    _db_data(force=True)
    return True


def get_key(key: str) -> str | None:
    """读当前值：**库优先**（env_kv > .env）。未配置返回 None。

    清理循环逐次经此取动态值，保存方写入后
    任一 worker 下次读即见；库/.env 都没有时返回 None（调用方退 settings 初值）。
    """
    dbv = _db_data()
    if key in dbv:
        return dbv[key]
    return _parsed().get(key)


_ENV_BACKUP_KEEP = 5


def _prune_env_backups(keep: int = _ENV_BACKUP_KEEP) -> None:
    """只留最近 N 份 .env 写前备份，避免含密钥备份无限累积。

    备份用于写坏时回滚，不是审计档案；保留最近 5 份足够覆盖一次失败写入/重启排查。
    文件名时间戳可排序；删除失败只记 debug，不影响主写入结果。
    """
    d = os.path.dirname(ENV_PATH) or "."
    prefix = os.path.basename(ENV_PATH) + ".bak-"
    try:
        names = [n for n in os.listdir(d) if n.startswith(prefix)]
    except OSError:
        return
    if len(names) <= keep:
        return
    for n in sorted(names, key=lambda x: x[len(prefix):])[:-keep]:
        try:
            os.unlink(os.path.join(d, n))
        except OSError as e:
            logger.debug("清理 .env 备份失败（忽略）: %s", e)


def set_keys(values: dict) -> bool:
    """写入/更新键值（.env 原子写 + env_kv 双写，拒写名单键只落 .env）。

    返回是否持久化成功 = .env 写成功 且 库写成功（拒写名单键不计库）。
    库写失败时 .env 已持久（下次启动 sync_with_db 自动补库），如实返 False 供审计。
    """
    with _LOCK:
        try:
            if not os.path.exists(ENV_PATH):
                open(ENV_PATH, "w").write("")
            before = open(ENV_PATH).read().splitlines()
            backup = ENV_PATH + f".bak-{datetime.now().strftime('%Y%m%d%H%M%S')}"
            shutil.copy2(ENV_PATH, backup)
            lines = list(before)
            for key, val in values.items():
                pat = re.compile(rf"^{re.escape(key)}=")
                lines = [f"{key}={val}" if pat.match(ln) else ln for ln in lines]
                if not any(pat.match(ln) for ln in lines):
                    lines.append(f"{key}={val}")
            tmp = ENV_PATH + ".tmp"
            with open(tmp, "w") as f:
                f.write("\n".join(lines) + "\n")
            os.replace(tmp, ENV_PATH)
            os.chmod(ENV_PATH, 0o600)
            # 完整性校验：行数不减（防截断丢数据）
            after = open(ENV_PATH).read().splitlines()
            if len(after) < len(before):
                shutil.copy2(backup, ENV_PATH)
                logger.error(".env 写后行数减少（%d→%d），已回滚备份 %s", len(before), len(after), backup)
                return False
            # 写后即刷新读缓存（防同秒内 mtime 不变读到旧值）
            data = {}
            for ln in after:
                if "=" in ln and not ln.lstrip().startswith("#"):
                    k, _, v = ln.partition("=")
                    data[k.strip()] = v.strip()
            try:
                _CACHE["mtime"] = os.path.getmtime(ENV_PATH)
            except OSError:
                _CACHE["mtime"] = None
            _CACHE["data"] = data
            _prune_env_backups()
        except Exception as e:
            logger.error(".env 写入失败（原文件保留）: %s", str(e)[:100])
            return False
    # 库双写（.env 写成功后再写库；拒写名单键跳过）
    db_ok = _db_put(values)
    return db_ok


def _file_mtime() -> float:
    try:
        return os.path.getmtime(ENV_PATH)
    except OSError:
        return 0.0


def _notice_reconcile(out: dict) -> None:
    """对账留痕：日志 + 站内信（只落 in_app_messages，**绝不走外发渠道**——外发属告警红线）。

    一次对账合并成一封站内信；无裁决动作时零消息（幂等二跑不留垃圾）。
    """
    notices = out.get("notices") or []
    if not notices:
        return
    for n in notices:
        logger.warning("env_store 对账留痕: %s", n)
    level = "warning" if any(("被库值" in n or "不一致" in n) for n in notices) else "info"
    try:
        from app.core.db import SessionLocal
        from app.services.notify import notify_inapp

        db = SessionLocal()
        try:
            # 平台系统消息（module=None）：配置对账不受业务提醒开关控制
            notify_inapp(db, "配置对账留痕（.env/配置冲突不静默）",
                         "\n".join(f"- {n}" for n in notices), level=level,
                         msg_type="system", audience="first_admin")
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("对账站内信写入失败（日志留痕仍在）: %s", str(e)[:80])


def sync_with_db() -> dict:
    """启动对账（.env / 环境变量注入 / env_kv 三面统一，幂等；口径见模块 docstring）。

    ① 入册：.env 有、库无的键（拒写名单外）→ 入库（存量配置一次收编）；
    ② 冲突（.env 与库同键不同值）按**谁后改谁生效**：.env mtime > 该键库 updated_at
       → 手改值**入册**（手改不丢，留痕“检测到手改 X 已入册”）；否则库值**回填** .env
       （留痕“X 被库值覆盖”）；
    ③ 缺失：库有 .env 无的键 → 回填 .env（容器重建后 .env 消费方即刻恢复）；
    ④ 环境变量注入（非空，拒写名单外，Settings 字段）：未入册收编入册（装机引导/
       compose 根 .env 手改不丢）；已入册异值不动值、留痕提示双轨。
    幂等：对账后各面一致，二跑零动作（仅持续存在的注入双轨会重复提示）。
    库不可用如实跳过（不阻塞启动）。
    """
    out = {"seeded": 0, "restored": 0, "adopted": 0, "env_seeded": 0, "env_conflicts": 0,
           "skipped": "", "notices": []}
    try:
        from app.core.db import SessionLocal
        from app.models.env_kv import EnvKv

        db = SessionLocal()
        try:
            rows = db.query(EnvKv.key, EnvKv.value, EnvKv.updated_at).all()
            db_rows = {k: (v if v is not None else "", ts) for k, v, ts in rows}
            file_rows = _parsed()
            f_mtime = _file_mtime()

            seed: dict = {}      # ① 文件有库无 → 入册（db.add）
            adopt: dict = {}     # ② 文件较新 → 手改入册（db.update）
            restore: dict = {}   # ② 库较新 / ③ 文件缺失 → 回填 .env（set_keys）
            winner: dict = {}    # 各键最终生效值（供 ④ 注入面对照）

            for k, fv in file_rows.items():
                if not _db_allowed(k):
                    continue
                if k not in db_rows:
                    seed[k] = fv
                    winner[k] = fv
                    continue
                dv, ts = db_rows[k]
                if fv == dv:
                    winner[k] = dv
                    continue
                if f_mtime > (ts.timestamp() if ts else 0.0):
                    adopt[k] = fv
                    winner[k] = fv
                    out["notices"].append(
                        f"检测到手改 {k}={fv[:60]!r} 已入册（.env 较新，以文件为准）")
                else:
                    restore[k] = dv
                    winner[k] = dv
                    out["notices"].append(
                        f"手改 {k}={fv[:60]!r} 被库值 {dv[:60]!r} 覆盖（库较新；改配置请走系统设置）")
            for k, (dv, _ts) in db_rows.items():
                if k not in file_rows and _db_allowed(k):
                    restore[k] = dv
                    winner[k] = dv

            # ④ 环境变量注入面（非空注入才算；空串=未注入，见 config._overlay 联动）
            try:
                from app.core.config import Settings as _St
                fields = set(_St.model_fields)
            except Exception:
                fields = set()
            for k in fields:
                ev = os.environ.get(k)
                if ev is None or not ev.strip() or not _db_allowed(k):
                    continue
                ev = ev.strip()
                if k not in db_rows and k not in file_rows:
                    restore[k] = ev     # 收编：走 set_keys 同时落文件+库
                    winner[k] = ev
                    out["env_seeded"] += 1
                    out["notices"].append(
                        f"检测到手改（环境变量注入）{k}={ev[:60]!r} 已入册")
                elif winner.get(k, "") != ev:
                    out["env_conflicts"] += 1
                    out["notices"].append(
                        f"环境变量注入 {k}={ev[:60]!r} 与生效值 {winner.get(k, '')[:60]!r} "
                        "不一致（双轨：settings 消费面读注入值、动态读取读配置库）；"
                        "请统一改走系统设置并清理注入")

            # —— 落库（①入册 + ②手改入册）——
            for k, v in seed.items():
                db.add(EnvKv(key=k, value=v))
            for k, v in adopt.items():
                row = db.get(EnvKv, k)
                if row is None:
                    db.add(EnvKv(key=k, value=v))
                else:
                    row.value = v
            db.commit()
            out["seeded"] = len(seed)
            out["adopted"] = len(adopt)
            if seed:
                out["notices"].append(f"存量 .env 配置入册 {len(seed)} 项")
        finally:
            db.close()
    except Exception as e:
        out["skipped"] = f"db_unavailable: {str(e)[:80]}"
        logger.warning("env_store 启动对账跳过（库不可用）: %s", str(e)[:120])
        return out
    _db_data(force=True)
    # 回填/收编 .env（走 set_keys 保持原子写+备份+校验同一口径；同时补库幂等）
    if restore:
        if set_keys(restore):
            out["restored"] = len(restore)
        else:
            out["skipped"] = out["skipped"] or "restore_failed"
    if (out["seeded"] or out["restored"] or out["adopted"]
            or out["env_seeded"] or out["env_conflicts"]):
        logger.info("env_store 启动对账: 入册 %d、手改入册 %d、注入收编 %d、回填/覆盖 %d、注入双轨 %d",
                    out["seeded"], out["adopted"], out["env_seeded"],
                    out["restored"], out["env_conflicts"])
    _notice_reconcile(out)
    return out


if __name__ == "__main__":  # pragma: no cover — 手改 .env 后手动入册/对账
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(sync_with_db(), ensure_ascii=False, indent=2))
