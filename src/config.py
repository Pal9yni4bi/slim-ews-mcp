"""Configuration loading and validation.

All settings come from environment variables, optionally provided via a .env
file in the project root. Secrets are never hardcoded and never logged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root regardless of the current working directory,
# so the server works no matter where the MCP client launches it from.
# Real environment variables take precedence over .env values.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

VALID_AUTH_TYPES = ("basic", "ntlm", "digest", "gssapi", "sspi")
VALID_ACCESS_TYPES = ("delegate", "impersonation")


class ConfigError(Exception):
    """Raised when the environment configuration is missing or invalid."""


def _get_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_bool(name: str, default: bool = False) -> bool:
    value = _get_str(name).lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _get_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = _get_str(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got: {raw!r}")
    if not min_value <= value <= max_value:
        raise ConfigError(f"{name} must be between {min_value} and {max_value}")
    return value


@dataclass(frozen=True)
class Config:
    endpoint: str | None
    autodiscover: bool
    email: str
    username: str
    password: str
    auth_type: str | None  # None = let exchangelib autodetect
    access_type: str
    insecure_tls: bool
    timeout: int
    max_list_limit: int


@lru_cache(maxsize=1)
def load_config() -> Config:
    """Read and validate configuration from the environment (cached)."""
    endpoint = _get_str("EWS_ENDPOINT") or None
    autodiscover = _get_bool("EWS_AUTODISCOVER")
    email = _get_str("EWS_EMAIL")
    username = _get_str("EWS_USERNAME") or email
    password = _get_str("EWS_PASSWORD")
    auth_type = _get_str("EWS_AUTH_TYPE").lower() or None
    access_type = _get_str("EWS_ACCESS_TYPE").lower() or "delegate"

    problems = []
    if not email or "@" not in email:
        problems.append("EWS_EMAIL must be a valid primary SMTP address")
    if not password:
        problems.append("EWS_PASSWORD is required")
    if not autodiscover and not endpoint:
        problems.append("EWS_ENDPOINT is required (or set EWS_AUTODISCOVER=true)")
    if endpoint and not endpoint.lower().startswith(("https://", "http://")):
        problems.append("EWS_ENDPOINT must be a full URL, e.g. https://mail.example.com/EWS/Exchange.asmx")
    if auth_type is not None and auth_type not in VALID_AUTH_TYPES:
        problems.append(f"EWS_AUTH_TYPE must be one of {', '.join(VALID_AUTH_TYPES)} (or empty for autodetect)")
    if access_type not in VALID_ACCESS_TYPES:
        problems.append(f"EWS_ACCESS_TYPE must be one of {', '.join(VALID_ACCESS_TYPES)}")
    if problems:
        raise ConfigError(
            "Invalid configuration:\n  - " + "\n  - ".join(problems)
            + "\nSee .env.example for the expected variables."
        )

    return Config(
        endpoint=endpoint,
        autodiscover=autodiscover,
        email=email,
        username=username,
        password=password,
        auth_type=auth_type,
        access_type=access_type,
        insecure_tls=_get_bool("EWS_INSECURE_TLS"),
        timeout=_get_int("EWS_TIMEOUT", default=30, min_value=1, max_value=600),
        max_list_limit=_get_int("EWS_MAX_LIST_LIMIT", default=100, min_value=1, max_value=1000),
    )
