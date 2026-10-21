"""
DevTools-panel parity for ariad, mixed into the Daemon class.

  Network      `network` list with ids/timing/size, `req <id>` (headers, payload, response
               headers, body, --curl, --save), `har <path>` (HAR 1.2, opens in DevTools)
  WebSockets   `ws` (sockets + frames sent/received)
  Sources      `debug break|exceptions|hits|list|clear|scripts|source` over CDP: logpoints
               and breakpoints that capture stack + locals, then auto-resume (never hangs the page)
  Performance  `perf` (nav timing, FCP/LCP/CLS/INP, long tasks, resources, CDP runtime metrics),
               `perf start|stop [path]` (Chrome trace: loads in DevTools > Performance)
  Application  `storage [local|session|cookies|idb|cache|sw]` read/write

Everything is collected per tab and survives between CLI calls like console/network did.
"""

import asyncio
import base64
import collections
import json
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

BODY_TYPES = {"xhr", "fetch", "document", "eventsource"}  # bodies cached eagerly (others: lazily)
BODY_MAX = 1_000_000  # per response
BODY_BUDGET = 40_000_000  # per tab
TEXTY = re.compile(r"json|text|javascript|xml|html|x-www-form-urlencoded|graphql|svg|csv", re.I)

# Started at document start in every frame so buffered-unfriendly entries (long tasks, INP) are kept.
PERF_INIT_JS = r"""
(() => { try {
  if (window.__ariaPerf) return;
  const s = {lcp: 0, lcpEl: '', cls: 0, lt: [], inp: 0};
  Object.defineProperty(window, '__ariaPerf', {value: s, enumerable: false});
  const o = (type, fn, extra) => { try { new PerformanceObserver(l => l.getEntries().forEach(fn)).observe(Object.assign({type, buffered: true}, extra || {})); } catch (e) {} };
  o('largest-contentful-paint', e => { s.lcp = e.startTime; const el = e.element; s.lcpEl = el ? el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') : (e.url || ''); });
  o('layout-shift', e => { if (!e.hadRecentInput) s.cls += e.value; });
  o('longtask', e => { s.lt.push([Math.round(e.startTime), Math.round(e.duration)]); if (s.lt.length > 300) s.lt.shift(); });
  o('event', e => { if (e.interactionId) s.inp = Math.max(s.inp, e.duration); }, {durationThreshold: 16});
} catch (e) {} })();
"""

PERF_JS = r"""
() => {
  const nav = performance.getEntriesByType('navigation')[0] || null;
  const paint = Object.fromEntries(performance.getEntriesByType('paint').map(p => [p.name, p.startTime]));
  const s = window.__ariaPerf || {};
  const res = performance.getEntriesByType('resource').map(r => [r.initiatorType, r.name, Math.round(r.duration), r.transferSize || 0, r.encodedBodySize || 0]);
  return {
    url: location.href,
    nav: nav && {ttfb: nav.responseStart, dcl: nav.domContentLoadedEventEnd, load: nav.loadEventEnd, type: nav.type,
                 transfer: nav.transferSize, proto: nav.nextHopProtocol, dns: nav.domainLookupEnd - nav.domainLookupStart,
                 connect: nav.connectEnd - nav.connectStart, redirect: nav.redirectEnd - nav.redirectStart},
    fcp: paint['first-contentful-paint'] || null, lcp: s.lcp || null, lcpEl: s.lcpEl || '', cls: s.cls == null ? null : s.cls,
    inp: s.inp || 0, lt: s.lt || [], res,
  };
}
"""

STORAGE_JS = r"""
([which, op, key, value]) => {
  const s = which === 'local' ? localStorage : sessionStorage;
  if (op === 'get') return s.getItem(key);
  if (op === 'set') { s.setItem(key, value); return 'ok'; }
  if (op === 'del') { s.removeItem(key); return 'ok'; }
  if (op === 'clear') { const n = s.length; s.clear(); return 'cleared ' + n; }
  const out = []; for (let i = 0; i < s.length; i++) { const k = s.key(i); out.push([k, s.getItem(k)]); } return out;
}
"""

STORAGE_SUMMARY_JS = r"""
async () => {
  const len = s => { try { let b = 0; for (let i = 0; i < s.length; i++) { const k = s.key(i); b += k.length + (s.getItem(k) || '').length; } return [s.length, b]; } catch (e) { return [0, 0]; } };
  let idb = null, caches_ = null, sw = null, est = null;
  try { idb = indexedDB.databases ? (await indexedDB.databases()).map(d => d.name) : null; } catch (e) {}
  try { caches_ = self.caches ? await caches.keys() : null; } catch (e) {}
  try { sw = navigator.serviceWorker ? (await navigator.serviceWorker.getRegistrations()).map(r => r.scope) : null; } catch (e) {}
  try { est = navigator.storage && await navigator.storage.estimate(); } catch (e) {}
  return {origin: location.origin, local: len(localStorage), session: len(sessionStorage), idb, caches: caches_, sw,
          usage: est && est.usage, quota: est && est.quota};
}
"""

IDB_JS = r"""
async ([db, store, n, cap]) => {
  const safe = v => { let t; try { t = typeof v === 'string' ? v : JSON.stringify(v); } catch (e) { t = String(v); } t = t === undefined ? String(v) : t; return t.length > cap ? t.slice(0, cap) + '…' : t; };
  const open = name => new Promise((res, rej) => { const r = indexedDB.open(name);
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    r.onupgradeneeded = () => { r.transaction.abort(); rej(new Error('no database ' + name)); }; });
  const count = (h, s) => new Promise(r => { try { const q = h.transaction(s).objectStore(s).count(); q.onsuccess = () => r(q.result); q.onerror = () => r(-1); } catch (e) { r(-1); } });
  if (!db) {
    if (!indexedDB.databases) return {error: 'indexedDB.databases() unsupported'};
    const out = [];
    for (const d of await indexedDB.databases()) {
      try { const h = await open(d.name); const stores = []; for (const s of h.objectStoreNames) stores.push([s, await count(h, s)]); h.close(); out.push({name: d.name, version: d.version, stores}); }
      catch (e) { out.push({name: d.name, error: String(e)}); }
    }
    return {dbs: out};
  }
  const h = await open(db);
  try {
    if (!store) {
      const out = [];
      for (const s of h.objectStoreNames) { const os = h.transaction(s).objectStore(s);
        out.push({name: s, keyPath: os.keyPath, autoIncrement: os.autoIncrement, indexes: [...os.indexNames], count: await count(h, s)}); }
      return {stores: out};
    }
    const os = h.transaction(store).objectStore(store);
    const rows = await new Promise((res, rej) => { const out = []; const q = os.openCursor();
      q.onsuccess = () => { const c = q.result; if (!c || out.length >= n) return res(out); out.push([safe(c.key), safe(c.value)]); c.continue(); };
      q.onerror = () => rej(q.error); });
    return {rows, total: await count(h, store)};
  } finally { h.close(); }
}
"""

