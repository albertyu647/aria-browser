#!/usr/bin/env python3
"""
Chrome Profile Storage State Sync for AI Agents
Extracts and decrypts authenticated cookies from Google Chrome profiles on macOS
into standard Playwright storage_state files for 100% silent headless automation.
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import argparse
import subprocess
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.backends import default_backend

sys.path.insert(0, str(Path(__file__).parent))
import profiles as profiles_cfg  # noqa: E402

STORAGE_DIR = Path.home() / ".config" / "chrome-storage-states"
CHROME_BASE = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


def get_keychain_password() -> bytes:
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
            stderr=subprocess.DEVNULL
        ).strip()
    except subprocess.CalledProcessError:
        raise RuntimeError("Failed to retrieve Chrome Safe Storage key from macOS Keychain.")


def derive_aes_key(password: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1003,
        backend=default_backend(),
    )
    return kdf.derive(password)


def decrypt_value(enc_bytes: bytes, key: bytes, iv: bytes, strip_hash: bool = False) -> str:
    if not enc_bytes:
        return ""
    if enc_bytes.startswith(b"v10") and strip_hash:
        # Cookie DB schema >= 24 (Chrome 130+): plaintext = SHA256(host_key)[32] + value
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        dec = decryptor.update(enc_bytes[3:]) + decryptor.finalize()
        pad = dec[-1]
        if isinstance(pad, int) and 1 <= pad <= 16:
            dec = dec[:-pad]
        return dec[32:].decode("utf-8", errors="replace")
    if enc_bytes.startswith(b"v10"):
        raw = enc_bytes[3:]
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        dec = decryptor.update(raw) + decryptor.finalize()
        pad = dec[-1]
        if isinstance(pad, int) and pad <= 16:
            dec = dec[:-pad]
        try:
            return dec.decode("utf-8")
        except UnicodeDecodeError:
            pass
        if len(dec) > 32:
            try:
                return dec[32:].decode("utf-8")
            except UnicodeDecodeError:
                pass
        return dec[32:].decode("utf-8", errors="ignore") if len(dec) > 32 else dec.decode("utf-8", errors="ignore")
    return enc_bytes.decode("utf-8", errors="ignore")


def is_valid_cookie_val(val: str) -> bool:
    if not val:
        return False
    for ch in val:
        if ord(ch) < 32 and ch not in "\t":
            return False
    return True


def samesite_to_str(val: int) -> str:
    if val == 1:
        return "Lax"
    if val == 2:
        return "Strict"
    return "None"


def list_profiles():
    local_state_path = CHROME_BASE / "Local State"
    if not local_state_path.exists():
        return []
    with open(local_state_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    info_cache = data.get("profile", {}).get("info_cache", {})
    profiles = []
    for folder, info in info_cache.items():
        name = info.get("name", folder)
        email = info.get("user_name", "")
        profiles.append({
            "folder": folder,
            "name": name,
            "email": email,
            "alias": profiles_cfg.alias_for(folder, name),
        })
    return profiles


def sync_profile(folder_name: str, alias: str, key: bytes, iv: bytes) -> Path:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    cookie_file = CHROME_BASE / folder_name / "Cookies"
    if not cookie_file.exists():
        cookie_file = CHROME_BASE / folder_name / "Network" / "Cookies"

    if not cookie_file.exists():
        raise FileNotFoundError(f"Cookies file not found for profile {folder_name} at {cookie_file}")

    tmp_db = Path(f"/tmp/chrome_sync_{alias}_{int(time.time()*1000)}.db")
    shutil.copy2(cookie_file, tmp_db)

    conn = sqlite3.connect(tmp_db)
    cur = conn.cursor()
    try:
        cur.execute("SELECT value FROM meta WHERE key='version'")
        db_version = int(cur.fetchone()[0])
    except Exception:
        db_version = 0
    strip_hash = db_version >= 24
    cur.execute("""
        SELECT host_key, name, encrypted_value, value, path, expires_utc, is_secure, is_httponly, samesite, last_access_utc
        FROM cookies
    """)
    rows = cur.fetchall()
    conn.close()
    if tmp_db.exists():
        tmp_db.unlink()

    now = time.time()
    valid_cookies = []

    # Oldest-accessed first: Chrome keeps ~3300 cookies (180/site) and evicts the oldest-inserted,
    # so ordering by last access makes the sites you actually use survive the load.
    rows.sort(key=lambda r: r[9] or 0)
    for host, name, enc_val, val, path, expires_utc, is_secure, is_http, samesite, _last in rows:
        cookie_val = decrypt_value(enc_val, key, iv, strip_hash) if enc_val else val
        if not cookie_val or not is_valid_cookie_val(cookie_val):
            continue

        secure = bool(is_secure)
        ss = samesite_to_str(samesite)
        if ss == "None" and not secure:
            ss = "Lax"

        c_obj = {
            "name": name,
            "value": cookie_val,
            "domain": host,
            "path": path or "/",
            "httpOnly": bool(is_http),
            "secure": secure,
            "sameSite": ss,
        }

        if expires_utc and expires_utc > 0:
            exp_sec = (expires_utc / 1000000.0) - 11644473600.0
            if exp_sec < now:
                continue
            c_obj["expires"] = exp_sec

        valid_cookies.append(c_obj)

    out_file = STORAGE_DIR / f"{alias}.json"
    storage_state = {"cookies": valid_cookies, "origins": []}
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(storage_state, f, indent=2)

    return out_file


def sync_all(quiet=False):
    pw = get_keychain_password()
    key = derive_aes_key(pw)
    iv = b" " * 16

    profiles = list_profiles()
    results = {}
    for p in profiles:
        out_file = sync_profile(p["folder"], p["alias"], key, iv)
        cookie_count = len(json.load(open(out_file))["cookies"])
        results[p["alias"]] = {
            "file": str(out_file),
            "profile": p,
            "cookie_count": cookie_count,
        }
        if not quiet:
            print(f"Synced [{p['alias'].upper()}] ({p['email']}) -> {out_file} ({cookie_count} cookies)")
    return results


def main():
    parser = argparse.ArgumentParser(description="Sync Chrome profiles to Playwright storage_state JSON files")
    parser.add_argument("--list", action="store_true", help="List detected Chrome profiles")
    parser.add_argument("--path", type=str, help="Print path to a profile's storage state (alias or folder)")
    parser.add_argument("--json", action="store_true", help="Output summary in JSON format")
    parser.add_argument("--alias", action="append", default=[], metavar="NAME=FOLDER",
                        help='Name a Chrome profile folder, e.g. --alias work=Default --alias personal="Profile 2"')
    parser.add_argument("--default", help="Profile alias used when --profile is not given")
    args = parser.parse_args()

    if args.alias or args.default:
        cfg = profiles_cfg.load()
        for a in args.alias:
            name, _, folder = a.partition("=")
            if not folder:
                parser.error(f"--alias expects NAME=FOLDER, got {a!r}")
            cfg["aliases"][name.strip()] = folder.strip()
        if args.default:
            cfg["default"] = args.default
        profiles_cfg.save(cfg)
        print(json.dumps(cfg, indent=2))
        return

    if args.list:
        profiles = list_profiles()
        print(json.dumps(profiles, indent=2) if args.json else "\n".join(
            f"• [{p['alias']}] {p['name']} ({p['email']}) [folder: {p['folder']}]" for p in profiles
        ))
        return

    if args.path:
        target = profiles_cfg.storage_path(args.path)
        if not target.exists():
            sync_all(quiet=True)
        print(str(target))
        return

    results = sync_all(quiet=args.json)
    if args.json:
        print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
