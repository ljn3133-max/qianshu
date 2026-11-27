"""K8s API 只读客户端（httpx 直调，无额外依赖）。

只读面：资源清单 / Pod 日志 / 连接测试。写路径（exec/delete）明确不做。
鉴权：ServiceAccount Bearer Token；自签集群 verify_tls=False。
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("qianshu.k8s")


class K8sError(RuntimeError):
    pass


def _client(api_url: str, token: str, verify_tls: bool = True) -> httpx.Client:
    return httpx.Client(
        base_url=api_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"},
        verify=verify_tls, timeout=20)


def ping(api_url: str, token: str, verify_tls: bool = True) -> dict:
    """连接测试：/version + /api 资源面可达性。"""
    try:
        with _client(api_url, token, verify_tls) as c:
            v = c.get("/version")
            ok = v.status_code == 200
            ver = (v.json() or {}).get("gitVersion", "") if ok else ""
            return {"ok": ok, "detail": f"K8s {ver}" if ok else f"HTTP {v.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:100]}"}


# kind → API 路径（单资源读取用；yaml 查看/编辑走 token 通道，不依赖 SSH——
# 未登记管理面的集群经 SSH 只会 Permission denied，不能将该响应当作 yaml）
RESOURCE_PATHS = {
    "Pod": ("/api/v1", True), "Service": ("/api/v1", True),
    "ConfigMap": ("/api/v1", True), "Secret": ("/api/v1", True),
    "Namespace": ("/api/v1", False), "Node": ("/api/v1", False),
    "PersistentVolume": ("/api/v1", False),
    "PersistentVolumeClaim": ("/api/v1", True),
    "Deployment": ("/apis/apps/v1", True), "StatefulSet": ("/apis/apps/v1", True),
    "DaemonSet": ("/apis/apps/v1", True), "ReplicaSet": ("/apis/apps/v1", True),
    "Ingress": ("/apis/networking.k8s.io/v1", True),
    "CronJob": ("/apis/batch/v1", True), "Job": ("/apis/batch/v1", True),
}


def resource_path(kind: str, name: str, namespace: str = "") -> str:
    """单资源 REST 路径（kind 白名单映射，无注入面）。"""
    if kind not in RESOURCE_PATHS:
        raise K8sError(f"不支持的资源类型: {kind}")
    base, namespaced = RESOURCE_PATHS[kind]
    seg = kind.lower() + "s"
    if kind == "Ingress":
        seg = "ingresses"
    if namespaced:
        return f"{base}/namespaces/{namespace or 'default'}/{seg}/{name}"
    return f"{base}/{seg}/{name}"


def get_resource_yaml(api_url: str, token: str, kind: str, name: str,
                      namespace: str = "", verify_tls: bool = True) -> dict:
    """yaml 查看（token 通道，只读）：GET 单资源 → 标准 yaml 文本。"""
    import yaml as _yaml

    with _client(api_url, token, verify_tls) as c:
        r = c.get(resource_path(kind, name, namespace))
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:150]}"}
        doc = r.json() or {}
    doc.pop("managedFields", None)   # 服务端字段噪音大，去掉
    return {"ok": True, "yaml": _yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)}


def list_events(api_url: str, token: str, namespace: str = "",
                verify_tls: bool = True, limit: int = 200) -> list:
    """集群事件（token 通道，只读）：结构化字段与 kubectl 对齐。"""
    path = f"/api/v1/namespaces/{namespace}/events" if namespace else "/api/v1/events"
    with _client(api_url, token, verify_tls) as c:
        r = c.get(path, params={"limit": limit})
        if r.status_code != 200:
            raise K8sError(f"事件拉取失败 HTTP {r.status_code}: {r.text[:120]}")
        items = (r.json() or {}).get("items") or []
    out = []
    for it in items:
        obj = it.get("involvedObject") or {}
        out.append({
            "type": it.get("type", ""), "reason": it.get("reason", ""),
            "namespace": obj.get("namespace", "") or it.get("metadata", {}).get("namespace", ""),
            "object_kind": obj.get("kind", ""), "object_name": obj.get("name", ""),
            "message": (it.get("message") or "")[:200],
            "count": it.get("count", 1),
            "last_time": it.get("lastTimestamp", "") or it.get("eventTime", ""),
        })
    return out


def list_resources(api_url: str, token: str, kind: str, namespaces: list,
                   verify_tls: bool = True, limit: int = 500) -> list:
    """资源清单（Pod/Service/Ingress/Deployment/Node）。返回扁平化 dict 列表。"""
    paths = {
        "Pod": "/api/v1/pods",
        "Service": "/api/v1/services",
        "Ingress": "/apis/networking.k8s.io/v1/ingresses",
        "Deployment": "/apis/apps/v1/deployments",
        "Node": "/api/v1/nodes",
        "Namespace": "/api/v1/namespaces",
        "PersistentVolumeClaim": "/api/v1/persistentvolumeclaims",
        "ConfigMap": "/api/v1/configmaps",
        "Secret": "/api/v1/secrets",
    }
    path = paths.get(kind)
    if not path:
        raise K8sError(f"不支持的资源类型: {kind}")
    with _client(api_url, token, verify_tls) as c:
        r = c.get(path, params={"limit": limit})
        if r.status_code != 200:
            raise K8sError(f"{kind} 拉取失败 HTTP {r.status_code}: {r.text[:120]}")
        items = (r.json() or {}).get("items") or []
    out = []
    ns_filter = set(namespaces or [])
    for it in items:
        meta = it.get("metadata") or {}
        ns = meta.get("namespace") or ""
        if ns_filter and kind != "Node" and ns not in ns_filter:
            continue
        status = it.get("status") or {}
        row = {
            "kind": kind, "namespace": ns, "name": meta.get("name", ""),
            "labels": meta.get("labels") or {},
            "created": meta.get("creationTimestamp", ""),
        }
        if kind == "Pod":
            container_statuses = status.get("containerStatuses") or []
            row["status"] = status.get("phase", "")
            row["node"] = (it.get("spec") or {}).get("nodeName", "")
            row["pod_ip"] = status.get("podIP", "")
            row["restarts"] = sum(cs.get("restartCount", 0) for cs in container_statuses)
            row["ready"] = all(cs.get("ready") for cs in container_statuses) if container_statuses else False
            spec_containers = (it.get("spec") or {}).get("containers") or []
            res_map = {c.get("name"): (c.get("resources") or {}) for c in spec_containers}
            row["containers"] = [{
                "name": cs.get("name"), "ready": cs.get("ready"),
                "restarts": cs.get("restartCount", 0),
                "state": list((cs.get("state") or {}).keys()),
                "last_reason": ((cs.get("lastState") or {}).get("terminated") or {}).get("reason", ""),
                "exit_code": ((cs.get("lastState") or {}).get("terminated") or {}).get("exitCode"),
                "resources": res_map.get(cs.get("name"), {}),   # 诊断要素：资源限额
            } for cs in container_statuses]
            row["raw"] = {"spec": {"containers": spec_containers},
                          "events_hint": status.get("reason", "")}
        elif kind == "Service":
            row["status"] = (it.get("spec") or {}).get("type", "")
            row["cluster_ip"] = (it.get("spec") or {}).get("clusterIP", "")
            row["ports"] = (it.get("spec") or {}).get("ports") or []
            row["selector"] = (it.get("spec") or {}).get("selector") or {}   # 关联：匹配 Pod
        elif kind == "Ingress":
            rules = (it.get("spec") or {}).get("rules") or []
            row["hosts"] = [r.get("host", "") for r in rules]
            row["status"] = "Ready" if status.get("loadBalancer") is not None else "Pending"
            # 关联：Ingress → 后端 Service（rules.http.paths[].backend.service.name）
            backends = set()
            for r in rules:
                for path in ((r.get("http") or {}).get("paths") or []):
                    svc = ((path.get("backend") or {}).get("service") or {}).get("name")
                    if svc:
                        backends.add(svc)
            row["backends"] = sorted(backends)
        elif kind == "Deployment":
            row["status"] = f"{status.get('readyReplicas', 0)}/{status.get('replicas', 0)} ready"
            row["ready"] = status.get("readyReplicas", 0) == status.get("replicas", 0)
        elif kind == "Node":
            row["status"] = "Ready" if any(
                cond.get("type") == "Ready" and cond.get("status") == "True"
                for cond in status.get("conditions") or []) else "NotReady"
            row["kubelet"] = status.get("nodeInfo", {}).get("kubeletVersion", "")
            # InternalIP：机器管理按 IP 匹配节点水位的对齐键。
            # NodeAddress 的 IP 字段名是 address（真集群实测 {type, address}），不能按 ip 读取。
            for addr in status.get("addresses") or []:
                if addr.get("type") == "InternalIP" and addr.get("address"):
                    row["internal_ip"] = addr["address"]
                    break
        out.append(row)
    return out


def api_versions(api_url: str, token: str, verify_tls: bool = True) -> list:
    """集群 API 版本清单（core v1 + 各 group versions）——升级预检废弃 API 扫描用。"""
    out = []
    with _client(api_url, token, verify_tls) as c:
        if c.get("/api/v1").status_code == 200:
            out.append("v1")
        r = c.get("/apis")
        if r.status_code == 200:
            for g in (r.json() or {}).get("groups") or []:
                for v in g.get("versions") or []:
                    if v.get("groupVersion"):
                        out.append(v["groupVersion"])
    return out


def list_names(api_url: str, token: str, path: str, verify_tls: bool = True,
               limit: int = 100) -> list:
    """通用资源实例名清单（path 为 REST 路径，代码模板生成不进用户输入）。"""
    with _client(api_url, token, verify_tls) as c:
        r = c.get(path, params={"limit": limit})
        if r.status_code != 200:
            return []
        items = (r.json() or {}).get("items") or []
    return [f"{(it.get('metadata') or {}).get('namespace', '')}/{(it.get('metadata') or {}).get('name', '')}"
            for it in items[:limit]]


def pod_logs(api_url: str, token: str, namespace: str, pod: str, container: str = "",
             tail_lines: int = 200, since_seconds: int = 0, verify_tls: bool = True) -> list:
    """Pod 日志（只读）。返回 [{ts, stream, line}]——stdout/stderr 合并按时间。"""
    params = {"tailLines": tail_lines, "timestamps": "true"}
    if container:
        params["container"] = container
    if since_seconds:
        params["sinceSeconds"] = since_seconds
    with _client(api_url, token, verify_tls) as c:
        r = c.get(f"/api/v1/namespaces/{namespace}/pods/{pod}/log", params=params)
        if r.status_code != 200:
            raise K8sError(f"日志拉取失败 HTTP {r.status_code}: {r.text[:120]}")
        text = r.text or ""
    out = []
    for ln in text.splitlines()[:tail_lines]:
        if " " not in ln:
            continue
        ts_s, _, body = ln.partition(" ")
        out.append({"ts": ts_s, "stream": "stdout", "line": body[:2000]})
    return out


def pod_events(api_url: str, token: str, namespace: str, pod: str,
               verify_tls: bool = True) -> list:
    """Pod 事件（失败分析的证据面）。"""
    with _client(api_url, token, verify_tls) as c:
        r = c.get("/api/v1/events",
                  params={"fieldSelector": f"involvedObject.name={pod}", "limit": 50})
        if r.status_code != 200:
            return []
        items = (r.json() or {}).get("items") or []
    return [{"reason": it.get("reason", ""), "message": (it.get("message") or "")[:200],
             "count": it.get("count", 1), "type": it.get("type", "")} for it in items]
