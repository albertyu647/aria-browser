#!/usr/bin/env python3
"""
ARIA browser benchmark: runs agent-style tasks through `bin/aria`, recording wall time,
output tokens (chars/4, what the agent actually ingests), items extracted, and success.

    python3 bench/bench.py                  # all tasks, 3 warm runs + 1 cold
    python3 bench/bench.py -t x_read -r 5   # one task
    python3 bench/bench.py --compare bench/chrome_baseline.json

Results: bench/results/<timestamp>.json and a markdown table on stdout.
"""

import argparse
import json
import re
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARIA = [str(ROOT / "bin" / "aria")]

POSTS_REDDIT = re.compile(r"\d+\s*(?:min|hr|day|mo|yr)s?\.? ago")
POSTS_X = re.compile(r"@\w+\]?(?:\([a-z0-9]+\))?\s*·")
HN_ITEMS = re.compile(r"\d+ points? by")

# name: (commands, success(out) -> bool, items(out) -> int)
TASKS = {
    # --- reading, text mode (compare with Claude in Chrome get_page_text)
    "hn_read": (["open news.ycombinator.com --text"],
                lambda o: len(HN_ITEMS.findall(o)) >= 25, lambda o: len(HN_ITEMS.findall(o))),
    "li_read": (["open www.linkedin.com/feed/ --text"],
                lambda o: "Feed post" in o and "Sign in" not in o[:400], lambda o: o.count("Feed post")),
    "rd_read": (["open www.reddit.com --text"],
                lambda o: len(POSTS_REDDIT.findall(o)) >= 3 and "blocked" not in o[:300],
                lambda o: len(POSTS_REDDIT.findall(o))),
    "x_read": (["open x.com/home --text"],
               lambda o: len(POSTS_X.findall(o)) >= 3 and "Log in to X" not in o,
               lambda o: len(POSTS_X.findall(o))),
    # --- reading, snapshot mode (refs included, ready to act)
    "hn_snap": (["open news.ycombinator.com"],
                lambda o: len(HN_ITEMS.findall(o)) >= 25, lambda o: len(HN_ITEMS.findall(o))),
    "li_snap": (["open www.linkedin.com/feed/ --max 8000"],
                lambda o: "Feed post" in o, lambda o: o.count("Feed post")),
    "rd_snap": (["open www.reddit.com"],
                lambda o: len(POSTS_REDDIT.findall(o)) >= 3, lambda o: len(POSTS_REDDIT.findall(o))),
    "x_snap": (["open x.com/home"],
               lambda o: len(POSTS_X.findall(o)) >= 3, lambda o: len(POSTS_X.findall(o))),
    # --- interaction
    "hn_click": (["open news.ycombinator.com --no-snap", "click new"],
                 lambda o: "/newest" in o, lambda o: 1),
    "wiki_search": (["open en.wikipedia.org --no-snap", "type 'Search Wikipedia' 'Playwright (software)' --submit"],
                    lambda o: "Playwright" in o.splitlines()[-1] or "Playwright" in o, lambda o: 1),
    "x_scroll_more": (["open x.com/home --no-snap", "scroll down --times 3 --diff --max 6000"],
                      lambda o: len(POSTS_X.findall(o)) >= 2, lambda o: len(POSTS_X.findall(o))),
    "rd_open_post": (["open www.reddit.com --no-snap", "find comments", ],
                     lambda o: "](" in o, lambda o: o.count("](")),
}


def run_cmd(cmd, profile):
    t0 = time.perf_counter()
    p = subprocess.run(ARIA + (["--profile", profile] if profile else []) + shlex.split(cmd), capture_output=True, text=True, timeout=120)
    return time.perf_counter() - t0, p.stdout + p.stderr, p.returncode


def run_task(name, profile):
    cmds, ok_fn, items_fn = TASKS[name]
    total_t, outs, codes = 0.0, [], []
    for c in cmds:
        dt, out, code = run_cmd(c, profile)
        total_t += dt
        outs.append(out)
        codes.append(code)
    out = "\n".join(outs)
    ok = all(c == 0 for c in codes) and bool(ok_fn(out))
    return {"secs": round(total_t, 3), "chars": len(out), "tokens": (len(out) + 3) // 4,
            "items": items_fn(out), "ok": ok, "calls": len(cmds)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-t", "--task", action="append")
    ap.add_argument("-r", "--runs", type=int, default=3)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--no-cold", action="store_true")
    ap.add_argument("--compare", default=str(ROOT / "bench" / "chrome_baseline.json"))
    ap.add_argument("--dump", action="store_true", help="print task output of last run")
    a = ap.parse_args()
    names = a.task or list(TASKS)

    results = {}
    if not a.no_cold:
        subprocess.run(ARIA + ["stop"], capture_output=True)
        cold = run_task(names[0], a.profile)
        results["_cold_start"] = {"task": names[0], **cold}
    for n in names:
        # be gentle with sites that rate-limit signed-in accounts (Reddit 429s after ~40 loads)
        reps = 1 if n.startswith("rd_") else a.runs
        runs = [run_task(n, a.profile) for _ in range(reps)]
        secs = [r["secs"] for r in runs]
        results[n] = {
            "secs_median": round(statistics.median(secs), 3), "secs_min": min(secs),
            "tokens": runs[-1]["tokens"], "items": runs[-1]["items"],
            "ok_rate": sum(r["ok"] for r in runs) / len(runs), "calls": runs[-1]["calls"],
        }
        print(f"{n:15s} {results[n]}", file=sys.stderr)
        if a.dump:
            _, out, _ = run_cmd(TASKS[n][0][-1], a.profile)
            print(out[:3000], file=sys.stderr)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ROOT / "bench" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stamp}.json").write_text(json.dumps(results, indent=2))

    base = {}
    if a.compare and Path(a.compare).exists():
        base = json.loads(Path(a.compare).read_text())
    print("| task | aria s | aria tok | aria items | tok/item | ok | chrome s | chrome tok | chrome items | chrome ok |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for n in names:
        r = results[n]
        b = base.get(n, {})
        tpi = round(r["tokens"] / r["items"]) if r["items"] else "-"
        print(f"| {n} | {r['secs_median']} | {r['tokens']} | {r['items']} | {tpi} | {r['ok_rate']:.0%} | "
              f"{b.get('secs', '-')} | {b.get('tokens', '-')} | {b.get('items', '-')} | {b.get('ok', '-')} |")
    if "_cold_start" in results:
        c = results["_cold_start"]
        print(f"\ncold start ({c['task']}, includes Chrome launch): {c['secs']}s ok={c['ok']}")


if __name__ == "__main__":
    main()
