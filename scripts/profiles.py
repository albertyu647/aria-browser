"""
Chrome profile aliases, kept in a local config file instead of code.

~/.config/chrome-storage-states/profiles.json
    {"aliases": {"work": "Default", "personal": "Profile 2"}, "default": "personal"}

Without a config, every Chrome profile is reachable by a slug of its display name
("Default" folder also answers to "default"), and the default profile is "default".
Set aliases with:  sync_auth.py --alias work=Default --alias personal="Profile 2" --default personal
"""

import json
import re
from pathlib import Path

STORAGE_DIR = Path.home() / ".config" / "chrome-storage-states"
CONFIG = STORAGE_DIR / "profiles.json"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "profile"


def load() -> dict:
    try:
        cfg = json.loads(CONFIG.read_text())
    except Exception:
        cfg = {}
    cfg.setdefault("aliases", {})
    cfg.setdefault("default", "default")
    return cfg


def save(cfg: dict):
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")


def alias_for(folder: str, display_name: str) -> str:
    """Alias a Chrome profile folder is stored under (its configured alias, else a slug)."""
    for alias, f in load()["aliases"].items():
        if f == folder:
            return alias
    return "default" if folder == "Default" else slug(display_name or folder)


def resolve(name: str) -> str:
    """Normalize what the user typed (alias, folder name, any case) to a stored alias."""
    cfg = load()
    n = (name or cfg["default"]).strip()
    if n.lower() in (a.lower() for a in cfg["aliases"]):
        return next(a for a in cfg["aliases"] if a.lower() == n.lower())
    for alias, f in cfg["aliases"].items():
        if f.lower() == n.lower():
            return alias
    return slug(n)


def storage_path(name: str) -> Path:
    return STORAGE_DIR / f"{resolve(name)}.json"


def default_profile() -> str:
    return load()["default"]
