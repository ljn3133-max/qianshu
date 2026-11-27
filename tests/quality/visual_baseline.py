#!/usr/bin/env python3
"""视觉基线对比：页面截图 vs 基线——只报变化。

首跑存基线；之后每跑对比：无变化的页面**不出现**在报告里（用户只看变化）。
变化页输出新旧两张图到 docs/tmp/visual/ 供人工确认。

像素比例容差：顶栏时钟、相对时间和告警角标会让精确哈希产生无效差异，因此按
逐像素变化比例判断，超过 2% 才报并给出百分比。
截图稳定性：截图前等待网络静默和加载遮罩消失，并注入停动画样式，避免把菜单过渡、
表格加载骨架或尚未出现的异步块（如 diagnose 建议区、DBA 慢 SQL 加载态）写进基线。
比较在 Chrome canvas 里做（原生逐像素，毫秒级），不引入 Pillow 依赖。
注意：视觉基线管布局/样式回归；文案/状态词的变化由 raw-key 契约和 i18n 检查兜底。
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chrome_run import chrome_popen
from credentials import require_test_credentials
from pages import LIVE_EXTERNAL_PAGES, PAGES

Q = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(Q))
BASE = os.path.join(Q, "baseline")
OUT = os.path.join(ROOT, "docs", "tmp", "visual")
SITE = os.environ.get("QIANSHU_SITE", "http://127.0.0.1:5273")
API = os.environ.get("QIANSHU_API", "http://127.0.0.1:8810")
USERNAME, PWD = require_test_credentials()
os.makedirs(BASE, exist_ok=True)
os.makedirs(OUT, exist_ok=True)
READONLY = os.environ.get("QIANSHU_QUALITY_READONLY") == "1"
SKIPPED_EXTERNAL = sorted(LIVE_EXTERNAL_PAGES) if READONLY else []
PAGES = [p for p in PAGES if p not in SKIPPED_EXTERNAL]
DIFF_PORT = 9345
# 变化像素占比阈值：2%。动态数据典型波动：时钟/采集耗时约 0.1%；监控折线图因 24h
# 数据窗平移整条线微动约 1%。布局/组件级回归（整块缺失、表格错位）通常 >5%。
DIFF_THRESHOLD = 0.02
# 用法：visual_baseline.py <口令> [--solidify]
# --solidify：仅在人工确认当前稳定截图后，将其覆盖为基线（不比对）
SOLIDIFY = "--solidify" in sys.argv[2:]
# 可只跑单页调试：visual_baseline.py <口令> dba
ONLY = next((a for a in sys.argv[2:] if a in PAGES), "")

# 页面清单统一在 pages.py（新增路由未纳入会由 gate.py 拦截）

KILL_ANIM_JS = """(() => {
  let s = document.getElementById('__qs_stable');
  if (!s) { s = document.createElement('style'); s.id = '__qs_stable'; document.documentElement.appendChild(s); }
  s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important;caret-color:transparent!important}';
  return true;
})()"""

# 在页面内用 canvas 逐像素比对（原生循环，1600x1000 约几十毫秒）
COMPARE_HTML = """<!doctype html><meta charset="utf-8"><script>
const q = new URLSearchParams(location.search);
const load = s => new Promise((ok, bad) => { const i = new Image(); i.onload = () => ok(i); i.onerror = bad; i.src = s; });
window.__diff = (async () => {
  const [a, b] = await Promise.all([load('/new/' + q.get('p') + '.png'), load('/old/' + q.get('p') + '.png')]);
  if (a.width !== b.width || a.height !== b.height) return {ratio: 1, changed: a.width * a.height, total: a.width * a.height};
  const cv = document.createElement('canvas'); cv.width = a.width; cv.height = a.height;
  const cx = cv.getContext('2d');
  cx.drawImage(a, 0, 0); const d1 = cx.getImageData(0, 0, a.width, a.height).data;
  cx.clearRect(0, 0, a.width, a.height); cx.drawImage(b, 0, 0); const d2 = cx.getImageData(0, 0, a.width, a.height).data;
  let n = 0; const tol = 10;
  for (let i = 0; i < d1.length; i += 4) {
    if (Math.abs(d1[i] - d2[i]) > tol || Math.abs(d1[i + 1] - d2[i + 1]) > tol || Math.abs(d1[i + 2] - d2[i + 2]) > tol) n++;
  }
  return {ratio: n / (a.width * a.height), changed: n, total: a.width * a.height};
})();
</script>"""


class _DiffHandler(BaseHTTPRequestHandler):
    """/compare.html + /new/<page>.png（当前截图）+ /old/<page>.png（基线）。"""

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/compare.html":
            self._send(COMPARE_HTML.encode(), "text/html; charset=utf-8")
            return
        for prefix, root in (("/new/", OUT), ("/old/", BASE)):
            if path.startswith(prefix):
                fp = os.path.join(root, os.path.basename(unquote(path)))
                if os.path.exists(fp):
                    self._send(open(fp, "rb").read(), "image/png")
                    return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_a):
        pass


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]


def main():
    login = json.loads(subprocess.run(
        ["curl", "-s", "-X", "POST", f"{API}/api/auth/login",
         "-H", "Content-Type: application/json",
         "-d", json.dumps({"username": USERNAME, "password": PWD})],
        capture_output=True, text=True).stdout)
    token = login.get("token", "")
    if not token:
        print("视觉基线对比: 登录失败，无法进行（检查 QIANSHU_TEST_USERNAME / PASSWORD 环境变量）")
        sys.exit(2)
    import websocket

    CHROME_ARGS = ["google-chrome", "--headless=new", "--disable-gpu",
                   "--window-size=1600,1000",
                   "--remote-debugging-port=9344", "--remote-allow-origins=*",
                   "--user-data-dir=/tmp/chrome-v", "about:blank"]
    ws = None
    mid = [0]
    events = []

    def send(method, params=None):
        mid[0] += 1
        ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if "method" in msg:
                events.append(msg)     # 顺手收集 Network 事件（等网络静默用）
            if msg.get("id") == mid[0]:
                return msg.get("result", {})

    def evaljs(expr):
        r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return r.get("result", {}).get("value")

    def _launch():
        subprocess.run("ps -eo pid,comm | awk '$2 ~ /chrome/ {print $1}' | while read p; do kill $p 2>/dev/null; done",
                       shell=True)
        time.sleep(1)   # 等进程退出再删目录，否则 rm 会报"目录非空"（噪音）
        subprocess.run("rm -rf /tmp/chrome-v; mkdir -p /tmp/chrome-v", shell=True)
        chrome_popen(CHROME_ARGS, "/tmp/chrome-v")
        time.sleep(8)

    def _connect():
        ts = json.loads(urllib.request.urlopen("http://127.0.0.1:9344/json/list").read())
        pg = next(t for t in ts if t["type"] == "page")
        return websocket.create_connection(pg["webSocketDebuggerUrl"], timeout=90)

    def _setup():
        """连接后统一初始化：视口钉死 + 登录态（重连/复活后可重复调用）。

        视口必须固定：headless 窗口尺寸可能在运行间漂移，导致哈希对比产生噪音，
        因此统一为 1600x1000。
        """
        nonlocal ws
        send("Page.enable")
        send("Runtime.enable")
        send("Network.enable")
        send("Emulation.setDeviceMetricsOverride",
             {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        send("Page.navigate", {"url": f"{SITE}/login"})
        time.sleep(3)
        evaljs(f"localStorage.setItem('qianshu.token', '{token}'); true")
        # 基线固定暗色：自动主题会随拍摄时段切换，亮/暗差异会淹没真实布局回归。
        evaljs("localStorage.setItem('qianshu.theme.preference', "
               "JSON.stringify({mode:'dark',date:new Date().toDateString()})); true")

    def _revive():
        """Chrome 中途退出后的恢复：重启浏览器、重连并恢复初始化。"""
        nonlocal ws
        try:
            ws.close()
        except Exception:
            pass
        _launch()
        ws = _connect()
        _setup()

    # 像素比对的本地服务（同源页面加载新旧 PNG，canvas 内逐像素比较）
    srv = ThreadingHTTPServer(("127.0.0.1", DIFF_PORT), _DiffHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    _launch()
    ws = _connect()
    _setup()

    def _wait_network_quiet(timeout=8.0, quiet=1.0):
        """等"无在飞请求且最近 quiet 秒无新请求"再截图。

        诊断页"建议"等多接口异步块可能晚于 pathname 和正文就绪，必须以网络静默
        作为终态判据，避免截图内容不稳定。
        """
        pending = set()
        # 不清空 events：清掉会丢掉"已发出、尚未完成"的请求（导航期间缓冲的事件），
        # 让静默窗口误判；顺序处理即可重建在飞集合。
        last = time.time()
        end = time.time() + timeout
        while time.time() < end:
            evaljs("1")   # 泵：send 会顺带收走已缓冲的 Network 事件
            while events:
                m = events.pop(0)
                meth = m.get("method")
                if meth == "Network.requestWillBeSent":
                    pending.add(m.get("params", {}).get("requestId"))
                    last = time.time()
                elif meth in ("Network.loadingFinished", "Network.loadingFailed"):
                    pending.discard(m.get("params", {}).get("requestId"))
                    last = time.time()
            if not pending and time.time() - last >= quiet:
                return True
            time.sleep(0.1)
        return False

    first_run = not any(f.endswith(".png") for f in os.listdir(BASE))
    changed, unchanged, solidified = [], 0, 0
    # 运行前清理遗留的 -new/-old，避免 visual_update 把过期截图误当变化固化进基线。
    for f in os.listdir(OUT):
        if f.endswith("-new.png") or f.endswith("-old.png"):
            os.remove(os.path.join(OUT, f))

    def _canvas_diff(p):
        """在 Chrome 里比对新旧截图，返回变化像素比例；失败返回 None（保守报变化）。"""
        send("Page.navigate", {"url": f"http://127.0.0.1:{DIFF_PORT}/compare.html?p={p}"})
        for _ in range(25):
            if evaljs("typeof window.__diff") == "object":
                break
            time.sleep(0.2)
        r = evaljs("window.__diff")
        if isinstance(r, dict) and "ratio" in r:
            return float(r["ratio"])
        return None

    def _grab(p):
        """单页截图：路由就绪双保险 + 失败复活重试。抛异常=整页放弃（不写基线）。

        仅凭 body 文本>300 可能把上一页的残留渲染当成就绪，必须等 location.pathname
        落到本页才截图，避免把错页写进基线。
        连续大视口截图偶发 renderer/ws 掉线甚至 Chrome 退出，捕获失败要恢复浏览器
        并重试，避免 KeyError 或连接中断使整个任务退出。
        """
        import base64

        for attempt in range(3):
            try:
                send("Page.navigate", {"url": f"{SITE}/{p}?t={int(time.time())}"})
                for _ in range(16):
                    time.sleep(0.5)
                    if (evaljs(f"location.pathname === '/{p}'")
                            and (evaljs("document.body.innerText.length") or 0) > 300
                            and evaljs("![...document.querySelectorAll('.el-loading-mask')]"
                                       ".some(e => e.getClientRects().length)")):
                        break
                # 等异步区块加载完（网络静默），再停动画/光标：
                # 否则会拍到菜单过渡/表格加载骨架/尚未出现的异步块，并把它写进基线
                _wait_network_quiet()
                # 加载遮罩可能晚于首个静默窗口才出现（如 DBA 慢 SQL 查询），必须等它
                # 真正消失；等不到就判本页失败（不写基线）——绝不把加载态固化进基线。
                for _ in range(60):
                    if not evaljs("[...document.querySelectorAll('.el-loading-mask')]"
                                  ".some(e => e.getClientRects().length)"):
                        break
                    time.sleep(0.5)
                else:
                    raise RuntimeError(f"{p} 加载遮罩 30s 未消失")
                evaljs(KILL_ANIM_JS)
                time.sleep(0.8)
                if evaljs(f"location.pathname === '/{p}'"):
                    shot = send("Page.captureScreenshot", {"format": "png"}) or {}
                    if shot.get("data"):
                        return base64.b64decode(shot["data"])
            except Exception as e:
                print(f"    捕获异常（第 {attempt + 1} 次）: {type(e).__name__}，复活浏览器重试", flush=True)
            time.sleep(2)
            try:
                _revive()
            except Exception as e:
                print(f"    复活失败: {type(e).__name__}: {str(e)[:80]}", flush=True)
        raise RuntimeError(f"{p} 连续 {3} 次捕获失败")

    for p in ([ONLY] if ONLY else PAGES):
        print(f"  拍摄 {p} ...", flush=True)
        try:
            data = _grab(p)
        except Exception as e:
            print(f"  · {p}: {e}，跳过（不写基线）", flush=True)
            continue

        cur = os.path.join(OUT, f"{p}.png")
        open(cur, "wb").write(data)
        base = os.path.join(BASE, f"{p}.png")
        if SOLIDIFY:
            shutil.copy2(cur, base)
            os.remove(cur)
            solidified += 1
        elif first_run or not os.path.exists(base):
            shutil.copy2(cur, base)
            changed.append((p, "首跑存基线"))
        else:
            ratio = 0.0 if sha(cur) == sha(base) else _canvas_diff(p)
            if ratio is None:
                changed.append((p, "视觉变化（像素比对失败，请人工看图）"))
            elif ratio > DIFF_THRESHOLD:
                changed.append((p, f"视觉变化 {ratio * 100:.2f}%"))
            else:
                unchanged += 1
                os.remove(cur)   # 容差内不产出（时钟/相对时间等动态数据）
                continue
            shutil.copy2(cur, os.path.join(OUT, f"{p}-new.png"))
            shutil.copy2(base, os.path.join(OUT, f"{p}-old.png"))
    ws.close()
    srv.shutdown()
    if SOLIDIFY:
        print(f"视觉基线: 已按 --solidify 重新固化 {solidified} 页")
        return []
    print(f"视觉基线对比: {unchanged} 页无变化 / {len(changed)} 页有变化")
    if SKIPPED_EXTERNAL:
        print("只读外部页面跳过: " + "、".join(SKIPPED_EXTERNAL) + "（默认加载会查询真实 SLS）")
    for p, why in changed:
        print(f"  · {p}: {why}（图在 docs/tmp/visual/）")
    return changed


if __name__ == "__main__":
    main()
