---
name: authed-silent-chrome-browser-aria
description: Silent, signed-in headless Chrome for any agent (Claude Code, Codex, Antigravity). Browse, read, click, type, scroll, screenshot and script authenticated sites (X, Reddit, LinkedIn, Google, GitHub, internal tools) using the user's real Chrome cookies from several Chrome profiles (e.g. work and personal) at once. Never opens a window or steals focus. A persistent daemon keeps tabs warm (~0.1-1 s per action) and returns compact ARIA snapshots with clickable refs (~35 tokens per HN story, 5-10x fewer than raw ARIA). Use instead of Claude in Chrome when you need sites the extension blocks (Reddit), both profiles, background operation, or a non-Claude agent.
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
| Debug | `eval "<js>"`, `console [--errors]`, `network [--failed] [--filter]`, `fetch <url>` (as the signed-in user) |
| Tabs | `tabs`, `tab new [url]`, `tab use t2`, `tab close [t2]` |
| Many steps, one call | `batch 'open x.com/home --no-snap' 'find search' 'type e12 "hi"'` |
| Auth | `resync` (re-read Chrome cookies live), `cookies-check` (which sites are signed in) |
| Daemon | `status`, `stop` (it also exits after 30 min idle, `ARIA_IDLE`) |

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
python3 bench/evals.py      # 20 feature-parity checks (forms, iframes, tabs, console, both profiles…)
python3 bench/bench.py      # speed/tokens/success vs bench/chrome_baseline.json (Claude in Chrome)
```

## Legacy (still works)
`node bin/cli.js browse <url> [--profile] [--click] [--fill sel=val] [--filter] [--screenshot p] [--mode aria|html]`
is a one-shot that now runs through the daemon. `node bin/cli.js` / `--list` / `--path <alias>` / `--alias` sync cookies and manage aliases.
`scripts/browse.py` is the original standalone script.
