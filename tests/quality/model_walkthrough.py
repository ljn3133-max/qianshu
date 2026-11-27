#!/usr/bin/env python3
"""Opt-in, read-only LLM product walkthrough with independent Chrome replay.

This is a delivery-quality tool, not an end-user browser agent and not part of
run_all.py. The model chooses structured API/browser tools; this process owns
all HTTP, JavaScript and CDP execution. Browser POST/PUT/PATCH/DELETE/etc. are
blocked at the Fetch request stage, including requests initiated by page code.

Live (API key is read only from QIANSHU_WALKTHROUGH_API_KEY):
  python3 tests/quality/model_walkthrough.py run \
    --task 'Check bootstrap status and the corresponding local page' \
    --origin http://127.0.0.1:5273 --start /agent --route /agent \
    --api-origin http://127.0.0.1:8810 --allow-api /api/bootstrap/status \
    --base-url http://127.0.0.1:9000/v1 --model my-model \
    --artifact /tmp/qianshu-walkthrough.json

Replay (no model configuration or model call is used):
  python3 tests/quality/model_walkthrough.py replay \
    --artifact /tmp/qianshu-walkthrough.json \
    --output /tmp/qianshu-walkthrough.replay.json

Environment equivalents for live model configuration:
  QIANSHU_WALKTHROUGH_BASE_URL, QIANSHU_WALKTHROUGH_MODEL,
  QIANSHU_WALKTHROUGH_API_KEY, QIANSHU_WALKTHROUGH_VERIFY_SSL.
QIANSHU_WALKTHROUGH_PLATFORM_TOKEN optionally authorizes direct platform GETs.
Artifacts contain bounded/redacted evidence, never request headers or API keys.
Only allowlist redacted status, catalog and aggregate APIs: raw log query/sample GETs
are rejected, and raw log/sample response fields are suppressed before model context.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import socket
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import httpx
import websocket

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_run import chrome_popen  # noqa: E402

SCHEMA = "qianshu.model-walkthrough.v1"
REPLAY_SCHEMA = "qianshu.model-walkthrough-replay.v1"
MAX_ROUNDS = 16
MAX_API_CHARS = 12000
MAX_BODY_CHARS = 8000
MAX_CONTROLS = 40
MAX_EVENTS = 30
MAX_TASK_CHARS = 4000
SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|cookie|password|passwd|secret|token|api[_-]?key|"
    r"access[_-]?key|secret[_-]?key|credential|signature)")
SENSITIVE_QUERY = re.compile(
    r"(?i)(token|secret|password|passwd|credential|signature|api.?key|access.?key|authorization)")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
RAW_LOG_PATH = re.compile(
    r"(?i)(?:(?:log|sls|syslog)[^?]*(?:query|sample|search|tail|raw|records|export|download)|"
    r"(?:query|sample|search|tail|raw|records|export|download)[^?]*(?:log|sls|syslog)|"
    r"/(?:logs|slowlogs)/?$)")
SAFE_LOG_SUFFIX = re.compile(
    r"(?i)/(?:status|summary|catalog|discovery|coverage|metadata|fields|projects|logstores|"
    r"aggregate|aggregation|stats|sample-count|sample_count)/?$")
RAW_LOG_QUERY_KEYS = {"q", "query", "keyword", "search", "sample", "samples", "raw", "message"}
RAW_LOG_RESPONSE_FIELDS = {
    "logs", "rawlogs", "logentries", "samplelogs", "logsamples", "samples", "messages"}


class WalkthroughError(RuntimeError):
    pass


class ToolError(WalkthroughError):
    pass


class SafetyError(WalkthroughError):
    pass


class ReplayError(WalkthroughError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value, limit=1000):
    text = str(value or "").replace("\x00", "")
    text = re.sub(
        r"(?i)(authorization|api[_-]?key|access[_-]?key|secret|token|password)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>", text)
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+", "Bearer <redacted>", text)
    return text[:limit]


def safe_path(path):
    """Return a query-free path suitable for network/console evidence."""
    try:
        return clean_text(urlsplit(path).path, 500)
    except Exception:
        return "<invalid-path>"


def redact_json(value, depth=0):
    if depth > 6:
        return "<max-depth>"
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 80:
                result["_truncated"] = "additional object keys omitted"
                break
            name = clean_text(key, 160)
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            redacted = SENSITIVE_KEY.search(str(key)) or normalized_key in RAW_LOG_RESPONSE_FIELDS
            result[name] = "<redacted>" if redacted else redact_json(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [redact_json(item, depth + 1) for item in value[:50]]
        if len(value) > 50:
            result.append("<truncated: additional items omitted>")
        return result
    if isinstance(value, str):
        return clean_text(value, 2000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return clean_text(value, 500)


def bounded_json(value):
    result = redact_json(value)
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) <= MAX_API_CHARS:
        return result
    # A truncated representation is intentional: assertions against missing
    # fields fail closed instead of reading the unbounded original response.
    return {"truncated": True, "text": clean_text(encoded, MAX_API_CHARS)}


def write_json(path, value):
    target = pathlib.Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp-" + uuid.uuid4().hex)
    descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(str(temporary), str(target))
    finally:
        if temporary.exists():
            temporary.unlink()
    return str(target)


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_origin(value):
    parsed = urlsplit((value or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError("origin must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ToolError("origin cannot contain credentials, query or fragment")
    if parsed.path not in ("", "/"):
        raise ToolError("origin cannot contain a path")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
        local = address.is_loopback
    except ValueError:
        local = host in LOCAL_HOSTS
    if not local:
        raise ToolError("only loopback origins are allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ToolError("invalid origin port") from exc
    netloc = host
    if ":" in host:
        netloc = "[" + host + "]"
    if port:
        netloc += ":" + str(port)
    return parsed.scheme + "://" + netloc


def validate_relative_path(value, patterns, kind="route"):
    raw = (value or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
        raise ToolError(kind + " must be a relative path without scheme, host or fragment")
    decoded = unquote(parsed.path)
    if "\x00" in decoded or any(part == ".." for part in decoded.split("/")):
        raise ToolError(kind + " contains a forbidden path segment")
    if len(raw) > 2000:
        raise ToolError(kind + " is too long")
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if SENSITIVE_QUERY.search(key):
            raise ToolError("sensitive query parameter is forbidden: " + clean_text(key, 80))
    matched = False
    for pattern in patterns:
        if pattern.endswith("*") and decoded.startswith(pattern[:-1]):
            matched = True
        elif decoded == pattern or decoded.startswith(pattern.rstrip("/") + "/"):
            matched = True
    if not matched:
        raise ToolError(kind + " is outside the explicit allowlist: " + safe_path(raw))
    return raw


def validate_patterns(patterns, kind):
    result = []
    for value in patterns:
        parsed = urlsplit(value)
        if not value.startswith("/") or parsed.query or parsed.fragment or parsed.netloc:
            raise ToolError("invalid " + kind + " allowlist pattern: " + clean_text(value, 200))
        result.append(value)
    if not result:
        raise ToolError("at least one " + kind + " allowlist pattern is required")
    return result


def json_path(value, path):
    if path in ("", "$", None):
        return value
    if any(SENSITIVE_KEY.search(part) for part in re.split(r"[.\[\]]+", str(path)) if part):
        raise ToolError("assertions against sensitive JSON fields are forbidden")
    current = value
    tokens = re.findall(r"[^.\[\]]+|\[\d+\]", str(path))
    for token in tokens:
        if token.startswith("["):
            index = int(token[1:-1])
            if not isinstance(current, list) or index >= len(current):
                raise ToolError("JSON path does not exist: " + clean_text(path, 200))
            current = current[index]
        else:
            if not isinstance(current, dict) or token not in current:
                raise ToolError("JSON path does not exist: " + clean_text(path, 200))
            current = current[token]
    return current


def assertion_key(spec):
    return json.dumps(spec, ensure_ascii=False, sort_keys=True)


def evaluate_assertion(spec, actual):
    operator = spec.get("operator")
    expected = spec.get("expected")
    if operator == "equals":
        passed = actual == expected
    elif operator == "contains":
        if isinstance(actual, str):
            passed = str(expected) in actual
        elif isinstance(actual, (list, dict)):
            passed = expected in actual
        else:
            passed = False
    elif operator == "not_contains":
        passed = str(expected) not in str(actual)
    else:
        raise ToolError("assert operator must be equals, contains or not_contains")
    return bool(passed), redact_json(actual)


class WalkthroughConfig:
    def __init__(self, origin, api_origin, start, routes, api_patterns):
        self.origin = validate_origin(origin)
        self.api_origin = validate_origin(api_origin or origin)
        self.routes = validate_patterns(routes, "route")
        self.api_patterns = validate_patterns(api_patterns, "API")
        self.start = validate_relative_path(start, self.routes, "start route")

    def api_path(self, path):
        safe = validate_relative_path(path, self.api_patterns, "API path")
        parsed = urlsplit(safe)
        decoded = unquote(parsed.path)
        query_keys = {key.lower() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
        looks_log_related = bool(re.search(r"(?i)(log|sls|syslog)", decoded))
        if looks_log_related and query_keys.intersection(RAW_LOG_QUERY_KEYS):
            raise ToolError("raw log query/sample parameters are forbidden")
        if looks_log_related and not SAFE_LOG_SUFFIX.search(decoded) and RAW_LOG_PATH.search(decoded):
            raise ToolError("raw log query/sample GET is forbidden; use redacted status/catalog/aggregate APIs")
        return safe

    def route_path(self, path):
        return validate_relative_path(path, self.routes, "route")

    def public(self):
        return {
            "origin": self.origin,
            "api_origin": self.api_origin,
            "start": self.start,
            "routes": list(self.routes),
            "allowed_api_paths": list(self.api_patterns),
        }

    @classmethod
    def from_artifact(cls, artifact):
        return cls(
            artifact["origin"], artifact.get("api_origin") or artifact["origin"],
            artifact["start"], artifact["routes"], artifact["allowed_api_paths"])


class PlatformGetClient:
    def __init__(self, config):
        self.config = config
        self.token = os.environ.get("QIANSHU_WALKTHROUGH_PLATFORM_TOKEN", "")

    def get(self, path):
        safe = self.config.api_path(path)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            response = httpx.get(
                self.config.api_origin + safe, headers=headers, timeout=20,
                follow_redirects=False)
        except httpx.HTTPError as exc:
            raise ToolError("platform GET failed: " + clean_text(exc, 300)) from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ToolError("platform GET returned HTTP " + str(response.status_code))
        try:
            payload = response.json()
        except ValueError as exc:
            raise ToolError("platform GET response was not JSON") from exc
        return {
            "method": "GET",
            "path": safe,
            "status": response.status_code,
            "json": bounded_json(payload),
        }


class OpenAICompatClient:
    """Small standalone equivalent of copilot/llm.py's config/endpoint pattern."""
    def __init__(self, base_url, model, api_key, verify_ssl=True):
        base = (base_url or "").strip().rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise WalkthroughError("model base_url must be http(s)")
        if base.endswith("/chat/completions"):
            self.endpoint = base
        else:
            if not base.endswith(("/v1", "/v2")):
                base += "/v1"
            self.endpoint = base + "/chat/completions"
        if not model:
            raise WalkthroughError("model is required")
        self.model = model
        self.api_key = api_key
        self.verify_ssl = verify_ssl

    def choose(self, messages):
        try:
            response = httpx.post(
                self.endpoint,
                json={"model": self.model, "messages": messages, "temperature": 0,
                      "max_tokens": 700, "stream": False},
                headers={"Authorization": "Bearer " + self.api_key},
                timeout=120, verify=self.verify_ssl)
        except httpx.HTTPError as exc:
            raise WalkthroughError("model request failed: " + clean_text(exc, 300)) from exc
        if response.status_code != HTTPStatus.OK:
            raise WalkthroughError("model returned HTTP " + str(response.status_code))
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise WalkthroughError("model response has no chat completion content") from exc
        if not isinstance(content, str) or len(content) > 10000:
            raise WalkthroughError("model tool response is empty or too large")
        return content


