"""Kebijakan kanal dan pengaturan efektif (subset identity#4): jalur baca `/svc/v1`, seed dev, dan otorisasi.

Setiap uji HTTP berjalan di SQLite (selalu) dan PostgreSQL (bila ASTRIA_TEST_DATABASE_URL diisi, skema sementara).
"""

import json
import os
import time
import uuid
from pathlib import Path

import pytest
from astria_common.auth import JwksSource, ServiceTokenVerifier, issue_service_token, load_or_create_signing_key
from astria_common.envguard import EnvironmentGuardError
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, text

from astria_config.main import build_app
from astria_config.settings import Settings
from astria_config.store import Seed, apply_seed, effective, setting

PG_URL = os.environ.get("ASTRIA_TEST_DATABASE_URL")
POLICIES = [
    {"channel_id": "telegram-tim", "active": True, "max_detail": "minimal", "band_min": "RENDAH"},
    {"channel_id": "wa-tim", "active": False, "max_detail": "ringkas", "band_min": "TINGGI"},
]
SEED = {"channel_policies": POLICIES, "settings": {"chat": {"chat.allow_t2": False}}}


@pytest.fixture(scope="module")
def key(tmp_path_factory: pytest.TempPathFactory):
    return load_or_create_signing_key(tmp_path_factory.mktemp("kunci") / "identity.pem")


def verifier_for(key) -> ServiceTokenVerifier:
    return ServiceTokenVerifier(JwksSource("http://identity.invalid", fetch=lambda: {"keys": [key.public_jwk()]}))


def write_seed(path: Path, seed: dict) -> Path:
    path.write_text(json.dumps(seed), encoding="utf-8")
    return path


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="tanpa PG"))])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> dict:
    if request.param == "sqlite":
        return {"database_url": SecretStr(f"sqlite+aiosqlite:///{tmp_path}/config_test.db")}
    return {"database_url": SecretStr(PG_URL or ""), "db_schema": f"t_{uuid.uuid4().hex[:8]}"}


@pytest.fixture
def client(tmp_path: Path, db: dict, key):
    settings = Settings(env="test", config_seed_file=write_seed(tmp_path / "seed.json", SEED), **db)
    app = build_app(settings, token_verifier=verifier_for(key))
    with TestClient(app) as c:
        deadline = time.monotonic() + 10
        while (ready := c.get("/ready")).status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.status_code == 200, ready.text
        yield c
        if "db_schema" in db:
            c.portal.call(_drop, app.state.db.engine, db["db_schema"])


async def _drop(engine, schema: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def bearer(key, subject: str = "svc:api", audience: str = "svc:config") -> dict[str, str]:
    token, _ = issue_service_token(key, subject=subject, audience=audience)
    return {"Authorization": f"Bearer {token}"}


def test_channel_policy_lists_seeded_channels(client: TestClient, key) -> None:
    for caller in ("svc:api", "svc:notifier"):
        resp = client.get("/svc/v1/channels/policy", headers=bearer(key, caller))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"items": POLICIES}


def test_effective_settings_by_scope(client: TestClient, key) -> None:
    resp = client.get("/svc/v1/settings/effective", params={"scope": "chat"}, headers=bearer(key))
    assert resp.json() == {"scope": "chat", "values": {"chat.allow_t2": False}}
    resp = client.get("/svc/v1/settings/effective", params={"scope": "triage"}, headers=bearer(key, "svc:triage"))
    assert resp.json() == {"scope": "triage", "values": {}}


def test_invalid_scope_is_400(client: TestClient, key) -> None:
    resp = client.get("/svc/v1/settings/effective", params={"scope": "Chat;--"}, headers=bearer(key))
    assert resp.status_code == 400
    assert client.get("/svc/v1/settings/effective", headers=bearer(key)).status_code == 400


def test_requires_service_token(client: TestClient, key) -> None:
    assert client.get("/svc/v1/channels/policy").status_code == 401
    resp = client.get("/svc/v1/channels/policy", headers=bearer(key, audience="svc:identity"))
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "AUDIENS_SALAH")


@pytest.mark.parametrize(
    ("path", "caller"),
    [
        ("/svc/v1/channels/policy", "svc:gateway"),
        ("/svc/v1/channels/policy", "svc:triage"),
        ("/svc/v1/settings/effective?scope=chat", "svc:gateway"),
    ],
)
def test_callers_are_restricted(client: TestClient, key, path: str, caller: str) -> None:
    resp = client.get(path, headers=bearer(key, caller))
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "PEMANGGIL_DITOLAK")


def test_seed_is_idempotent_and_versions_changes(client: TestClient) -> None:
    engine = client.app.state.db.engine
    same = Seed.model_validate(SEED)
    changed = Seed.model_validate(SEED | {"settings": {"chat": {"chat.allow_t2": True}}})

    async def run() -> tuple[dict, list[int]]:
        versions = []
        for seed in (same, changed, changed):
            async with engine.begin() as conn:
                await apply_seed(conn, seed)
                versions.append(await conn.scalar(select(setting.c.value_version)))
        async with engine.connect() as conn:
            return await effective(conn, "chat"), versions

    values, versions = client.portal.call(run)
    assert values == {"chat.allow_t2": True}
    assert versions == [1, 2, 2]


def test_store_unavailable_without_database(key) -> None:
    with TestClient(build_app(Settings(env="test"), token_verifier=verifier_for(key))) as c:
        resp = c.get("/svc/v1/channels/policy", headers=bearer(key))
        assert (resp.status_code, resp.json()["error"]["code"]) == (503, "STORE_UNAVAILABLE")


def test_prod_refuses_seed_file(tmp_path: Path) -> None:
    settings = Settings(
        env="prod",
        database_url=SecretStr("postgresql+psycopg://u:p@postgres:5432/config"),
        config_seed_file=write_seed(tmp_path / "seed.json", SEED),
    )
    with pytest.raises(EnvironmentGuardError, match="seed"):
        build_app(settings)


@pytest.mark.parametrize(
    "seed",
    [
        {"channel_policies": [POLICIES[0] | {"max_detail": "semua"}]},
        {"channel_policies": [POLICIES[0] | {"band_min": "rendah"}]},
        {"channel_policies": [POLICIES[0], POLICIES[0]]},
        {"settings": {"chat": {"triage.x": 1}}},
        {"settings": {"model": {"model.api_key": "x"}}},
        {"settings": {"Chat": {"Chat.x": 1}}},
        {"lainnya": []},
    ],
)
def test_seed_rejects_bad_files(seed: dict) -> None:
    with pytest.raises(ValidationError):
        Seed.model_validate(seed)
