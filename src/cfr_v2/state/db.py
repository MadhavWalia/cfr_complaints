from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str, **engine_kwargs) -> sa.Engine:
    engine_kwargs.setdefault("pool_pre_ping", True)
    return sa.create_engine(database_url, **engine_kwargs)


def make_sessionmaker(engine: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def run_migrations(database_url: str, revision: str = "head") -> None:
    """Apply Alembic migrations programmatically (used by tests and tooling)."""
    from importlib.resources import files

    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(files("cfr_v2.state") / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(cfg, revision)