def parse_model_call(content):
    text = content.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ToolError("model must return exactly one JSON tool call") from exc
    if not isinstance(value, dict) or not isinstance(value.get("tool"), str):
        raise ToolError("model JSON requires string field 'tool'")
    params = value.get("params", {})
    if not isinstance(params, dict):
        raise ToolError("tool params must be an object")
    return value["tool"], params


OBSERVE_JS = r"""
(() => {
  const visible = el => !!(el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
  const roleOf = el => el.getAttribute('role') ||
    ({A:'link', BUTTON:'button', TEXTAREA:'textbox', SELECT:'combobox', INPUT:
      (['button','submit','reset'].includes(el.type) ? 'button' : 'textbox')}[el.tagName] || el.tagName.toLowerCase());
  const nameOf = el => {
    const label = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    return (el.getAttribute('aria-label') || (label && label.innerText) || el.innerText ||
      el.getAttribute('placeholder') || el.getAttribute('title') || el.value || '').trim();
  };
  const nodes = [...document.querySelectorAll(
    'button,a,input,textarea,select,[role="button"],[role="link"],[role="textbox"],[tabindex]')]
    .filter(el => visible(el) && !(el.tagName === 'INPUT' && el.type === 'password'));
  const controls = nodes.slice(0, __MAX_CONTROLS__).map((el, i) => ({
    ref: 'c' + (i + 1), role: roleOf(el), name: nameOf(el).slice(0, 300),
    tag: el.tagName.toLowerCase(), type: el.type || '',
    test_id: el.getAttribute('data-testid') || '', disabled: !!el.disabled
  }));
  return {
    url: location.origin + location.pathname,
    title: (document.title || '').slice(0, 300),
    text: (document.body && document.body.innerText || '').slice(0, __MAX_BODY__),
    controls: controls,
    truncated_controls: nodes.length > controls.length
  };
})()
""".replace("__MAX_CONTROLS__", str(MAX_CONTROLS)).replace("__MAX_BODY__", str(MAX_BODY_CHARS))

