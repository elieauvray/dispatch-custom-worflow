"""Configuration de l'application, lue depuis les variables d'environnement.

Sur Upsun, la relation PostgreSQL expose `DATABASE_URL` (et les variables
`DATABASE_*`). En local, `DATABASE_URL` est défini par docker-compose.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        # Fallback : relation Upsun décodée depuis PLATFORM_RELATIONSHIPS.
        raw = os.environ.get("PLATFORM_RELATIONSHIPS")
        if raw:
            rels = json.loads(base64.b64decode(raw))
            db = rels.get("database", [{}])[0]
            if db:
                url = (
                    f"postgresql://{db['username']}:{db['password']}@{db['host']}:"
                    f"{db['port']}/{db['path']}"
                )
    if not url:
        url = "postgresql://dispatch:dispatch@localhost:5432/dispatch"
    # SQLAlchemy + psycopg 3
    # `upsun tunnel:single` renvoie une URL en pgsql:// ; Heroku-like : postgres://
    for prefix in ("pgsql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql://" + url[len(prefix) :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _public_base_url() -> str:
    explicit = os.environ.get("PUBLIC_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    raw = os.environ.get("PLATFORM_ROUTES")
    if raw:
        routes = json.loads(base64.b64decode(raw))
        for url, route in routes.items():
            if route.get("primary") and url.startswith("https://"):
                return url.rstrip("/")
        for url, route in routes.items():
            if route.get("type") == "upstream" and url.startswith("https://"):
                return url.rstrip("/")
    return "http://localhost:8000"


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=_database_url)
    secret_key: str = field(default_factory=lambda: os.environ.get("APP_SECRET_KEY", ""))
    encryption_key: str = field(default_factory=lambda: os.environ.get("ENCRYPTION_KEY", ""))
    webhook_secret: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_WEBHOOK_SECRET", "")
    )
    cookie_secure: bool = field(
        default_factory=lambda: _bool(os.environ.get("COOKIE_SECURE"), True)
    )
    public_base_url: str = field(default_factory=_public_base_url)
    default_model: str = "claude-opus-5-5"
    login_max_attempts: int = 5
    login_window_seconds: int = 15 * 60


AVAILABLE_MODELS: list[tuple[str, str]] = [
    ("claude-opus-5-5", "Claude Opus 5.5 (recommandé)"),
    ("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ("claude-haiku-5-5", "Claude Haiku 5.5"),
    ("claude-fable-5-2", "Claude Fable 5.2"),
]


@lru_cache
def get_settings() -> Settings:
    return Settings()
