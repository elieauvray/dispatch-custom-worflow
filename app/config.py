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


def _pg_url(user: str, password: str, host: str, port: str | int, database: str) -> str:
    """Construit l'URL SQLAlchemy en encodant correctement les caractères spéciaux."""
    from sqlalchemy.engine import URL

    url = URL.create(
        "postgresql+psycopg",
        username=user,
        password=password,
        host=host,
        port=int(port) if port else None,
        database=database,
    )
    return url.render_as_string(hide_password=False)


def _database_url() -> str:
    # 1. Upsun : relation « database » (PLATFORM_RELATIONSHIPS, toujours présent sur Upsun).
    raw = os.environ.get("PLATFORM_RELATIONSHIPS")
    if raw:
        try:
            rels = json.loads(base64.b64decode(raw))
            db = (rels.get("database") or [{}])[0]
        except (ValueError, KeyError, IndexError):
            db = {}
        if db.get("host"):
            return _pg_url(db["username"], db["password"], db["host"], db["port"], db["path"])
    # 2. Variables DATABASE_* (exposées par Upsun pour la relation « database »).
    if os.environ.get("DATABASE_HOST") and not os.environ.get("DATABASE_URL"):
        return _pg_url(
            os.environ.get("DATABASE_USERNAME", ""),
            os.environ.get("DATABASE_PASSWORD", ""),
            os.environ["DATABASE_HOST"],
            os.environ.get("DATABASE_PORT", "5432"),
            os.environ.get("DATABASE_PATH", ""),
        )
    # 3. DATABASE_URL explicite (local, tunnel Upsun en pgsql://, etc.).
    url = os.environ.get("DATABASE_URL", "") or "postgresql://dispatch:dispatch@localhost:5432/dispatch"
    for prefix in ("pgsql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql://" + url[len(prefix) :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def missing_secrets() -> list[str]:
    """Variables obligatoires absentes ou invalides (sans jamais exposer leur valeur)."""
    missing: list[str] = []
    if len(os.environ.get("APP_SECRET_KEY", "")) < 32:
        missing.append("APP_SECRET_KEY")
    key = os.environ.get("ENCRYPTION_KEY", "")
    try:
        from cryptography.fernet import Fernet

        Fernet(key.encode())
    except (ValueError, TypeError):
        missing.append("ENCRYPTION_KEY")
    return missing


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