FIND_JS = r"""
(() => {
  const locator = __LOCATOR__;
  const visible = el => !!(el.getClientRects().length &&
    getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none');
  const roleOf = el => el.getAttribute('role') ||
    ({A:'link', BUTTON:'button', TEXTAREA:'textbox', SELECT:'combobox', INPUT:
      (['button','submit','reset'].includes(el.type) ? 'button' : 'textbox')}[el.tagName] || el.tagName.toLowerCase());
  const nameOf = el => {
    const label = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    return (el.getAttribute('aria-label') || (label && label.innerText) || el.innerText ||
      el.getAttribute('placeholder') || el.getAttribute('title') || el.value || '').trim();
  };
  let candidates = [...document.querySelectorAll(
    'button,a,input,textarea,select,[role="button"],[role="link"],[role="textbox"],[tabindex]')]
    .filter(el => visible(el) && !(el.tagName === 'INPUT' && el.type === 'password'));
  if (locator.test_id) candidates = candidates.filter(el => el.getAttribute('data-testid') === locator.test_id);
  if (locator.role) candidates = candidates.filter(el => roleOf(el) === locator.role);
  if (locator.name) candidates = candidates.filter(el => nameOf(el) === locator.name);
  if (locator.tag) candidates = candidates.filter(el => el.tagName.toLowerCase() === locator.tag);
  if (candidates.length !== 1) return {ok:false, matches:candidates.length};
  const el = candidates[0];
  if (el.disabled) return {ok:false, disabled:true};
  el.scrollIntoView({block:'center'});
  if (el.tagName === 'A') el.target = '_self';
  const r = el.getBoundingClientRect();
  return {ok:true, x:r.x+r.width/2, y:r.y+r.height/2};
})()
"""

