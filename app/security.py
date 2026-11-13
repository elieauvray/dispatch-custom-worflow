"""Hachage des mots de passe (argon2), chiffrement au repos (Fernet) et CSRF."""

from __future__ import annotations

import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# Hash factice pour garder un temps de réponse constant quand le login n'existe pas.
DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
        )
    return Fernet(key.encode())


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:  # clé changée ou donnée corrompue
        raise RuntimeError("Could not decrypt a stored value (was ENCRYPTION_KEY changed?)") from exc


def last4(value: str) -> str:
    return value[-4:] if len(value) >= 4 else ""


# --- CSRF -------------------------------------------------------------------

CSRF_SESSION_KEY = "csrf_token"


def get_or_create_csrf_token(session: dict[str, object]) -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_matches(session: dict[str, object], submitted: str | None) -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    if not isinstance(expected, str) or not submitted:
        return False
    return hmac.compare_digest(expected, submitted)
