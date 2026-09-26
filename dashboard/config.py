from __future__ import annotations

import copy
import ipaddress
import json
import os
from pathlib import Path

LOOPBACK = "127.0.0.1"

DESKTOP_DIR = "freebuff-desktop"

BYOK_PRODUCT = "freebuff"

BYOK_STORE = "byok/connections.json"

CONFIG_NAME = "config.json"

DEFAULT_PORT = 8770

DEFAULT_CLOSE_SECONDS = 30.0
MAX_CLOSE_SECONDS = 3600.0

DEFAULTS = {
    "server": {"host": LOOPBACK, "port": DEFAULT_PORT, "open_browser": True,
               "open_path": "/"},
    "data": {"freebuff_config_root": None, "projects": None, "exclude": [],
             "watch_seconds": 1},
    "activity": {"close_seconds": DEFAULT_CLOSE_SECONDS},
    "index": {"enabled": True, "path": "index/freebuff-dashboard.db",
              "categories": ["conversation"], "refresh": "on-load",
              "refresh_seconds": 10, "refresh_min_seconds": 10},
    "search": {"mode": "words", "scope": "all",
               "categories": ["user", "assistant"],
               "limit": 50, "snippet_chars": 240},
    "ui": {"theme": "system", "hide_closed": False, "show_thinking": False},
}

MODES = ("words", "exact")
SCOPES = ("thread", "project", "all")
ROLES = ("user", "assistant")

CATEGORIES = ("user", "assistant", "reasoning", "tools", "changes")
DEFAULT_CATEGORIES = ("user", "assistant")
REFRESH = ("on-load", "manual", "never")

MIN_LIMIT = 1
MAX_LIMIT = 200

class ConfigError(Exception):
    pass

def config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg and Path(xdg).is_absolute():
        return Path(xdg)
    return Path.home() / ".config"

def default_root() -> Path:
    primary = config_home() / DESKTOP_DIR
    classic = Path.home() / ".config" / DESKTOP_DIR
    candidates = [primary] if primary == classic else [primary, classic]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return primary

def byok_path() -> Path:
    primary = config_home() / BYOK_PRODUCT / BYOK_STORE
    classic = Path.home() / ".config" / BYOK_PRODUCT / BYOK_STORE
    candidates = [primary] if primary == classic else [primary, classic]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return primary

def root_of(config: dict) -> Path:
    override = (config.get("data") or {}).get("freebuff_config_root")
    return Path(override) if override else default_root()

def default_path(folder: Path | None = None,
                 beside: Path | None = None) -> Path | None:
    here = Path(folder) if folder is not None else Path.cwd()
    script = (Path(beside) if beside is not None
              else Path(__file__).resolve().parent.parent / CONFIG_NAME)
    for candidate in (here / CONFIG_NAME, script):
        if candidate.is_file():
            return candidate
    return None

def close_seconds(config: dict) -> float:
    value = (config.get("activity") or {}).get("close_seconds")
    return DEFAULT_CLOSE_SECONDS if value is None else float(value)

def deep_merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base

def address_of(text: str):
    try:
        found = ipaddress.ip_address(str(text).strip())
    except ValueError:
        return None
    if isinstance(found, ipaddress.IPv6Address) and found.ipv4_mapped is not None:
        return found.ipv4_mapped
    return found

def parse_allowed_hosts(values) -> frozenset:
    found = set()
    for value in values or ():
        for piece in str(value).split(","):
            text = piece.strip()
            if not text:
                continue
            address = address_of(text)
            if address is None:
                raise ConfigError(f"--allowed-hosts {text!r} is not an IP "
                                  f"address")
            found.add(address)
    return frozenset(found)

def client_allowed(client: str, allowed) -> bool:
    if allowed is None:
        return True
    address = address_of(client)
    if address is None:
        return False
    return address.is_loopback or address in allowed

def load(path: str | Path | None = None, *, allow_remote: bool = False) -> dict:
    found = copy.deepcopy(DEFAULTS)
    if path is not None:
        target = Path(path)
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config {target}: {exc}") from exc
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ConfigError(f"config {target} is not JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"config {target} must be a JSON object, "
                              f"not {type(raw).__name__}")
        deep_merge(found, raw)
    validate(found, allow_remote=allow_remote)
    return found

def validate(config: dict, *, allow_remote: bool = False) -> None:
    server = config.get("server") or {}
    host = server.get("host", LOOPBACK)
    if host != LOOPBACK and not allow_remote:
        raise ConfigError(
            f"host {host!r} is refused: this tool has no security of its own "
            f"and would expose every conversation to anyone who can reach the "
            f"port, so it binds {LOOPBACK} only. Binding it to a wider "
            f"interface is a deliberate act: pass --i-know-the-security-risks, "
            f"and read README.md's network section first.")
    port = server.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) \
            or not 1 <= port <= 65535:
        raise ConfigError(f"server.port {port!r} is not a TCP port (1..65535)")
    if "open_browser" in server and not isinstance(server["open_browser"], bool):
        raise ConfigError("server.open_browser must be true or false")
    open_path = server.get("open_path")
    if open_path is not None and (not isinstance(open_path, str)
                                  or not open_path.startswith("/")):
        raise ConfigError(f"server.open_path {open_path!r} must be a path on "
                          f"this server, beginning with /")

    search = config.get("search") or {}
    for key, allowed in (("mode", MODES), ("scope", SCOPES)):
        value = search.get(key)
        if value is not None and value not in allowed:
            raise ConfigError(f"search.{key} {value!r} is not one of "
                              f"{', '.join(allowed)}")
    categories = search.get("categories")
    if categories is not None:
        if not isinstance(categories, list) or any(name not in CATEGORIES
                                                  for name in categories):
            raise ConfigError(f"search.categories {categories!r} must be a list "
                              f"drawn from {', '.join(CATEGORIES)}")
    for key, low, high in (("limit", MIN_LIMIT, MAX_LIMIT),
                           ("snippet_chars", 1, 100_000)):
        value = search.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) \
                or not low <= value <= high:
            raise ConfigError(f"search.{key} {value!r} must be an integer in "
                              f"{low}..{high}")

    index = config.get("index") or {}
    if index.get("refresh") is not None and index["refresh"] not in REFRESH:
        raise ConfigError(f"index.refresh {index['refresh']!r} is not one of "
                          f"{', '.join(REFRESH)}")

    data = config.get("data") or {}
    exclude = data.get("exclude")
    if exclude is not None and not isinstance(exclude, list):
        raise ConfigError("data.exclude must be a list of project labels or paths")
    watch = data.get("watch_seconds")
    if watch is not None and (isinstance(watch, bool)
                              or not isinstance(watch, (int, float))
                              or watch < 0):
        raise ConfigError("data.watch_seconds must be a number of seconds "
                          "(0 turns the change log off)")

    activity = config.get("activity") or {}
    close = activity.get("close_seconds")
    if close is not None and (isinstance(close, bool)
                              or not isinstance(close, (int, float))
                              or not 0 <= close <= MAX_CLOSE_SECONDS):
        raise ConfigError(f"activity.close_seconds {close!r} must be a number "
                          f"of seconds in 0..{MAX_CLOSE_SECONDS:g} — how long "
                          f"the activity page shows a finished turn before it "
                          f"is removed (0 removes it at once)")