ELEMENT_TEXT_JS = r"""
(() => {
  const locator = __LOCATOR__;
  if (!locator) return document.body ? document.body.innerText : '';
  const visible = el => !!el.getClientRects().length;
  const roleOf = el => el.getAttribute('role') ||
    ({A:'link', BUTTON:'button', TEXTAREA:'textbox', SELECT:'combobox', INPUT:
      (['button','submit','reset'].includes(el.type) ? 'button' : 'textbox')}[el.tagName] || el.tagName.toLowerCase());
  const nameOf = el => {
    const label = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    return (el.getAttribute('aria-label') || (label && label.innerText) || el.innerText ||
      el.getAttribute('placeholder') || el.getAttribute('title') || el.value || '').trim();
  };
  let candidates = [...document.querySelectorAll(
    'button,a,input,textarea,select,[role="button"],[role="link"],[role="textbox"],[tabindex]')].filter(visible);
  if (locator.test_id) candidates = candidates.filter(el => el.getAttribute('data-testid') === locator.test_id);
  if (locator.role) candidates = candidates.filter(el => roleOf(el) === locator.role);
  if (locator.name) candidates = candidates.filter(el => nameOf(el) === locator.name);
  if (locator.tag) candidates = candidates.filter(el => el.tagName.toLowerCase() === locator.tag);
  return candidates.length === 1 ? (candidates[0].innerText || candidates[0].value || '') : null;
})()
"""


def with_locator(script, locator):
    return script.replace("__LOCATOR__", json.dumps(locator, ensure_ascii=False))


