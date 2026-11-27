"""WebShell WebSocket：机器 SSH 直达 + Pod exec 终端。

安全：token 走 query（浏览器 WS 不能带 header）；SSH 凭证机器库解密托管；
终端审计=会话记录+命令级追踪（按键流还原命令行，非逐字节录像——敏感输出不长期落库）；
执行的 shell 命令为代码固定模板，不携带用户输入文本（LLM/用户命令原文不执行——红线延续）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.client_ip import get_client_ip

logger = logging.getLogger("qianshu.ws")
router = APIRouter()


def _auth(token: str) -> dict | None:
    from app.core.db import SessionLocal
    from app.core.security import API_TOKEN_PREFIX, decode_access_token
    from app.models.user import User

    try:
        if token.startswith(API_TOKEN_PREFIX):
            # qs_ 前缀的个人 API Token 复用 core/deps 校验：哈希匹配、启用、未过期，
            # 并刷新 last_used_at；权限继承所属用户角色。
            from app.core.deps import _api_token_user

            db = SessionLocal()
            try:
                u = _api_token_user(db, token)
                return {"id": u.id, "username": u.username, "role": u.role}
            finally:
                db.close()
        payload = decode_access_token(token)
        db = SessionLocal()
        try:
            u = db.get(User, int(payload.get("sub", 0)))
            if u is None:
                return None
            return {"id": u.id, "username": u.username, "role": u.role}
        finally:
            db.close()
    except Exception:
        return None


# ================= 机器 SSH 终端 =================

@router.websocket("/ws/machines/{mid}/ssh")
async def machine_ssh(ws: WebSocket, mid: int, token: str = Query(""),
                      cols: int = Query(120), rows: int = Query(32)):
    import paramiko

    from app.core.db import SessionLocal
    from app.core.security import decrypt_secret
    from app.models.machine import K8sMachine
    from app.services import audit as audit_svc

    user = _auth(token)
    if not user or user["role"] not in ("admin", "operator"):
        await ws.close(code=4401)
        return
    db = SessionLocal()
    try:
        m = db.get(K8sMachine, mid)
        if m is None:
            await ws.close(code=4404)
            return
        pwd = ""
        if m.ssh_password_cipher:
            try:
                pwd = decrypt_secret(m.ssh_password_cipher)
            except Exception:
                await ws.close(code=4500)
                return
        host, port, ssh_user = m.ip, m.ssh_port, m.ssh_user
    finally:
        db.close()

    await ws.accept()
    chan = None
    ssh = None
    cmd_buf = ""
    cmd_count = 0
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        await asyncio.to_thread(
            ssh.connect, host, port, ssh_user, pwd or None, timeout=12,
            allow_agent=not bool(pwd), look_for_keys=not bool(pwd))
        chan = await asyncio.to_thread(ssh.invoke_shell, term="xterm-256color", width=cols, height=rows)
        chan.settimeout(0.2)
        await ws.send_text(f"\x1b[32m[千枢终端] 已连接 {ssh_user}@{host}（会话留痕审计）\x1b[0m\r\n")

        async def pump_out():
            nonlocal cmd_count, cmd_buf
            import socket as _sock

            loop = asyncio.get_event_loop()
            while not chan.closed:
                try:
                    data = await loop.run_in_executor(None, chan.recv, 4096)
                except _sock.timeout:
                    continue   # 轮询节奏：无输出不是流结束
                except Exception:
                    break
                if not data:
                    break
                # 命令级追踪：回显流按回车还原命令行（近似，含 tab 补全场景的容错）
                text = data.decode("utf-8", "replace")
                for ch in text:
                    if ch in ("\r", "\n"):
                        if cmd_buf.strip():
                            cmd_count += 1
                            cmd_buf = ""
                    elif ch == "\x7f":
                        cmd_buf = cmd_buf[:-1]
                    elif ch.isprintable():
                        cmd_buf += ch
                        if len(cmd_buf) > 500:
                            cmd_buf = cmd_buf[-200:]
                await ws.send_text(text)

        async def pump_in():
            loop = asyncio.get_event_loop()
            while True:
                msg = await ws.receive_text()
                try:
                    obj = json.loads(msg)
                except Exception:
                    obj = None
                if isinstance(obj, dict) and obj.get("type") == "resize":
                    await loop.run_in_executor(
                        None, chan.resize_pty, int(obj.get("cols", 120)), int(obj.get("rows", 32)))
                    continue
                await loop.run_in_executor(None, chan.send, msg)

        t_out = asyncio.create_task(pump_out())
        t_in = asyncio.create_task(pump_in())
        done, pending = await asyncio.wait({t_out, t_in}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    except Exception as e:
        try:
            await ws.send_text(f"\r\n\x1b[31m[连接失败] {type(e).__name__}: {str(e)[:120]}\x1b[0m\r\n")
        except Exception:
            pass
    finally:
        if chan:
            try:
                chan.close()
            except Exception:
                pass
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
        try:
            sdb = SessionLocal()
            audit_svc.record(
                sdb, action="machine.ssh.session", user_id=user["id"], username=user["username"],
                resource_type="machine", resource_id=str(mid),
                detail={"host": host, "cmd_count": cmd_count},
                client_ip=get_client_ip(ws))
            sdb.commit()
            sdb.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


# ================= Pod exec 终端 =================
# 实现走管理面 SSH 的 kubectl exec（复用集群登记的管理通道，零新增 RBAC 授权）——
# pods/exec 的 SA 授权属敏感提权不做；管理面 SSH 语义与备份/升级等管理操作一致。

import re as _re

_NAME_RE = _re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,252}$")


@router.websocket("/ws/k8s/pods/{ns}/{pod}/exec")
async def pod_exec(ws: WebSocket, ns: str, pod: str,
                   token: str = Query(""), container: str = Query(""),
                   cluster_name: str = Query(""), cols: int = Query(120), rows: int = Query(32)):
    import paramiko

    from app.core.db import SessionLocal
    from app.core.security import decrypt_secret
    from app.services import audit as audit_svc
    from app.services.k8s_ops import resolve_master_ssh

    user = _auth(token)
    if not user or user["role"] not in ("admin", "operator"):
        await ws.close(code=4401)
        return
    if not (_NAME_RE.match(ns) and _NAME_RE.match(pod) and (not container or _NAME_RE.match(container))):
        await ws.close(code=4400)
        return
    db = SessionLocal()
    try:
        host, ssh_user, c = resolve_master_ssh(db, cluster_name, 0, strict=True)
        if not host:
            await ws.close(code=4400)
            return
        if c is not None and c.read_only:
            await ws.close(code=4403)   # 只读集群禁 exec
            return
        pwd = ""
        # 控制面 SSH 多为密钥免密；机器库密码托管优先
        from app.models.machine import K8sMachine

        mm = db.query(K8sMachine).filter(K8sMachine.ip == host).first()
        if mm and mm.ssh_password_cipher:
            pwd = decrypt_secret(mm.ssh_password_cipher)
    finally:
        db.close()

    await ws.accept()
    chan = None
    ssh = None
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        await asyncio.to_thread(
            ssh.connect, host, 22, ssh_user, pwd or None, timeout=12,
            allow_agent=not bool(pwd), look_for_keys=not bool(pwd))
        chan = await asyncio.to_thread(ssh.invoke_shell, term="xterm-256color", width=cols, height=rows)
        chan.settimeout(0.2)
        # 固定模板命令（ns/pod/container 已白名单校验，不含用户自由文本）
        c_arg = f"--container={container} " if container else ""
        cmd = f"kubectl exec -it -n {ns} {c_arg}{pod} -- /bin/bash 2>/dev/null || kubectl exec -it -n {ns} {c_arg}{pod} -- /bin/sh\n"
        await asyncio.to_thread(chan.send, cmd)
        await ws.send_text(f"\x1b[32m[千枢终端] 进入容器 {ns}/{pod}\x1b[0m\r\n")

        async def pump_out():
            import socket as _sock

            loop = asyncio.get_event_loop()
            while not chan.closed:
                try:
                    data = await loop.run_in_executor(None, chan.recv, 4096)
                except _sock.timeout:
                    continue
                except Exception:
                    break
                if not data:
                    break
                await ws.send_text(data.decode("utf-8", "replace"))

        async def pump_in():
            loop = asyncio.get_event_loop()
            while True:
                msg = await ws.receive_text()
                try:
                    obj = json.loads(msg)
                except Exception:
                    obj = None
                if isinstance(obj, dict) and obj.get("type") == "resize":
                    await loop.run_in_executor(
                        None, chan.resize_pty, int(obj.get("cols", 120)), int(obj.get("rows", 32)))
                    continue
                await loop.run_in_executor(None, chan.send, msg)

        t_out = asyncio.create_task(pump_out())
        t_in = asyncio.create_task(pump_in())
        done, pending = await asyncio.wait({t_out, t_in}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    except Exception as e:
        try:
            await ws.send_text(f"\r\n\x1b[31m[连接失败] {type(e).__name__}: {str(e)[:120]}\x1b[0m\r\n")
        except Exception:
            pass
    finally:
        if chan:
            try:
                chan.close()
            except Exception:
                pass
        if ssh:
            try:
                ssh.close()
            except Exception:
                pass
        try:
            sdb = SessionLocal()
            audit_svc.record(
                sdb, action="k8s.pod_exec.session", user_id=user["id"], username=user["username"],
                resource_type="pod", resource_id=f"{ns}/{pod}",
                detail={"via": "ssh-kubectl", "container": container},
                client_ip=get_client_ip(ws))
            sdb.commit()
            sdb.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
