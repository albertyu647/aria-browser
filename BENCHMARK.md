# aria (authed-silent-chrome-browser-aria) vs vercel-labs/agent-desktop 1.0.2

Run on 2026-11-02 on Albert's Mac. Tokens = output chars ÷ 4 (what the agent reads).
aria: 3 reps per task, median wall time. agent-desktop: single runs, driving a separate
Chrome for Testing window (temp profile) so real Chrome tabs were never touched.
The agent-desktop runs were stopped partway because they were interrupting Albert's work.

## What each tool is

| | aria | agent-desktop |
|---|---|---|
| Scope | Web only: headless real Chrome via Playwright | Any desktop app via the OS accessibility API (macOS/Windows/Linux) |
| Sign-ins | Copies cookies from each Chrome profile; several profiles at once | None of its own: it drives whatever browser window is open |
| Navigation | `open <url>` | No URL command; needs `open -a …` or typing into the address bar |
| Window | None (headless) | Needs a real on-screen window |

## Results

| Facet | aria | agent-desktop |
|---|---|---|
| **Press-and-hold (local 3 s widget)** | ❌ No hold command. `click` sends trusted down/up with 0 ms between them. A JS hold via `eval` is `isTrusted=false`. Playwright, the engine aria runs on, did a trusted 3.0 s hold headlessly in a standalone script (3.3 s wall). | ⚠️ `--headed drag --from REF --to REF --duration 3500` held a real pointer for 4.7 s, but it takes over the physical mouse and focuses the window. `mouse-down`/`mouse-up` are disabled by design. |
| **Background / no disturbance** | ✅ No change of frontmost app and no cursor movement in any of 18 runs | ⚠️ Mixed. Its default headless actions (set-value, check, click) never changed the frontmost app and never moved the cursor. Two things did disturb Albert: `select` opened Chrome's native dropdown menu, which brought the test window forward over his work and grabbed the mouse, and the opt-in `--headed` hold moved the real cursor. The window also has to exist on screen, though it can sit behind others. |
| **Read speed** | HN with refs 0.89 s, HN text 0.73 s (includes page load) | Snapshot of an already-loaded page 0.14–0.25 s (accessibility read only; load time not included) |
| **Action speed** | Click 0.85 s. Actions on small pages wait ~4 s each because of the settle bug below | set-value / check / click 0.2–0.6 s. `select` timed out at 5 s |
| **Tokens** | 4-field form, interactive refs: ~55 tok. Action reply: ~6–20 tok. HN front page with refs: 1,621 tok | Same form, `snapshot -i --compact`: 6.6–7.6k chars ≈ 1,650–1,900 tok, since it includes browser UI (toolbar, tabs, infobar). Each action reply is ~150–250 tok of JSON |
| **Form task (name, dropdown, checkbox, submit)** | ✅ 3/3 correct (`Ada Lovelace \| Pro \| true`) | ❌ Submitted `Free`: native `<select>` failed headless (TIMEOUT) and headed (STALE_REF) |
| **HN click "new" / Wikipedia search** | ✅ 3/3 each (0.85 s, 2.6 s) | Not run (stopped) |

Not measured: agent-desktop on HN or Wikipedia, and a re-run of the hold on the fixed widget.

## Why agent-desktop disturbed the desktop

- **Headed drag:** with a ref endpoint it raises and focuses the target window (`AXRaise`/`AXMain` in
  `crates/macos/src/system/window/window_ops.rs`), then moves the real cursor for the whole hold.
- **`select` on an HTML `<select>`:** it sends `AXShowMenu` (`actions/select/select_menu.rs`). Chrome then
  opens a native macOS popup menu, which takes over the mouse and keyboard while it is open. When
  `select` timed out, that menu was probably left open (likely cause; not confirmed).
- **Navigation:** agent-desktop has no URL command, so a browser has to be pointed at a page with something
  like `open -a`, which can bring its window forward.

## Bug found in aria

`SETTLE_JS` in `scripts/ariad.py` only returns early when `sig > 200` (more than 200 chars of body text).
On smaller pages, every `open` and action waits the full ceiling (3.5–4 s). As a result, the 4-action form
batch took 17 s and `open` on the test pages took 3.6 s.

## Verdict

- **Web tasks you want done in the background:** aria wins on disturbance (none at all), token use
  (10–30× fewer on forms), reliability (passed every task except the hold) and per-task speed on real sites.
- **Press-and-hold:** neither works cleanly. agent-desktop can hold, but only by taking over your mouse
  and screen. aria cannot hold today, though its engine supports a trusted headless hold.
- **Native apps (Finder, Slack, System Settings):** agent-desktop is the only option of the two, and its
  accessibility reads are very fast.