class CDPBrowser:
    def __init__(self, config):
        self.config = config
        self.profile = tempfile.mkdtemp(prefix="qianshu-model-walkthrough-")
        self.profile_id = uuid.uuid4().hex
        self.port = self._free_port()
        self.proc = None
        self.ws = None
        self.message_id = 0
        self.console = []
        self.network = []
        self.violations = []

    @staticmethod
    def _free_port():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def start(self):
        self.proc = chrome_popen([
            "google-chrome", "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking",
            "--remote-debugging-port=" + str(self.port), "--remote-allow-origins=*",
            "--user-data-dir=" + self.profile, "about:blank"], self.profile)
        page = None
        for _ in range(80):
            try:
                targets = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:" + str(self.port) + "/json/list", timeout=1).read())
                page = next((item for item in targets if item.get("type") == "page"), None)
                if page:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        if not page:
            raise WalkthroughError("headless Chrome debugging endpoint did not become ready")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
        for method in ("Page.enable", "Runtime.enable", "Network.enable"):
            self.send(method)
        self.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        self.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": "window.open = () => null;"})
        self.send("Emulation.setDeviceMetricsOverride", {
            "width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False})

    def _append(self, items, value):
        items.append(value)
        del items[:-MAX_EVENTS]

    def _event(self, message):
        method = message.get("method")
        params = message.get("params", {})
        if method == "Fetch.requestPaused":
            request = params.get("request", {})
            request_id = params.get("requestId")
            verb = str(request.get("method") or "").upper()
            url = str(request.get("url") or "")
            parsed = urlsplit(url)
            evidence = {"method": verb, "path": safe_path(url), "resource_type": params.get("resourceType", "")}
            if verb != "GET":
                evidence["reason"] = "non_get_blocked"
                self._append(self.violations, evidence)
                self._fire("Fetch.failRequest", {"requestId": request_id, "errorReason": "AccessDenied"})
                return
            allowed = True
            reason = ""
            if parsed.scheme in ("http", "https"):
                origin = parsed.scheme + "://" + (parsed.netloc or "")
                if origin != self.config.origin:
                    allowed, reason = False, "off_origin_get_blocked"
                elif params.get("resourceType") == "Document":
                    try:
                        self.config.route_path(parsed.path)
                    except ToolError:
                        allowed, reason = False, "off_route_navigation_blocked"
            if not allowed:
                evidence["reason"] = reason
                self._append(self.violations, evidence)
                self._fire("Fetch.failRequest", {"requestId": request_id, "errorReason": "AccessDenied"})
                return
            self._fire("Fetch.continueRequest", {"requestId": request_id})
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails", {})
            self._append(self.console, {"kind": "exception", "text": clean_text(
                detail.get("exception", {}).get("description") or detail.get("text"), 1000)})
        elif method == "Runtime.consoleAPICalled":
            args = params.get("args", [])
            text = " ".join(str(arg.get("value") or arg.get("description") or "") for arg in args[:5])
            self._append(self.console, {"kind": str(params.get("type") or "log"), "text": clean_text(text, 1000)})
        elif method == "Network.responseReceived":
            response = params.get("response", {})
            self._append(self.network, {
                "kind": "response", "status": response.get("status"),
                "path": safe_path(response.get("url", "")), "resource_type": params.get("type", "")})
        elif method == "Network.loadingFailed":
            self._append(self.network, {"kind": "failed", "error": clean_text(params.get("errorText"), 300)})
        elif method == "Page.javascriptDialogOpening":
            self._append(self.console, {"kind": "dialog", "text": clean_text(params.get("message"), 500)})
            self._fire("Page.handleJavaScriptDialog", {"accept": False})

    def _fire(self, method, params=None):
        self.message_id += 1
        self.ws.send(json.dumps({"id": self.message_id, "method": method, "params": params or {}}))

    def send(self, method, params=None):
        self.message_id += 1
        current = self.message_id
        self.ws.send(json.dumps({"id": current, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == current:
                if "error" in message:
                    raise ToolError("CDP " + method + ": " + clean_text(message["error"], 400))
                return message.get("result", {})
            self._event(message)

    def evaluate(self, expression):
        result = self.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True})
        if "exceptionDetails" in result:
            raise ToolError("browser evaluation failed: " + clean_text(result["exceptionDetails"], 500))
        return result.get("result", {}).get("value")

    def settle(self):
        # Chrome 146 can collect an unreferenced Runtime.evaluate timer promise.
        # Sleep in the executor, then pump CDP events with a scalar evaluation.
        time.sleep(0.25)
        self.evaluate("true")

    def navigate(self, path, setup=False):
        safe = self.config.route_path(path)
        self.send("Page.navigate", {"url": self.config.origin + safe})
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if self.evaluate("document.readyState === 'complete'"):
                    self.settle()
                    return self.observe()
            except ToolError:
                pass
            time.sleep(0.1)
        raise ToolError("navigation did not reach document.readyState=complete")

    def observe(self):
        raw = self.evaluate(OBSERVE_JS)
        if not isinstance(raw, dict):
            raise ToolError("browser observation returned no document")
        observation = redact_json(raw)
        observation["console"] = copy.deepcopy(self.console[-10:])
        observation["network"] = copy.deepcopy(self.network[-20:])
        observation["blocked_requests"] = copy.deepcopy(self.violations[-10:])
        refs = {}
        for control in observation.get("controls", []):
            refs[control["ref"]] = {
                "role": control.get("role") or "", "name": control.get("name") or "",
                "tag": control.get("tag") or "", "test_id": control.get("test_id") or ""}
        return observation, refs

    def _find(self, locator):
        result = self.evaluate(with_locator(FIND_JS, locator))
        if not result or not result.get("ok"):
            raise ToolError("semantic control is missing, ambiguous or disabled: " +
                            clean_text(result or {}, 300))
        return result

    def click(self, locator):
        point = self._find(locator)
        # Anchor new tabs would create an unobserved target outside this adapter.
        self.evaluate(with_locator(FIND_JS, locator))
        for event in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", {
                "type": event, "x": point["x"], "y": point["y"],
                "button": "left", "clickCount": 1})
        self.settle()
        return self.observe()

    def input(self, locator, value):
        if not isinstance(value, str) or len(value) > 500:
            raise ToolError("input value must be a string of at most 500 characters")
        self._find(locator)
        # Fixed executor-owned expression; model data is JSON-encoded, never code.
        script = with_locator(FIND_JS.replace(
            "return {ok:true, x:r.x+r.width/2, y:r.y+r.height/2};",
            "el.focus(); el.value = " + json.dumps(value, ensure_ascii=False) +
            "; el.dispatchEvent(new Event('input',{bubbles:true}));" +
            " el.dispatchEvent(new Event('change',{bubbles:true})); return {ok:true};"), locator)
        result = self.evaluate(script)
        if not result or not result.get("ok"):
            raise ToolError("semantic input failed")
        self.settle()
        return self.observe()

    def dom_text(self, locator=None):
        return self.evaluate(with_locator(ELEMENT_TEXT_JS, locator)) if locator else self.evaluate(
            "document.body ? document.body.innerText : ''")

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        # The profile belongs exclusively to this adapter instance.
        import shutil
        shutil.rmtree(self.profile, ignore_errors=True)


SYSTEM_PROMPT = """You are performing a read-only delivery-quality product walkthrough.
Choose exactly one tool per turn and return only a JSON object:
{"tool":"platform_api_get","params":{"path":"/api/registered-get"}}
{"tool":"browser_observe","params":{}}
{"tool":"browser_navigate","params":{"path":"/allowed-route"}}
{"tool":"browser_click","params":{"ref":"c1"}}
{"tool":"browser_input","params":{"ref":"c1","value":"text"}}
{"tool":"assert","params":{"source":"dom","operator":"contains","expected":"visible text"}}
{"tool":"assert","params":{"source":"api","evidence_id":"e1","path":"count","operator":"equals","expected":3}}
{"tool":"finish","params":{"summary":"verified outcome"}}
Use returned API content and browser observations to choose subsequent calls. Use only
control refs from the latest observation. For DOM assertions, optional "ref" selects a
control from the latest observation; without it, assertion reads the page body.
Before finish you MUST have selected at least one successful platform_api_get, one
browser_observe, one navigate/click/input action, one passing API content assertion,
and one passing DOM content assertion. An unrelated DOM assertion is insufficient.
Do not claim success after a failed assertion or blocked request. No JavaScript, shell,
SQL, absolute URLs, selectors or non-GET operations are available. Evidence is bounded
and redacted; do not request secrets."""


def new_artifact(config, task, model_name):
    return {
        "schema": SCHEMA,
        "created_at": utc_now(),
        "status": "running",
        "task": clean_text(task, MAX_TASK_CHARS),
        **config.public(),
        "model": {"name": clean_text(model_name, 200), "calls": 0},
        "browser_session_id": "",
        "steps": [],
        "evidence": [],
        "model_errors": [],
        "errors": [],
    }


class LiveWalkthrough:
    def __init__(self, config, task, llm, artifact_path, max_rounds=MAX_ROUNDS):
        self.config = config
        self.task = task
        self.llm = llm
        self.artifact_path = artifact_path
        self.max_rounds = min(max_rounds, MAX_ROUNDS)
        self.api = PlatformGetClient(config)
        self.browser = CDPBrowser(config)
        self.artifact = new_artifact(config, task, llm.model)
        self.refs = {}
        self.api_evidence = {}
        self.counts = {"api": 0, "observe": 0, "action": 0, "assertion": 0,
                       "api_assertion": 0, "dom_assertion": 0}
        self.failed_assertions = {}
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT + "\nAllowed routes: " +
             json.dumps(config.routes) + "\nAllowed API paths: " + json.dumps(config.api_patterns)},
            {"role": "user", "content": clean_text(task, MAX_TASK_CHARS)},
        ]

    def evidence(self, kind, data):
        evidence_id = "e" + str(len(self.artifact["evidence"]) + 1)
        self.artifact["evidence"].append({"id": evidence_id, "kind": kind, **redact_json(data)})
        return evidence_id

    def observe_evidence(self):
        observation, self.refs = self.browser.observe()
        evidence_id = self.evidence("observation", observation)
        self.counts["observe"] += 1
        return evidence_id, observation

    def action_evidence(self, tool, params):
        if tool == "browser_navigate":
            path = self.config.route_path(params.get("path"))
            observation, self.refs = self.browser.navigate(path)
            action = {"kind": "navigate", "path": path}
        elif tool == "browser_click":
            ref = str(params.get("ref") or "")
            locator = self.refs.get(ref)
            if not locator:
                raise ToolError("click ref is absent from the latest observation")
            observation, self.refs = self.browser.click(locator)
            action = {"kind": "click", "locator": locator}
        elif tool == "browser_input":
            ref = str(params.get("ref") or "")
            locator = self.refs.get(ref)
            if not locator:
                raise ToolError("input ref is absent from the latest observation")
            value = params.get("value")
            observation, self.refs = self.browser.input(locator, value)
            action = {"kind": "input", "locator": locator, "value": clean_text(value, 500)}
        else:
            raise ToolError("unknown browser action")
        observation_id = self.evidence("observation", observation)
        action_id = self.evidence("action", {"action": action, "observation_id": observation_id})
        self.counts["action"] += 1
        return [action_id, observation_id], {"action_id": action_id, "observation_id": observation_id,
                                             "observation": observation}

    def assert_evidence(self, params):
        source = params.get("source")
        spec = {"source": source, "operator": params.get("operator"),
                "expected": redact_json(params.get("expected"))}
        if source == "api":
            evidence_id = str(params.get("evidence_id") or "")
            if evidence_id not in self.api_evidence:
                raise ToolError("API assertion requires an existing API evidence_id")
            spec.update({"evidence_id": evidence_id, "path": clean_text(params.get("path"), 200)})
            actual = json_path(self.api_evidence[evidence_id]["response"]["json"], params.get("path"))
        elif source == "dom":
            ref = params.get("ref")
            locator = None
            if ref:
                locator = self.refs.get(str(ref))
                if not locator:
                    raise ToolError("DOM assertion ref is absent from the latest observation")
            spec["locator"] = locator
            actual = self.browser.dom_text(locator)
            if actual is None:
                raise ToolError("DOM assertion target is missing or ambiguous")
            actual = clean_text(actual, MAX_BODY_CHARS)
        else:
            raise ToolError("assert source must be api or dom")
        passed, safe_actual = evaluate_assertion(spec, actual)
        evidence_id = self.evidence("assertion", {
            "assertion": spec, "actual": safe_actual, "passed": passed})
        key = assertion_key(spec)
        if passed:
            self.counts["assertion"] += 1
            self.counts[source + "_assertion"] += 1
            self.failed_assertions.pop(key, None)
        else:
            self.failed_assertions[key] = evidence_id
        return [evidence_id], {"assertion_id": evidence_id, "passed": passed, "actual": safe_actual}

    def missing_requirements(self):
        missing = [name for name, count in self.counts.items() if count < 1]
        if self.failed_assertions:
            missing.append("failed_assertions")
        if self.browser.violations:
            missing.append("blocked_browser_requests")
        return missing

    def execute_tool(self, tool, params):
        if tool == "platform_api_get":
            result = self.api.get(params.get("path"))
            evidence_id = self.evidence("api", {"request": {"method": "GET", "path": result["path"]},
                                                "response": {"status": result["status"], "json": result["json"]}})
            self.api_evidence[evidence_id] = self.artifact["evidence"][-1]
            self.counts["api"] += 1
            return [evidence_id], {"api_evidence_id": evidence_id, **result}
        if tool == "browser_observe":
            evidence_id, observation = self.observe_evidence()
            return [evidence_id], {"observation_id": evidence_id, "observation": observation}
        if tool in ("browser_navigate", "browser_click", "browser_input"):
            return self.action_evidence(tool, params)
        if tool == "assert":
            return self.assert_evidence(params)
        raise ToolError("unknown or forbidden tool: " + clean_text(tool, 100))

    def finish(self, params, turn):
        self.browser.settle()
        if self.browser.violations:
            raise SafetyError("forbidden browser request was blocked; walkthrough cannot pass")
        missing = self.missing_requirements()
        evidence_id = self.evidence("finish", {"summary": clean_text(params.get("summary"), 1000),
                                               "missing": missing})
        self.artifact["steps"].append({"index": len(self.artifact["steps"]) + 1, "model_turn": turn,
                                       "tool": "finish", "params": redact_json(params),
                                       "evidence_refs": [evidence_id], "ok": not missing})
        if missing:
            return False, {"finish_rejected": missing, "evidence_id": evidence_id}
        self.artifact["status"] = "live_passed"
        self.artifact["requirements"] = dict(self.counts)
        return True, {"finished": True, "evidence_id": evidence_id}

    def run(self):
        try:
            self.browser.start()
            self.artifact["browser_session_id"] = self.browser.profile_id
            _observation, self.refs = self.browser.navigate(self.config.start, setup=True)
            for turn in range(1, self.max_rounds + 1):
                content = self.llm.choose(self.messages)
                self.artifact["model"]["calls"] += 1
                self.messages.append({"role": "assistant", "content": content})
                try:
                    tool, params = parse_model_call(content)
                    if tool == "finish":
                        done, result = self.finish(params, turn)
                        if done:
                            return self.artifact
                    else:
                        step = {"index": len(self.artifact["steps"]) + 1, "model_turn": turn,
                                "tool": tool, "params": redact_json(params), "evidence_refs": []}
                        try:
                            refs, result = self.execute_tool(tool, params)
                            step["evidence_refs"] = refs
                            step["ok"] = all(
                                item.get("passed", True) for item in self.artifact["evidence"]
                                if item["id"] in refs and item["kind"] == "assertion")
                        except ToolError as exc:
                            evidence_id = self.evidence("tool_error", {"error": clean_text(exc, 1000)})
                            step["evidence_refs"] = [evidence_id]
                            step["ok"] = False
                            result = {"tool_error": clean_text(exc, 1000), "evidence_id": evidence_id}
                        self.artifact["steps"].append(step)
                except ToolError as exc:
                    self.artifact["model_errors"].append(clean_text(exc, 500))
                    result = {"tool_error": clean_text(exc, 1000)}
                self.messages.append({"role": "user", "content": json.dumps(
                    {"tool_result": result}, ensure_ascii=False)})
            raise WalkthroughError("model did not finish within the bounded round limit")
        except Exception as exc:
            self.artifact["status"] = "failed"
            self.artifact["errors"].append(clean_text(exc, 1500))
            raise
        finally:
            self.artifact["safety"] = {"blocked_requests": copy.deepcopy(self.browser.violations),
                                       "policy": "GET only; loopback origin and route/API allowlists"}
            write_json(self.artifact_path, self.artifact)
            self.browser.close()


