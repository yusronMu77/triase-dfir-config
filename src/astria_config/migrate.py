"""Migrasi skema config (Alembic): satu-satunya jalur pembuatan tabel di start layanan dan di `astria-config migrate`.

Aturan `ensure_schema` (idempoten, aman diulang):
- ada `alembic_version`: `upgrade head` (no-op bila sudah di head);
- tidak ada riwayat tapi tabel `setting` sudah ada (DB dari `create_all` versi lama): `stamp head`,
  tabel tidak disentuh;
- basis data kosong: `upgrade head` membuat seluruh skema.

Keluaran hanya nama tindakan ("baru", "stamp", "ada"), tidak pernah URL atau nilai rahasia.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from alembic import command
from alembic.config import Config
from astria_common.log import get_logger
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, make_url, text
from sqlalchemy.engine import Engine

log = get_logger(__name__)

MIGRATIONS = Path(__file__).parent / "migrations"
Action = Literal["baru", "stamp", "ada"]


def sync_url(url: str) -> str:
    """URL sinkron untuk Alembic: psycopg 3 bekerja sinkron maupun async; SQLite memakai driver bawaan."""
    parsed = make_url(url)
    backend = parsed.get_backend_name()
    if backend == "postgresql":
        return parsed.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)
    if backend == "sqlite":
        return parsed.set(drivername="sqlite").render_as_string(hide_password=False)
    raise ValueError(f"dialek basis data tidak didukung: {backend!r} (pakai postgresql atau sqlite)")


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


def _connect_args(url: str, schema: str | None) -> dict[str, object]:
    if schema and _is_postgres(url):
        return {"options": f"-csearch_path={schema}"}
    return {}


def _engine(url: str, schema: str | None) -> Engine:
    return create_engine(sync_url(url), connect_args=_connect_args(url, schema))


def _config(url: str, schema: str | None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["url"] = url
    cfg.attributes["schema"] = schema
    return cfg


def ensure_schema(url: SecretStr | str, schema: str | None = None) -> Action:
    """Bawa skema basis data ke head. Lihat docstring modul untuk aturannya."""
    raw = url.get_secret_value() if isinstance(url, SecretStr) else url
    cfg = _config(raw, schema)
    engine = _engine(raw, schema)
    try:
        if schema and _is_postgres(raw):
            with engine.begin() as conn:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    if "alembic_version" in tables:
        action: Action = "ada"
        command.upgrade(cfg, "head")
    elif "setting" in tables:
        action = "stamp"
        command.stamp(cfg, "head")
    else:
        action = "baru"
        command.upgrade(cfg, "head")
    log.info("skema_migrasi", tindakan=action, skema=schema or "bawaan")
    return action
