"""SQLAlchemy engine / session wiring.

The engine is created lazily so that layers with no DB dependency (e.g. Layer 1
ingestion and its tests) can be imported and exercised without a running
Postgres. Idempotency and reconciliation tests DO require real Postgres — see
docker-compose.yml.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from config import settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


class Base(DeclarativeBase):
    pass


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        # The Phase 4 burst test opens one real connection per concurrent worker;
        # the SQLAlchemy default (5 + 10 overflow) would serialise them at the
        # pool and make the concurrency test vacuous. Sized for the burst.
        _engine = create_engine(
            settings.database_url,
            future=True,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
    return _engine


def get_session() -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), autoflush=False, future=True)
    return _SessionFactory()


def init_db() -> None:
    """Create all tables. Demo-scale: production would use Alembic migrations."""
    import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=get_engine())