def artifact_digest(artifact):
    encoded = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ReplayWalkthrough:
    def __init__(self, source, output_path):
        if source.get("schema") != SCHEMA or source.get("status") != "live_passed":
            raise ReplayError("only a complete live_passed v1 artifact can be replayed")
        self.source = source
        self.output_path = output_path
        self.config = WalkthroughConfig.from_artifact(source)
        self.api = PlatformGetClient(self.config)
        self.browser = CDPBrowser(self.config)
        self.evidence_by_id = {item.get("id"): item for item in source.get("evidence", [])}
        self.replayed_apis = {}
        self.artifact = {
            "schema": REPLAY_SCHEMA, "created_at": utc_now(), "status": "running",
            "replay_of_sha256": artifact_digest(source), "model": {"calls": 0, "disabled": True},
            "source_browser_session_id": source.get("browser_session_id"),
            "browser_session_id": self.browser.profile_id,
            "origin": self.config.origin, "api_origin": self.config.api_origin,
            "steps": [], "evidence": [], "errors": [],
        }
        if self.artifact["browser_session_id"] == self.artifact["source_browser_session_id"]:
            raise ReplayError("fresh browser session was not created")

    def evidence(self, kind, data):
        evidence_id = "r" + str(len(self.artifact["evidence"]) + 1)
        self.artifact["evidence"].append({"id": evidence_id, "kind": kind, **redact_json(data)})
        return evidence_id

    def require_evidence(self, refs, kinds):
        found = [self.evidence_by_id.get(ref) for ref in refs]
        if not found or any(item is None for item in found):
            raise ReplayError("artifact has missing evidence references")
        for item in found:
            if item["kind"] not in kinds:
                raise ReplayError("artifact evidence kind does not match its step")
        return found

    def run(self):
        try:
            steps = self.source.get("steps", [])
            if not any(step.get("tool") == "platform_api_get" and step.get("ok") for step in steps):
                raise ReplayError("artifact has no successful model-selected API call")
            if not any(step.get("tool") in ("browser_navigate", "browser_click", "browser_input")
                       and step.get("ok") for step in steps):
                raise ReplayError("artifact has no model-selected browser action")
            assertion_sources = set()
            for step in steps:
                if step.get("tool") != "assert" or not step.get("ok"):
                    continue
                evidence = self.require_evidence(step.get("evidence_refs") or [], {"assertion"})[0]
                if evidence.get("passed"):
                    assertion_sources.add((evidence.get("assertion") or {}).get("source"))
            if assertion_sources != {"api", "dom"}:
                raise ReplayError("artifact requires passing API and DOM content assertions")
            self.browser.start()
            self.browser.navigate(self.config.start, setup=True)
            for step in steps:
                tool = step.get("tool")
                if tool == "finish":
                    continue
                refs = step.get("evidence_refs") or []
                if tool == "platform_api_get":
                    old = self.require_evidence(refs, {"api"})[0]
                    path = old["request"]["path"]
                    result = self.api.get(path)
                    new_id = self.evidence("api", {"request": {"method": "GET", "path": path},
                                                   "response": {"status": result["status"], "json": result["json"]}})
                    self.replayed_apis[old["id"]] = self.artifact["evidence"][-1]
                elif tool == "browser_observe":
                    self.require_evidence(refs, {"observation"})
                    observation, _refs = self.browser.observe()
                    new_id = self.evidence("observation", observation)
                elif tool in ("browser_navigate", "browser_click", "browser_input"):
                    old_action, old_observation = self.require_evidence(refs, {"action", "observation"})
                    action = old_action.get("action") or {}
                    kind = action.get("kind")
                    if tool == "browser_navigate" and kind == "navigate":
                        observation, _refs = self.browser.navigate(action["path"])
                    elif tool == "browser_click" and kind == "click":
                        observation, _refs = self.browser.click(action["locator"])
                    elif tool == "browser_input" and kind == "input":
                        observation, _refs = self.browser.input(action["locator"], action.get("value", ""))
                    else:
                        raise ReplayError("browser action does not match its recorded tool")
                    new_id = self.evidence("action", {"action": action})
                    self.evidence("observation", observation)
                elif tool == "assert":
                    old_assert = self.require_evidence(refs, {"assertion"})[0]
                    spec = old_assert.get("assertion") or {}
                    if spec.get("source") == "api":
                        replayed = self.replayed_apis.get(spec.get("evidence_id"))
                        if replayed is None:
                            raise ReplayError("API assertion has no re-fetched API evidence")
                        actual = json_path(replayed["response"]["json"], spec.get("path"))
                    elif spec.get("source") == "dom":
                        actual = self.browser.dom_text(spec.get("locator"))
                        if actual is None:
                            raise ReplayError("replayed DOM assertion target is missing or ambiguous")
                        actual = clean_text(actual, MAX_BODY_CHARS)
                    else:
                        raise ReplayError("unknown assertion source")
                    passed, safe_actual = evaluate_assertion(spec, actual)
                    new_id = self.evidence("assertion", {"assertion": spec, "actual": safe_actual,
                                                         "passed": passed})
                    if not passed:
                        raise ReplayError("replayed assertion failed: " + clean_text(spec, 600))
                else:
                    raise ReplayError("artifact contains an unknown replay step")
                self.artifact["steps"].append({"source_step": step.get("index"), "tool": tool,
                                               "evidence_refs": [new_id], "ok": True})
            if self.browser.violations:
                raise ReplayError("replay blocked a forbidden browser request")
            self.artifact["status"] = "passed"
            return self.artifact
        except Exception as exc:
            self.artifact["status"] = "failed"
            self.artifact["errors"].append(clean_text(exc, 1500))
            if isinstance(exc, ReplayError):
                raise
            raise ReplayError(clean_text(exc, 1000)) from exc
        finally:
            self.artifact["safety"] = {"blocked_requests": copy.deepcopy(self.browser.violations),
                                       "policy": "fresh profile; model disabled; GET only"}
            write_json(self.output_path, self.artifact)
            self.browser.close()


