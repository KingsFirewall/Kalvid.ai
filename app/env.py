"""Minimal .env loader — no python-dotenv dependency.

Real environment variables always win, so a shell export overrides the file.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> int:
    path = path or ROOT / ".env"
    if not path.exists():
        return 0
    loaded = 0
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:      # never clobber a real env var
            os.environ[key] = value
            loaded += 1
    return loaded


load_env()