CACHE_JS = r"""
async ([name, n]) => {
  if (!self.caches) return {error: 'no CacheStorage on this origin (needs https)'};
  const keys = await caches.keys();
  if (!name) { const out = []; for (const k of keys) { const reqs = await (await caches.open(k)).keys(); out.push([k, reqs.length, reqs.slice(0, 3).map(r => r.url)]); } return {caches: out}; }
  if (!keys.includes(name)) return {error: 'no cache ' + name + '; have: ' + keys.join(', ')};
  const reqs = await (await caches.open(name)).keys();
  return {entries: reqs.slice(0, n).map(r => r.method + ' ' + r.url), total: reqs.length};
}
"""

SW_JS = r"""
async (unregister) => {
  if (!navigator.serviceWorker) return [];
  const regs = await navigator.serviceWorker.getRegistrations();
  if (unregister) { for (const r of regs) await r.unregister(); }
  return regs.map(r => ({scope: r.scope, script: (r.active || r.waiting || r.installing || {}).scriptURL || '',
    state: r.active ? r.active.state : (r.waiting ? 'waiting' : (r.installing ? 'installing' : '-'))}));
}
"""

LOGPOINT_FMT = "(v => { try { return v !== null && typeof v === 'object' ? JSON.stringify(v).slice(0, 800) : String(v); } catch (e) { return String(v); } })"


class NetEntry:
    __slots__ = ("id", "method", "url", "rtype", "status", "status_text", "req_headers", "post", "post_bytes",
                 "resp_headers", "ms", "size", "failure", "started", "request", "response", "body", "body_note")

    def __init__(self, rid, req):
        self.id = rid
        self.method = req.method
        self.url = req.url
        self.rtype = req.resource_type
        self.status = None
        self.status_text = ""
        self.req_headers = dict(req.headers)
        self.post, self.post_bytes = None, None
        try:
            self.post = req.post_data
        except Exception:
            try:
                buf = req.post_data_buffer
                self.post_bytes = buf
                self.post = f"<binary {len(buf):,} B>" if buf else None
            except Exception:
                pass
        self.resp_headers = {}
        self.ms = None
        self.size = None
        self.failure = None
        self.started = time.time()
        self.request = req
        self.response = None
        self.body = None
        self.body_note = ""


