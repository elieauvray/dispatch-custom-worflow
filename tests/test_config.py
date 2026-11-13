from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from app import config
from app.main import create_app


def _relationships(password: str) -> str:
    rels = {"database": [{"host": "database.internal", "port": 5432, "username": "main",
                          "password": password, "path": "main", "scheme": "pgsql"}]}
    return base64.b64encode(json.dumps(rels).encode()).decode()


def test_database_url_from_upsun_relationship(monkeypatch):
    monkeypatch.setenv("PLATFORM_RELATIONSHIPS", _relationships("p@ss:w/rd#1"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    url = make_url(config._database_url())
    assert url.drivername == "postgresql+psycopg"
    assert url.host == "database.internal" and url.port == 5432 and url.database == "main"
    assert url.password == "p@ss:w/rd#1"  # caractères spéciaux correctement encodés


def test_database_url_from_upsun_tunnel(monkeypatch):
    monkeypatch.delenv("PLATFORM_RELATIONSHIPS", raising=False)
    monkeypatch.delenv("DATABASE_HOST", raising=False)
    monkeypatch.setenv("DATABASE_URL", "pgsql://main:secret@127.0.0.1:30000/main")
    assert config._database_url() == "postgresql+psycopg://main:secret@127.0.0.1:30000/main"


def test_missing_secrets_serves_503_instead_of_crashing(monkeypatch):
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    monkeypatch.setenv("ENCRYPTION_KEY", "pas-une-cle-fernet")
    assert config.missing_secrets() == ["APP_SECRET_KEY", "ENCRYPTION_KEY"]
    client = TestClient(create_app())
    r = client.get("/settings")
    assert r.status_code == 503
    assert "APP_SECRET_KEY, ENCRYPTION_KEY" in r.text and "upsun variable:create" in r.text
    assert client.get("/healthz").status_code == 503
