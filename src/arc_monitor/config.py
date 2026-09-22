"""Small, literal dotenv reader. Never expands variables or executes shell syntax."""

from __future__ import annotations

import os
from pathlib import Path
import re


def env_number(name, default, kind=float):
    try:
        return kind(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        raise ValueError(f"Invalid numeric configuration: {name}") from None


def load_env(path=".env", environ=None, *, required=False) -> list[str]:
    env = os.environ if environ is None else environ
    p = Path(path)
    if not p.is_file():
        if required:
            raise ValueError("Specified environment file does not exist")
        return []
    if p.stat().st_size > 65536:
        raise ValueError("Environment file exceeds 64 KiB")
    pending = {}
    for number, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid environment assignment at line {number}")
        if value[:1] in ('"', "'"):
            quote = value[0]
            end = value.find(quote, 1)
            if end < 0 or (value[end + 1 :].strip() and not value[end + 1 :].lstrip().startswith("#")):
                raise ValueError(f"Invalid environment quoting at line {number}")
            value = value[1:end]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        # Only application configuration is read; PATH and other process settings are untouched.
        if key.startswith("ARC_") or key in {"BITQUERY_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}:
            pending[key] = value
    loaded = []
    for key, value in pending.items():
        if key not in env:
            env[key] = value
            loaded.append(key)
    return loaded
