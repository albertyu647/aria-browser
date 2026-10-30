#!/usr/bin/env python3
"""
Read the user's OWN saved Chrome passwords (macOS) for autofill on their behalf.

This is a thin query layer over `sync_auth.py`: it reuses the exact same
Keychain-derived AES key and `decrypt_value` routine the cookie sync already
uses, and only adds a SELECT against each profile's `Login Data` DB.

Safety model:
  * Secrets are decrypted on demand and returned in memory. Nothing is written
    to disk, and `redact()` keeps passwords out of any logged/printed string.
  * `credentials_for(url, ...)` only returns logins whose stored origin shares
    the registrable domain of `url` — the daemon's `login` command enforces the
    same check against the LIVE page URL, so a saved password is never typed
    into a look-alike origin (the protection Chrome's own autofill gives you).
  * Federated (Sign-in-with-Google etc.) entries have no password and are
    skipped here; use the `oauth` command for those.
"""

import sys
import time
import shutil
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync_auth  # noqa: E402  reuse get_keychain_password / derive_aes_key / decrypt_value
import profiles as profiles_cfg  # noqa: E402

CHROME_BASE = sync_auth.CHROME_BASE


def registrable(host: str) -> str:
    """Approximate eTLD+1 (last two labels) — matches how the daemon buckets cookies."""
    host = (host or "").lstrip(".").lower()
    # strip port and path if a full origin slipped in
    host = host.split("/")[0].split(":")[0]
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _host_of(url: str) -> str:
    u = url or ""
    if "://" in u:
        u = u.split("://", 1)[1]
    return u.split("/")[0].split("?")[0]


def folder_for_alias(alias: str) -> str:
    """Resolve a profile alias/slug to its Chrome folder (e.g. 'work' -> 'Default')."""
    want = profiles_cfg.resolve(alias)
    for p in sync_auth.list_profiles():
        if p["alias"] == want:
            return p["folder"]
    # fall back: maybe the caller passed a raw folder name
    if (CHROME_BASE / alias).exists():
        return alias
    raise FileNotFoundError(f"no Chrome profile for {alias!r}; run sync_auth.py --list")


def _login_db(folder: str) -> Path:
    for name in ("Login Data", "Login Data For Account"):
        f = CHROME_BASE / folder / name
        if f.exists():
            return f
    raise FileNotFoundError(f"no Login Data for profile folder {folder!r}")


def _all_logins(folder: str):
    """Yield (origin_url, signon_realm, username, encrypted_password, federation_url, last_used)."""
    src = _login_db(folder)
    tmp = Path(f"/tmp/aria_logins_{folder.replace(' ', '_')}_{int(time.time()*1000)}.db")
    shutil.copy2(src, tmp)
    try:
        conn = sqlite3.connect(tmp)
        try:
            rows = conn.execute(
                "SELECT origin_url, signon_realm, username_value, password_value, "
                "federation_url, date_last_used FROM logins WHERE blacklisted_by_user=0"
            ).fetchall()
        finally:
            conn.close()
    finally:
        tmp.unlink(missing_ok=True)
    return rows


def credentials_for(url: str, alias: str = "default"):
    """Saved logins whose origin matches url's registrable domain, best (most recently used) first.

    Returns a list of dicts: {username, password, origin, realm, federated(bool)}.
    `password` is "" for federated entries.
    """
    folder = folder_for_alias(alias)
    key = sync_auth.derive_aes_key(sync_auth.get_keychain_password())
    iv = b" " * 16
    want = registrable(_host_of(url))
    out = []
    for origin, realm, user, enc_pw, fed, last_used in _all_logins(folder):
        site = registrable(_host_of(origin or realm or ""))
        if site != want:
            continue
        federated = bool(fed)
        pw = ""
        if not federated and enc_pw:
            try:
                pw = sync_auth.decrypt_value(enc_pw, key, iv, strip_hash=False)
            except Exception:
                pw = ""
        out.append({
            "username": user or "",
            "password": pw,
            "origin": origin or realm or "",
            "realm": realm or "",
            "federated": federated,
            "_last": last_used or 0,
        })
    out.sort(key=lambda r: r["_last"], reverse=True)
    for r in out:
        r.pop("_last", None)
    return out


def redact(text: str, secrets) -> str:
    """Replace any secret substrings with ••• so they never reach logs/snapshots."""
    if not text:
        return text
    for s in secrets:
        if s and len(s) >= 3 and s in text:
            text = text.replace(s, "•••")
    return text


def _main(argv):
    """Diagnostic: list matching accounts for a URL WITHOUT revealing passwords."""
    import argparse
    ap = argparse.ArgumentParser(description="List saved Chrome logins matching a URL (passwords hidden)")
    ap.add_argument("url")
    ap.add_argument("--profile", default="default")
    o = ap.parse_args(argv)
    creds = credentials_for(o.url, o.profile)
    if not creds:
        print(f"no saved logins for {registrable(_host_of(o.url))} in profile {o.profile!r}")
        return
    for c in creds:
        kind = "federated→oauth" if c["federated"] else ("password set" if c["password"] else "no password")
        print(f"• {c['username'] or '(no username)'}  [{kind}]  <{c['origin']}>")


if __name__ == "__main__":
    _main(sys.argv[1:])
