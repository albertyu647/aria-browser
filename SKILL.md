---
name: authed-silent-chrome-browser-aria
description: Silent, signed-in headless Chrome for any agent (Claude Code, Codex, Antigravity). Browse, read, click, type, scroll, screenshot, script and debug authenticated sites (X, Reddit, LinkedIn, Google, GitHub, internal tools) using the user's real Chrome cookies from several Chrome profiles (e.g. work and personal) at once, including DevTools data: request headers and bodies, WebSocket frames, breakpoints, performance traces and storage. Never opens a window or steals focus. A persistent daemon keeps tabs warm (~0.1-1 s per action) and returns compact ARIA snapshots with clickable refs (~35 tokens per HN story, 5-10x fewer than raw ARIA). Use instead of Claude in Chrome when you need sites the extension blocks (Reddit), both profiles, background operation, or a non-Claude agent.
---

# Authed Silent Chrome Browser (ARIA v2)

A persistent headless **real Chrome** (`channel="chrome"`) loaded with the user's decrypted Chrome cookies.
It's a daemon plus a thin CLI. The first call starts Chrome (~2.5 s). After that, each command is a
localhost round trip, and tabs, refs, console and network logs persist between calls.

```bash
A=~/.agents/skills/authed-silent-chrome-browser-aria/bin/aria      # or: node .../bin/cli.js
```

## Profiles: several live at the same time

Every Chrome profile on the machine is available. Run `node bin/cli.js --list` to see them. Name the ones you use once:

```bash
node bin/cli.js --alias work=Default --alias personal="Profile 2" --default personal
```

The aliases are stored in `~/.config/chrome-storage-states/profiles.json`. Each profile gets its own context and tabs,
so `aria --profile work open …` and `aria --profile personal open …` run side by side without touching the
user's open Chrome windows. `ARIA_PROFILE=<alias>` overrides the default for a single agent.

## Core loop: open → read → act on refs

```bash
$A open news.ycombinator.com                 # navigate + compact snapshot with refs
$A open x.com/home --text                    # navigate + plain text (cheapest read)
$A click e17                                 # click by ref from the last snapshot
$A click "new"                               # …or by visible text / label / CSS
$A type "Search Wikipedia" "Playwright" --submit
$A click e40 --snap                          # act and read in the same call (--snap | --text | --diff)
```

Snapshot format (compact, and every control carries a ref):
```
1. [upvote](e36) [Voxtral 3](e40) ( [mistral.ai](e42) )
321 points by [ilreb](e47) [3 hours ago](e49) | [hide](e50) | [82 comments](e51)
[button: Like](e438)  [textbox: Search](e42)  [checkbox: I agree](e9){checked}
<dialog: Log in to X>   <article: …>   ## Heading
```
Refs (`e17`) are valid until the next snapshot of that tab. If a ref is stale, run `snap` again.

## Commands

| Need | Command |
|---|---|
| Navigate | `open <url> [--new] [--text] [-i] [--no-snap] [--filter a\|b] [--max N]` |
| Read | `snap [-i] [--ref eN] [--diff] [--urls] [--filter] [--offset N]`, `text [--selector css]` |
| Locate | `find <words>` → matching lines/controls with refs |
| Act | `click`, `type [--submit --append --slow]`, `press <Key>`, `hover`, `select <t> <vals…>`, `check [--off]`, `upload <t> <files…>` |
| Move | `scroll down\|up\|top\|bottom\|<ref> [--times N]`, `back`, `forward`, `reload`, `wait [s] [--for text] [--url part]` |
| Capture | `shot [path] [--full] [--ref eN]`, `pdf <path>` |
| Debug | `eval "<js>"`, `console [--errors]`, `fetch <url>` (as the signed-in user) |
| Network panel | `network [--type api] [--failed] [--slow ms] [--method M] [--filter]`, `req <id> [--headers\|--body\|--curl\|--save f]`, `har <path>` |
| WebSockets | `ws [--id ws1] [--filter] [--full]` |
| Sources panel | `debug break <url>:<line>[:<col>] [--log expr] [--cond expr]`, `debug exceptions all\|uncaught\|off`, `debug hits`, `debug list\|clear`, `debug scripts`, `debug source <url> --line N [--col C]` |
| Performance panel | `perf` (vitals + resources + runtime), `perf start [--screenshots]` → act → `perf stop [path]` |
| Application panel | `storage`, `storage local\|session [get\|set\|del\|clear]`, `storage cookies [set\|del]`, `storage idb [db] [store]`, `storage cache [name]`, `storage sw [unregister]` |
| Tabs | `tabs`, `tab new [url]`, `tab use t2`, `tab close [t2]` |
| Many steps, one call | `batch 'open x.com/home --no-snap' 'find search' 'type e12 "hi"'` |
| Auth | `resync` (re-read Chrome cookies live), `cookies-check` (which sites are signed in) |
| Daemon | `status`, `stop` (it also exits after 30 min idle, `ARIA_IDLE`) |

