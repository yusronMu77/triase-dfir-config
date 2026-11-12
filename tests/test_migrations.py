"""Migrasi Alembic: baseline dari kosong setara metadata, stamp untuk DB dari create_all, idempoten, CHECK tetap jalan.

Uji SQLite selalu jalan; uji PostgreSQL memakai `ASTRIA_TEST_DATABASE_URL` dengan skema sementara per uji.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from astria_common import outbox
from astria_common.auth import SigningKey
from fastapi.testclient import TestClient
from helpers import DB_URL, make_app, wait_ready
from pydantic import SecretStr
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from astria_config import __main__ as entry
from astria_config.db import metadata
from astria_config.migrate import _config, _engine, ensure_schema


def _expected() -> MetaData:
    """Skema yang diharapkan: metadata config plus tabel outbox milik astria-common."""
    combined = MetaData()
    for source in (metadata, outbox.metadata):
        for table in source.tables.values():
            table.to_metadata(combined)
    return combined


def _diff(url: str, schema: str | None = None) -> list:
    engine = _engine(url, schema)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True, "compare_server_default": False})
            return compare_metadata(ctx, _expected())
    finally:
        engine.dispose()


def _version(url: str, schema: str | None = None) -> str | None:
    engine = _engine(url, schema)
    try:
        with engine.connect() as conn:
            return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()


def _sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'config.db').as_posix()}"


def _sqlite_sync(tmp_path: Path) -> str:
    return f"sqlite:///{(tmp_path / 'config.db').as_posix()}"


def _sqlite_exec(tmp_path: Path, statement: str) -> None:
    with sqlite3.connect(tmp_path / "config.db") as conn:
        conn.execute(statement)


# ---- SQLite ----------------------------------------------------------------------------------------------------------


def test_empty_database_is_upgraded_to_metadata_schema(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    assert _diff(url) != []  # pembanding harus bisa gagal: basis data kosong jelas berbeda dari metadata
    assert ensure_schema(url) == "baru"
    assert _version(url) == "0001"
    assert _diff(url) == []


def test_existing_create_all_database_is_stamped_without_changes(tmp_path: Path) -> None:
    # DB dev lama: dibuat `create_all` (tanpa alembic_version). Harus di-stamp, bukan dibuat ulang.
    engine = create_engine(_sqlite_sync(tmp_path))
    _expected().create_all(engine)
    engine.dispose()
    _sqlite_exec(
        tmp_path,
        "INSERT INTO setting (key, value, is_secret, version, updated_by, updated_at) "
        "VALUES ('chat.allow_t2', 'true', 0, 1, 'usr_a', '2026-11-12T00:00:00')",
    )

    assert ensure_schema(_sqlite_url(tmp_path)) == "stamp"
    assert _version(_sqlite_url(tmp_path)) == "0001"
    assert _diff(_sqlite_url(tmp_path)) == []
    with sqlite3.connect(tmp_path / "config.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM setting").fetchone() == (1,)  # data tidak tersentuh


def test_migration_is_idempotent(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    assert ensure_schema(url) == "baru"
    assert ensure_schema(url) == "ada"
    assert ensure_schema(url) == "ada"
    assert _version(url) == "0001"
    assert _diff(url) == []


def test_downgrade_and_upgrade_round_trip(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    ensure_schema(url)
    cfg = _config(url, None)
    command.downgrade(cfg, "base")
    engine = create_engine(_sqlite_sync(tmp_path))
    assert "setting" not in inspect(engine).get_table_names()
    engine.dispose()
    command.upgrade(cfg, "head")
    assert _diff(url) == []


def test_constraints_hold_after_migration(tmp_path: Path) -> None:
    url = _sqlite_url(tmp_path)
    ensure_schema(url)
    catalog = (
        "INSERT INTO catalog_version (kind, version, status, content, content_hash, schema_version, created_by, "
        "created_at{extra}) VALUES ('{kind}', {version}, '{status}', '{{}}', 'sha256:0', '1', 'usr_a', "
        "'2026-11-12T00:00:00'{value})"
    )
    # Empat mata di basis data: peninjau sama dengan pembuat ditolak.
    with pytest.raises(sqlite3.IntegrityError):
        _sqlite_exec(
            tmp_path,
            catalog.format(extra=", reviewed_by", kind="langkah", version=1, status="ditinjau", value=", 'usr_a'"),
        )
    # Satu versi aktif per jenis.
    _sqlite_exec(tmp_path, catalog.format(extra="", kind="langkah", version=1, status="aktif", value=""))
    with pytest.raises(sqlite3.IntegrityError):
        _sqlite_exec(tmp_path, catalog.format(extra="", kind="langkah", version=2, status="aktif", value=""))
    # Rahasia wajib punya ciphertext.
    with pytest.raises(sqlite3.IntegrityError):
        _sqlite_exec(
            tmp_path,
            "INSERT INTO setting (key, is_secret, version, updated_by, updated_at) "
            "VALUES ('model.api_key', 1, 1, 'usr_a', '2026-11-12T00:00:00')",
        )
    # Kebijakan kanal hanya menerima tingkat detail yang dikenal.
    with pytest.raises(sqlite3.IntegrityError):
        _sqlite_exec(
            tmp_path,
            "INSERT INTO channel_policy (channel_id, active, max_detail, version, updated_by, updated_at) "
            "VALUES ('x', 1, 'rahasia', 1, 'usr_a', '2026-11-12T00:00:00')",
        )


def test_app_start_stamps_existing_database(tmp_path: Path, key: SigningKey) -> None:
    # Start layanan pada DB yang sudah ada dari create_all: stamp head, lalu /ready hidup.
    engine = create_engine(_sqlite_sync(tmp_path))
    _expected().create_all(engine)
    engine.dispose()
    with TestClient(make_app(tmp_path, key)) as client:
        wait_ready(client)
    assert _version(_sqlite_url(tmp_path)) == "0001"


def test_app_start_on_empty_database_migrates(tmp_path: Path, key: SigningKey) -> None:
    with TestClient(make_app(tmp_path, key)) as client:
        wait_ready(client)
    assert _version(_sqlite_url(tmp_path)) == "0001"
    assert _diff(_sqlite_url(tmp_path)) == []


def test_unreachable_database_keeps_ready_down(tmp_path: Path, key: SigningKey) -> None:
    bad = SecretStr(f"sqlite+aiosqlite:///{(tmp_path / 'tidak-ada' / 'x.db').as_posix()}")
    with TestClient(make_app(tmp_path, key, database_url=bad)) as client:
        assert client.get("/ready").status_code == 503


def test_cli_migrate_prints_action_and_is_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ASTRIA_ENV", "test")
    monkeypatch.setenv("ASTRIA_DATABASE_URL", _sqlite_url(tmp_path))
    assert entry.migrate() == 0
    assert "skema dibuat dari nol" in capsys.readouterr().out
    assert entry.migrate() == 0
    assert "riwayat sudah ada" in capsys.readouterr().out


def test_cli_migrate_without_database_url_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ASTRIA_ENV", "test")
    monkeypatch.delenv("ASTRIA_DATABASE_URL", raising=False)
    assert entry.migrate() == 2
    assert "ASTRIA_DATABASE_URL" in capsys.readouterr().err


# ---- PostgreSQL ------------------------------------------------------------------------------------------------------

pg = pytest.mark.skipif(not DB_URL, reason="ASTRIA_TEST_DATABASE_URL tidak diisi")


def _drop(url: str, schema: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    engine.dispose()


@pg
def test_postgres_empty_schema_matches_metadata() -> None:
    schema = f"m_{uuid.uuid4().hex[:8]}"
    url = DB_URL or ""
    try:
        assert ensure_schema(url, schema) == "baru"
        assert _version(url, schema) == "0001"
        assert _diff(url, schema) == []
    finally:
        _drop(url, schema)


@pg
def test_postgres_stamp_and_idempotent() -> None:
    schema = f"m_{uuid.uuid4().hex[:8]}"
    url = DB_URL or ""
    try:
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine.dispose()
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        _expected().create_all(engine)
        engine.dispose()
        assert ensure_schema(url, schema) == "stamp"
        assert ensure_schema(url, schema) == "ada"
        assert _version(url, schema) == "0001"
        assert _diff(url, schema) == []
    finally:
        _drop(url, schema)


@pg
def test_postgres_constraints_hold_after_migration() -> None:
    schema = f"m_{uuid.uuid4().hex[:8]}"
    url = DB_URL or ""
    try:
        ensure_schema(url, schema)
        engine = _engine(url, schema)
        try:
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO catalog_version (kind, version, status, content, content_hash, schema_version, "
                        "created_by, created_at, reviewed_by) VALUES ('langkah', 1, 'ditinjau', '{}', 'sha256:0', "
                        "'1', 'usr_a', now(), 'usr_a')"
                    )
                )
            with pytest.raises(IntegrityError), engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO channel_policy (channel_id, active, max_detail, version, updated_by, updated_at) "
                        "VALUES ('x', true, 'rahasia', 1, 'usr_a', now())"
                    )
                )
        finally:
            engine.dispose()
    finally:
        _drop(url, schema)
