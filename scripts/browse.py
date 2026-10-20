#!/usr/bin/env python3
"""
Deterministic Token-Efficient Headless Browser for AI Agents
Primary: ARIA accessibility tree snapshots (page.aria_snapshot) to eliminate 90-97% of HTML token bloat.
Auto-Fallback: If ARIA is empty/unavailable, automatically falls back to sanitized/pruned HTML.
Runs with authenticated Chrome profile storage states in pure headless background mode.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from playwright.async_api import async_playwright

STORAGE_DIR = Path.home() / ".config" / "chrome-storage-states"

def count_tokens(text: str) -> int:
    if not text:
        return 0
    try:
        import tiktoken
        enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        return len(text) // 4

def prune_html(raw: str) -> str:
    """Strips massive token hogs (scripts, styles, SVGs, base64 images) while preserving HTML structure."""
    if not raw:
        return ""
    # Strip script blocks
    cleaned = re.sub(r'<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>', '', raw, flags=re.IGNORECASE)
    # Strip style blocks
    cleaned = re.sub(r'<style\b[^<]*(?:(?!<\/style>)<[^<]*)*<\/style>', '', cleaned, flags=re.IGNORECASE)
    # Strip massive SVG path data
    cleaned = re.sub(r'<svg\b[^<]*(?:(?!<\/svg>)<[^<]*)*<\/svg>', '[SVG_ICON]', cleaned, flags=re.IGNORECASE)
    # Strip base64 inline images
    cleaned = re.sub(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', '[data:image/...]', cleaned)
    # Strip HTML comments
    cleaned = re.sub(r'<!--[\s\S]*?-->', '', cleaned)
    # Collapse multiple blank lines
    cleaned = re.sub(r'\n\s*\n', '\n', cleaned)
    return cleaned.strip()

def is_meaningful_aria(tree: str) -> bool:
    """Checks whether the ARIA snapshot returned actual interactive/content nodes."""
    if not tree or len(tree.strip()) < 60:
        return False
    lines = [l.strip() for l in tree.strip().splitlines() if l.strip()]
    if len(lines) <= 2 and all(l in ["- document", "- root", "- application", "- generic"] for l in lines):
        return False
    return True

def get_storage_path(profile: str) -> Path:
    sys.path.insert(0, str(Path(__file__).parent))
    import profiles
    return profiles.storage_path(profile)

async def run_browser(args):
    storage_path = get_storage_path(args.profile)
    
    # Auto-sync if file doesn't exist
    if not storage_path.exists():
        sync_script = Path(__file__).parent / "sync_auth.py"
        if sync_script.exists():
            os.system(f"python3 {sync_script} > /dev/null 2>&1")
    
    state_file = str(storage_path) if storage_path.exists() else None
    
    t0 = time.perf_counter()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        context_args = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        }
        if state_file:
            context_args["storage_state"] = state_file
            
        context = await browser.new_context(**context_args)
        page = await context.new_page()
        
        try:
            await page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        except Exception as e:
            await browser.close()
            sys.stderr.write(f"Error navigating to {args.url}: {e}\n")
            sys.exit(1)
            
        # Hydration buffer for modern SPAs (TikTok, X, Reddit, Google Maps)
        if args.wait > 0:
            await asyncio.sleep(args.wait)
            
        # Optional pre-actions (clicks, text input)
        if args.click:
            try:
                locator = page.get_by_text(args.click, exact=False)
                if await locator.count() > 0:
                    await locator.first.click(timeout=3000)
                else:
                    await page.locator(args.click).first.click(timeout=3000)
                if args.wait > 0:
                    await asyncio.sleep(1.0)
            except Exception as e:
                sys.stderr.write(f"Warning: Failed to click '{args.click}': {e}\n")

        if args.fill:
            try:
                if "=" in args.fill:
                    target, val = args.fill.split("=", 1)
                    await page.locator(target).first.fill(val, timeout=3000)
                else:
                    await page.locator("input, textarea").first.fill(args.fill, timeout=3000)
                if args.wait > 0:
                    await asyncio.sleep(1.0)
            except Exception as e:
                sys.stderr.write(f"Warning: Failed to fill '{args.fill}': {e}\n")
                
        # Optional screenshot
        screenshot_bytes = 0
        if args.screenshot:
            screenshot_path = Path(args.screenshot).expanduser().resolve()
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshot_path), full_page=args.full_page)
            screenshot_bytes = os.path.getsize(str(screenshot_path))
            
        # Content Extraction Logic (ARIA vs Fallback HTML)
        output_content = ""
        output_mode = "ARIA"
        fallback_occurred = False
        
        if args.mode in ["auto", "aria"]:
            try:
                aria_tree = await page.aria_snapshot()
                if is_meaningful_aria(aria_tree) or args.mode == "aria":
                    output_content = aria_tree
                    output_mode = "ARIA Snapshot"
                else:
                    fallback_occurred = True
            except Exception:
                fallback_occurred = True
                
        if args.mode == "html" or (args.mode == "auto" and fallback_occurred):
            raw_html = await page.content()
            output_content = prune_html(raw_html)
            output_mode = "Pruned HTML (Auto-Fallback: ARIA was empty)" if fallback_occurred else "Pruned HTML"
            
        elif args.mode == "raw-html":
            output_content = await page.content()
            output_mode = "Raw HTML (Warning: High Token Usage)"
            
        # Optional keyword filtering
        if args.filter:
            lines = output_content.splitlines()
            matching_lines = [l for l in lines if args.filter.lower() in l.lower()]
            output_content = "\n".join(matching_lines) if matching_lines else f"[No nodes matched filter: '{args.filter}']"
            
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        tokens = count_tokens(output_content)
        
        await browser.close()
        
    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(output_content)
            
    if args.json:
        result = {
            "url": args.url,
            "profile": args.profile,
            "mode": output_mode,
            "fallback": fallback_occurred,
            "elapsed_ms": elapsed_ms,
            "tokens": tokens,
            "chars": len(output_content),
            "screenshot": str(args.screenshot) if args.screenshot else None,
            "screenshot_bytes": screenshot_bytes,
            "snapshot_file": str(args.output) if args.output else None,
            "content": output_content
        }
        print(json.dumps(result, indent=2))
    else:
        stats = f"# [{output_mode}] {tokens:,} tokens | {len(output_content):,} chars | {elapsed_ms}ms | Profile: {args.profile}"
        if args.screenshot:
            stats += f" | Screenshot: {args.screenshot} ({screenshot_bytes:,}B)"
        print(stats)
        print("---")
        print(output_content)

def main():
    parser = argparse.ArgumentParser(
        description="Deterministic Token-Efficient Headless Browser for Agents (ARIA with Auto-Fallback to Pruned HTML)"
    )
    parser.add_argument("url", type=str, help="Target URL to browse")
    parser.add_argument("--profile", type=str, default=None,
                        help="Chrome profile alias (default: from profiles.json)")
    parser.add_argument("--mode", type=str, default="auto", choices=["auto", "aria", "html", "raw-html"],
                        help="Extraction mode: 'auto' (tries ARIA, falls back to pruned HTML), 'aria', 'html' (sanitized), or 'raw-html' (default: auto)")
    parser.add_argument("--wait", type=float, default=2.0,
                        help="Hydration buffer in seconds after domcontentloaded (default: 2.0s)")
    parser.add_argument("--timeout", type=int, default=25,
                        help="Navigation timeout in seconds (default: 25s)")
    parser.add_argument("--screenshot", type=str, default=None,
                        help="Path to save page screenshot (PNG)")
    parser.add_argument("--full-page", action="store_true",
                        help="Capture full scrollable page for screenshot")
    parser.add_argument("--click", type=str, default=None,
                        help="Text or selector to click before capturing snapshot")
    parser.add_argument("--fill", type=str, default=None,
                        help="Form fill action in format 'selector=value'")
    parser.add_argument("--filter", type=str, default=None,
                        help="Filter output to lines containing this keyword")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Save output to a file")
    parser.add_argument("--json", action="store_true",
                        help="Output result as JSON object with metadata")
                        
    args = parser.parse_args()
    asyncio.run(run_browser(args))

if __name__ == "__main__":
    main()