## DevTools panels (no window needed)
The daemon reads the same data DevTools shows, through Playwright + CDP, per tab and kept between calls.

- **Network.** `network` lists `#id status method type time size url` (last 600 per tab). `req 12` prints request
  headers (real ones, cookies included), payload (JSON pretty-printed), response headers and body. XHR/fetch/document
  bodies are cached as they arrive, so they stay readable after navigation. Other bodies are fetched on demand and
  Chrome drops them once the page navigates. `req 12 --curl` replays outside the browser, `har f.har` imports into DevTools.
- **WebSockets.** `ws` lists sockets and the frames sent (↑) and received (↓).
- **Sources.** Breakpoints never leave the page frozen. `--log` makes a logpoint (writes to `console`, no pause).
  A plain `debug break` pauses, records the stack plus local/closure variables, and resumes at once (auto-removed after
  `--max-hits 20`). `debug exceptions uncaught` does the same for thrown errors. Read results with `debug hits`.
  Lines are 1-based. For minified bundles, find the spot with `debug source <url> --line 1 --col N`, then
  `debug break <url>:1:N`. Script lists reset on navigation, so set breakpoints after `open` and before the action.
- **Performance.** `perf` prints TTFB/DCL/load, FCP, LCP (+ element), CLS, INP, long tasks, resource totals, slowest
  and largest files, and CDP runtime counters (JS heap, DOM nodes, script/layout/style time). `perf start` … `perf stop`
  saves a Chrome trace (opens in DevTools > Performance or ui.perfetto.dev) and prints a main-thread breakdown.
- **Application.** `storage` gives an overview. Cookie/storage writes only touch the headless copy: real Chrome is
  unchanged, and `resync` restores cookies.
- **Screenshots** (`shot`) are real renders of the headless tab, not of windows on screen.

## Token discipline
- Reading content: use `--text`. Clicking things: use the default snapshot. For a form, use `-i` (controls only).
- Narrow with `--filter "comments|points"`, `--ref eN` (one subtree) or `--max`. Output is capped at
  24k chars by default, with an `--offset` hint.
- After an action, `--diff` returns only the lines that changed.
- Never dump raw HTML. `snap --yaml` and `snap --raw` exist for debugging only.

## Auth notes
- Cookies come from Chrome's encrypted DB (`scripts/sync_auth.py`, macOS Keychain). Chrome keeps at most ~3,300
  cookies per profile, so the daemon loads the most recently used ones first, which keeps X, Google and LinkedIn sessions intact.
- If a page shows a login wall, the daemon re-syncs cookies from Chrome once and reloads. If it's still signed out,
  the user needs to sign in to that site in real Chrome. Then run `aria resync`.
- Reddit rate-limits (HTTP 429) after a few dozen rapid loads, and the header reports the status. Slow down.

## Benchmarks & evals
```bash
python3 bench/evals.py      # 31 feature-parity checks (forms, iframes, tabs, console, both profiles, DevTools panels…)
python3 bench/bench.py      # speed/tokens/success vs bench/chrome_baseline.json (Claude in Chrome)
```

## Legacy (still works)
`node bin/cli.js browse <url> [--profile] [--click] [--fill sel=val] [--filter] [--screenshot p] [--mode aria|html]`
is a one-shot that now runs through the daemon. `node bin/cli.js` / `--list` / `--path <alias>` / `--alias` sync cookies and manage aliases.
`scripts/browse.py` is the original standalone script.
