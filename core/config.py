"""Application configuration (app_spec.md §1.1).

Settings come from ``config.toml`` at the repo root, or from the file named
by the ``EXPENSE_TRACKER_CONFIG`` environment variable.  The file is
optional and every key in it is optional; anything missing takes the
default below.  No other module may hardcode these values.

``EXPENSE_TRACKER_DB`` still overrides ``storage.db_path`` (tests use it).
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field, fields
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


@dataclass(frozen=True)
class LLMConfig:
    host: str = "http://localhost:11434"
    model: str = "llama3.1"
    chunk_timeout_s: float = 120.0
    max_retries: int = 1


@dataclass(frozen=True)
class MoneyConfig:
    currency_code: str = "INR"
    currency_symbol: str = "₹"
    # Fixed for the life of a database: stored amounts and transaction ids
    # are in these units.
    minor_units: int = 2


@dataclass(frozen=True)
class StorageConfig:
    db_path: str = "data/expenses.db"


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(frozen=True)
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    money: MoneyConfig = field(default_factory=MoneyConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    web: WebConfig = field(default_factory=WebConfig)
    source: Optional[Path] = None  # the file the values came from, if any

    @property
    def db_path(self) -> Path:
        """Database file: ``EXPENSE_TRACKER_DB``, else ``storage.db_path``
        (relative paths are relative to the repo root)."""
        override = os.environ.get("EXPENSE_TRACKER_DB")
        path = Path(override or self.storage.db_path)
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def llm_is_local(self) -> bool:
        return is_loopback(urlparse(self.llm.host).hostname or "")

    @property
    def web_is_local(self) -> bool:
        return is_loopback(self.web.host)


_SECTIONS = {"llm": LLMConfig, "money": MoneyConfig, "storage": StorageConfig, "web": WebConfig}


class ConfigError(ValueError):
    """config.toml is malformed; the message names the bad key."""


def load_config(path: Optional[str | Path] = None) -> Config:
    """Read *path* (default: env var, then ``config.toml``) into a Config."""
    if path is None:
        path = os.environ.get("EXPENSE_TRACKER_CONFIG") or DEFAULT_CONFIG_PATH
    path = Path(path)
    if not path.exists():
        return Config()

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc

    unknown = set(raw) - set(_SECTIONS)
    if unknown:
        raise ConfigError(f"{path}: unknown section(s) {sorted(unknown)}")

    sections = {}
    for name, cls in _SECTIONS.items():
        values = raw.get(name, {})
        if not isinstance(values, dict):
            raise ConfigError(f"{path}: [{name}] must be a table")
        sections[name] = _build(cls, values, f"{path}: [{name}]")
    return Config(**sections, source=path)


def _build(cls, values: dict, where: str):
    allowed = {f.name: f for f in fields(cls)}
    unknown = set(values) - set(allowed)
    if unknown:
        raise ConfigError(f"{where} unknown key(s) {sorted(unknown)}")
    kwargs = {}
    for key, value in values.items():
        expected = type(getattr(cls(), key))
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if type(value) is not expected:
            raise ConfigError(f"{where} {key} must be {expected.__name__}, got {value!r}")
        kwargs[key] = value
    result = cls(**kwargs)
    _check(result, where)
    return result


def _check(section, where: str) -> None:
    if isinstance(section, LLMConfig):
        if urlparse(section.host).scheme not in ("http", "https"):
            raise ConfigError(f"{where} host must be an http(s) URL, got {section.host!r}")
        if section.chunk_timeout_s <= 0 or section.max_retries < 0:
            raise ConfigError(f"{where} chunk_timeout_s must be > 0 and max_retries >= 0")
    if isinstance(section, MoneyConfig) and not 0 <= section.minor_units <= 4:
        raise ConfigError(f"{where} minor_units must be between 0 and 4")
    if isinstance(section, WebConfig) and not 0 < section.port < 65536:
        raise ConfigError(f"{where} port must be 1-65535")


def is_loopback(host: str) -> bool:
    """True for ``localhost`` and loopback IPs (127.0.0.0/8, ::1)."""
    host = host.strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@lru_cache(maxsize=1)
def get_config() -> Config:
    """The process-wide configuration, loaded once."""
    return load_config()