def run_live(config, task, llm, artifact_path, max_rounds=MAX_ROUNDS):
    return LiveWalkthrough(config, task, llm, artifact_path, max_rounds).run()


def run_replay(artifact_path, output_path):
    return ReplayWalkthrough(read_json(artifact_path), output_path).run()


def replay_path_for(path):
    target = pathlib.Path(path)
    return str(target.with_name(target.stem + ".replay" + target.suffix))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("run", help="run a live model walkthrough and independent fresh-profile replay")
    live.add_argument("--task", required=True)
    live.add_argument("--origin", required=True, help="loopback frontend origin")
    live.add_argument("--api-origin", help="loopback API origin; defaults to --origin")
    live.add_argument("--start", required=True, help="initial allowed route (setup, not counted as a model action)")
    live.add_argument("--route", action="append", required=True, help="allowed route/prefix; repeatable")
    live.add_argument("--allow-api", action="append", required=True,
                      help="redacted status/catalog/aggregate GET path or prefix*; raw log query/sample paths forbidden")
    live.add_argument("--base-url", default=os.environ.get("QIANSHU_WALKTHROUGH_BASE_URL", ""))
    live.add_argument("--model", default=os.environ.get("QIANSHU_WALKTHROUGH_MODEL", ""))
    live.add_argument("--artifact", required=True)
    live.add_argument("--replay-artifact", help="default: ARTIFACT with .replay before its suffix")
    live.add_argument("--max-rounds", type=int, default=MAX_ROUNDS)
    replay = sub.add_parser("replay", help="replay a sanitized artifact with no model")
    replay.add_argument("--artifact", required=True)
    replay.add_argument("--output", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "replay":
        result = run_replay(args.artifact, args.output)
        print("REPLAY PASS " + args.output)
        print(json.dumps({"model_calls": result["model"]["calls"],
                          "browser_session_id": result["browser_session_id"]}, ensure_ascii=False))
        return 0
    verify = os.environ.get("QIANSHU_WALKTHROUGH_VERIFY_SSL", "true").lower() not in ("0", "false", "no")
    llm = OpenAICompatClient(args.base_url, args.model,
                             os.environ.get("QIANSHU_WALKTHROUGH_API_KEY", ""), verify)
    config = WalkthroughConfig(args.origin, args.api_origin, args.start, args.route, args.allow_api)
    live = run_live(config, args.task, llm, args.artifact, args.max_rounds)
    replay_output = args.replay_artifact or replay_path_for(args.artifact)
    try:
        replay = run_replay(args.artifact, replay_output)
    except Exception:
        print("LIVE PASS BUT REPLAY FAIL " + replay_output, file=sys.stderr)
        raise
    print("LIVE PASS " + args.artifact)
    print("REPLAY PASS " + replay_output)
    print(json.dumps({"live_model_calls": live["model"]["calls"],
                      "replay_model_calls": replay["model"]["calls"],
                      "requirements": live["requirements"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("MODEL WALKTHROUGH FAIL: " + clean_text(error, 1500), file=sys.stderr)
        raise SystemExit(1)
