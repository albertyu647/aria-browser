#!/usr/bin/env python3
"""
Feature-parity evals vs Claude in Chrome's tool surface (navigate, read_page, find, get_page_text,
computer click/type/key/scroll/hover/screenshot, form_input, file_upload, javascript_tool,
read_console_messages, read_network_requests, tabs_*), plus aria-only extras
(two profiles at once, batch, diff, fetch-as-user) and DevTools-panel parity
(request headers/payload/body, HAR, WebSockets, breakpoints/logpoints, perf, storage).

Uses a local data: URL fixture for forms so it never hammers real sites.
    python3 bench/evals.py
"""

import http.server
import json
import shlex
import subprocess
import threading
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARIA = [str(ROOT / "bin" / "aria")]

FIXTURE = """<!doctype html><title>Aria Fixture</title>
<main><h1>Fixture</h1>
<label>Name <input id=name placeholder="Your name"></label>
<label>Color <select id=color><option>red</option><option>green</option><option>blue</option></select></label>
<label><input type=checkbox id=agree> I agree</label>
<input type=file id=file>
<button id=go onclick="document.getElementById('out').textContent='Hello '+document.getElementById('name').value+' '+document.getElementById('color').value+' '+document.getElementById('agree').checked+' '+(document.getElementById('file').files[0]||{}).name;console.error('clicked-go')">Submit</button>
<p id=out>pending</p>
<div id=hov onmouseover="this.textContent='hovered!'">hover me</div>
<a href="#two" id=lnk>Section two</a>
<div style="height:3000px"></div><h2 id=two>Bottom section</h2>
<iframe srcdoc="<button onclick=&quot;this.textContent='iframe ok'&quot;>Frame button</button>"></iframe>
</main>"""
FIX_URL = "data:text/html," + urllib.parse.quote(FIXTURE)


DT_JS = """function compute(items, factor) {
  const total = items.reduce((a, b) => a + b, 0) * factor;
  document.getElementById('out').textContent = 'total-' + total;
  return total;
}
function boom() { null.x; }
"""
DT_HTML = """<!doctype html><title>DT Fixture</title><script src="/app.js"></script>
<main><p id=out>-</p><button onclick="compute([1,2,3], 7)">Compute</button>
<button onclick="fetch('/api/items?q=x',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({hello:'world'})})">Post</button>
<button onclick="setTimeout(boom)">Boom</button>
<button onclick="const w=new WebSocket('wss://echo.websocket.org');w.onopen=()=>w.send('ping-aria')">WS</button></main>
<script>localStorage.setItem('user','{"id":7}');
const r=indexedDB.open('appdb',1);r.onupgradeneeded=()=>r.result.createObjectStore('notes',{keyPath:'id'});
r.onsuccess=()=>r.result.transaction('notes','readwrite').objectStore('notes').put({id:1,text:'first note'});</script>"""


class DTHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, ctype, body):
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("x-fixture", "1")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/app.js"):
            return self._send("application/javascript", DT_JS.encode())
        self._send("text/html", DT_HTML.encode())

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        self._send("application/json", json.dumps({"items": ["a", "b", "c"]}).encode())


def aria(*args, profile=None):
    t0 = time.perf_counter()
    p = subprocess.run(ARIA + (["--profile", profile] if profile else []) + list(args), capture_output=True, text=True, timeout=120)
    return p.stdout + p.stderr, p.returncode, time.perf_counter() - t0


RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}"[:200])


