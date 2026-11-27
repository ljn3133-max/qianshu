"""通用平台接口通道：智能体对千枢平台自身做管理操作的唯一通道。

供两处复用（避免两套口径）：
  - change_tools.platform_api_call   写通道（变更工单 → 确认门 → 本模块直调）
  - tools_read.platform_api_query    读通道（只读工具，任意平台 GET）

安全边界（代码级保障）：
  1. 只允许调用**本进程 FastAPI 已注册**（openapi）的 /api/* 路径——没有任意
     URL/SQL/shell 通道；LLM 产出只作为参数（method/path/body），不执行原文。
  2. 黑名单恒拒（见 _BLACKLIST）：wipe/升级/删除类/集群管理/认证密码凭证类/
     平台外资源操作（ECS 开关机）/对外发消息/变更确认门自批/daemon 专属通道。
  3. 权限随会话用户：按目标端点 require_roles 语义校验 + 角色矩阵
     （admin 全量 / operator 限非用户·权限域 / viewer 只读），越权 403。
  4. 执行=直调端点函数（复用各 API 的业务校验与审计；内部手工鉴权仍生效），
     不走 HTTP 环路、不伪造登录态。
"""
from __future__ import annotations

import inspect
import json
import re
from types import SimpleNamespace
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

from fastapi import Request
from fastapi import params as fparams
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models.user import User


class PlatformApiError(ValueError):
    """平台接口调用被拒（未注册路径/黑名单/越权/参数无法解析）。面向用户可展示。"""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


# ---------------- 黑名单（恒拒，先于一切权限判断） ----------------
# (method 集合或 None=全部, 路径正则, 拒绝理由——每条都对应安全红线)
_BLACKLIST = [
    (None, r"(?i)wipe", "wipe/清空类端点（危险操作永远拒绝执行）"),
    (None, r"(?i)upgrade", "升级类端点（恒拒）"),
    ({"DELETE"}, r"", "删除类操作永远拒绝执行"),
    (None, r"^/api/k8mgmt/", "K8s 集群侧管理/高危动作（create/wipe/升级/驱逐/Secret 揭示等）——"
                            "平台外资源变更恒拒"),
    (None, r"^/api/k8s/clusters", "K8s 集群 create/delete/变更（指定黑名单）"),
    (None, r"(?i)/batch", "批量类端点（batch 删除/批量变更恒拒）"),
    (None, r"^/api/auth(?:/|$)", "认证/密码/OTP/权限策略端点——须走专用工具"
                                "（reset_user_password/save_permission_policy）+ 二次确认"),
    (None, r"(?i)credential", "凭证录入/揭示端点——须走云账号工具 +「我已知晓」警示审计"),
    (None, r"^/api/clouds/accounts", "云账号 AK/SK 录入——须走云账号工具 +「我已知晓」警示审计"),
    (None, r"^/api/assets/[^/]+/actions", "平台外资源操作（ECS 开关机/重启）——恒拒"),
    (None, r"^/api/settings/seed-demo", "demo 数据播种/清除端点（wipe 类）"),
    (None, r"^/api/bootstrap/(?:apply-config|complete|probe)", "安装向导一次性通道，不入通用平台通道"),
    (None, r"^/api/agent/lifecycle", "平台生命周期通道（daemon 专属）"),
    (None, r"^/api/agent/changes/[^/]+/(?:confirm|rollback|dismiss)",
     "变更确认门端点——智能体不得自批/自回滚，须用户在确认卡操作"),
    (None, r"^/api/mcp$", "MCP 专用通道，不走通用平台通道"),
    (None, r"^/api/events/webhook", "外部事件回调通道，不走通用平台通道"),
    (None, r"^/api/alerts/channels/[^/]+/test", "对外发消息类操作只能由用户主动触发（红线）"),
    (None, r"^/api/integrations/check-channel/", "对外发消息类操作只能由用户主动触发（红线）"),
]

# 用户/权限域（operator 写操作禁区）：用户管理、认证、权限策略
_USER_PERM_DOMAIN = re.compile(r"^/api/(?:users|auth|permissions)(?:/|$)")

