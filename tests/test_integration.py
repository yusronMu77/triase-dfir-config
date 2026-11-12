"""Uji integrasi dengan PostgreSQL sungguhan (`ASTRIA_TEST_DATABASE_URL`, tersedia di kontainer py devenv).

Skema dibuat dari kosong tiap uji lewat migrasi Alembic (`ensure_schema`), jadi uji ini juga membuktikan migrasi
baseline hidup di PostgreSQL kosong. Audit dipalsukan; tidak ada panggilan jaringan keluar.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from astria_common import outbox
from astria_common.auth import SigningKey
from astria_common.db import Database
from astria_common.errors import AstriaError
from fastapi.testclient import TestClient
from helpers import ADMIN2_ACT, ADMIN_ACT, ANALIS_ACT, DB_URL, LANGKAH, FakeAudit, fresh, headers, make_app, wait_ready
from pydantic import SecretStr
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from astria_config import cli
from astria_config.auth import Actor
from astria_config.db import catalog_version, channel_policy, setting, utcnow
from astria_config.layers import Layers
from astria_config.migrate import ensure_schema
from astria_config.rotation import rotate_master
from astria_config.secrets import MasterKey, Sealed
from astria_config.service import ConfigService

pytestmark = [pytest.mark.anyio, pytest.mark.skipif(not DB_URL, reason="ASTRIA_TEST_DATABASE_URL tidak diisi")]

ADMIN = Actor("usr_admin01", frozenset({"admin"}), None)
ADMIN2 = Actor("usr_admin02", frozenset({"admin"}), None)
ANALIS = Actor("usr_analis01", frozenset({"analis"}), None)
CANARY = "nilai-uji-tidak-boleh-bocor-2"


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    schema = f"t_{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(ensure_schema, DB_URL or "", schema)  # skema dari migrasi, bukan create_all
    database = Database(SecretStr(DB_URL or ""), schema=schema)
    await database.start()
    await database.wait_ready(10)
    yield database
    async with database.engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await database.stop()


@pytest.fixture
def audit() -> FakeAudit:
    return FakeAudit()


@pytest.fixture
def master() -> MasterKey:
    return MasterKey.generate()


@pytest.fixture
def svc(db: Database, audit: FakeAudit, master: MasterKey) -> ConfigService:
    return ConfigService(db=db, layers=Layers(), master=master, audit=audit, gateway=None)


def _version(kind: str, number: int, status: str, **over: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "version": number,
        "status": status,
        "content": LANGKAH,
        "content_hash": "sha256:" + "0" * 64,
        "schema_version": "1",
        "created_by": "usr_a",
        "created_at": utcnow(),
    } | over


async def _insert(db: Database, table: Any, **values: Any) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(insert(table).values(**values))


# ---- skema dari kosong -----------------------------------------------------------------------------------------


async def test_schema_is_created_by_migration_from_empty_database(db: Database) -> None:
    async with db.engine.connect() as conn:
        tables = {
            r[0]
            for r in await conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
            )
        }
    assert {"setting", "setting_history", "catalog_version", "channel_policy", "outbox"} <= tables


async def test_database_enforces_one_active_version_per_kind(db: Database) -> None:
    await _insert(db, catalog_version, **_version("langkah", 1, "aktif"))
    with pytest.raises(IntegrityError):
        await _insert(db, catalog_version, **_version("langkah", 2, "aktif"))
    await _insert(db, catalog_version, **_version("langkah", 2, "arsip"))  # bukan aktif: boleh
    await _insert(db, catalog_version, **_version("sla", 1, "aktif", content={"band": {}}))  # jenis lain: boleh


async def test_database_enforces_four_eyes_and_status(db: Database) -> None:
    with pytest.raises(IntegrityError):
        await _insert(db, catalog_version, **_version("langkah", 1, "ditinjau", reviewed_by="usr_a"))
    with pytest.raises(IntegrityError):
        await _insert(db, catalog_version, **_version("langkah", 2, "sembarang"))
    await _insert(db, catalog_version, **_version("langkah", 3, "ditinjau", reviewed_by="usr_b"))


async def test_database_enforces_secret_has_ciphertext(db: Database) -> None:
    base = {"version": 1, "updated_by": "u", "updated_at": utcnow()}
    with pytest.raises(IntegrityError):
        await _insert(db, setting, key="model.api_key", is_secret=True, **base)  # rahasia tanpa ciphertext
    with pytest.raises(IntegrityError):
        await _insert(db, setting, key="chat.allow_t2", is_secret=False, ciphertext=b"x", nonce=b"n", **base)
    with pytest.raises(IntegrityError):
        await _insert(db, channel_policy, channel_id="x", active=True, max_detail="rahasia", **base)


# ---- konkurensi -------------------------------------------------------------------------------------------------


async def test_concurrent_put_setting_has_exactly_one_winner(svc: ConfigService, db: Database) -> None:
    async def attempt(n: int) -> str:
        try:
            await svc.put_setting(ADMIN, "model.timeout_seconds", 20 + n, 0, None)
        except AstriaError as exc:
            return exc.code
        return "ok"

    results = await asyncio.gather(*(attempt(n) for n in range(1, 9)))
    assert results.count("ok") == 1
    assert set(results) - {"ok"} == {"VERSI_KONFLIK"}
    async with db.engine.connect() as conn:
        row = (await conn.execute(select(setting).where(setting.c.key == "model.timeout_seconds"))).mappings().one()
        events = (await conn.execute(select(outbox.outbox).where(outbox.outbox.c.event_type == "config.changed"))).all()
    assert row["version"] == 1
    assert len(events) == 1  # hanya yang menang yang menerbitkan event


async def test_concurrent_secret_writes_never_corrupt(svc: ConfigService) -> None:
    results = await asyncio.gather(
        *(svc.put_secret(ADMIN, "model.api_key", f"sk-{n}") for n in range(6)), return_exceptions=True
    )
    ok = [r for r in results if isinstance(r, dict)]
    assert ok and all(isinstance(r, AstriaError) and r.code == "VERSI_KONFLIK" for r in results if r not in ok)
    # Apa pun pemenangnya, hasil akhirnya terbaca (AAD cocok dengan versi tersimpan).
    got = await svc.read_model_key("narrative")
    assert got["value"] in {f"sk-{n}" for n in range(6)}


async def test_concurrent_activation_leaves_exactly_one_active(svc: ConfigService, db: Database) -> None:
    for n in (1, 2):
        await _insert(
            db, catalog_version, **_version("langkah", n, "ditinjau", reviewed_by="usr_r", created_by="usr_a")
        )

    async def activate(n: int) -> str:
        try:
            await svc.activate(ADMIN, "langkah", n)
        except AstriaError as exc:
            return exc.code
        return "ok"

    results = await asyncio.gather(activate(1), activate(2))
    assert "ok" in results
    assert set(results) <= {"ok", "VERSI_KONFLIK", "KATALOG_BELUM_DITINJAU"}
    async with db.engine.connect() as conn:
        active = (
            await conn.execute(
                select(catalog_version.c.version).where(
                    catalog_version.c.kind == "langkah", catalog_version.c.status == "aktif"
                )
            )
        ).all()
    assert len(active) == 1


async def test_concurrent_drafts_get_distinct_versions(svc: ConfigService, db: Database) -> None:
    results = await asyncio.gather(
        *(svc.create_draft(ANALIS, "langkah", LANGKAH, None) for _ in range(5)), return_exceptions=True
    )
    numbers = sorted(r["version"] for r in results if isinstance(r, dict))
    assert numbers and numbers == sorted(set(numbers))
    assert all(isinstance(r, AstriaError) and r.code == "VERSI_KONFLIK" for r in results if not isinstance(r, dict))


# ---- alur lengkap di PostgreSQL ---------------------------------------------------------------------------------


async def test_catalog_flow_on_postgres(svc: ConfigService, audit: FakeAudit, db: Database) -> None:
    draft = await svc.create_draft(ANALIS, "langkah", LANGKAH, None)
    reviewed = await svc.review(ADMIN, "langkah", draft["version"])
    assert reviewed["status"] == "ditinjau"
    with pytest.raises(AstriaError) as same:
        await svc.activate(ANALIS, "langkah", draft["version"])
    assert same.value.code == "PENINJAU_SAMA_DENGAN_PENULIS"
    active = await svc.activate(ADMIN2, "langkah", draft["version"])
    assert active["status"] == "aktif"
    served = await svc.active_catalog("langkah")
    assert served["catalog_version"] == 1 and served["langkah"] == LANGKAH["langkah"]
    assert audit.actions() == ["config.catalog.activated"]
    async with db.engine.connect() as conn:
        types = [
            r[0] for r in await conn.execute(select(outbox.outbox.c.event_type).order_by(outbox.outbox.c.created_at))
        ]
    assert types.count("config.changed") == 1 and types.count("audit.record") == 2  # draf + tinjauan


async def test_audit_down_changes_nothing_on_postgres(svc: ConfigService, audit: FakeAudit, db: Database) -> None:
    audit.down = True
    with pytest.raises(Exception, match=r".*") as caught:
        await svc.put_setting(ADMIN, "chat.allow_t2", True, 0, None)
    assert type(caught.value).__name__ == "AuditUnavailable"
    async with db.engine.connect() as conn:
        assert (await conn.execute(select(setting))).all() == []
        assert (await conn.execute(select(outbox.outbox))).all() == []


# ---- rotate-master -----------------------------------------------------------------------------------------------


async def _two_secrets(svc: ConfigService, db: Database) -> None:
    await svc.put_secret(ADMIN, "model.api_key", CANARY)
    # Rahasia kedua dimasukkan langsung: hanya `model.api_key` ada di registri, jadi baris tiruan.
    sealed = svc._master.seal("lain.rahasia", 1, "nilai-lain")  # noqa: SLF001
    await _insert(
        db,
        setting,
        key="lain.rahasia",
        is_secret=True,
        ciphertext=sealed.ciphertext,
        nonce=sealed.nonce,
        key_version=sealed.key_version,
        version=1,
        updated_by="u",
        updated_at=utcnow(),
    )


async def _cipher(db: Database) -> dict[str, tuple[bytes, bytes, int]]:
    async with db.engine.connect() as conn:
        rows = (await conn.execute(select(setting).where(setting.c.is_secret.is_(True)))).mappings().all()
    return {r["key"]: (bytes(r["ciphertext"]), bytes(r["nonce"]), int(r["key_version"])) for r in rows}


async def test_rotate_master_is_one_transaction(svc: ConfigService, db: Database, master: MasterKey) -> None:
    await _two_secrets(svc, db)
    before = await _cipher(db)
    new = MasterKey.generate()

    async with db.engine.begin() as conn:
        assert await rotate_master(conn, master, new) == 2
    after = await _cipher(db)
    assert all(after[k][0] != before[k][0] and after[k][2] == before[k][2] + 1 for k in before)
    ciphertext, nonce, key_version = after["model.api_key"]
    assert new.open("model.api_key", 1, Sealed(ciphertext, nonce, key_version)) == CANARY

    # Kunci baru jadi kunci layanan: pembacaan berjalan.
    svc._master = new  # noqa: SLF001
    assert (await svc.read_model_key("narrative"))["value"] == CANARY


async def test_rotate_master_failure_rolls_back_everything(svc: ConfigService, db: Database, master: MasterKey) -> None:
    await _two_secrets(svc, db)
    async with db.engine.begin() as conn:  # rusak satu baris: ciphertext terpotong
        await conn.execute(
            setting.update().where(setting.c.key == "lain.rahasia").values(ciphertext=b"rusak-sekali-tidak-valid-xx")
        )
    before = await _cipher(db)
    with pytest.raises(Exception, match=r".*") as caught:
        async with db.engine.begin() as conn:
            await rotate_master(conn, master, MasterKey.generate())
    assert type(caught.value).__name__ == "SecretUnreadable"
    assert await _cipher(db) == before  # tidak ada baris yang setengah dirotasi


# ---- aplikasi penuh di PostgreSQL --------------------------------------------------------------------------------


def test_full_app_on_postgres_from_empty_database(tmp_path: Path, key: SigningKey) -> None:
    schema = f"t_{uuid.uuid4().hex[:8]}"
    audit = FakeAudit()
    app = make_app(
        tmp_path,
        key,
        audit=audit,
        database_url=SecretStr(DB_URL or ""),
        db_schema=schema,
        seed_channels=("wa-tim",),
    )
    try:
        with TestClient(app) as client:
            wait_ready(client)
            h = headers(key, ADMIN_ACT)
            assert client.get("/v1/channels/policy", headers=h).json()["items"][0]["channel_id"] == "wa-tim"
            put = client.put("/v1/settings/chat.allow_t2", json={"value": True, "if_version": 0}, headers=h)
            assert put.status_code == 200, put.text
            secret = client.put(
                "/v1/secrets/model.api_key", json={"value": CANARY}, headers=headers(key, ADMIN_ACT, reauth=fresh())
            )
            assert secret.status_code == 200 and CANARY not in secret.text
            got = client.get("/svc/v1/secrets/model.api_key", headers=headers(key, None, subject="svc:narrative"))
            assert got.json()["value"] == CANARY
            d = client.post(
                "/v1/catalogs/langkah/versions", json={"content": LANGKAH}, headers=headers(key, ANALIS_ACT)
            )
            assert d.status_code == 201
            assert client.post("/v1/catalogs/langkah/versions/1/review", headers=h).status_code == 200
            act = client.post(
                "/v1/catalogs/langkah/versions/1/activate", headers=headers(key, ADMIN2_ACT, reauth=fresh())
            )
            assert act.status_code == 200, act.text
            effective = client.get(
                "/svc/v1/settings/effective?scope=chat", headers=headers(key, None, subject="svc:triage")
            ).json()
            assert effective == {"scope": "chat", "values": {"chat.allow_t2": True}}
    finally:
        import psycopg

        with psycopg.connect(DB_URL.replace("+psycopg", ""), autocommit=True) as conn:  # type: ignore[union-attr]
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_cli_catalog_seed_on_postgres(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    schema = f"t_{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("ASTRIA_ENV", "test")
    monkeypatch.setenv("ASTRIA_DATABASE_URL", DB_URL or "")
    monkeypatch.setenv("ASTRIA_DB_SCHEMA", schema)
    path = tmp_path / "langkah.json"
    path.write_text('{"langkah": {"S-x": {"judul": "x", "risiko": "aman"}}}', encoding="utf-8")
    try:
        with pytest.raises(SystemExit) as done:
            cli.main(["catalog", "seed", "langkah", str(path), "--by", "seed-dev"])
        assert done.value.code == 0
    finally:
        import psycopg

        with psycopg.connect(DB_URL.replace("+psycopg", ""), autocommit=True) as conn:  # type: ignore[union-attr]
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
