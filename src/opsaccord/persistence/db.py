"""Engine and session management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from opsaccord.config import Settings, get_settings


def build_engine(url: str, *, pool_size: int = 5, echo: bool = False) -> Engine:
    return create_engine(
        url,
        pool_size=pool_size,
        max_overflow=5,
        pool_pre_ping=True,
        future=True,
        echo=echo,
    )


class Database:
    """Thin owner of an engine plus short-lived transactional sessions."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self.engine = build_engine(url, echo=echo)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def read_session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()
            session.rollback() if session.in_transaction() else None

    def dispose(self) -> None:
        self.engine.dispose()


def database_from_settings(settings: Settings | None = None) -> Database:
    settings = settings or get_settings()
    return Database(settings.state_database_url)
