from __future__ import annotations

import os
from pathlib import Path


def get_cookie_file(
    provider: str,
    *,
    explicit: str | None = None,
    cookie_dir: str | None = None,
) -> str | None:
    if explicit:
        return explicit

    key = provider.upper().replace("-", "_")
    for env_key in (
        f"PAPER_DOWNLOAD_{key}_COOKIE_FILE",
        f"PUBLISHER_NATIVE_{key}_COOKIE_FILE",
    ):
        value = os.getenv(env_key)
        if value:
            return value

    root = Path(
        cookie_dir
        or os.getenv("PUBLISHER_COOKIE_DIR")
        or os.getenv("PAPER_DOWNLOAD_COOKIE_DIR")
        or "config/cookies"
    )
    for name in (
        f"{provider}.cookies.txt",
        f"{provider}.cookies.cdut.txt",
    ):
        candidate = root / name
        if candidate.exists():
            return str(candidate)
    return None