def human(n):
    if n is None:
        return "-"
    for unit in ("B", "kB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def ms_s(v):
    if v is None:
        return "-"
    return f"{v:.0f}ms" if v < 1000 else f"{v / 1000:.2f}s"


def shorten(u, n=160):
    return u if len(u) <= n else u[: n - 10] + "…" + u[-9:]


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def remote_desc(v):
    """CDP RemoteObject → short readable value."""
    if not isinstance(v, dict):
        return str(v)
    t = v.get("type")
    if "value" in v and t != "object":
        s = json.dumps(v["value"], ensure_ascii=False) if t == "string" else str(v["value"])
        return s if len(s) <= 160 else s[:150] + "…"
    if t == "undefined":
        return "undefined"
    pv = v.get("preview")
    if pv and pv.get("properties") is not None:
        props = ", ".join(
            (f"{p['name']}: {p.get('value', p.get('type'))}" if pv.get("subtype") != "array" else str(p.get("value", p.get("type"))))
            for p in pv["properties"][:8]
        )
        more = "…" if pv.get("overflow") else ""
        br = "[]" if pv.get("subtype") == "array" else "{}"
        lead = v.get("description", "") + " " if pv.get("subtype") != "array" and v.get("className") not in ("Object",) else ""
        return f"{lead}{br[0]}{props}{more}{br[1]}"[:200]
    return (v.get("description") or v.get("className") or t or "?")[:200]


class DevtoolsMixin:
    # ------------------------------------------------------------ wiring
    def devtools_attach(self, tab):
        tab.reqs = collections.deque()
        tab.req_by = {}
        tab.req_seq = 0
        tab.body_bytes = 0
        tab.ws = collections.deque(maxlen=800)
        tab.sockets = {}
        tab.cdp = None
        tab.dbg_on = False
        tab.scripts = {}  # scriptId -> (url, lines, chars)
        tab.bps = {}  # b1 -> {...}
        tab.bp_seq = 0
        tab.hits = collections.deque(maxlen=100)
        page = tab.page
        page.on("request", lambda r: self._net_req(tab, r))
        page.on("response", lambda r: self._net_resp(tab, r))
        page.on("requestfinished", lambda r: asyncio.ensure_future(self._net_done(tab, r)))
        page.on("requestfailed", lambda r: self._net_fail(tab, r))
        page.on("websocket", lambda ws: self._ws_open(tab, ws))
        asyncio.ensure_future(self._perf_enable(tab))

    async def _perf_enable(self, tab):
        try:  # start CDP runtime counters (script/layout/style time) from tab creation
            await (await self.cdp(tab)).send("Performance.enable")
        except Exception:
            pass

    def _net_req(self, tab, r):
        tab.req_seq += 1
        e = NetEntry(tab.req_seq, r)
        if len(tab.reqs) >= 600:
            old = tab.reqs.popleft()
            tab.req_by.pop(old.request, None)
            if old.body:
                tab.body_bytes -= len(old.body)
        tab.reqs.append(e)
        tab.req_by[r] = e

    def _net_resp(self, tab, resp):
        e = tab.req_by.get(resp.request)
        if e:
            e.status, e.status_text, e.response = resp.status, resp.status_text, resp
            e.resp_headers = dict(resp.headers)

    def _net_fail(self, tab, r):
        e = tab.req_by.get(r)
        if e:
            e.failure = r.failure or "failed"
            try:
                end = r.timing.get("responseEnd", -1)
                e.ms = end if end and end > 0 else (time.time() - e.started) * 1000
            except Exception:
                pass

    async def _net_done(self, tab, r):
        e = tab.req_by.get(r)
        if not e:
            return
        try:
            end = r.timing.get("responseEnd", -1)
            e.ms = end if end and end > 0 else (time.time() - e.started) * 1000
        except Exception:
            pass
        try:
            e.req_headers = await r.all_headers()
            resp = e.response or await r.response()
            if resp:
                e.resp_headers = await resp.all_headers()
            e.size = (await r.sizes()).get("responseBodySize")
            if resp and e.rtype in BODY_TYPES and not (300 <= (e.status or 0) < 400) and (e.size or 0) <= BODY_MAX \
                    and tab.body_bytes < BODY_BUDGET:
                e.body = await resp.body()
                tab.body_bytes += len(e.body)
        except Exception:
            pass

    async def _body(self, e):
        if e.body is not None:
            return e.body
        if not e.response:
            e.body_note = "no response" + (f" ({e.failure})" if e.failure else "")
            return None
        if 300 <= (e.status or 0) < 400:
            e.body_note = f"redirect → {e.resp_headers.get('location', '?')}"
            return None
        try:
            return await asyncio.wait_for(e.response.body(), 10)
        except Exception as ex:
            e.body_note = f"body unavailable ({str(ex).splitlines()[0][:120]}); Chrome drops bodies after the page navigates — re-run the action and read it sooner"
            return None

    def _ctype(self, e):
        return e.resp_headers.get("content-type", "")

    def _decode(self, e, body, raw=False):
        ct = self._ctype(e)
        if body is None:
            return None
        if TEXTY.search(ct) or not ct:
            try:
                txt = body.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if not raw and ("json" in ct or txt[:1] in "{["):
                try:
                    return json.dumps(json.loads(txt), indent=1, ensure_ascii=False)
                except Exception:
                    pass
            return txt
        return None

    def _req_line(self, e):
        st = "FAIL" if e.failure else (str(e.status) if e.status else "…")
        tail = f"  ✗ {e.failure}" if e.failure else ""
        return f"#{e.id} {st} {e.method} {e.rtype} {ms_s(e.ms)} {human(e.size)} {shorten(e.url, 220)}{tail}"

    def _entry(self, tab, rid):
        rid = str(rid).lstrip("#")
        for e in tab.reqs:
            if str(e.id) == rid:
                return e
        raise self.usage(f"no request #{rid} in this tab (log keeps the last 600); run `network`")

    # ------------------------------------------------------------ parsers
    def devtools_parsers(self, mk, reads):
        p = mk("network", "Network log with ids, status, type, timing, size (see `req <id>` for details)")
        p.add_argument("--filter", default=None, help="URL/line substring; a|b for any")
        p.add_argument("--type", default=None, help="xhr,fetch,document,script,… or 'api' (= xhr,fetch)")
        p.add_argument("--method", default=None)
        p.add_argument("--failed", action="store_true", help="only 4xx/5xx/failed")
        p.add_argument("--slow", type=int, default=None, help="only requests slower than N ms")
        p.add_argument("--clear", action="store_true")
        p.add_argument("-n", type=int, default=50)

        p = mk("req", "One request in full: headers, payload, response headers, body (--curl, --save)")
        p.add_argument("id")
        p.add_argument("--headers", action="store_true", help="headers + payload only")
        p.add_argument("--body", action="store_true", help="response body only")
        p.add_argument("--curl", action="store_true", help="print as a curl command (includes cookies)")
        p.add_argument("--save", default=None, help="write the raw response body to this file")
        p.add_argument("--raw", action="store_true", help="don't pretty-print JSON")
        p.add_argument("--max", type=int, default=None)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--filter", default=None)

        p = mk("har", "Write this tab's network log as HAR 1.2 (import in DevTools > Network)")
        p.add_argument("path")
        p.add_argument("--bodies", action="store_true", help="also fetch bodies not cached yet (slower)")

        p = mk("ws", "WebSockets: open sockets and frames sent (↑) / received (↓)")
        p.add_argument("--id", default=None, help="only this socket (ws1…)")
        p.add_argument("--filter", default=None)
        p.add_argument("--full", action="store_true", help="don't truncate frames at 300 chars")
        p.add_argument("--clear", action="store_true")
        p.add_argument("-n", type=int, default=40)

        p = mk("debug", "Debugger: break <url|scriptId>:<line>[:<col>] [--log expr] [--cond expr] · exceptions all|uncaught|off · hits · list · clear [bN] · scripts · source <url|id> [--line N]")
        p.add_argument("action", choices=["break", "exceptions", "hits", "list", "clear", "scripts", "source"])
        p.add_argument("args", nargs="*")
        p.add_argument("--log", default=None, help="logpoint: log this expression (comma-separated ok) instead of pausing")
        p.add_argument("--cond", default=None, help="only hit when this expression is truthy")
        p.add_argument("--max-hits", dest="max_hits", type=int, default=20, help="auto-remove a pausing breakpoint after N hits")
        p.add_argument("--line", type=int, default=None)
        p.add_argument("--col", type=int, default=None)
        p.add_argument("--context", type=int, default=15)
        p.add_argument("--all", action="store_true", help="scripts: include inline/anonymous")
        p.add_argument("--clear", action="store_true")
        p.add_argument("-n", type=int, default=10)
        p.add_argument("--max", type=int, default=None)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--filter", default=None)

        p = mk("perf", "Performance: metrics (default) · start [--screenshots] · stop [path] (Chrome trace + summary)")
        p.add_argument("action", nargs="?", default="metrics", choices=["metrics", "start", "stop"])
        p.add_argument("path", nargs="?", default=None)
        p.add_argument("--screenshots", action="store_true")

        p = mk("storage", "Application: overview · local|session [get|set|del|clear …] · cookies [set|del …] · idb [db] [store] · cache [name] · sw [unregister]")
        p.add_argument("kind", nargs="?", default=None, choices=["local", "session", "cookies", "idb", "cache", "sw"])
        p.add_argument("rest", nargs="*")
        p.add_argument("--full", action="store_true", help="don't truncate values")
        p.add_argument("--all", action="store_true", help="cookies: every cookie in the profile, not just this URL")
        p.add_argument("-n", type=int, default=20)
        p.add_argument("--max", type=int, default=None)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--filter", default=None)

    # ------------------------------------------------------------ network
    async def c_network(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        items = list(tab.reqs)
        if o.failed:
            items = [e for e in items if e.failure or (e.status or 0) >= 400]
        if o.type:
            types = {t.strip() for t in o.type.replace("api", "xhr,fetch").split(",")}
            items = [e for e in items if e.rtype in types]
        if o.method:
            items = [e for e in items if e.method.upper() == o.method.upper()]
        if o.slow:
            items = [e for e in items if (e.ms or 0) >= o.slow]
        lines = [self._req_line(e) for e in items]
        if o.filter:
            words = [w.lower() for w in o.filter.split("|")]
            lines = [l for l in lines if any(w in l.lower() for w in words)]
        if o.clear:
            tab.reqs.clear()
            tab.req_by.clear()
            tab.body_bytes = 0
        if not lines:
            return "[no requests]"
        hidden = len(lines) - o.n
        return (f"[{hidden} earlier — -n {len(lines)} for all]\n" if hidden > 0 else "") + "\n".join(lines[-o.n:])

    async def c_req(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        e = self._entry(tab, o.id)
        if o.curl:
            parts = ["curl", shlex.quote(e.url)]
            if e.method != "GET":
                parts += ["-X", e.method]
            for k, v in e.req_headers.items():
                if not k.startswith(":") and k not in ("content-length", "connection"):
                    parts += ["-H", shlex.quote(f"{k}: {v}")]
            if e.post and not e.post_bytes:
                parts += ["--data-raw", shlex.quote(e.post)]
            return " ".join(parts)
        body = None
        if o.save or not o.headers:
            body = await self._body(e)
        if o.save:
            if body is None:
                raise self.usage(e.body_note or "no body")
            path = Path(o.save).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            return f"{path} ({len(body):,} B, {self._ctype(e) or '?'})"
        out = []
        if not o.body:
            st = f"FAILED {e.failure}" if e.failure else f"{e.status} {e.status_text}".strip() if e.status else "pending"
            out.append(f"#{e.id} {e.method} {e.url}\nstatus {st} · type {e.rtype} · {ms_s(e.ms)} · {human(e.size)} · started {iso(e.started)}")
            out.append("\n## Request headers")
            out += [f"{k}: {v}" for k, v in sorted(e.req_headers.items())]
            if e.post:
                post = e.post
                if not o.raw:
                    try:
                        post = json.dumps(json.loads(post), indent=1, ensure_ascii=False)
                    except Exception:
                        pass
                out.append("\n## Payload\n" + post)
            if e.resp_headers:
                out.append("\n## Response headers")
                out += [f"{k}: {v}" for k, v in sorted(e.resp_headers.items())]
        if not o.headers:
            if not o.body:
                out.append("\n## Response body")
            if body is None:
                out.append(f"[{e.body_note or 'no body'}]")
            else:
                txt = self._decode(e, body, o.raw)
                out.append(txt if txt is not None else f"[binary {len(body):,} B, {self._ctype(e) or 'unknown type'} — `req {e.id} --save <file>`]")
        return self.window("\n".join(out), o, f"req {e.id}")

    async def c_har(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        entries = []
        for e in list(tab.reqs):
            body = e.body
            if body is None and o.bodies and e.response:
                body = await self._body(e)
            content = {"size": e.size if e.size is not None else (len(body) if body else 0), "mimeType": self._ctype(e)}
            if body is not None:
                try:
                    content["text"] = body.decode("utf-8")
                except UnicodeDecodeError:
                    content["text"], content["encoding"] = base64.b64encode(body).decode(), "base64"
            req = {
                "method": e.method, "url": e.url, "httpVersion": "HTTP/1.1", "cookies": [], "headersSize": -1,
                "headers": [{"name": k, "value": v} for k, v in e.req_headers.items()],
                "queryString": [], "bodySize": len(e.post or ""),
            }
            if e.post:
                req["postData"] = {"mimeType": e.req_headers.get("content-type", ""), "text": e.post}
            entries.append({
                "startedDateTime": iso(e.started), "time": round(e.ms or 0, 1), "request": req,
                "response": {
                    "status": e.status or 0, "statusText": e.status_text or (e.failure or ""), "httpVersion": "HTTP/1.1",
                    "cookies": [], "headers": [{"name": k, "value": v} for k, v in e.resp_headers.items()],
                    "content": content, "redirectURL": e.resp_headers.get("location", ""), "headersSize": -1,
                    "bodySize": e.size if e.size is not None else -1,
                },
                "cache": {}, "timings": {"send": 0, "wait": round(e.ms or 0, 1), "receive": 0},
                "_resourceType": e.rtype,
            })
        har = {"log": {"version": "1.2", "creator": {"name": "aria", "version": "2"}, "pages": [], "entries": entries}}
        path = Path(o.path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(har, ensure_ascii=False))
        with_body = sum(1 for x in entries if "text" in x["response"]["content"])
        return f"{path} ({len(entries)} requests, {with_body} with bodies, {path.stat().st_size:,} B)"

    # ------------------------------------------------------------ websockets
    def _ws_open(self, tab, ws):
        wid = f"ws{len(tab.sockets) + 1}"
        info = {"url": ws.url, "open": True, "sent": 0, "recv": 0, "t": time.time()}
        tab.sockets[wid] = info

        def frame(d, payload):
            info["sent" if d == "↑" else "recv"] += 1
            txt = f"<binary {len(payload):,} B>" if isinstance(payload, (bytes, bytearray)) else str(payload)
            tab.ws.append((wid, d, time.time(), txt[:20000]))

        ws.on("framesent", lambda p: frame("↑", p))
        ws.on("framereceived", lambda p: frame("↓", p))
        ws.on("socketerror", lambda err: tab.ws.append((wid, "!", time.time(), str(err))))
        ws.on("close", lambda _w: info.update(open=False))

    async def c_ws(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        if not tab.sockets:
            return "[no websockets opened in this tab since it was created]"
        head = [f"{wid} {'open' if s['open'] else 'closed'} ↑{s['sent']} ↓{s['recv']} {shorten(s['url'])}" for wid, s in tab.sockets.items()]
        frames = [f for f in tab.ws if not o.id or f[0] == o.id]
        if o.filter:
            words = [w.lower() for w in o.filter.split("|")]
            frames = [f for f in frames if any(w in f[3].lower() for w in words)]
        lines = []
        for wid, d, t, txt in frames[-o.n:]:
            txt = txt if o.full or len(txt) <= 300 else txt[:300] + f"… [{len(txt):,} chars, --full]"
            lines.append(f"{wid} {d} {datetime.fromtimestamp(t).strftime('%H:%M:%S.%f')[:-3]} {txt}")
        if o.clear:
            tab.ws.clear()
        return "\n".join(head) + "\n---\n" + ("\n".join(lines) or "[no frames]")

    # ------------------------------------------------------------ debugger
    async def cdp(self, tab):
        if tab.cdp is None:
            tab.cdp = await tab.page.context.new_cdp_session(tab.page)
        return tab.cdp

    async def _dbg(self, tab):
        s = await self.cdp(tab)
        if tab.dbg_on:
            return s

        def parsed(p):
            tab.scripts[p["scriptId"]] = (p.get("url") or "", p.get("endLine", 0) + 1, p.get("length", 0))

        s.on("Debugger.scriptParsed", parsed)
        s.on("Debugger.globalObjectCleared", lambda _p: tab.scripts.clear())
        s.on("Debugger.paused", lambda p: asyncio.ensure_future(self._paused(tab, p)))
        await s.send("Debugger.enable")
        tab.dbg_on = True
        await asyncio.sleep(0.05)  # let the initial scriptParsed burst arrive
        return s

    def _loc(self, tab, loc, fallback_url=""):
        url = tab.scripts.get(loc.get("scriptId"), (fallback_url,))[0] or fallback_url or f"script:{loc.get('scriptId')}"
        return f"{shorten(url, 90)}:{loc.get('lineNumber', 0) + 1}:{loc.get('columnNumber', 0) + 1}"

    async def _paused(self, tab, p):
        s = tab.cdp
        hit = {"t": time.time(), "reason": p.get("reason"), "bps": [], "stack": [], "locals": [], "error": None}
        try:
            frames = p.get("callFrames", [])
            byid = {b["cdp"]: k for k, b in tab.bps.items()}
            hit["bps"] = [byid.get(b, b) for b in p.get("hitBreakpoints", [])]
            for f in frames[:8]:
                hit["stack"].append(f"{f.get('functionName') or '(anonymous)'} @ {self._loc(tab, f['location'], f.get('url', ''))}")
            if p.get("reason") in ("exception", "promiseRejection") and p.get("data"):
                hit["error"] = remote_desc(p["data"])
                d = p["data"].get("description")
                if d:
                    hit["error"] = d.splitlines()[0][:300]
            if frames:
                top = frames[0]
                this = top.get("this") or {}
                if this.get("type") == "object" and this.get("className") not in ("Window", None):
                    hit["locals"].append(f"this = {remote_desc(this)}")
                taken = 0
                for sc in top.get("scopeChain", []):
                    if sc["type"] not in ("local", "block", "closure", "catch") or taken >= 40:
                        continue
                    oid = sc.get("object", {}).get("objectId")
                    if not oid:
                        continue
                    props = await s.send("Runtime.getProperties", {"objectId": oid, "ownProperties": True, "generatePreview": True})
                    for pr in props.get("result", [])[:25]:
                        if "value" in pr:
                            hit["locals"].append(f"{sc['type'][:5]} {pr['name']} = {remote_desc(pr['value'])}")
                            taken += 1
                    if sc["type"] == "closure":
                        break  # one closure level is enough; globals are noise
        except Exception as ex:
            hit["locals"].append(f"[capture error: {str(ex)[:120]}]")
        finally:
            try:
                await s.send("Debugger.resume")
            except Exception:
                pass
        tab.hits.append(hit)
        for k in hit["bps"]:
            b = tab.bps.get(k)
            if b:
                b["hits"] += 1
                if b["kind"] == "pause" and b["hits"] >= b["max"]:
                    try:
                        await s.send("Debugger.removeBreakpoint", {"breakpointId": b["cdp"]})
                        b["kind"] = f"pause (removed after {b['max']} hits)"
                    except Exception:
                        pass

    def _script_for(self, tab, ref):
        if ref in tab.scripts:
            return ref, tab.scripts[ref]
        hits = [(sid, v) for sid, v in tab.scripts.items() if v[0] and ref in v[0]]
        if not hits:
            raise self.usage(f"no loaded script matches {ref!r}; `debug scripts --filter …` (scripts list resets on navigation)")
        return hits[-1]

    async def c_debug(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        s = await self._dbg(tab)
        a = o.action

        if a == "break":
            if not o.args:
                raise self.usage("debug break <url-part|scriptId>:<line>[:<col>]  (1-based; see `debug scripts`, `debug source`)")
            m = re.match(r"^(.*?):(\d+)(?::(\d+))?$", o.args[0])
            if not m:
                raise self.usage("location must look like app.js:120 or app.js:1:4567 or 42:10 (scriptId:line)")
            where, line, col = m.group(1), int(m.group(2)) - 1, int(m.group(3)) - 1 if m.group(3) else None
            tab.bp_seq += 1
            key = f"b{tab.bp_seq}"
            if o.log:
                label = json.dumps(f"[logpoint {key}]")
                cond = f"({o.cond}) && " if o.cond else ""
                condition = f"{cond}(console.log({label}, ...[{o.log}].map({LOGPOINT_FMT})), false)"
                kind = "log"
            else:
                condition = o.cond or ""
                kind = "pause"
            if where in tab.scripts:
                loc = {"scriptId": where, "lineNumber": line}
                if col is not None:
                    loc["columnNumber"] = col
                r = await s.send("Debugger.setBreakpoint", {"location": loc, "condition": condition})
                bid, locs = r["breakpointId"], [r.get("actualLocation")]
            else:
                args = {"urlRegex": re.escape(where), "lineNumber": line, "condition": condition}
                if col is not None:
                    args["columnNumber"] = col
                r = await s.send("Debugger.setBreakpointByUrl", args)
                bid, locs = r["breakpointId"], r.get("locations", [])
            tab.bps[key] = {"cdp": bid, "spec": o.args[0], "kind": kind, "cond": o.cond, "log": o.log, "hits": 0, "max": o.max_hits}
            where_s = ", ".join(self._loc(tab, l) for l in locs if l) or "not bound yet (script not loaded, or no code on that line) — binds when a matching script loads"
            how = "logs to `console` / `debug hits`, never pauses" if kind == "log" else \
                f"captures stack + locals into `debug hits`, then resumes; auto-removed after {o.max_hits} hits"
            return f"{key} set at {where_s}\n{how}"

        if a == "exceptions":
            mode = (o.args[0] if o.args else "uncaught").lower()
            state = {"off": "none", "none": "none", "uncaught": "uncaught", "all": "all", "caught": "caught"}.get(mode)
            if not state:
                raise self.usage("debug exceptions all|uncaught|caught|off")
            await s.send("Debugger.setPauseOnExceptions", {"state": state})
            return f"pause on exceptions: {state} (each pause records stack + locals into `debug hits`, then resumes)"

        if a == "hits":
            out = []
            for h in list(tab.hits)[-o.n:]:
                t = datetime.fromtimestamp(h["t"]).strftime("%H:%M:%S.%f")[:-3]
                tag = ",".join(h["bps"]) or h["reason"]
                out.append(f"── {t} {tag}" + (f" · {h['error']}" if h["error"] else ""))
                out += ["  at " + x for x in h["stack"]]
                out += ["  " + x for x in h["locals"]]
            logs = [l for l in tab.console if "[logpoint " in l][-o.n:]
            if logs:
                out.append("── logpoints")
                out += ["  " + l for l in logs]
            if o.clear:
                tab.hits.clear()
            return self.window("\n".join(out), o, "debug hits") if out else "[no hits yet — trigger the code path, e.g. click something, then re-run]"

        if a == "list":
            for k, b in tab.bps.items():
                if b["kind"] == "log":
                    b["hits"] = sum(1 for l in tab.console if f"[logpoint {k}]" in l)
            rows = [f"{k} {b['kind']} {b['spec']} hits={b['hits']}" + (f" cond={b['cond']}" if b["cond"] else "") + (f" log={b['log']}" if b["log"] else "")
                    for k, b in tab.bps.items()]
            return "\n".join(rows) or "[no breakpoints]"

        if a == "clear":
            keys = o.args or list(tab.bps)
            for k in keys:
                b = tab.bps.pop(k, None)
                if b:
                    try:
                        await s.send("Debugger.removeBreakpoint", {"breakpointId": b["cdp"]})
                    except Exception:
                        pass
            if not o.args:
                await s.send("Debugger.setPauseOnExceptions", {"state": "none"})
            return f"cleared {', '.join(keys) or 'nothing'}" + ("" if o.args else "; pause-on-exceptions off")

        if a == "scripts":
            rows = []
            for sid, (url, lines, chars) in tab.scripts.items():
                if not url and not o.all:
                    continue
                if url.startswith(("chrome-extension:", "extensions::")):
                    continue
                rows.append(f"{sid:>6} {lines:>6}L {human(chars):>8} {shorten(url or '(inline)', 200)}")
            hidden = sum(1 for v in tab.scripts.values() if not v[0])
            txt = "\n".join(rows) or "[no scripts]"
            if hidden and not o.all:
                txt += f"\n[{hidden} inline/anonymous scripts hidden — --all]"
            return self.window(txt, o, "debug scripts")

        if a == "source":
            if not o.args:
                raise self.usage("debug source <url-part|scriptId> [--line N] [--col C] [--context 15]")
            sid, (url, _, _) = self._script_for(tab, o.args[0])
            src = (await s.send("Debugger.getScriptSource", {"scriptId": sid}))["scriptSource"]
            lines = src.split("\n")
            head = f"script {sid} {url or '(inline)'} · {len(lines):,} lines · {len(src):,} chars"
            if o.line:
                lo, hi = max(1, o.line - o.context), min(len(lines), o.line + o.context)
            else:
                lo, hi = 1, min(len(lines), 2 * o.context + 1)
            out = [head]
            for n in range(lo, hi + 1):
                ln = lines[n - 1]
                if o.col and n == o.line:
                    c = o.col - 1
                    ln = ("…" if c > 150 else "") + ln[max(0, c - 150):c] + "⟨HERE⟩" + ln[c:c + 150] + ("…" if c + 150 < len(ln) else "")
                elif len(ln) > 300:
                    ln = ln[:300] + f"… [{len(ln):,} chars; minified? use --line {n} --col <N>]"
                out.append(f"{'>' if n == o.line else ' '}{n:>6}│ {ln}")
            return self.window("\n".join(out), o, "debug source")

    # ------------------------------------------------------------ performance
    async def c_perf(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        if o.action == "start":
            if getattr(self, "tracing_tab", None):
                raise self.usage(f"already tracing {self.tracing_tab}; `perf stop` first")
            await self.browser.start_tracing(page=tab.page, screenshots=o.screenshots)
            self.tracing_tab = tab.id
            return f"tracing {tab.id} — do the thing (open/click/scroll), then `perf stop [path]`"
        if o.action == "stop":
            if not getattr(self, "tracing_tab", None):
                raise self.usage("not tracing; `perf start` first")
            data = await self.browser.stop_tracing()
            self.tracing_tab = None
            path = Path(o.path).expanduser().resolve() if o.path else self.state_dir / "traces" / f"{tab.id}-{int(time.time())}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return f"{path} ({len(data):,} B) — load in Chrome DevTools > Performance, or ui.perfetto.dev\n" + summarize_trace(data)

        d = await tab.page.evaluate(PERF_JS)
        out = [f"{shorten(d['url'], 120)}"]
        n = d.get("nav")
        if n:
            out.append(f"Navigation ({n['type']}, {n['proto'] or '?'}): TTFB {ms_s(n['ttfb'])} · DOMContentLoaded {ms_s(n['dcl'])} · "
                       f"load {ms_s(n['load']) if n['load'] else 'pending'} · document {human(n['transfer'])}"
                       + (f" · redirect {ms_s(n['redirect'])}" if n["redirect"] else "")
                       + f" · dns {ms_s(n['dns'])} · connect {ms_s(n['connect'])}")
        lt = d["lt"]
        cls = d["cls"]
        out.append(f"Vitals: FCP {ms_s(d['fcp'])} · LCP {ms_s(d['lcp'])}{(' (' + shorten(d['lcpEl'], 60) + ')') if d['lcpEl'] else ''} · "
                   f"CLS {cls:.3f} · INP {ms_s(d['inp']) if d['inp'] else '- (no interactions)'} · "
                   f"long tasks {len(lt)}" + (f" (total {ms_s(sum(x[1] for x in lt))}, worst {ms_s(max(x[1] for x in lt))})" if lt else ""))
        res = d["res"]
        if res:
            by = collections.defaultdict(lambda: [0, 0])
            for t, _, _, sz, _ in res:
                by[t][0] += 1
                by[t][1] += sz
            kinds = " · ".join(f"{k} {v[0]} ({human(v[1])})" for k, v in sorted(by.items(), key=lambda kv: -kv[1][1]))
            out.append(f"Resources: {len(res)} · {human(sum(r[3] for r in res))} transferred · {kinds}")
            out.append("Slowest: " + " | ".join(f"{ms_s(r[2])} {r[0]} {shorten(r[1], 70)}" for r in sorted(res, key=lambda r: -r[2])[:3]))
            out.append("Largest: " + " | ".join(f"{human(r[4])} {r[0]} {shorten(r[1], 70)}" for r in sorted(res, key=lambda r: -r[4])[:3]))
        try:
            s = await self.cdp(tab)
            m = {x["name"]: x["value"] for x in (await s.send("Performance.getMetrics"))["metrics"]}
            out.append(f"Runtime: JS heap {human(m.get('JSHeapUsedSize'))}/{human(m.get('JSHeapTotalSize'))} · DOM nodes {int(m.get('Nodes', 0)):,} · "
                       f"listeners {int(m.get('JSEventListeners', 0)):,} · script {ms_s(m.get('ScriptDuration', 0) * 1000)} · "
                       f"layout {int(m.get('LayoutCount', 0))}× {ms_s(m.get('LayoutDuration', 0) * 1000)} · "
                       f"style {int(m.get('RecalcStyleCount', 0))}× {ms_s(m.get('RecalcStyleDuration', 0) * 1000)} · "
                       f"tasks {ms_s(m.get('TaskDuration', 0) * 1000)} · frames {int(m.get('Frames', 0))}")
        except Exception as ex:
            out.append(f"Runtime: unavailable ({str(ex)[:80]})")
        if lt:
            out.append("Long tasks (start→dur): " + ", ".join(f"{s}ms→{dur}ms" for s, dur in sorted(lt, key=lambda x: -x[1])[:6]))
        return "\n".join(out)

    # ------------------------------------------------------------ application
    async def c_storage(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        page = tab.page
        cap = 10**9 if o.full else 120

        def trunc(v):
            v = "" if v is None else str(v)
            return v if len(v) <= cap else v[:cap] + f"… [{len(v):,} chars, --full]"

        k, r = o.kind, o.rest
        if k is None:
            d = await page.evaluate(STORAGE_SUMMARY_JS)
            cookies = await page.context.cookies(page.url)
            lines = [f"origin {d['origin']}",
                     f"localStorage   {d['local'][0]} keys ({human(d['local'][1] * 2)})",
                     f"sessionStorage {d['session'][0]} keys ({human(d['session'][1] * 2)})",
                     f"cookies        {len(cookies)} for this URL ({sum(1 for c in cookies if c.get('httpOnly'))} HttpOnly)",
                     f"IndexedDB      {', '.join(d['idb']) if d['idb'] else ('none' if d['idb'] is not None else 'n/a')}",
                     f"Cache Storage  {', '.join(d['caches']) if d['caches'] else ('none' if d['caches'] is not None else 'n/a')}",
                     f"ServiceWorkers {', '.join(d['sw']) if d['sw'] else 'none'}"]
            if d.get("quota"):
                lines.append(f"quota          {human(d['usage'])} used of {human(d['quota'])}")
            lines.append("details: storage local|session|cookies|idb|cache|sw")
            return "\n".join(lines)

        if k in ("local", "session"):
            op = r[0] if r else "list"
            if op not in ("list", "get", "set", "del", "clear"):
                raise self.usage(f"storage {k} [get <key> | set <key> <value> | del <key> | clear]")
            need = {"get": 2, "set": 3, "del": 2, "clear": 1}.get(op, 0)
            if len(r) < need:
                raise self.usage(f"storage {k} {op} needs {need - 1} argument(s)")
            res = await page.evaluate(STORAGE_JS, [k, op, r[1] if len(r) > 1 else None, r[2] if len(r) > 2 else None])
            if op == "get":
                return "[missing]" if res is None else self.window(res if o.full else self._pretty(res), o, f"storage {k} get")
            if op != "list":
                return str(res)
            if not res:
                return f"[{k}Storage empty]"
            return self.window("\n".join(f"{key} = {trunc(v)}" for key, v in res), o, f"storage {k}")

        if k == "cookies":
            ctx = page.context
            op = r[0] if r else "list"
            if op == "set":
                if len(r) < 3:
                    raise self.usage("storage cookies set <name> <value>  (scoped to this page's URL; headless profile only)")
                await ctx.add_cookies([{"name": r[1], "value": r[2], "url": page.url}])
                return f"set {r[1]} for {page.url} (headless copy only; real Chrome unchanged, `resync` restores)"
            if op in ("del", "clear"):
                victims = [c for c in await ctx.cookies(page.url) if op == "clear" or c["name"] == (r[1] if len(r) > 1 else None)]
                for c in victims:
                    await ctx.clear_cookies(name=c["name"], domain=c["domain"], path=c["path"])
                return f"removed {len(victims)} cookie(s) from the headless profile (real Chrome unchanged, `resync` restores)"
            cs = await (ctx.cookies() if o.all else ctx.cookies(page.url))
            rows = []
            for c in sorted(cs, key=lambda c: (c["domain"], c["name"])):
                exp = "session" if c.get("expires", -1) in (-1, None) else datetime.fromtimestamp(c["expires"]).strftime("%Y-%m-%d")
                flags = " ".join(f for f, on in (("HttpOnly", c.get("httpOnly")), ("Secure", c.get("secure"))) if on)
                rows.append(f"{c['name']} = {trunc(c['value'])}  · {c['domain']}{c['path']} · {exp} · {flags} {c.get('sameSite', '')}".rstrip())
            return self.window("\n".join(rows) or "[no cookies]", o, "storage cookies")

        if k == "idb":
            d = await page.evaluate(IDB_JS, [r[0] if r else None, r[1] if len(r) > 1 else None, o.n, cap])
            if d.get("error"):
                return d["error"]
            if "dbs" in d:
                rows = [f"{x['name']} v{x.get('version', '?')}: " + (x.get("error") or ", ".join(f"{s}({c})" for s, c in x["stores"])) for x in d["dbs"]]
                return "\n".join(rows) or "[no IndexedDB databases]"
            if "stores" in d:
                return "\n".join(f"{x['name']} ({x['count']} rows) keyPath={x['keyPath']} autoInc={x['autoIncrement']} indexes={x['indexes']}" for x in d["stores"]) or "[no stores]"
            rows = [f"{key} → {val}" for key, val in d["rows"]]
            more = f"\n[{d['total'] - len(rows)} more — -n]" if d["total"] > len(rows) else ""
            return self.window("\n".join(rows) + more or "[empty]", o, "storage idb")

        if k == "cache":
            d = await page.evaluate(CACHE_JS, [r[0] if r else None, o.n])
            if d.get("error"):
                return d["error"]
            if "caches" in d:
                return "\n".join(f"{n} ({c} entries) e.g. {', '.join(shorten(u, 80) for u in ex)}" for n, c, ex in d["caches"]) or "[no caches]"
            return "\n".join(d["entries"]) + (f"\n[{d['total'] - len(d['entries'])} more — -n]" if d["total"] > len(d["entries"]) else "")

        if k == "sw":
            regs = await page.evaluate(SW_JS, bool(r and r[0] == "unregister"))
            rows = [f"{x['scope']} · {x['state']} · {x['script']}" for x in regs]
            return ("unregistered:\n" if r and r[0] == "unregister" else "") + ("\n".join(rows) or "[no service workers]")

    def _pretty(self, v):
        try:
            return json.dumps(json.loads(v), indent=1, ensure_ascii=False)
        except Exception:
            return v


def summarize_trace(data: bytes) -> str:
    """Main-thread breakdown from a Chrome trace (renderer CrRendererMain threads only)."""
    try:
        doc = json.loads(data)
        evs = doc["traceEvents"] if isinstance(doc, dict) else doc
    except Exception:
        return "[could not parse trace]"
    mains = {(e.get("pid"), e.get("tid")) for e in evs
             if e.get("ph") == "M" and e.get("name") == "thread_name" and e.get("args", {}).get("name") == "CrRendererMain"}
    groups = {
        "scripting": ("EvaluateScript", "FunctionCall", "v8.compile", "v8.callFunction", "TimerFire", "EventDispatch", "FireAnimationFrame", "RunMicrotasks"),
        "rendering": ("Layout", "UpdateLayoutTree", "RecalculateStyles", "PrePaint", "Layerize"),
        "painting": ("Paint", "PaintImage", "Rasterize", "CompositeLayers", "Commit"),
        "parsing": ("ParseHTML", "ParseAuthorStyleSheet"),
        "gc": ("MajorGC", "MinorGC", "V8.GC_SCAVENGER", "BlinkGC.AtomicPhase"),
    }
    tot = collections.Counter()
    tasks = []
    t0, t1 = None, None
    for e in evs:
        if (e.get("pid"), e.get("tid")) not in mains or e.get("ph") != "X":
            continue
        dur = e.get("dur", 0) / 1000
        ts = e.get("ts", 0) / 1000
        t0 = ts if t0 is None else min(t0, ts)
        t1 = ts + dur if t1 is None else max(t1, ts + dur)
        name = e.get("name", "")
        if name == "RunTask":
            tasks.append(dur)
        for g, names in groups.items():
            if name in names:
                tot[g] += dur
    if not tasks:
        return "[no renderer main-thread tasks in trace]"
    long = [t for t in tasks if t >= 50]
    span = (t1 - t0) if t0 is not None else 0
    parts = " · ".join(f"{g} {ms_s(v)}" for g, v in tot.most_common())
    return (f"Main thread over {ms_s(span)}: busy {ms_s(sum(tasks))} in {len(tasks):,} tasks · long tasks (≥50ms) {len(long)}"
            + (f", worst {ms_s(max(long))}, blocking {ms_s(sum(t - 50 for t in long))}" if long else "")
            + f"\nBy category (nested, approx): {parts or '-'}")
