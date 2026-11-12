"""Lingkungan Alembic config. URL dari `ASTRIA_DATABASE_URL` (atau dari `attributes` bila dipanggil `migrate.py`)."""

from __future__ import annotations

from alembic import context
from astria_common import outbox

from astria_config.db import metadata
from astria_config.migrate import _engine
from astria_config.settings import Settings

# Skema yang diharapkan untuk `alembic check` dan `revision --autogenerate`; migrasi tetap ditulis tangan.
TARGET = [metadata, outbox.metadata]


def _target() -> tuple[str, str | None]:
    url = context.config.attributes.get("url")
    if url is not None:
        return url, context.config.attributes.get("schema")
    settings = Settings()
    if settings.database_url is None:
        raise RuntimeError("ASTRIA_DATABASE_URL wajib diisi untuk migrasi")
    return settings.database_url.get_secret_value(), settings.db_schema


def run_migrations_online() -> None:
    url, schema = _target()
    connectable = _engine(url, schema)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=TARGET,
            version_table_schema=schema,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


run_migrations_online()
