"""Schema migrations as numbered SQL files (``migrations/NNNN_name.sql``), replacing Alembic.

All pending files apply in one transaction under an advisory lock, so concurrent
callers are safe and a failure leaves the schema unchanged. A database migrated
earlier by Alembic is adopted from its ``alembic_version`` row.

    python -m cfr_v2.state.migrate            # uses CFR_STATE_DATABASE_URL
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from importlib import resources

from .db import Database

_LOCK_KEY = 7_436_011_201  # arbitrary, fixed: serializes migration runs


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str


def available_migrations() -> list[Migration]:
    folder = resources.files("cfr_v2.state").joinpath("migrations")
    found = []
    for entry in folder.iterdir():
        if entry.name.endswith(".sql"):
            version, _, rest = entry.name.partition("_")
            found.append(Migration(version, rest.removesuffix(".sql"), entry.read_text(encoding="utf-8")))
    return sorted(found, key=lambda m: m.version)


def applied_versions(db: Database) -> list[str]:
    with db.transaction() as tx:
        if tx.scalar("SELECT to_regclass('schema_migration')") is None:
            return []
        return tx.column("SELECT version FROM schema_migration ORDER BY version")


def run_migrations(target: str | Database) -> list[str]:
    """Apply pending migrations; returns the versions applied by this call."""
    db = target if isinstance(target, Database) else Database(target, max_connections=1)
    try:
        with db.transaction() as tx:
            tx.execute("SELECT pg_advisory_xact_lock(%(k)s)", {"k": _LOCK_KEY})
            tx.execute(
                "CREATE TABLE IF NOT EXISTS schema_migration ("
                " version TEXT PRIMARY KEY, name TEXT NOT NULL,"
                " applied_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL)"
            )
            migrations = available_migrations()
            done = set(tx.column("SELECT version FROM schema_migration"))
            if not done and tx.scalar("SELECT to_regclass('alembic_version')") is not None:
                head = tx.scalar("SELECT version_num FROM alembic_version")
                for m in migrations:
                    if m.version <= head:
                        tx.execute(
                            "INSERT INTO schema_migration (version, name) VALUES (%(v)s, %(n)s)",
                            {"v": m.version, "n": m.name},
                        )
                        done.add(m.version)
            applied = []
            for m in migrations:
                if m.version in done:
                    continue
                tx.execute(m.sql)
                tx.execute(
                    "INSERT INTO schema_migration (version, name) VALUES (%(v)s, %(n)s)", {"v": m.version, "n": m.name}
                )
                applied.append(m.version)
        return applied
    finally:
        if db is not target:
            db.close()


if __name__ == "__main__":
    url = os.environ.get("CFR_STATE_DATABASE_URL", "postgresql://cfr:cfr@localhost:5432/cfr_state")
    print("applied:", run_migrations(url) or "nothing (up to date)")
