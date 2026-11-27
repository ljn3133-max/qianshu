#!/usr/bin/env python3
"""契约级假 K8s API：最小端点集验证千枢全链（接入/同步/日志/失败分析）。

模拟：2 Pod（1 健康 + 1 OOMKilled CrashLoop）、Service/Ingress/Deployment/Node、
日志流、事件（OOMKilled/FailedScheduling）。纯标准库。
"""
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

NOW = int(time.time())
PODS = {
    "items": [
        {"metadata": {"name": "web-7d9f-abc", "namespace": "prod", "labels": {"app": "web"},
                      "creationTimestamp": "2026-10-20T08:00:00Z"},
         "spec": {"nodeName": "node-1", "containers": [
             {"name": "web", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                           "limits": {"cpu": "500m", "memory": "256Mi"}}}]},
         "status": {"phase": "Running", "containerStatuses": [
             {"name": "web", "ready": True, "restartCount": 0, "state": {"running": {}}}]}},
        {"metadata": {"name": "api-6c4b-xyz", "namespace": "prod", "labels": {"app": "api"},
                      "creationTimestamp": "2026-10-25T10:00:00Z"},
         "spec": {"nodeName": "node-1", "containers": [
             {"name": "api", "resources": {"requests": {"memory": "256Mi"},
                                           "limits": {"memory": "512Mi"}}}]},
         "status": {"phase": "Running", "containerStatuses": [
             {"name": "api", "ready": False, "restartCount": 7,
              "state": {"waiting": {"reason": "CrashLoopBackOff"}},
              "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}},
    ]
}
SERVICES = {"items": [
    {"metadata": {"name": "web-svc", "namespace": "prod", "labels": {"app": "web"}},
     "spec": {"type": "ClusterIP", "clusterIP": "10.0.0.10", "ports": [{"port": 80}]},
     "status": {}}]}
INGRESSES = {"items": [
    {"metadata": {"name": "web-ing", "namespace": "prod"},
     "spec": {"rules": [{"host": "www.example.com"}]}, "status": {"loadBalancer": {}}}]}
DEPLOYS = {"items": [
    {"metadata": {"name": "web", "namespace": "prod"}, "spec": {},
     "status": {"replicas": 2, "readyReplicas": 2}}]}
NODES = {"items": [
    {"metadata": {"name": "node-1"}, "spec": {},
     "status": {"conditions": [{"type": "Ready", "status": "True"}],
                "nodeInfo": {"kubeletVersion": "v1.29.4"}}}]}
def _log_text():
    """动态时间戳（产品增量采集按时间过滤——固定旧时间戳会被全过滤）"""
    import datetime as _dt
    base = _dt.datetime.utcnow() - _dt.timedelta(minutes=2)
    lines = []
    for i in range(3):
        t = (base + _dt.timedelta(seconds=20 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{t} INFO  api started ok")
    for i in range(2):
        t = (base + _dt.timedelta(seconds=20 * (3 + i))).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"{t} ERROR java.lang.OutOfMemoryError: Java heap space")
    t = (base + _dt.timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines.append(f"{t} FATAL process terminated (OOMKilled)")
    return "\n".join(lines)


LOG_TEXT = _log_text()
EVENTS = {"items": [
    {"reason": "OOMKilling", "message": "Container api exceeded memory limit (512Mi)", "count": 7, "type": "Warning"},
    {"reason": "BackOff", "message": "Back-off restarting failed container", "count": 5, "type": "Warning"}]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/version":
            return self._send({"gitVersion": "v1.29.4-fake"})
        if p == "/api/v1/pods":
            return self._send(PODS)
        if p == "/api/v1/services":
            return self._send(SERVICES)
        if p == "/apis/networking.k8s.io/v1/ingresses":
            return self._send(INGRESSES)
        if p == "/apis/apps/v1/deployments":
            return self._send(DEPLOYS)
        if p == "/api/v1/nodes":
            return self._send(NODES)
        if p == "/api/v1/events":
            return self._send(EVENTS)
        if "/log" in p:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(LOG_TEXT.encode())
            return
        return self._send({"error": "not found"}, 404)


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 9999), H).serve_forever()
