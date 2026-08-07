"""The SQLAlchemy engine and session factory.

Responsible for: one engine, one pool, for the process.
Used by: build_container, which reuses these rather than opening a second pool.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.settings import settings

def get_engine():
    """
    Creates and configures the SQLAlchemy engine using application settings.
    """
    if not settings.database_url:
        raise ValueError("database_url must be set to initialize the engine.")
        
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )

engine = get_engine() if settings.database_url else None

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False
)