_QUERY_PARAM_CLASSES = (fparams.Param,)   # Query/Path/Header/Cookie 的公共基类


# ---------------- 路由解析（只认 openapi 注册路径） ----------------

def _normalize_path(path: str) -> str:
    path = (path or "").strip()
    if not path:
        raise PlatformApiError("path 不能为空", 400)
    if not path.startswith("/"):
        path = "/" + path
    if not path.startswith("/api/"):
        path = "/api" + path
    return path


def _split_query(path: str) -> tuple:
    parts = urlsplit(path)
    query = dict(parse_qsl(parts.query))
    return parts.path, query


def _match_route(method: str, path: str):
    from app.main import app

    method = (method or "").upper()
    candidates = []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        if method not in (r.methods or set()):
            continue
        pattern = re.sub(r"\{(\w+)(?::[^}]+)?\}", r"(?P<\1>[^/]+)", r.path)
        m = re.fullmatch(pattern, path)
        if m:
            candidates.append((r, m.groupdict()))
    if not candidates:
        raise PlatformApiError(f"路径未注册（只允许平台 openapi 注册路径）：{method} {path}", 404)
    # 模板优先级：静态段多的排前（/api/workflows/runs 优先于 /api/workflows/{key}）
    candidates.sort(key=lambda c: -c[0].path.count("/"))
    return candidates[0]


# ---------------- 黑名单 + 权限（require_roles 语义 + 角色矩阵） ----------------

def guard(method: str, path: str, user=None) -> None:
    """黑名单恒拒 + 角色校验。不过则抛 PlatformApiError（status=403/400/404）。"""
    method = (method or "").upper()
    path, _ = _split_query(_normalize_path(path))
    for methods, pattern, reason in _BLACKLIST:
        if methods is not None and method not in methods:
            continue
        if re.search(pattern, path):
            raise PlatformApiError(f"拒绝执行 {method} {path}——{reason}")
    route, _ = _match_route(method, path)
    role = getattr(user, "role", None) or "viewer"   # 无用户上下文=最低只读档
    roles = _route_roles(route)
    if roles and role not in roles:
        raise PlatformApiError(
            f"越权：{method} {path} 需要角色 {'/'.join(sorted(roles))}，当前 {role}")
    if method == "GET":
        return    # viewer 只读：GET 随目标 require_roles 放行
    if role == "viewer":
        raise PlatformApiError(f"越权：viewer 只读，禁止写操作 {method} {path}")
    if role == "operator" and _USER_PERM_DOMAIN.search(path):
        raise PlatformApiError(f"越权：operator 限非用户/权限域，禁止 {method} {path}")
    if role != "admin" and not roles:
        raise PlatformApiError(f"越权：{method} {path} 需要管理员")


def _route_roles(route: APIRoute) -> set:
    """从 require_roles(...) 闭包解析目标端点的角色语义（require_roles 依赖的唯一来源）。"""
    roles: set = set()

    def walk(dep) -> None:
        for d in getattr(dep, "dependencies", None) or []:
            fn = getattr(d, "call", None)
            if fn is not None and "require_roles" in (getattr(fn, "__qualname__", "") or ""):
                for cell in (fn.__closure__ or ()):
                    content = cell.cell_contents
                    if isinstance(content, tuple):
                        roles.update(content)
            walk(d)

    walk(route.dependant)
    return roles


# ---------------- 直调端点函数（复用既有业务校验/审计） ----------------

def _request_stub() -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host="copilot"))