def main():
    tmp = Path(tempfile.mkdtemp())
    upload = tmp / "hello.txt"
    upload.write_text("hi")

    out, _, dt = aria("open", FIX_URL, "--new")
    check("open+snapshot", "Fixture" in out and "](e" in out, f"{dt:.2f}s")

    out, _, _ = aria("find", "submit")
    check("find returns ref", "[button: Submit](e" in out, out.strip()[:80])

    out, _, _ = aria("type", "Your name", "Ada")
    out2, _, _ = aria("select", "Color", "blue")
    out3, _, _ = aria("check", "I agree")
    out4, _, _ = aria("upload", "#file", str(upload))
    out5, _, dt = aria("click", "Submit", "--text", "--filter", "Hello")
    check("type/select/check/upload/click", "Hello Ada blue true hello.txt" in out5, out5.strip().splitlines()[-1][:80])

    out, _, _ = aria("console", "--errors")
    check("console capture", "clicked-go" in out)

    out, _, _ = aria("hover", "hover me", "--text", "--filter", "hovered")
    check("hover", "hovered!" in out)

    out, _, _ = aria("snap", "-i")
    ref = next((l.split("](")[1].split(")")[0] for l in out.splitlines() if "Frame button" in l), None)
    out, _, _ = aria("click", ref or "Frame button", "--snap", "--filter", "iframe ok")
    check("iframe click via ref", "iframe ok" in out, f"ref={ref}")

    out, _, _ = aria("scroll", "Bottom section")
    check("scroll into view", "scrollY" in out and "scrollY 0/" not in out, out.strip()[-40:])

    out, _, _ = aria("eval", "document.title")
    check("eval expression", "Aria Fixture" in out)
    out, _, _ = aria("eval", "const x = 20; return x + 22")
    check("eval statements", out.strip() == "42")

    shot = tmp / "s.png"
    out, _, _ = aria("shot", str(shot))
    check("screenshot", shot.exists() and shot.stat().st_size > 1000, out.strip()[-30:])

    out, _, _ = aria("open", "news.ycombinator.com", "--no-snap")
    out, _, dt = aria("click", "new")
    check("click by text navigates", "/newest" in out, f"{dt:.2f}s")
    out, _, _ = aria("back")
    check("back", out.strip().endswith("news.ycombinator.com/>") or "ycombinator.com/>" in out or "/news" in out, out.strip()[:90])
    out, _, _ = aria("network", "-n", "3")
    check("network log", "GET" in out)

    out, _, _ = aria("tab", "new", "https://en.wikipedia.org/wiki/Main_Page")
    out, _, _ = aria("tabs")
    check("multi-tab", out.count("\n") >= 1, out.replace("\n", " | ")[:150])

    out, _, dt = aria("type", "Search Wikipedia", "Playwright (software)", "--submit")
    check("type+submit search", "Playwright" in out, f"{dt:.2f}s")

    out, _, dt = aria("batch", "open news.ycombinator.com --no-snap", "find comments -n 2", "eval location.host")
    check("batch (3 cmds, 1 call)", "news.ycombinator.com" in out and "](" in out, f"{dt:.2f}s")

    aria("open", "news.ycombinator.com", "--no-snap")
    aria("snap")
    out, _, _ = aria("click", "new", "--diff")
    check("diff after action", out.count("\n+ ") >= 5, f"{out.count(chr(10))} lines")

    out, _, _ = aria("fetch", "https://news.ycombinator.com/news", "--max", "200")
    check("fetch as user", out.startswith("200"))

    # two Chrome profiles live at the same time (Claude in Chrome: one connected profile)
    import os
    p1, p2 = os.environ.get("ARIA_EVAL_PROFILES", "work,personal").split(",")
    o1, _, _ = aria("open", "https://www.linkedin.com/feed/", "--no-snap", profile=p1)
    o2, _, _ = aria("open", "https://www.linkedin.com/feed/", "--no-snap", profile=p2)
    st, _, _ = aria("status")
    check(f"{p1} + {p2} profiles concurrently", p1 in st and p2 in st and "LinkedIn" in o1 and "LinkedIn" in o2, st.strip()[:120])

    out, _, _ = aria("open", "https://x.com/home", "--no-snap")
    check("signed-in X (login wall auto-resync)", "Log in" not in out and "/home" in out, out.strip()[:90])

    devtools_evals(tmp)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} evals passed")
    sys.exit(0 if passed == len(RESULTS) else 1)


def devtools_evals(tmp):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DTHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/"
    aria("open", base, "--new", "--no-snap")

    aria("click", "Post")
    time.sleep(0.3)
    out, _, _ = aria("network", "--method", "POST")
    rid = out.strip().splitlines()[-1].split()[0] if out.startswith("#") else "#0"
    out, _, _ = aria("req", rid)
    check("req: headers + payload + body", "content-type: application/json" in out and '"hello": "world"' in out
          and "x-fixture: 1" in out and '"items"' in out, rid)
    out, _, _ = aria("req", rid, "--curl")
    check("req --curl", out.startswith("curl ") and "--data-raw" in out)
    har = tmp / "t.har"
    aria("har", str(har))
    ok = har.exists() and any(e["request"].get("postData") for e in json.loads(har.read_text())["log"]["entries"])
    check("har export", ok)

    aria("click", "WS")
    aria("wait", "2")
    out, _, _ = aria("ws")
    check("websocket frames", "↑" in out and "ping-aria" in out, out.strip().splitlines()[0][:80])

    out, _, _ = aria("debug", "break", "app.js:2", "--log", "items, factor")
    aria("debug", "break", "app.js:3", "--cond", "factor > 5")
    aria("click", "Compute")
    out, _, _ = aria("debug", "hits")
    check("logpoint + breakpoint locals", "[logpoint b1] [1,2,3] 7" in out and "total = 42" in out, out.count("\n"))
    aria("debug", "exceptions", "uncaught")
    aria("click", "Boom")
    aria("wait", "0.3")
    out, _, _ = aria("debug", "hits", "-n", "1")
    check("pause on exception (auto-resumed)", "TypeError" in out and "boom @" in out)
    aria("debug", "clear")
    out, _, _ = aria("eval", "1+1")
    check("page not left paused", out.strip() == "2")
    out, _, _ = aria("debug", "source", "app.js", "--line", "2", "--context", "0")
    check("debug source", "reduce" in out)

    out, _, _ = aria("perf")
    check("perf metrics", "TTFB" in out and "FCP" in out and "JS heap" in out)
    aria("perf", "start")
    aria("reload")
    trace = tmp / "trace.json"
    out, _, _ = aria("perf", "stop", str(trace))
    check("perf trace", trace.exists() and "Main thread" in out, out.strip().splitlines()[-1][:80])

    out, _, _ = aria("storage", "local", "get", "user")
    out2, _, _ = aria("storage", "idb", "appdb", "notes")
    out3, _, _ = aria("storage")
    check("storage local / idb / overview", '"id": 7' in out and "first note" in out2 and "IndexedDB      appdb" in out3)
    srv.shutdown()


if __name__ == "__main__":
    main()
