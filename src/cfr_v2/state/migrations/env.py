from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from cfr_v2.state.schema import metadata

config = context.config
url = config.get_main_option("sqlalchemy.url") or os.environ.get("CFR_STATE_DATABASE_URL")
if not url:
    raise RuntimeError("set sqlalchemy.url or CFR_STATE_DATABASE_URL")


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
