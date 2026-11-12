"""Perkakas uji: aplikasi config di SQLite dengan audit, gateway, dan token palsu."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from astria_common.audit_client import AuditUnavailable
from astria_common.auth import JwksSource, ServiceTokenVerifier, SigningKey, issue_service_token
from astria_common.events import decode
from astria_contracts.events.audit_record import AuditRecord
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from astria_config.gateway_client import GatewayRejected, GatewayUnavailable
from astria_config.main import build_app
from astria_config.settings import Settings

DB_URL = os.environ.get("ASTRIA_TEST_DATABASE_URL")

ADMIN_ACT = {"sub": "usr_admin01", "roles": ["admin"]}
ADMIN2_ACT = {"sub": "usr_admin02", "roles": ["admin"]}
ANALIS_ACT = {"sub": "usr_analis01", "roles": ["analis"]}
JAGA_ACT = {"sub": "usr_jaga01", "roles": ["jaga"]}

LANGKAH = {"langkah": {"S-isolasi": {"judul": "Isolasi host", "risiko": "perlu_persetujuan"}}}
POLA = {"pola": {"brute-force": {"judul": "Brute force", "langkah": ["S-isolasi", "S-tidak-ada"]}}}


def fresh(minutes: float = 1) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def verifier_for(key: SigningKey) -> ServiceTokenVerifier:
    return ServiceTokenVerifier(JwksSource("http://identity.invalid", fetch=lambda: {"keys": [key.public_jwk()]}))


def headers(
    key: SigningKey,
    act: dict[str, Any] | None = ADMIN_ACT,
    *,
    subject: str = "svc:api",
    audience: str = "svc:config",
    reauth: str | None = None,
) -> dict[str, str]:
    """Token seperti yang dibawa api; `reauth` mengisi `act.reauth_at` (waktu ISO)."""
    claim = None if act is None else {**act, **({"reauth_at": reauth} if reauth else {})}
    token, _ = issue_service_token(key, subject=subject, audience=audience, act=claim)
    return {"Authorization": f"Bearer {token}"}


class FakeAudit:
    """Klien audit palsu: merekam catatan; `down=True` meniru audit tidak tersedia."""

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        self.down = False

    async def record(self, record: AuditRecord, *, act: dict[str, Any] | None = None) -> None:
        if self.down:
            raise AuditUnavailable("audit mati (uji)")
        self.records.append(record)

    def actions(self) -> list[str]:
        return [r.action for r in self.records]


class FakeGateway:
    def __init__(self) -> None:
        self.received: list[tuple[str, str]] = []
        self.down = False
        self.reject: str | None = None

    async def put_secret(self, key: str, value: str) -> None:
        if self.down:
            raise GatewayUnavailable("gateway mati (uji)")
        if self.reject is not None:
            raise GatewayRejected(self.reject)
        self.received.append((key, value))


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "database_url": SecretStr(f"sqlite+aiosqlite:///{(tmp_path / 'config.db').as_posix()}"),
        "master_key_file": tmp_path / "master.key",
    }
    return Settings(**(base | overrides))


def make_app(
    tmp_path: Path,
    key: SigningKey,
    *,
    audit: FakeAudit | None = None,
    gateway: FakeGateway | None = None,
    environ: dict[str, str] | None = None,
    **overrides: Any,
) -> FastAPI:
    return build_app(
        settings_for(tmp_path, **overrides),
        audit=audit if audit is not None else FakeAudit(),
        gateway=gateway if gateway is not None else FakeGateway(),
        token_verifier=verifier_for(key),
        environ=environ or {},
    )


def outbox_events(tmp_path: Path) -> list[tuple[Any, Any]]:
    """Event di outbox (SQLite) sebagai pasangan (amplop, model data kontrak); `decode` memvalidasi `data`."""
    with closing(sqlite3.connect(tmp_path / "config.db")) as conn:
        rows = conn.execute("SELECT envelope FROM outbox ORDER BY created_at, rowid").fetchall()
    return [decode(row[0].encode() if isinstance(row[0], str) else row[0]) for row in rows]


def changed(tmp_path: Path) -> list[dict[str, Any]]:
    """`data` event config.changed saja."""
    return [env.data for env, _model in outbox_events(tmp_path) if env.type == "config.changed"]


def wait_ready(client: TestClient, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/ready").status_code == 200:
            return
        time.sleep(0.05)
    raise AssertionError(client.get("/ready").text)
