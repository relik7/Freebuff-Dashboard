from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path

from . import __version__

TRACKED = ("fb-dashboard.py", "dashboard")

SKIP_DIRS = frozenset({"__pycache__"})
SKIP_SUFFIXES = (".pyc", ".pyo")

DIGEST_CHARS = 12

DIGEST_SEPARATOR = b"\0"

def source_files(root: Path, paths=TRACKED):
    for rel in paths:
        target = root / rel
        if target.is_file():
            found = [target]
        elif target.is_dir():
            found = [path for path in target.rglob("*") if path.is_file()
                     and not SKIP_DIRS.intersection(path.parts)
                     and not path.name.endswith(SKIP_SUFFIXES)]
        else:
            continue
        yield from sorted(found)

def fingerprint(root: Path, paths=TRACKED) -> str:
    digest = hashlib.sha256()
    for path in source_files(root, paths):
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(DIGEST_SEPARATOR)
        digest.update(data)
        digest.update(DIGEST_SEPARATOR)
    return digest.hexdigest()[:DIGEST_CHARS]

def git_revision(root: Path) -> str | None:
    def run(*argv: str) -> str | None:
        try:
            done = subprocess.run(["git", *argv], cwd=root, capture_output=True,
                                  text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    sha = run("rev-parse", "--short", "HEAD")
    if not sha:
        return None
    status = run("status", "--porcelain", "--", *TRACKED)
    if status is None:
        return sha
    return f"{sha}-dirty" if status else sha

@lru_cache(maxsize=None)
def identity(root: Path | None = None) -> dict:
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    revision = git_revision(root)
    digest = fingerprint(root)
    return {
        "version": __version__,
        "git": revision,
        "fingerprint": digest,
        "id": f"{__version__}+{revision or 'unknown'}.{digest}",
    }

def describe(root: Path | None = None) -> str:
    found = identity(root)
    return f"{found['version']} (build {found['id']})"
