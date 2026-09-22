"""Configuration loading.

A tiny .env reader rather than a dependency. Values already present in the
environment win, so an exported key overrides the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

PLACEHOLDER_VALUES = {"", "PASTE_YOUR_KEY_HERE", "your-key-here", "changeme"}


def load_env(path: str | Path = ".env", override: bool = False) -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ."""
    file = Path(path)
    loaded: dict[str, str] = {}
    if not file.exists():
        return loaded
    for line in file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value in PLACEHOLDER_VALUES:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
        loaded[key] = value
    return loaded


@dataclass(frozen=True)
class Settings:
    api_key: Optional[str]
    model: str
    base_url: str
    db_path: str
    input_price_per_mtok: Optional[float]
    price_as_of: Optional[str]

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key and self.api_key not in PLACEHOLDER_VALUES)

    @property
    def default_mode(self) -> str:
        """Fixture unless a usable key exists. Live is always opt-in."""
        return "live" if self.has_api_key else "fixture"


def get_settings(env_path: str | Path = ".env") -> Settings:
    load_env(env_path)
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    price_raw = os.environ.get("TYPESAFE_INPUT_PRICE_PER_MTOK", "").strip()
    try:
        price = float(price_raw) if price_raw else None
    except ValueError:
        price = None
    return Settings(
        api_key=key or None,
        model=os.environ.get("TYPESAFE_MODEL", "jev-1.13.0").strip(),
        base_url=os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").strip(),
        db_path=os.environ.get("SYNCROUTE_DB_PATH", "syncroute.db").strip(),
        input_price_per_mtok=price,
        price_as_of=os.environ.get("TYPESAFE_PRICE_AS_OF", "").strip() or None,
    )