def _jsonable(value):
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _resolve_kwargs(route: APIRoute, path_params: dict, body: Optional[dict],
                    query: dict, db: Session, user) -> dict:
    """把端点函数签名解析成可直调的实参：path/query/body/db/user/request 各归其位。"""
    import typing

    kwargs: dict = {}
    sig = inspect.signature(route.endpoint)
    # from __future__ import annotations 让注解变字符串——get_type_hints 还原真实类型
    try:
        hints = typing.get_type_hints(route.endpoint)
    except Exception:   # noqa: BLE001  还原失败按原始注解走（多半是无注解老函数）
        hints = {}
    for name, param in sig.parameters.items():
        ann = hints.get(name, param.annotation)
        default = param.default
        # 注入类：db / request / user
        if ann is Session or name == "db":
            if db is None:
                raise PlatformApiError("内部错误：缺少数据库会话", 500)
            kwargs[name] = db
            continue
        if (ann is Request) or name == "request":
            kwargs[name] = _request_stub()
            continue
        origin = typing.get_origin(ann)
        if ann is User or (isinstance(ann, type) and issubclass(ann, User)) \
                or name in ("user", "_user"):
            kwargs[name] = user if user is not None else SimpleNamespace(
                id=0, username="copilot", role="viewer", is_active=True)
            continue
        # 路径参数
        if name in path_params:
            raw = path_params[name]
            if ann is int:
                try:
                    kwargs[name] = int(raw)
                except (TypeError, ValueError):
                    raise PlatformApiError(f"路径参数 {name} 不是整数: {raw}", 400)
            else:
                kwargs[name] = raw
            continue
        # body：pydantic 模型
        model = None
        if isinstance(ann, type) and issubclass(ann, BaseModel):
            model = ann
        elif origin is not None:
            for arg in typing.get_args(ann):
                if isinstance(arg, type) and issubclass(arg, BaseModel):
                    model = arg
                    break
        if model is not None:
            try:
                kwargs[name] = model(**(body or {}))
            except Exception as e:   # noqa: BLE001  参数校验不过=400
                raise PlatformApiError(f"body 参数不合法：{type(e).__name__}: {str(e)[:200]}", 400)
            continue
        # 查询参数：优先用调用方 query，否则用声明默认值（Query(...) 解包）
        if name in query:
            kwargs[name] = query[name]
            continue
        if isinstance(default, _QUERY_PARAM_CLASSES):
            if default.default is ... or default.default is fparams.Required:
                raise PlatformApiError(f"端点必填参数 {name} 未提供", 400)
            kwargs[name] = default.default
            continue
        if isinstance(default, fparams.Depends):
            raise PlatformApiError(f"端点依赖 {name} 无法直调（不支持该端点）", 400)
        if default is inspect.Parameter.empty:
            raise PlatformApiError(f"端点必填参数 {name} 无法解析（不支持该端点）", 400)
    return kwargs


def call(method: str, path: str, body: Optional[dict] = None,
         query: Optional[dict] = None, db: Session = None, user=None) -> dict:
    """解析 → 黑名单/权限 → 直调端点函数 → 结构化返回。全部异常转 PlatformApiError。"""
    method = (method or "").upper()
    full_path, embedded_query = _split_query(_normalize_path(path))
    merged_query = dict(embedded_query)
    merged_query.update({k: v for k, v in (query or {}).items()})
    guard(method, full_path, user)
    route, path_params = _match_route(method, full_path)
    kwargs = _resolve_kwargs(route, path_params, body, merged_query, db, user)
    try:
        result = route.endpoint(**kwargs)
    except PlatformApiError:
        raise
    except Exception as e:   # noqa: BLE001  端点自有校验（400/403/409…）如实上抛/转述
        status = int(getattr(e, "status_code", 0) or 0)
        detail = getattr(e, "detail", None)
        msg = f"{type(e).__name__}: {str(detail or e)[:200]}"
        if status:
            raise PlatformApiError(f"目标端点返回 {status}：{msg}", status)
        raise PlatformApiError(f"目标端点执行失败：{msg}", 500)
    return {"ok": True, "method": method, "path": full_path, "data": _jsonable(result)}


def call_get(path: str, query: Optional[dict] = None, db: Session = None, user=None) -> dict:
    """只读查询（platform_api_query 用）。"""
    return call("GET", path, body=None, query=query, db=db, user=user)


def try_get(path: str, query: Optional[dict] = None, db: Session = None, user=None):
    """尽力 GET（快照用）：失败返回 None，绝不抛。"""
    try:
        out = call_get(path, query=query, db=db, user=user)
        raw = json.dumps(out.get("data"), ensure_ascii=False, default=str)
        return raw[:4000]
    except Exception:   # noqa: BLE001
        return None
