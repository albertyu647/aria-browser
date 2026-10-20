#!/usr/bin/env python3
"""
ariad — persistent, silent, authenticated headless Chrome for agents.

One background process keeps a real Chrome (channel="chrome", headless) warm with one
context per Chrome profile (e.g. work / personal), so every command after the first is a
round trip over localhost instead of a 4-5 s browser launch. Tabs, refs, console and
network logs survive between calls.

Transport: TCP on 127.0.0.1 (cross-platform), port + random token written to
~/.cache/aria-browser/ariad.json (mode 600). One JSON line in, one JSON line out:
    {"token": "...", "argv": ["open", "https://..."], "profile": "personal"}
    {"ok": true, "out": "...", "code": 0}
"""

import argparse
import asyncio
import collections
import difflib
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).parent))
from compact import compact, dominant_prefix  # noqa: E402
import profiles as profiles_cfg  # noqa: E402

STATE_DIR = Path(os.environ.get("ARIA_HOME", Path.home() / ".cache" / "aria-browser"))
INFO_FILE = STATE_DIR / "ariad.json"
SYNC_SCRIPT = Path(__file__).parent / "sync_auth.py"
IDLE_SECS = float(os.environ.get("ARIA_IDLE", "1800"))
BLOCK_TYPES = {t for t in os.environ.get("ARIA_BLOCK", "").split(",") if t}
PER_SITE_CAP = 170
TOTAL_CAP = 3000
DEFAULT_MAX = int(os.environ.get("ARIA_MAX_CHARS", "24000"))

LOGIN_WALL = re.compile(
    r"<dialog: (Log ?in|Sign ?in)[^>]*>|\[textbox: (Password|Email or username|Phone, email)"
    r"|Sign in to continue|You've been blocked by network security",
    re.I,
)
LOGIN_URL = re.compile(r"/(login|signin|sign-in|i/flow/login|uas/login|authwall)\b|accounts\.google\.com", re.I)
REF_RE = re.compile(r"^(?:f\d+)?e\d+$")

TEXT_JS = r"""
(sel) => {
  let pick;
  if (sel) pick = document.querySelector(sel);
  else {
    const m = document.querySelector('main, [role=main]');
    const bl = (document.body && document.body.innerText || '').length;
    // use <main> only when it really holds the page (Reddit's <main> is a 9% fragment)
    pick = (m && m.innerText.length >= 0.4 * bl) ? m : document.body;
  }
  if (!pick) return '';
  let t = pick.innerText || '';
  return t.replace(/[ \t ]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}
"""

SETTLE_JS = r"""
async ([quietMs, maxMs]) => {
  const t0 = performance.now();
  let last = -1, stableSince = performance.now();
  while (performance.now() - t0 < maxMs) {
    const b = document.body;
    const sig = b ? b.innerText.length : 0;
    // a visible spinner / busy region means content is still on its way
    const busy = [...document.querySelectorAll('[role=progressbar],[aria-busy=true]')]
      .some(e => { const r = e.getBoundingClientRect(); return r.width >= 8 && r.height >= 8 && r.bottom > 0 && r.top < innerHeight; });
    if (busy) { stableSince = performance.now(); await new Promise(r => setTimeout(r, 100)); continue; }
    // tolerate tiny churn (live counters, relative timestamps) so busy feeds still count as settled
    if (Math.abs(sig - last) > Math.max(40, 0.02 * last)) { last = sig; stableSince = performance.now(); }
    else if (sig > 200 && performance.now() - stableSince >= quietMs) return Math.round(performance.now() - t0);
    await new Promise(r => setTimeout(r, 100));
  }
  return -1;
}
"""


def tok(s: str) -> int:
    return (len(s) + 3) // 4


def storage_path(profile: str) -> Path:
    return profiles_cfg.storage_path(profile)


def canon_profile(profile: str) -> str:
    return profiles_cfg.resolve(profile or profiles_cfg.default_profile())


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(f"{self.prog}: {message}")

    def exit(self, status=0, message=None):
        raise UsageError(message or self.format_help())


class Tab:
    def __init__(self, tid, page, profile):
        self.id = tid
        self.page = page
        self.profile = profile
        self.console = collections.deque(maxlen=400)
        self.network = collections.deque(maxlen=600)
        self.last_snap = None
        self.dialogs = collections.deque(maxlen=20)
        self.ref_prefix = ""

    def ref(self, r):
        return (self.ref_prefix + r) if r.startswith("e") else r


