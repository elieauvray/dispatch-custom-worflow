from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def configure_engine(url: str | None = None, **kwargs: object) -> Engine:
    """(Re)configure le moteur. Utilisé par l'app, le worker et les tests."""
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    if url.startswith("postgresql"):
        kwargs.setdefault("pool_pre_ping", True)
        kwargs.setdefault("pool_size", 5)
    _engine = create_engine(url, **kwargs)  # type: ignore[arg-type]
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        configure_engine()
    assert _engine is not None
    return _engine


def SessionLocal() -> Session:  # noqa: N802 - fabrique
    if _SessionLocal is None:
        configure_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
