from __future__ import annotations

import argparse
import sys

from dashboard.build_id import describe
from dashboard import config as config_module
from dashboard.config import DEFAULT_PORT, LOOPBACK, ConfigError, load, validate

def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(
        prog="fb-dashboard.py",
        description="Freebuff Dashboard: read-only search and activity over the "
                    "Freebuff Desktop conversation databases",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    found.add_argument("--host", default=None,
                       help=f"interface to bind (default {LOOPBACK}; "
                            "0.0.0.0 is refused)")
    found.add_argument("--port", type=int, default=None,
                       help=f"port to bind (default {DEFAULT_PORT})")
    found.add_argument("--config",
                       help="a config.json to read instead of the one found in "
                            "the folder you run from (or beside the launcher)")
    found.add_argument("--no-index", action="store_true",
                       help="live SQL only: build nothing")
    found.add_argument("--activity", action="store_true",
                       help="start on the activity page (the turns in flight)"
                            " instead of the search page")
    found.add_argument("--rebuild-index", action="store_true",
                       help="discard and rebuild the conversation index "
                            "(not built yet)")
    found.add_argument("--version", action="store_true",
                       help="print the build identity and exit")
    return found

def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)

    if args.version:
        print(f"freebuff-dashboard {describe()}")
        return 0

    if args.rebuild_index:
        print("fb-dashboard: --rebuild-index is not built yet; nothing was done.")
        return 2

    source = args.config or config_module.default_path()
    try:
        config = load(source)
    except ConfigError as exc:
        print(f"fb-dashboard: {exc}", file=sys.stderr)
        return 2

    if args.host is not None:
        config["server"]["host"] = args.host
    if args.port is not None:
        config["server"]["port"] = args.port
    if args.no_index:
        config["index"]["enabled"] = False
    if args.activity:
        config["server"]["open_path"] = "/activity"
    try:
        validate(config)
    except ConfigError as exc:
        print(f"fb-dashboard: {exc}", file=sys.stderr)
        return 2

    from dashboard.app import serve

    return serve(config, str(source) if source is not None else None)

if __name__ == "__main__":
    sys.exit(main())
