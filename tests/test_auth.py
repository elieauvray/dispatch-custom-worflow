from __future__ import annotations

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from tests.conftest import PASSWORD, csrf, login, make_user


def test_first_run_redirects_to_setup(client):
    r = client.get("/prompt", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    r = client.get("/login", follow_redirects=False)
    assert r.headers["location"] == "/setup"


def test_setup_creates_admin_then_404_forever(client):
    token = csrf(client, "/setup")
    r = client.post(
        "/setup",
        data={"login": "admin", "password": PASSWORD, "password_confirm": "autre-chose-123",
              "csrf_token": token},
    )
    assert r.status_code == 400 and "do not match" in r.text

    r = client.post(
        "/setup",
        data={"login": "admin", "password": PASSWORD, "password_confirm": PASSWORD, "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.login == "admin"))
        assert admin is not None and admin.is_admin
        assert admin.password_hash.startswith("$argon2")

    # Connecté directement, settings non remplis => bandeau
    page = client.get("/settings")
    assert page.status_code == 200 and "required settings are incomplete" in page.text

    assert client.get("/setup").status_code == 404
    token = csrf(client, "/settings")
    r = client.post(
        "/setup",
        data={"login": "pirate", "password": PASSWORD, "password_confirm": PASSWORD, "csrf_token": token},
    )
    assert r.status_code == 404


def test_session_cookie_flags(client):
    make_user("alice")
    r = login(client, "alice")
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie


def test_csrf_required(client):
    make_user("alice")
    r = client.post("/login", data={"login": "alice", "password": PASSWORD})
    assert r.status_code == 403


def test_login_rate_limit(client):
    make_user("alice")
    for _ in range(5):
        assert login(client, "alice", "mauvais-mot-de-passe").status_code == 401
    r = login(client, "alice", PASSWORD)
    assert r.status_code == 429


def test_logout(client):
    make_user("alice")
    login(client, "alice")
    assert client.get("/prompt", follow_redirects=False).status_code == 200
    token = csrf(client, "/prompt")
    client.post("/logout", data={"csrf_token": token})
    r = client.get("/prompt", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_unconfigured_user_is_blocked(client):
    make_user("admin", admin=True)
    make_user("bob", configured=False)
    login(client, "bob")
    r = client.get("/prompt", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings"
    assert client.get("/workflows", follow_redirects=False).status_code == 303
    assert client.get("/settings").status_code == 200
