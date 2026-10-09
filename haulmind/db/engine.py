"""Engine and session factory with SQLite WAL pragmas.

Synchronous SQLAlchemy is used on purpose: transactions are tiny, SQLite is local, and it keeps
the dependency footprint small. Async code paths keep each session short-lived.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from haulmind.config import Settings, get_settings

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _apply_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()


def build_engine(url: str) -> Engine:
    """Create an engine for ``url`` with WAL pragmas applied on every connection."""
    is_memory = url.endswith(":memory:") or url == "sqlite://"
    kwargs: dict[str, Any] = {"future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        if is_memory:
            kwargs["poolclass"] = StaticPool
        else:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite") and not is_memory:
        event.listen(engine, "connect", _apply_pragmas)
    elif is_memory:
        # foreign keys still matter for tests
        @event.listens_for(engine, "connect")
        def _fk(dbapi_conn: Any, _record: Any) -> None:
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    """Process-wide engine singleton."""
    global _engine, _session_factory
    if _engine is None:
        s = settings or get_settings()
        s.ensure_dirs()
        _engine = build_engine(s.sqlalchemy_url)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def set_engine(engine: Engine) -> None:
    """Testing hook: install a specific engine (e.g. in-memory)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = sessionmaker(bind=engine, expire_on_commit=False)


def reset_engine() -> None:
    """Testing hook: dispose and forget the engine."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success, roll back on error."""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(engine: Engine | None = None) -> None:
    """Create tables directly from the ORM (tests and in-memory use; prod uses Alembic)."""
    from haulmind.db.models import Base

    Base.metadata.create_all(engine or get_engine())