class Daemon:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.ua = None
        self.contexts = {}  # profile -> context
        self.tabs = {}  # tid -> Tab
        self.current = {}  # profile -> tid
        self.seq = 0
        self.lock = asyncio.Lock()
        self.last_used = time.time()
        self.resynced_at = {}
        self.started = time.time()
        self.block_media = True

    # ------------------------------------------------------------------ setup
    async def ensure_browser(self):
        if self.browser and self.browser.is_connected():
            return
        if not self.pw:
            self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"],
        )
        major = self.browser.version.split(".")[0]
        plat = {
            "darwin": "Macintosh; Intel Mac OS X 10_15_7",
            "win32": "Windows NT 10.0; Win64; x64",
        }.get(sys.platform, "X11; Linux x86_64")
        self.ua = f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
        self.contexts.clear()

    async def ensure_context(self, profile):
        await self.ensure_browser()
        ctx = self.contexts.get(profile)
        if ctx:
            return ctx
        sp = storage_path(profile)
        if not sp.exists():
            await self.run_sync()
        ctx = await self.browser.new_context(viewport={"width": 1280, "height": int(os.environ.get("ARIA_VH", "900"))}, user_agent=self.ua, locale="en-US")
        if sp.exists():
            await self.load_cookies(ctx, sp)
        await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        ctx.on("page", lambda pg, prof=profile: self._register(pg, prof, make_current=True))
        if BLOCK_TYPES:
            async def _route(route):
                if self.block_media and route.request.resource_type in BLOCK_TYPES:
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", _route)
        self.contexts[profile] = ctx
        return ctx

    def _register(self, page, profile, make_current=False):
        for t in self.tabs.values():
            if t.page is page:
                return t
        self.seq += 1
        tab = Tab(f"t{self.seq}", page, profile)
        self.tabs[tab.id] = tab
        page.on("console", lambda m, tab=tab: tab.console.append(f"[{m.type}] {m.text}"[:500]))
        page.on("pageerror", lambda e, tab=tab: tab.console.append(f"[pageerror] {e}"[:500]))
        page.on(
            "response",
            lambda r, tab=tab: tab.network.append(
                f"{r.status} {r.request.method} {r.request.resource_type} {r.url[:300]}"
            ),
        )
        page.on("requestfailed", lambda r, tab=tab: tab.network.append(f"FAIL {r.request.method if hasattr(r,'request') else r.method} {r.url[:300]} {r.failure}"))
        page.on("dialog", lambda d, tab=tab: asyncio.ensure_future(self._dialog(tab, d)))
        page.on("close", lambda _p, tab=tab: self._closed(tab))
        if make_current or profile not in self.current:
            self.current[profile] = tab.id
        return tab

    async def _dialog(self, tab, d):
        tab.dialogs.append(f"{d.type}: {d.message}")
        try:
            await d.dismiss() if d.type != "beforeunload" else await d.accept()
        except Exception:
            pass

    def _closed(self, tab):
        self.tabs.pop(tab.id, None)
        if self.current.get(tab.profile) == tab.id:
            rest = [t.id for t in self.tabs.values() if t.profile == tab.profile]
            if rest:
                self.current[tab.profile] = rest[-1]
            else:
                self.current.pop(tab.profile, None)

    async def tab_for(self, profile, tid=None, create=True):
        if tid:
            t = self.tabs.get(tid)
            if not t:
                raise UsageError(f"no tab {tid}; run `tabs`")
            return t
        cur = self.current.get(profile)
        if cur and cur in self.tabs:
            return self.tabs[cur]
        if not create:
            raise UsageError("no open tab; run `open <url>` first")
        ctx = await self.ensure_context(profile)
        page = await ctx.new_page()
        return self._register(page, profile, make_current=True)

    async def run_sync(self):
        if not SYNC_SCRIPT.exists():
            return False
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SYNC_SCRIPT), "--json",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def load_cookies(self, ctx, sp):
        """Chrome keeps at most ~3300 cookies (180 per site) and silently evicts the rest.
        The sync file is ordered oldest→newest access, so take the newest within caps."""
        cookies = json.loads(sp.read_text()).get("cookies", [])
        per_site, picked = collections.Counter(), []
        for c in reversed(cookies):
            site = ".".join(c["domain"].lstrip(".").split(".")[-2:])
            if per_site[site] >= PER_SITE_CAP:
                continue
            per_site[site] += 1
            picked.append(c)
            if len(picked) >= TOTAL_CAP:
                break
        picked.reverse()  # insert oldest first so the newest are least likely to be evicted
        for i in range(0, len(picked), 500):
            chunk = picked[i:i + 500]
            try:
                await ctx.add_cookies(chunk)
            except Exception:
                for c in chunk:
                    try:
                        await ctx.add_cookies([c])
                    except Exception:
                        pass
        return len(picked)

    async def resync(self, profile):
        ok = await self.run_sync()
        ctx = self.contexts.get(profile)
        sp = storage_path(profile)
        if ok and ctx and sp.exists():
            await ctx.clear_cookies()
            await self.load_cookies(ctx, sp)
        self.resynced_at[profile] = time.time()
        return ok

    # --------------------------------------------------------------- helpers
    async def settle(self, page, max_ms=3500, quiet_ms=350):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=max_ms)
        except Exception:
            pass
        try:
            return await page.evaluate(SETTLE_JS, [quiet_ms, max_ms])
        except Exception:
            # navigation in progress → wait once more
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=max_ms)
                return await page.evaluate(SETTLE_JS, [quiet_ms, max_ms])
            except Exception:
                return -1

    def locate(self, tab, target, nth=None):
        page = tab.page
        if REF_RE.match(target):
            return page.locator(f"aria-ref={tab.ref(target)}")
        if target.startswith(("css=", "xpath=", "text=", "#", ".", "//", "[")):
            return page.locator(target[4:] if target.startswith("css=") else target)
        return None  # resolve by text asynchronously

    async def resolve(self, tab, target, roles=None):
        loc = self.locate(tab, target)
        if loc is not None:
            return loc
        page = tab.page
        roles = roles or ["button", "link", "tab", "menuitem", "option", "checkbox", "radio", "switch", "textbox", "searchbox", "combobox"]
        candidates = []
        for exact in (True, False):
            for role in roles:
                candidates.append(page.get_by_role(role, name=target, exact=exact))
            candidates.append(page.get_by_label(target, exact=exact))
            candidates.append(page.get_by_placeholder(target, exact=exact))
            candidates.append(page.get_by_text(target, exact=exact))
        for c in candidates:
            try:
                n = await c.count()
            except Exception:
                continue
            if n == 0:
                continue
            for i in range(min(n, 10)):
                el = c.nth(i)
                try:
                    if await el.is_visible():
                        return el
                except Exception:
                    pass
            return c.first
        raise UsageError(f"nothing matches {target!r}; use `find` or `snap -i` and pass a ref like e12")

    async def title(self, tab):
        try:
            return await tab.page.title()
        except Exception:
            return ""

    async def head(self, tab, note=""):
        t = await self.title(tab)
        dl = ""
        if tab.dialogs:
            dl = " | dialogs dismissed: " + "; ".join(tab.dialogs)
            tab.dialogs.clear()
        u = tab.page.url
        u = u if len(u) <= 160 else u[:150] + "…"
        return f"[{tab.id}·{tab.profile}] {t} <{u}>{(' ' + note) if note else ''}{dl}"

    async def snapshot_text(self, tab, o):
        page = tab.page
        if getattr(o, "raw", False):
            return await page.aria_snapshot()
        if getattr(o, "yaml", False):
            return await page.aria_snapshot(mode="ai")
        root = page
        if getattr(o, "ref", None):
            root = page.locator(f"aria-ref={tab.ref(o.ref)}")
        elif getattr(o, "selector", None):
            root = page.locator(o.selector).first
        yaml = await (root.aria_snapshot(mode="ai") if root is not page else page.aria_snapshot(mode="ai"))
        tab.ref_prefix = dominant_prefix(yaml)
        out = compact(yaml, urls=o.urls, images=o.images, interactive_only=o.i, prefix=tab.ref_prefix)
        return out

    def window(self, text, o, label="snap"):
        if getattr(o, "filter", None):
            words = [w.lower() for w in o.filter.split("|")]
            lines = [l for l in text.splitlines() if any(w in l.lower() for w in words)]
            text = "\n".join(lines) if lines else f"[no lines match {o.filter!r}]"
        start = max(0, getattr(o, "offset", 0) or 0)
        mx = getattr(o, "max", None) or DEFAULT_MAX
        total = len(text)
        chunk = text[start:start + mx]
        if start + mx < total:
            cut = chunk.rfind("\n")
            if cut > mx * 0.6:
                chunk = chunk[:cut]
            chunk += f"\n… [{total - start - len(chunk):,} more chars — `{label} --offset {start + len(chunk)}` or narrow with --filter / --ref]"
        return chunk

    async def page_text(self, tab, selector=None):
        """innerText of <main>/body; if that misses content (shadow DOM, iframes) use the ARIA tree's text."""
        t = await tab.page.evaluate(TEXT_JS, selector)
        if selector:
            return t
        try:
            yaml = await tab.page.aria_snapshot(mode="ai")
            tab.ref_prefix = dominant_prefix(yaml)
            snap = compact(yaml, prefix=tab.ref_prefix)
            plain = strip_refs(snap)
            if len(t) < 0.6 * len(plain):
                return plain
        except Exception:
            pass
        return t

    async def read_after(self, tab, o):
        """Optional page read appended to action results (--snap / --text / --diff)."""
        if getattr(o, "text", False):
            t = await self.page_text(tab)
            return "\n---\n" + self.window(t, o, "text")
        if getattr(o, "snap", False) or getattr(o, "diff", False):
            s = await self.snapshot_text(tab, argparse.Namespace(urls=False, images=False, i=False, raw=False, yaml=False, ref=None, selector=None))
            prev = tab.last_snap
            tab.last_snap = s
            if getattr(o, "diff", False) and prev:
                return "\n---\n" + self.window(diff_lines(prev, s), o, "snap")
            return "\n---\n" + self.window(s, o, "snap")
        return ""

    # -------------------------------------------------------------- commands
    def parsers(self):
        P = {}

        def mk(name, help_):
            p = Parser(prog=name, description=help_, add_help=True)
            p.add_argument("--tab", help="tab id (default: current tab of the profile)")
            P[name] = p
            return p

        def reads(p):
            p.add_argument("--snap", action="store_true", help="append compact snapshot")
            p.add_argument("--diff", action="store_true", help="append only lines that changed since last snapshot")
            p.add_argument("--text", action="store_true", help="append page text")
            p.add_argument("--max", type=int, default=None)
            p.add_argument("--offset", type=int, default=0)
            p.add_argument("--filter", default=None)

        p = mk("open", "Navigate (current tab, or --new tab) and return a compact snapshot")
        p.add_argument("url")
        p.add_argument("--new", action="store_true", help="open in a new tab")
        p.add_argument("--no-snap", dest="nosnap", action="store_true", help="only print the header")
        p.add_argument("--text", action="store_true", help="return page text instead of snapshot")
        p.add_argument("-i", action="store_true", help="interactive elements only")
        p.add_argument("--urls", action="store_true")
        p.add_argument("--images", action="store_true")
        p.add_argument("--wait", type=float, default=None, help="extra seconds after settle")
        p.add_argument("--settle", type=int, default=3500, help="max ms to wait for DOM to go quiet")
        p.add_argument("--max", type=int, default=None)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--filter", default=None, help="keep lines containing any of a|b|c")
        p.add_argument("--no-auth-retry", dest="noauth", action="store_true")

        p = mk("snap", "Compact ARIA snapshot with refs")
        p.add_argument("-i", action="store_true", help="interactive elements only")
        p.add_argument("--urls", action="store_true", help="include link URLs")
        p.add_argument("--images", action="store_true", help="include named images")
        p.add_argument("--ref", help="only the subtree under this ref")
        p.add_argument("--selector", help="only the subtree under this CSS selector")
        p.add_argument("--diff", action="store_true", help="only lines added since the last snapshot of this tab")
        p.add_argument("--yaml", action="store_true", help="raw AI-mode YAML (refs, verbose)")
        p.add_argument("--raw", action="store_true", help="original aria_snapshot YAML (no refs)")
        p.add_argument("--max", type=int, default=None)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--filter", default=None)

        p = mk("text", "Visible text of <main> (or body / --selector), like get_page_text")
        p.add_argument("--selector")
        p.add_argument("--max", type=int, default=None)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--filter", default=None)

        p = mk("find", "Find elements/lines matching words; returns refs")
        p.add_argument("query", nargs="+")
        p.add_argument("-n", type=int, default=15)

        p = mk("click", "Click a ref (e12), visible text, label or CSS selector")
        p.add_argument("target")
        p.add_argument("--double", action="store_true")
        p.add_argument("--right", action="store_true")
        p.add_argument("--modifiers", default=None, help="e.g. Meta,Shift")
        p.add_argument("--force", action="store_true")
        reads(p)

        p = mk("type", "Type into a field (ref/label/placeholder); --submit presses Enter")
        p.add_argument("target")
        p.add_argument("value")
        p.add_argument("--submit", action="store_true")
        p.add_argument("--append", action="store_true", help="don't clear first")
        p.add_argument("--slow", action="store_true", help="keystroke-by-keystroke (for picky widgets)")
        reads(p)

        p = mk("press", "Press a key or chord, e.g. Enter, Escape, Meta+A, PageDown")
        p.add_argument("key")
        p.add_argument("--target", default=None, help="focus this ref/text first")
        reads(p)

        p = mk("hover", "Hover a ref/text")
        p.add_argument("target")
        reads(p)

        p = mk("select", "Select option(s) in a <select>")
        p.add_argument("target")
        p.add_argument("values", nargs="+")
        reads(p)

        p = mk("check", "Check/uncheck a checkbox/radio/switch")
        p.add_argument("target")
        p.add_argument("--off", action="store_true")
        reads(p)

        p = mk("upload", "Set files on a file input (ref/selector)")
        p.add_argument("target")
        p.add_argument("files", nargs="+")

        p = mk("scroll", "Scroll: down|up|top|bottom or a ref/text into view")
        p.add_argument("where", nargs="?", default="down")
        p.add_argument("--px", type=int, default=None)
        p.add_argument("--times", type=int, default=1)
        reads(p)

        p = mk("wait", "Wait seconds, or until text/url appears")
        p.add_argument("seconds", nargs="?", type=float, default=None)
        p.add_argument("--for", dest="for_text", default=None, help="text to appear")
        p.add_argument("--gone", default=None, help="text to disappear")
        p.add_argument("--url", default=None, help="substring/glob the URL must contain")
        p.add_argument("--timeout", type=float, default=15)
        reads(p)

        for nm in ("back", "forward", "reload"):
            p = mk(nm, f"History {nm}")
            reads(p)

        p = mk("shot", "Screenshot PNG (viewport, --full page, or --ref element)")
        p.add_argument("path", nargs="?", default=None)
        p.add_argument("--full", action="store_true")
        p.add_argument("--ref", default=None)

        p = mk("pdf", "Save page as PDF")
        p.add_argument("path")

        p = mk("eval", "Evaluate JavaScript in the page; prints JSON result")
        p.add_argument("js")

        p = mk("console", "Console messages and page errors")
        p.add_argument("--errors", action="store_true")
        p.add_argument("--filter", default=None)
        p.add_argument("--clear", action="store_true")
        p.add_argument("-n", type=int, default=50)

        p = mk("network", "Recent network responses")
        p.add_argument("--filter", default=None)
        p.add_argument("--failed", action="store_true", help="only 4xx/5xx/failed")
        p.add_argument("--clear", action="store_true")
        p.add_argument("-n", type=int, default=50)

        p = mk("fetch", "fetch() from the page with its cookies (API calls as the signed-in user)")
        p.add_argument("url")
        p.add_argument("--method", default="GET")
        p.add_argument("--body", default=None)
        p.add_argument("--max", type=int, default=None)
        p.add_argument("--offset", type=int, default=0)
        p.add_argument("--filter", default=None)

        mk("tabs", "List tabs")
        p = mk("tab", "tab new [url] | tab use <id> | tab close [id]")
        p.add_argument("action", choices=["new", "use", "close"])
        p.add_argument("arg", nargs="?")

        mk("resync", "Re-read cookies from Chrome into this profile's live context")
        mk("status", "Daemon status")
        mk("stop", "Shut the daemon down")
        mk("cookies-check", "Show which sites are signed in (cookie names only)")
        return P

    async def dispatch(self, argv, profile):
        if not argv:
            raise UsageError("no command; try `help`")
        cmd, rest = argv[0], argv[1:]
        if cmd in ("help", "-h", "--help"):
            P = self.parsers()
            return "\n".join(f"{k:14s} {v.description}" for k, v in P.items()) + \
                "\nGlobal: --profile <alias> (see sync --list)  · `batch 'cmd1' 'cmd2' …` runs several in one call · `<cmd> -h` for flags"
        if cmd == "batch":
            outs = []
            for i, line in enumerate(rest):
                sub = shlex.split(line)
                try:
                    outs.append(await self.dispatch(sub, profile))
                except UsageError as e:
                    outs.append(f"[{i}] {line}: ERROR {e}")
                    break
                except Exception as e:
                    outs.append(f"[{i}] {line}: ERROR {type(e).__name__}: {str(e).splitlines()[0][:300]}")
                    break
            return "\n".join(o for o in outs if o)
        P = self.parsers()
        if cmd == "go":
            cmd = "open"
        if cmd not in P:
            raise UsageError(f"unknown command {cmd!r}; try `help`")
        o = P[cmd].parse_args(rest)
        return await getattr(self, "c_" + cmd.replace("-", "_"))(o, profile)

    # ---- navigation
    async def c_open(self, o, profile):
        url = o.url
        if not re.match(r"^[a-z][a-z0-9+.-]*:", url):
            url = "https://" + url
        if o.new:
            ctx = await self.ensure_context(profile)
            tab = self._register(await ctx.new_page(), profile, make_current=True)
        else:
            tab = await self.tab_for(profile, o.tab)
        page = tab.page
        t0 = time.perf_counter()
        note = ""
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if resp is not None and resp.status >= 400:
                note = f"HTTP {resp.status}" + (" (rate limited — slow down)" if resp.status == 429 else "")
        except Exception as e:
            msg = str(e).splitlines()[0]
            st = [l for l in tab.network if l.startswith(("4", "5")) and " document " in l]
            if st:
                code = st[-1].split()[0]
                raise UsageError(f"HTTP {code} from {url}" + (" (rate limited by the site — wait a few minutes)" if code == "429" else ""))
            if "interrupted by another navigation" not in msg:
                raise UsageError(f"navigation failed: {msg[:300]}")
        await self.settle(page, max_ms=o.settle)
        if o.wait:
            await asyncio.sleep(o.wait)
        body = ""
        if not o.nosnap or not o.noauth:
            body = await self.read_open(tab, o)
            if not o.noauth and self._login_wall(page.url, body) and \
                    time.time() - self.resynced_at.get(profile, 0) > 120:
                await self.resync(profile)
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await self.settle(page, max_ms=o.settle)
                body = await self.read_open(tab, o)
                note = "(login wall → cookies re-synced from Chrome, reloaded)"
                if self._login_wall(page.url, body):
                    note = "(still signed out after re-sync: sign in to this site in your real Chrome profile, then `resync`)"
        ms = round((time.perf_counter() - t0) * 1000)
        h = await self.head(tab, f"{ms}ms {note}".strip())
        if o.nosnap:
            return h
        return h + "\n---\n" + body

    async def read_open(self, tab, o):
        if o.text:
            t = await self.page_text(tab)
            return self.window(t, o, "text")
        s = await self.snapshot_text(tab, argparse.Namespace(urls=o.urls, images=o.images, i=o.i, raw=False, yaml=False, ref=None, selector=None))
        tab.last_snap = s
        return self.window(s, o, "snap")

    def _login_wall(self, url, body):
        return bool(LOGIN_URL.search(url or "")) or bool(LOGIN_WALL.search(body[:6000] if body else ""))

    async def _nav(self, o, profile, fn):
        tab = await self.tab_for(profile, o.tab, create=False)
        await fn(tab.page)
        await self.settle(tab.page)
        return await self.head(tab) + await self.read_after(tab, o)

    async def c_back(self, o, profile):
        return await self._nav(o, profile, lambda p: p.go_back(wait_until="domcontentloaded"))

    async def c_forward(self, o, profile):
        return await self._nav(o, profile, lambda p: p.go_forward(wait_until="domcontentloaded"))

    async def c_reload(self, o, profile):
        return await self._nav(o, profile, lambda p: p.reload(wait_until="domcontentloaded"))

    # ---- reading
    async def c_snap(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        s = await self.snapshot_text(tab, o)
        prev = tab.last_snap
        if not (o.raw or o.yaml or o.ref or o.selector or o.i):
            tab.last_snap = s
        if o.diff:
            if not prev:
                return await self.head(tab, "(no previous snapshot; full)") + "\n---\n" + self.window(s, o)
            return await self.head(tab, "(diff)") + "\n---\n" + self.window(diff_lines(prev, s), o)
        return await self.head(tab) + "\n---\n" + self.window(s, o)

    async def c_text(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        t = await self.page_text(tab, o.selector)
        return await self.head(tab) + "\n---\n" + self.window(t, o, "text")

    async def c_find(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        s = await self.snapshot_text(tab, argparse.Namespace(urls=True, images=True, i=False, raw=False, yaml=False, ref=None, selector=None))
        q = " ".join(o.query).lower()
        words = [w for w in re.split(r"\W+", q) if w]
        scored = []
        for line in s.splitlines():
            if "](" not in line:
                continue
            ll = line.lower()
            hit = sum(1 for w in words if w in ll)
            if not hit:
                continue
            # break long lines into individual controls that matched
            ctrls = re.findall(r"\[[^\]]*\]\((?:f\d+)?e\d+\)(?:<[^>]*>)?(?:\{[^}]*\})?", line)
            m = [c for c in ctrls if any(w in c.lower() for w in words)]
            score = hit / len(words) + (0.5 if q in ll else 0) + (0.3 if m else 0)
            show = line if len(line) <= 220 else (" … ".join(m) if m else line[:220] + "…")
            scored.append((score, show))
        scored.sort(key=lambda x: -x[0])
        seen, out = set(), []
        for _, l in scored:
            if l not in seen:
                seen.add(l)
                out.append(l)
            if len(out) >= o.n:
                break
        return "\n".join(out) if out else f"[nothing matches {q!r}]"

    # ---- actions
    async def _after_action(self, tab, o, before_url, ctx_pages_before):
        # popup / new tab?
        await asyncio.sleep(0.15)
        ctx = self.contexts.get(tab.profile)
        new_tab = None
        if ctx and len(ctx.pages) > ctx_pages_before:
            newest = ctx.pages[-1]
            new_tab = self._register(newest, tab.profile, make_current=True)
        target = new_tab or tab
        await self.settle(target.page, max_ms=4000, quiet_ms=400)
        note = ""
        if new_tab:
            note = f"(opened new tab {new_tab.id})"
        elif target.page.url != before_url:
            note = "(navigated)"
        return await self.head(target, note) + await self.read_after(target, o)

    async def _act(self, o, profile, fn, roles=None):
        tab = await self.tab_for(profile, o.tab, create=False)
        el = await self.resolve(tab, o.target, roles) if getattr(o, "target", None) else None
        before = tab.page.url
        n_pages = len(self.contexts[tab.profile].pages)
        await fn(tab.page, el)
        return await self._after_action(tab, o, before, n_pages)

    async def c_click(self, o, profile):
        async def fn(page, el):
            kw = {"timeout": 8000}
            if o.modifiers:
                kw["modifiers"] = o.modifiers.split(",")
            if o.force:
                kw["force"] = True
            if o.right:
                kw["button"] = "right"
            try:
                await (el.dblclick(**kw) if o.double else el.click(**kw))
            except Exception as e:
                if "intercepts pointer events" in str(e) or "not visible" in str(e) or "outside of the viewport" in str(e):
                    await el.dispatch_event("click")
                else:
                    raise
        return await self._act(o, profile, fn)

    async def c_type(self, o, profile):
        async def fn(page, el):
            if o.slow:
                if not o.append:
                    await el.fill("", timeout=8000)
                await el.press_sequentially(o.value, delay=20, timeout=20000)
            elif o.append:
                await el.focus()
                await page.keyboard.insert_text(o.value)
            else:
                try:
                    await el.fill(o.value, timeout=8000)
                except Exception:
                    # contenteditable / custom editors
                    await el.click(timeout=8000)
                    await page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
                    await page.keyboard.insert_text(o.value)
            if o.submit:
                await el.press("Enter")
        return await self._act(o, profile, fn, roles=["textbox", "searchbox", "combobox", "spinbutton"])

    async def c_press(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        before = tab.page.url
        n_pages = len(self.contexts[tab.profile].pages)
        if o.target:
            el = await self.resolve(tab, o.target)
            await el.press(o.key)
        else:
            await tab.page.keyboard.press(o.key)
        return await self._after_action(tab, o, before, n_pages)

    async def c_hover(self, o, profile):
        return await self._act(o, profile, lambda page, el: el.hover(timeout=8000))

    async def c_select(self, o, profile):
        return await self._act(o, profile, lambda page, el: el.select_option(o.values, timeout=8000), roles=["combobox", "listbox"])

    async def c_check(self, o, profile):
        return await self._act(o, profile, lambda page, el: el.set_checked(not o.off, timeout=8000), roles=["checkbox", "radio", "switch", "menuitemcheckbox"])

    async def c_upload(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        files = [str(Path(f).expanduser().resolve()) for f in o.files]
        for f in files:
            if not Path(f).exists():
                raise UsageError(f"file not found: {f}")
        loc = self.locate(tab, o.target) or tab.page.locator("input[type=file]").first
        try:
            await loc.set_input_files(files, timeout=8000)
        except Exception:
            async with tab.page.expect_file_chooser(timeout=8000) as fc:
                await loc.click()
            await (await fc.value).set_files(files)
        return await self.head(tab, f"(uploaded {len(files)} file(s))")

    async def c_scroll(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        page = tab.page
        w = o.where
        for _ in range(max(1, o.times)):
            if w in ("down", "up"):
                px = o.px or 900
                await page.mouse.wheel(0, px if w == "down" else -px)
            elif w == "top":
                await page.evaluate("window.scrollTo(0,0)")
            elif w == "bottom":
                await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
            else:
                el = await self.resolve(tab, w)
                await el.scroll_into_view_if_needed(timeout=8000)
            await asyncio.sleep(0.25)
        await self.settle(page, max_ms=2500, quiet_ms=350)
        y = await page.evaluate("[Math.round(scrollY), document.body.scrollHeight]")
        return await self.head(tab, f"(scrollY {y[0]}/{y[1]})") + await self.read_after(tab, o)

    async def c_wait(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        page = tab.page
        to = o.timeout * 1000
        if o.for_text:
            await page.get_by_text(o.for_text).first.wait_for(state="visible", timeout=to)
        elif o.gone:
            await page.get_by_text(o.gone).first.wait_for(state="hidden", timeout=to)
        elif o.url:
            pat = o.url if any(ch in o.url for ch in "*?") else f"**{o.url}**"
            await page.wait_for_url(pat, timeout=to)
        elif o.seconds is not None:
            await asyncio.sleep(o.seconds)
        else:
            await self.settle(page, max_ms=to)
        return await self.head(tab) + await self.read_after(tab, o)

    # ---- capture
    async def c_shot(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        path = Path(o.path).expanduser() if o.path else STATE_DIR / "shots" / f"{tab.id}-{int(time.time())}.png"
        path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        if o.ref:
            await tab.page.locator(f"aria-ref={tab.ref(o.ref)}").screenshot(path=str(path), timeout=10000)
        else:
            await tab.page.screenshot(path=str(path), full_page=o.full)
        return f"{path} ({path.stat().st_size:,} B)"

    async def c_pdf(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        path = Path(o.path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        await tab.page.pdf(path=str(path))
        return f"{path} ({path.stat().st_size:,} B)"

    async def c_eval(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        js = o.js.strip()
        try:
            res = await tab.page.evaluate(js)  # expression or function (promises awaited)
        except Exception as e:
            if "SyntaxError" not in str(e) and "Illegal return" not in str(e):
                raise
            res = await tab.page.evaluate(f"async () => {{ {js} }}")  # statement body with `return`
        out = res if isinstance(res, str) else json.dumps(res, ensure_ascii=False, default=str)
        return self.window(out, argparse.Namespace(max=None, offset=0, filter=None), "eval")

    async def c_console(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        items = list(tab.console)
        if o.errors:
            items = [i for i in items if i.startswith(("[error]", "[pageerror]"))]
        if o.filter:
            items = [i for i in items if o.filter.lower() in i.lower()]
        if o.clear:
            tab.console.clear()
        return "\n".join(items[-o.n:]) or "[no console messages]"

    async def c_network(self, o, profile):
        tab = await self.tab_for(profile, o.tab, create=False)
        items = list(tab.network)
        if o.failed:
            items = [i for i in items if i.startswith("FAIL") or i[:1] in "45"]
        if o.filter:
            items = [i for i in items if o.filter.lower() in i.lower()]
        if o.clear:
            tab.network.clear()
        return "\n".join(items[-o.n:]) or "[no requests]"

    async def c_fetch(self, o, profile):
        tab = await self.tab_for(profile, o.tab)
        js = """async ([u, m, b]) => { const r = await fetch(u, {method: m, body: b || undefined, credentials: 'include'});
                 return [r.status, r.headers.get('content-type') || '', await r.text()]; }"""
        status, ctype, body = await tab.page.evaluate(js, [o.url, o.method, o.body])
        return f"{status} {ctype}\n---\n" + self.window(body, o, "fetch")

    # ---- tabs & lifecycle
    async def c_tabs(self, o, profile):
        rows = []
        for t in self.tabs.values():
            cur = "*" if self.current.get(t.profile) == t.id else " "
            rows.append(f"{cur}{t.id} [{t.profile}] {(await self.title(t))[:70]} <{t.page.url[:120]}>")
        return "\n".join(rows) or "[no tabs]"

    async def c_tab(self, o, profile):
        if o.action == "new":
            ctx = await self.ensure_context(profile)
            tab = self._register(await ctx.new_page(), profile, make_current=True)
            if o.arg:
                ns = self.parsers()["open"].parse_args([o.arg, "--tab", tab.id])
                return await self.c_open(ns, profile)
            return await self.head(tab)
        if o.action == "use":
            t = self.tabs.get(o.arg or "")
            if not t:
                raise UsageError(f"no tab {o.arg}")
            self.current[t.profile] = t.id
            try:
                await t.page.bring_to_front()
            except Exception:
                pass
            return await self.head(t)
        t = self.tabs.get(o.arg) if o.arg else await self.tab_for(profile, o.tab, create=False)
        if not t:
            raise UsageError(f"no tab {o.arg}")
        await t.page.close()
        return f"closed {t.id}"

    async def c_resync(self, o, profile):
        await self.ensure_context(profile)
        ok = await self.resync(profile)
        return f"resync {'ok' if ok else 'FAILED'} for {profile}"

    async def c_cookies_check(self, o, profile):
        ctx = await self.ensure_context(profile)
        cs = await ctx.cookies()
        markers = {
            "x.com": ["auth_token", "ct0"], "linkedin.com": ["li_at"], "reddit.com": ["reddit_session", "token_v2"],
            "google.com": ["SID", "__Secure-1PSID"], "github.com": ["user_session"], "tiktok.com": ["sessionid"],
            "youtube.com": ["LOGIN_INFO"], "facebook.com": ["c_user"], "instagram.com": ["sessionid"],
        }
        rows = []
        for site, names in markers.items():
            have = {c["name"] for c in cs if c["domain"].lstrip(".").endswith(site)}
            hit = [n for n in names if n in have]
            rows.append(f"{site:14s} {'signed-in' if hit else 'no session cookie'} ({', '.join(hit) or '-'})")
        return f"{profile}: {len(cs)} cookies\n" + "\n".join(rows)

    async def c_status(self, o, profile):
        return json.dumps({
            "pid": os.getpid(), "uptime_s": round(time.time() - self.started),
            "chrome": self.browser.version if self.browser else None,
            "profiles": list(self.contexts), "tabs": len(self.tabs), "idle_exit_s": IDLE_SECS,
        })

    async def c_stop(self, o, profile):
        asyncio.get_event_loop().call_later(0.1, lambda: os._exit(0))
        return "stopping"

    # -------------------------------------------------------------- server
    async def handle(self, reader, writer):
        try:
            line = await reader.readline()
            req = json.loads(line or b"{}")
            if req.get("token") != self.token:
                writer.write(b'{"ok":false,"out":"bad token","code":2}\n')
                return
            self.last_used = time.time()
            profile = canon_profile(req.get("profile"))
            async with self.lock:
                try:
                    out = await self.dispatch(req.get("argv") or [], profile)
                    resp = {"ok": True, "out": out, "code": 0}
                except UsageError as e:
                    resp = {"ok": False, "out": str(e).strip(), "code": 2}
                except Exception as e:
                    msg = str(e).strip().splitlines()
                    short = "; ".join(l.strip() for l in msg[:3])[:600]
                    if "aria-ref" in short:
                        short += "  (ref is stale — take a new `snap` and use the fresh ref)"
                    resp = {"ok": False, "out": f"{type(e).__name__}: {short}", "code": 1}
            self.last_used = time.time()
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        except Exception as e:  # pragma: no cover
            try:
                writer.write((json.dumps({"ok": False, "out": f"daemon error: {e}", "code": 1}) + "\n").encode())
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def idle_watch(self):
        while True:
            await asyncio.sleep(30)
            if time.time() - self.last_used > IDLE_SECS and not self.lock.locked():
                os._exit(0)

    async def main(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_hex(16)
        server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=2**24)
        port = server.sockets[0].getsockname()[1]
        await self.ensure_browser()  # warm Chrome before announcing readiness
        tmp = INFO_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"port": port, "token": self.token, "pid": os.getpid()}))
        os.chmod(tmp, 0o600)
        tmp.replace(INFO_FILE)
        asyncio.ensure_future(self.idle_watch())
        async with server:
            await server.serve_forever()


def strip_refs(s: str) -> str:
    s = re.sub(r"\[(?:[a-z]+: )?([^\]]*)\]\((?:f\d+)?e\d+\)(?:\{[^}]*\})?", r"\1", s)
    return re.sub(r"[ \t]+", " ", s)


def diff_lines(prev: str, cur: str) -> str:
    a, b = prev.splitlines(), cur.splitlines()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.extend("+ " + l for l in b[j1:j2])
        if tag in ("replace", "delete") and i2 - i1 <= 5:
            out.extend("- " + l for l in a[i1:i2])
        elif tag in ("replace", "delete"):
            out.append(f"- [{i2 - i1} lines removed]")
    return "\n".join(out) or "[no changes]"


if __name__ == "__main__":
    try:
        asyncio.run(Daemon().main())
    except KeyboardInterrupt:
        pass
