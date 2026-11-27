#!/usr/bin/env python3
"""Isolated fake-model + mock-site/API + real-Chrome walkthrough tests.

No platform service, account, real cloud resource or external notification is used.
Run: python3 -m unittest -v tests/quality/model_walkthrough_test.py
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.quality import model_walkthrough as mw


PAGE = """<!doctype html><html><head><title>Mock catalog</title></head><body>
<h1>Delivery catalog</h1><label for="query">Filter query</label>
<input id="query" data-testid="query" type="text">
<button id="apply">Apply filter</button>
__ALPHA__
<button id="beta" data-testid="show-beta">Show Beta</button>
<div id="status">No filter</div><div id="result">Waiting</div>
<script>
document.getElementById('apply').onclick = () => {
  document.getElementById('status').textContent = 'Filter ' + document.getElementById('query').value;
};
function show(name) {
  document.getElementById('result').textContent = name + ': __PAGE_COUNT__ items';
  if (__POST_ON_CLICK__) {
    fetch('/api/write', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:name})}).catch(() => {});
  }
}
__LISTENERS__
</script></body></html>"""


class MockState:
    def __init__(self):
        self.target = "Alpha"
        self.query = "Alpha"
        self.count = 3
        self.page_count = 3
        self.hide_alpha = False
        self.post_on_click = False
        self.skip_api_assert = False
        self.model_calls = 0
        self.api_gets = 0
        self.raw_log_gets = 0
        self.browser_posts = []
        self.model_requests = []


class FakeModel:
    """Content-dependent fake: it must inspect tool results to know what to do."""
    def decide(self, messages):
        calls = [json.loads(item["content"]) for item in messages if item.get("role") == "assistant"]
        results = []
        for item in messages:
            if item.get("role") != "user":
                continue
            try:
                wrapped = json.loads(item.get("content") or "")
            except ValueError:
                continue
            if "tool_result" in wrapped:
                results.append(wrapped["tool_result"])
        if not calls:
            return {"tool": "platform_api_get", "params": {"path": "/api/task"}}
        api_result = next(result for result in results if "api_evidence_id" in result)
        data = api_result["json"]
        last = calls[-1]["tool"]
        if last == "platform_api_get":
            # Route comes from API evidence, not from a preprogrammed walkthrough.
            return {"tool": "browser_navigate", "params": {"path": data["route"]}}
        if last == "browser_navigate":
            return {"tool": "browser_observe", "params": {}}
        if last in ("browser_observe", "browser_input"):
            input_count = sum(call["tool"] == "browser_input" for call in calls)
            latest = results[-1]["observation"]
            if not input_count:
                control = next(item for item in latest["controls"]
                               if item["role"] == "textbox" and item["name"] == "Filter query")
                return {"tool": "browser_input", "params": {"ref": control["ref"], "value": data["query"]}}
            # Target name and semantic ref come from API + current DOM observation.
            control = next(item for item in latest["controls"]
                           if item["name"] == "Show " + data["target"])
            return {"tool": "browser_click", "params": {"ref": control["ref"]}}
        if last == "browser_click":
            return {"tool": "assert", "params": {
                "source": "dom", "operator": "contains",
                "expected": data["target"] + ": " + str(data["count"]) + " items"}}
        if last == "assert":
            assertions = sum(call["tool"] == "assert" for call in calls)
            if assertions == 1 and not Handler.state.skip_api_assert:
                return {"tool": "assert", "params": {
                    "source": "api", "evidence_id": api_result["api_evidence_id"],
                    "path": "count", "operator": "equals", "expected": data["count"]}}
            return {"tool": "finish", "params": {
                "summary": "Verified " + data["target"] + " using API and DOM evidence"}}
        if last == "finish":
            # A safety rejection must never turn into a different/write tool.
            return {"tool": "finish", "params": {"summary": "cannot bypass rejection"}}
        raise AssertionError("unexpected fake-model state: " + last)


class Handler(BaseHTTPRequestHandler):
    state = None
    fake = FakeModel()

    def log_message(self, _format, *_args):
        return

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/task":
            self.state.api_gets += 1
            self.send_json({"route": "/catalog", "target": self.state.target,
                            "query": self.state.query, "count": self.state.count})
            return
        if self.path.startswith("/api/logs/search"):
            self.state.raw_log_gets += 1
            self.send_json({"logs": [{"message": "raw-customer-log-must-not-reach-model"}]})
            return
        if self.path == "/catalog":
            alpha = "" if self.state.hide_alpha else '<button id="alpha" data-testid="show-alpha">Show Alpha</button>'
            listeners = []
            if not self.state.hide_alpha:
                listeners.append("document.getElementById('alpha').onclick=()=>show('Alpha');")
            listeners.append("document.getElementById('beta').onclick=()=>show('Beta');")
            body = PAGE.replace("__ALPHA__", alpha).replace("__PAGE_COUNT__", str(self.state.page_count))
            body = body.replace("__POST_ON_CLICK__", "true" if self.state.post_on_click else "false")
            body = body.replace("__LISTENERS__", "\n".join(listeners)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/":
            body = b"<!doctype html><title>Mock start</title><h1>Start</h1>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.path == "/v1/chat/completions":
            if self.headers.get("Authorization") != "Bearer isolated-test-key":
                self.send_json({"error": "bad test authorization"}, 401)
                return
            request = json.loads(raw or b"{}")
            self.state.model_calls += 1
            self.state.model_requests.append(request)
            choice = self.fake.decide(request["messages"])
            self.send_json({"choices": [{"message": {"role": "assistant",
                                                     "content": json.dumps(choice, ensure_ascii=False)}}],
                            "usage": {"total_tokens": 1}})
            return
        if self.path == "/api/write":
            self.state.browser_posts.append(json.loads(raw or b"{}"))
            self.send_json({"ok": True})
            return
        self.send_json({"error": "not found"}, 404)


class ModelWalkthroughEndToEnd(unittest.TestCase):
    def setUp(self):
        self.state = MockState()
        Handler.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = "http://127.0.0.1:" + str(self.server.server_address[1])
        self.temp = tempfile.TemporaryDirectory(prefix="qianshu-model-walkthrough-test-")
        self.root = pathlib.Path(self.temp.name)
        self.old_key = os.environ.get("QIANSHU_WALKTHROUGH_API_KEY")
        self.old_platform_token = os.environ.pop("QIANSHU_WALKTHROUGH_PLATFORM_TOKEN", None)
        os.environ["QIANSHU_WALKTHROUGH_API_KEY"] = "isolated-test-key"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()
        if self.old_key is None:
            os.environ.pop("QIANSHU_WALKTHROUGH_API_KEY", None)
        else:
            os.environ["QIANSHU_WALKTHROUGH_API_KEY"] = self.old_key
        if self.old_platform_token is not None:
            os.environ["QIANSHU_WALKTHROUGH_PLATFORM_TOKEN"] = self.old_platform_token

    def config(self):
        return mw.WalkthroughConfig(self.origin, self.origin, "/", ["/", "/catalog"], ["/api/task"])

    def llm(self):
        return mw.OpenAICompatClient(self.origin + "/v1", "content-selecting-fake",
                                     os.environ["QIANSHU_WALKTHROUGH_API_KEY"])

    def run_live(self, name):
        path = self.root / name
        artifact = mw.run_live(self.config(), "Inspect /api/task, follow its route, filter and verify its target",
                               self.llm(), str(path))
        return path, artifact

    @staticmethod
    def action(artifact, kind):
        return next(item["action"] for item in artifact["evidence"]
                    if item["kind"] == "action" and item["action"]["kind"] == kind)

    def test_content_choices_fresh_replay_and_negative_mutations(self):
        live_path, live = self.run_live("alpha.json")
        self.assertEqual("live_passed", live["status"])
        self.assertEqual(8, live["model"]["calls"])
        self.assertGreaterEqual(live["requirements"]["api"], 1)
        self.assertGreaterEqual(live["requirements"]["observe"], 1)
        self.assertGreaterEqual(live["requirements"]["action"], 3)
        self.assertEqual(2, live["requirements"]["assertion"])
        self.assertEqual(1, live["requirements"]["api_assertion"])
        self.assertEqual(1, live["requirements"]["dom_assertion"])
        self.assertEqual("Alpha", self.action(live, "input")["value"])
        self.assertEqual("Show Alpha", self.action(live, "click")["locator"]["name"])
        evidence_ids = {item["id"] for item in live["evidence"]}
        for step in live["steps"]:
            self.assertTrue(set(step["evidence_refs"]).issubset(evidence_ids), step)
        self.assertNotIn("isolated-test-key", live_path.read_text(encoding="utf-8"))
        self.assertEqual(0o600, live_path.stat().st_mode & 0o777)

        # Independent replay uses another profile, performs GETs again, and never calls the model.
        model_calls = self.state.model_calls
        api_gets = self.state.api_gets
        replay_path = self.root / "alpha.replay.json"
        replay = mw.run_replay(str(live_path), str(replay_path))
        self.assertEqual("passed", replay["status"])
        self.assertNotEqual(live["browser_session_id"], replay["browser_session_id"])
        self.assertEqual(model_calls, self.state.model_calls)
        self.assertGreater(self.state.api_gets, api_gets)
        self.assertEqual(0, replay["model"]["calls"])
        self.assertTrue(any(item["kind"] == "api" for item in replay["evidence"]))

        # Same runner, different API content: the fake model must choose Beta and 5,
        # demonstrating that the normal Alpha path is not hard-coded in the executor.
        self.state.target = self.state.query = "Beta"
        self.state.count = self.state.page_count = 5
        _beta_path, beta = self.run_live("beta.json")
        self.assertEqual("Show Beta", self.action(beta, "click")["locator"]["name"])
        self.assertEqual("Beta", self.action(beta, "input")["value"])
        beta_assertions = [item for item in beta["evidence"] if item["kind"] == "assertion"]
        self.assertTrue(any(item["actual"] == 5 for item in beta_assertions))

        # Re-fetch detects an API mutation even when the old DOM remains valid.
        self.state.target = self.state.query = "Alpha"
        self.state.page_count = 3
        self.state.count = 9
        wrong_api_path = self.root / "wrong-api.replay.json"
        with self.assertRaisesRegex(mw.ReplayError, "assertion"):
            mw.run_replay(str(live_path), str(wrong_api_path))
        self.assertEqual("failed", mw.read_json(wrong_api_path)["status"])

        # Removing the semantic control also fails; replay cannot silently skip it.
        self.state.count = 3
        self.state.hide_alpha = True
        removed_path = self.root / "removed-control.replay.json"
        with self.assertRaisesRegex(mw.ReplayError, "control|missing|ambiguous"):
            mw.run_replay(str(live_path), str(removed_path))
        self.assertEqual("failed", mw.read_json(removed_path)["status"])

        # Missing evidence fails closed before it can be treated as a valid replay.
        broken = copy.deepcopy(live)
        api_id = next(item["id"] for item in broken["evidence"] if item["kind"] == "api")
        broken["evidence"] = [item for item in broken["evidence"] if item["id"] != api_id]
        broken_path = self.root / "missing-evidence.json"
        mw.write_json(broken_path, broken)
        with self.assertRaisesRegex(mw.ReplayError, "missing evidence"):
            mw.run_replay(str(broken_path), str(self.root / "missing-evidence.replay.json"))

        # Removing the API assertion invalidates replay even if an unrelated DOM
        # assertion and the earlier API tool call remain present.
        dom_only = copy.deepcopy(live)
        api_assertion_id = next(item["id"] for item in dom_only["evidence"]
                                if item["kind"] == "assertion" and item["assertion"]["source"] == "api")
        dom_only["evidence"] = [item for item in dom_only["evidence"] if item["id"] != api_assertion_id]
        dom_only["steps"] = [step for step in dom_only["steps"]
                             if api_assertion_id not in step["evidence_refs"]]
        dom_only_path = self.root / "dom-only.json"
        mw.write_json(dom_only_path, dom_only)
        with self.assertRaisesRegex(mw.ReplayError, "API and DOM"):
            mw.run_replay(str(dom_only_path), str(self.root / "dom-only.replay.json"))

    def test_browser_non_get_is_blocked_and_fails_run(self):
        self.state.post_on_click = True
        artifact_path = self.root / "non-get.json"
        with self.assertRaises(mw.WalkthroughError):
            self.run_live("non-get.json")
        artifact = mw.read_json(artifact_path)
        self.assertEqual("failed", artifact["status"])
        self.assertEqual([], self.state.browser_posts, "CDP must block POST before the mock server receives it")
        blocked = artifact["safety"]["blocked_requests"]
        self.assertTrue(any(item["method"] == "POST" and item["reason"] == "non_get_blocked"
                            and item["path"] == "/api/write" for item in blocked), blocked)

    def test_api_assertion_is_mandatory_for_live_pass(self):
        self.state.skip_api_assert = True
        path = self.root / "missing-api-assertion.json"
        runner = mw.LiveWalkthrough(
            self.config(), "DOM assertion alone must not validate API content",
            self.llm(), str(path), max_rounds=9)
        with self.assertRaisesRegex(mw.WalkthroughError, "round limit"):
            runner.run()
        artifact = mw.read_json(path)
        self.assertEqual("failed", artifact["status"])
        finishes = [item for item in artifact["evidence"] if item["kind"] == "finish"]
        self.assertTrue(finishes)
        self.assertIn("api_assertion", finishes[-1]["missing"])

    def test_raw_log_get_is_rejected_and_response_fields_are_suppressed(self):
        config = mw.WalkthroughConfig(
            self.origin, self.origin, "/", ["/", "/catalog"],
            ["/api/task", "/api/logs/*", "/api/k8s/*", "/api/bootstrap/sls-catalog"])
        with self.assertRaisesRegex(mw.ToolError, "raw log"):
            mw.PlatformGetClient(config).get("/api/logs/search?query=customer-secret")
        self.assertEqual(0, self.state.raw_log_gets, "rejection must happen before HTTP dispatch")
        with self.assertRaisesRegex(mw.ToolError, "raw log"):
            config.api_path("/api/k8s/clusters/1/pods/pod-a/logs")
        # Catalog, discovery and aggregate status paths are not blanket-blocked.
        self.assertEqual("/api/logs/logstores?project=mock",
                         config.api_path("/api/logs/logstores?project=mock"))
        self.assertEqual("/api/bootstrap/sls-catalog",
                         config.api_path("/api/bootstrap/sls-catalog"))
        redacted = mw.bounded_json({
            "status": "ok", "sample_count": 3,
            "logs": [{"message": "raw-customer-log-must-not-reach-model"}],
            "samples": ["another-raw-sample"]})
        encoded = json.dumps(redacted)
        self.assertNotIn("raw-customer-log", encoded)
        self.assertNotIn("another-raw-sample", encoded)
        self.assertEqual(3, redacted["sample_count"])

    def test_origin_and_arbitrary_url_guards(self):
        with self.assertRaises(mw.ToolError):
            mw.WalkthroughConfig("http://example.com", self.origin, "/", ["/"], ["/api/task"])
        config = self.config()
        with self.assertRaises(mw.ToolError):
            config.route_path("http://example.com/escape")
        with self.assertRaises(mw.ToolError):
            config.api_path("/api/task?access_token=do-not-persist")
        with self.assertRaises(mw.ToolError):
            config.api_path("/api/not-allowed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
