"""API config di SQLite: otorisasi, aksi tulis (audit sinkron, outbox, konkurensi), katalog, kanal, dan rahasia."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from astria_common.auth import SigningKey
from astria_contracts.events.config_changed import ConfigChanged
from fastapi.testclient import TestClient
from helpers import (
    ADMIN2_ACT,
    ADMIN_ACT,
    ANALIS_ACT,
    JAGA_ACT,
    LANGKAH,
    POLA,
    FakeAudit,
    FakeGateway,
    changed,
    fresh,
    headers,
    make_app,
    outbox_events,
    settings_for,
    verifier_for,
    wait_ready,
)
from pydantic import SecretStr

from astria_config import catalogs
from astria_config.layers import ConfigError
from astria_config.main import build_app
from astria_config.settings import Settings

CANARY = "nilai-uji-tidak-boleh-bocor-1"


class Env:
    """Satu aplikasi uji beserta tiruannya."""

    def __init__(self, client: TestClient, key: SigningKey, audit: FakeAudit, gateway: FakeGateway, path: Path) -> None:
        self.client, self.key, self.audit, self.gateway, self.path = client, key, audit, gateway, path

    def h(self, act: dict[str, Any] | None = ADMIN_ACT, **kw: Any) -> dict[str, str]:
        return headers(self.key, act, **kw)

    def admin(self, *, reauth: bool = False, act: dict[str, Any] = ADMIN_ACT) -> dict[str, str]:
        return self.h(act, reauth=fresh(1) if reauth else None)

    def call(self, method: str, path: str, hdr: dict[str, str] | None = None, **kw: Any):
        return self.client.request(method, path, headers=hdr if hdr is not None else self.admin(), **kw)


def _build(tmp_path: Path, key: SigningKey, **kw: Any) -> Iterator[Env]:
    audit, gateway = kw.pop("audit", None) or FakeAudit(), kw.pop("gateway", None) or FakeGateway()
    with TestClient(make_app(tmp_path, key, audit=audit, gateway=gateway, **kw)) as client:
        wait_ready(client)
        yield Env(client, key, audit, gateway, tmp_path)


@pytest.fixture
def env(tmp_path: Path, key: SigningKey) -> Iterator[Env]:
    yield from _build(tmp_path, key)


def _code(resp: Any) -> str:
    return resp.json()["error"]["code"]


# ---- autentikasi dan otorisasi --------------------------------------------------------------------------------

ADMIN_ENDPOINTS = [
    ("get", "/v1/settings", None),
    ("put", "/v1/settings/model.timeout_seconds", {"value": 30, "if_version": 0}),
    ("put", "/v1/secrets/model.api_key", {"value": CANARY}),
    ("put", "/v1/secrets/gateway/telegram.bot_token", {"value": CANARY}),
    ("get", "/v1/channels/policy", None),
    ("put", "/v1/channels/policy/email-tim", {"active": True, "max_detail": "ringkas", "if_version": 1}),
    ("get", "/v1/catalogs/langkah/versions", None),
    ("post", "/v1/catalogs/langkah/versions", {"content": LANGKAH}),
    ("post", "/v1/catalogs/langkah/versions/1/review", None),
    ("post", "/v1/catalogs/langkah/versions/1/activate", None),
    ("get", "/v1/catalogs/langkah/coverage", None),
]
SVC_ENDPOINTS = [
    "/svc/v1/settings/effective?scope=model",
    "/svc/v1/catalogs/langkah/active",
    "/svc/v1/channels/policy",
    "/svc/v1/secrets/model.api_key",
]


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
def test_admin_requires_token(env: Env, method: str, path: str, body: Any) -> None:
    resp = env.client.request(method, path, json=body)
    assert (resp.status_code, _code(resp)) == (401, "TOKEN_WAJIB")


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
def test_admin_wrong_audience(env: Env, method: str, path: str, body: Any) -> None:
    resp = env.call(method, path, env.h(audience="svc:dfir"), json=body)
    assert (resp.status_code, _code(resp)) == (403, "AUDIENS_SALAH")


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
@pytest.mark.parametrize("subject", ["svc:triage", "svc:narrative", "svc:gateway"])
def test_admin_only_from_api(env: Env, method: str, path: str, body: Any, subject: str) -> None:
    resp = env.call(method, path, env.h(subject=subject), json=body)
    assert (resp.status_code, _code(resp)) == (403, "PEMANGGIL_DITOLAK")


@pytest.mark.parametrize(("method", "path", "body"), ADMIN_ENDPOINTS)
def test_admin_requires_a_user(env: Env, method: str, path: str, body: Any) -> None:
    resp = env.call(method, path, env.h(None), json=body)
    assert (resp.status_code, _code(resp)) == (403, "PERAN_TIDAK_CUKUP")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [e for e in ADMIN_ENDPOINTS if e[0] != "get" and "catalogs/langkah/versions" not in e[1] or e[0] == "post"],
)
def test_jaga_has_no_admin_access(env: Env, method: str, path: str, body: Any) -> None:
    resp = env.call(method, path, env.h(JAGA_ACT, reauth=fresh()), json=body)
    assert (resp.status_code, _code(resp)) == (403, "PERAN_TIDAK_CUKUP")


def test_analis_reads_and_uploads_drafts_but_cannot_write(env: Env) -> None:
    analis = env.h(ANALIS_ACT, reauth=fresh())
    assert env.call("get", "/v1/channels/policy", analis).status_code == 200
    assert env.call("get", "/v1/catalogs/langkah/versions", analis).status_code == 200
    assert env.call("post", "/v1/catalogs/langkah/versions", analis, json={"content": LANGKAH}).status_code == 201
    for method, path, body in [
        ("get", "/v1/settings", None),
        ("put", "/v1/settings/model.timeout_seconds", {"value": 30, "if_version": 0}),
        ("put", "/v1/secrets/model.api_key", {"value": CANARY}),
        ("put", "/v1/channels/policy/email-tim", {"active": False, "max_detail": "ringkas", "if_version": 1}),
        ("post", "/v1/catalogs/langkah/versions/1/review", None),
        ("post", "/v1/catalogs/langkah/versions/1/activate", None),
    ]:
        resp = env.call(method, path, analis, json=body)
        assert (resp.status_code, _code(resp)) == (403, "PERAN_TIDAK_CUKUP"), path


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("put", "/v1/secrets/model.api_key", {"value": CANARY}),
        ("put", "/v1/secrets/gateway/telegram.bot_token", {"value": CANARY}),
    ],
)
def test_secret_writes_require_fresh_reauth(env: Env, method: str, path: str, body: Any) -> None:
    for hdr in (env.h(ADMIN_ACT), env.h(ADMIN_ACT, reauth=fresh(11)), env.h(ADMIN_ACT, reauth="bukan-waktu")):
        resp = env.call(method, path, hdr, json=body)
        assert (resp.status_code, _code(resp)) == (403, "REAUTH_DIPERLUKAN")
    assert env.audit.records == []


def test_reauth_window_is_configurable(tmp_path: Path, key: SigningKey) -> None:
    for env in _build(tmp_path, key, reauth_window_min=1):
        resp = env.call("put", "/v1/secrets/model.api_key", env.h(reauth=fresh(2)), json={"value": CANARY})
        assert _code(resp) == "REAUTH_DIPERLUKAN"
        assert (
            env.call("put", "/v1/secrets/model.api_key", env.h(reauth=fresh(0.2)), json={"value": CANARY}).status_code
            == 200
        )


@pytest.mark.parametrize("path", SVC_ENDPOINTS)
def test_svc_requires_token_and_audience(env: Env, path: str) -> None:
    assert _code(env.client.get(path)) == "TOKEN_WAJIB"
    resp = env.client.get(path, headers=env.h(None, subject="svc:triage", audience="svc:ingest"))
    assert (resp.status_code, _code(resp)) == (403, "AUDIENS_SALAH")


# ---- pengaturan -----------------------------------------------------------------------------------------------


def _setting(env: Env, key: str) -> dict[str, Any]:
    items = env.call("get", "/v1/settings").json()["items"]
    return next(i for i in items if i["key"] == key)


def test_list_settings_shows_sources_and_hides_secret_value(env: Env) -> None:
    items = env.call("get", "/v1/settings").json()["items"]
    by_key = {i["key"]: i for i in items}
    assert by_key["model.timeout_seconds"]["value"] == 20 and by_key["model.timeout_seconds"]["source"] == "bawaan"
    assert by_key["model.api_key"] == {
        "key": "model.api_key",
        "terisi": False,
        "version": 0,
        "updated_by": None,
        "updated_at": None,
    }
    assert "value" not in by_key["model.api_key"]


def test_put_setting_changes_value_audits_and_emits(env: Env, tmp_path: Path) -> None:
    resp = env.call("put", "/v1/settings/model.timeout_seconds", json={"value": 45, "if_version": 0, "alasan": "uji"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["value"], body["source"], body["version"], body["updated_by"]) == (45, "database", 1, "usr_admin01")

    assert _setting(env, "model.timeout_seconds")["value"] == 45
    [record] = env.audit.records
    assert (record.action, record.actor, record.target_id) == (
        "config.setting.changed",
        "usr_admin01",
        "model.timeout_seconds",
    )
    assert record.payload["version"] == 1
    assert record.payload["before_hash"] != record.payload["after_hash"]
    assert changed(tmp_path) == [{"scope": "setting", "key": "model.timeout_seconds", "value_version": 1}]


def test_config_changed_events_match_contract_model(env: Env, tmp_path: Path) -> None:
    env.call("put", "/v1/settings/chat.allow_t2", json={"value": True, "if_version": 0})
    env.call("put", "/v1/secrets/model.api_key", env.admin(reauth=True), json={"value": CANARY})
    env.call("put", "/v1/channels/policy/email-tim", json={"active": False, "max_detail": "ringkas", "if_version": 0})
    events = [(e, m) for e, m in outbox_events(tmp_path) if e.type == "config.changed"]
    assert [m.scope.root if hasattr(m.scope, "root") else m.scope for _e, m in events] == [
        "setting",
        "secret",
        "channel_policy",
    ]
    for _env, model in events:
        assert isinstance(model, ConfigChanged)
    assert CANARY not in str([e.model_dump() for e, _m in outbox_events(tmp_path)])


def test_put_setting_if_version_conflict(env: Env) -> None:
    assert env.call("put", "/v1/settings/chat.allow_t2", json={"value": True, "if_version": 0}).status_code == 200
    resp = env.call("put", "/v1/settings/chat.allow_t2", json={"value": False, "if_version": 0})
    assert (resp.status_code, _code(resp)) == (409, "VERSI_KONFLIK")
    assert _setting(env, "chat.allow_t2")["value"] is True
    ok = env.call("put", "/v1/settings/chat.allow_t2", json={"value": False, "if_version": 1})
    assert (ok.status_code, ok.json()["version"]) == (200, 2)
    # Versi tidak cocok ditolak sebelum audit: tidak ada catatan audit palsu.
    assert env.audit.actions() == ["config.setting.changed"] * 2


def test_put_setting_validation_and_unknown_keys(env: Env) -> None:
    unknown = env.call("put", "/v1/settings/tidak.ada", json={"value": 1, "if_version": 0})
    assert (unknown.status_code, _code(unknown)) == (404, "KUNCI_TIDAK_DIKENAL")
    bad = env.call("put", "/v1/settings/model.timeout_seconds", json={"value": 9999, "if_version": 0})
    assert (bad.status_code, _code(bad)) == (422, "VALIDASI_GAGAL")
    assert "9999" not in bad.json()["error"]["message"]
    secret_via_settings = env.call("put", "/v1/settings/model.api_key", json={"value": CANARY, "if_version": 0})
    assert (secret_via_settings.status_code, _code(secret_via_settings)) == (422, "VALIDASI_GAGAL")
    assert env.audit.records == []


@pytest.mark.parametrize(
    "body",
    [{}, {"value": 1}, {"if_version": 0}, {"value": 1, "if_version": -1}, {"value": 1, "if_version": 0, "lain": 1}],
)
def test_put_setting_rejects_malformed_body(env: Env, body: dict[str, Any]) -> None:
    resp = env.call("put", "/v1/settings/model.timeout_seconds", json=body)
    assert resp.status_code in (400, 422)
    assert _code(resp) == "VALIDASI_GAGAL"


def test_env_locks_a_key(tmp_path: Path, key: SigningKey) -> None:
    for env in _build(tmp_path, key, environ={"ASTRIA_CFG_MODEL__TIMEOUT_SECONDS": "90"}):
        item = _setting(env, "model.timeout_seconds")
        assert (item["value"], item["source"], item["locked"]) == (90, "env", True)
        resp = env.call("put", "/v1/settings/model.timeout_seconds", json={"value": 30, "if_version": 0})
        assert (resp.status_code, _code(resp)) == (409, "DIKUNCI_ENV")
        assert env.audit.records == []
        effective = env.client.get(
            "/svc/v1/settings/effective?scope=model", headers=env.h(None, subject="svc:triage")
        ).json()
        assert effective["values"]["model.timeout_seconds"] == 90


def test_file_layer_and_database_override(tmp_path: Path, key: SigningKey) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("chat:\n  allow_t2: true\n", encoding="utf-8")
    for env in _build(tmp_path, key, config_file=cfg):
        item = _setting(env, "chat.allow_t2")
        assert (item["value"], item["source"], item["locked"]) == (True, "berkas", False)
        env.call("put", "/v1/settings/chat.allow_t2", json={"value": False, "if_version": 0})
        item = _setting(env, "chat.allow_t2")
        assert (item["value"], item["source"]) == (False, "database")


def test_invalid_start_config_is_refused(tmp_path: Path, key: SigningKey) -> None:
    with pytest.raises(ConfigError):
        make_app(tmp_path, key, environ={"ASTRIA_CFG_CHAT__ALLOW_T2": "mungkin"})
    bad = tmp_path / "bad.yaml"
    bad.write_text("model:\n  timeout_seconds: 9999\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        make_app(tmp_path, key, config_file=bad)


def test_audit_down_rejects_write_without_changes(env: Env, tmp_path: Path) -> None:
    env.audit.down = True
    for method, path, body, reauth in [
        ("put", "/v1/settings/model.timeout_seconds", {"value": 45, "if_version": 0}, False),
        ("put", "/v1/secrets/model.api_key", {"value": CANARY}, True),
        ("put", "/v1/channels/policy/email-tim", {"active": False, "max_detail": "ringkas", "if_version": 0}, False),
    ]:
        resp = env.call(method, path, env.admin(reauth=reauth), json=body)
        assert (resp.status_code, _code(resp)) == (503, "AUDIT_TIDAK_TERSEDIA"), path
    env.audit.down = False
    assert _setting(env, "model.timeout_seconds")["source"] == "bawaan"
    assert _setting(env, "model.api_key")["terisi"] is False
    assert env.call("get", "/v1/channels/policy").json() == {"items": []}
    assert changed(tmp_path) == []


def test_unconfigured_audit_client_fails_closed(tmp_path: Path, key: SigningKey) -> None:
    # Tanpa kunci layanan, config memakai klien audit yang selalu menolak (bukan melewati audit diam-diam).
    app = build_app(settings_for(tmp_path), token_verifier=verifier_for(key), environ={})
    with TestClient(app) as client:
        wait_ready(client)
        resp = client.put(
            "/v1/settings/chat.allow_t2",
            json={"value": True, "if_version": 0},
            headers=headers(key),
        )
        assert (resp.status_code, _code(resp)) == (503, "AUDIT_TIDAK_TERSEDIA")


# ---- rahasia --------------------------------------------------------------------------------------------------


def test_secret_is_write_only(env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    resp = env.call("put", "/v1/secrets/model.api_key", env.admin(reauth=True), json={"value": CANARY})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "key": "model.api_key",
        "terisi": True,
        "version": 1,
        "updated_by": "usr_admin01",
        "updated_at": resp.json()["updated_at"],
    }
    assert CANARY not in resp.text
    listing = env.call("get", "/v1/settings")
    assert CANARY not in listing.text
    assert _setting(env, "model.api_key")["terisi"] is True

    [record] = env.audit.records
    assert record.action == "config.secret.updated" and CANARY not in record.model_dump_json()
    assert record.payload["value_recorded"] is False
    logged = capsys.readouterr()
    assert "rahasia" in logged.out  # log layanan memang tertangkap (structlog ke stdout)
    assert CANARY not in logged.out + logged.err

    raw = sqlite3.connect(tmp_path / "config.db").execute("SELECT * FROM setting WHERE key='model.api_key'").fetchone()
    assert CANARY.encode() not in b"".join(c if isinstance(c, bytes) else str(c).encode() for c in raw)


def test_secret_rewrite_bumps_version(env: Env) -> None:
    hdr = env.admin(reauth=True)
    assert env.call("put", "/v1/secrets/model.api_key", hdr, json={"value": CANARY}).json()["version"] == 1
    assert env.call("put", "/v1/secrets/model.api_key", hdr, json={"value": CANARY + "2"}).json()["version"] == 2
    got = env.client.get("/svc/v1/secrets/model.api_key", headers=env.h(None, subject="svc:narrative"))
    assert got.json() == {"key": "model.api_key", "value": CANARY + "2"}


def test_non_secret_key_cannot_be_written_as_secret(env: Env) -> None:
    for key in ("model.timeout_seconds", "tidak.ada"):
        resp = env.call("put", f"/v1/secrets/{key}", env.admin(reauth=True), json={"value": CANARY})
        assert (resp.status_code, _code(resp)) == (404, "KUNCI_TIDAK_DIKENAL")


def test_only_narrative_reads_model_key(env: Env) -> None:
    env.call("put", "/v1/secrets/model.api_key", env.admin(reauth=True), json={"value": CANARY})
    for subject in ("svc:triage", "svc:api", "svc:gateway", "svc:dfir"):
        resp = env.client.get("/svc/v1/secrets/model.api_key", headers=env.h(None, subject=subject))
        assert (resp.status_code, _code(resp)) == (403, "PEMANGGIL_DITOLAK"), subject
    ok = env.client.get("/svc/v1/secrets/model.api_key", headers=env.h(None, subject="svc:narrative"))
    assert ok.status_code == 200 and ok.json()["value"] == CANARY
    assert ok.headers["cache-control"] == "no-store"


def test_secret_read_is_audited_without_value(env: Env, tmp_path: Path) -> None:
    env.call("put", "/v1/secrets/model.api_key", env.admin(reauth=True), json={"value": CANARY})
    env.client.get("/svc/v1/secrets/model.api_key", headers=env.h(None, subject="svc:narrative"))
    reads = [m for e, m in outbox_events(tmp_path) if e.type == "audit.record" and m.action == "config.secret.read"]
    assert len(reads) == 1
    assert (reads[0].actor, reads[0].actor_type) == ("svc:narrative", "service")
    assert CANARY not in reads[0].model_dump_json()


def test_unset_model_key_is_404(env: Env) -> None:
    resp = env.client.get("/svc/v1/secrets/model.api_key", headers=env.h(None, subject="svc:narrative"))
    assert (resp.status_code, _code(resp)) == (404, "RAHASIA_BELUM_DIISI")


def test_unreadable_secret_is_503_after_master_key_change(tmp_path: Path, key: SigningKey) -> None:
    for env in _build(tmp_path, key):
        env.call("put", "/v1/secrets/model.api_key", env.admin(reauth=True), json={"value": CANARY})
    (tmp_path / "master.key").unlink()  # kunci induk hilang: dibuat baru, ciphertext lama tak terbaca
    for env in _build(tmp_path, key):
        resp = env.client.get("/svc/v1/secrets/model.api_key", headers=env.h(None, subject="svc:narrative"))
        assert (resp.status_code, _code(resp)) == (503, "RAHASIA_TIDAK_TERBACA")
        assert CANARY not in resp.text
        fixed = env.call("put", "/v1/secrets/model.api_key", env.admin(reauth=True), json={"value": CANARY})
        assert fixed.status_code == 200
        again = env.client.get("/svc/v1/secrets/model.api_key", headers=env.h(None, subject="svc:narrative"))
        assert again.json()["value"] == CANARY


# ---- penerusan rahasia ke gateway -------------------------------------------------------------------------------


def test_forward_gateway_secret(env: Env, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    resp = env.call("put", "/v1/secrets/gateway/telegram.bot_token", env.admin(reauth=True), json={"value": CANARY})
    assert resp.status_code == 204 and resp.content == b""
    assert env.gateway.received == [("telegram.bot_token", CANARY)]
    [record] = env.audit.records
    assert record.action == "config.secret.forwarded" and CANARY not in record.model_dump_json()
    logged = capsys.readouterr()
    assert "rahasia" in logged.out  # log layanan memang tertangkap (structlog ke stdout)
    assert CANARY not in logged.out + logged.err
    stored = sqlite3.connect(tmp_path / "config.db").execute("SELECT count(*) FROM setting").fetchone()[0]
    assert stored == 0  # tidak disimpan di Core
    assert changed(tmp_path) == []


def test_forward_gateway_down_is_503(env: Env) -> None:
    env.gateway.down = True
    resp = env.call("put", "/v1/secrets/gateway/telegram.bot_token", env.admin(reauth=True), json={"value": CANARY})
    assert (resp.status_code, _code(resp)) == (503, "GATEWAY_TIDAK_TERSEDIA")
    assert CANARY not in resp.text


def test_forward_audit_down_is_503_and_nothing_sent(env: Env) -> None:
    env.audit.down = True
    resp = env.call("put", "/v1/secrets/gateway/telegram.bot_token", env.admin(reauth=True), json={"value": CANARY})
    assert (resp.status_code, _code(resp)) == (503, "AUDIT_TIDAK_TERSEDIA")
    assert env.gateway.received == []


@pytest.mark.parametrize("bad", ["Telegram", "../x", "a b", "-awal"])
def test_forward_rejects_bad_keys(env: Env, bad: str) -> None:
    resp = env.call("put", f"/v1/secrets/gateway/{bad}", env.admin(reauth=True), json={"value": CANARY})
    assert resp.status_code in (400, 404, 422)
    assert env.gateway.received == []


# ---- kebijakan kanal ------------------------------------------------------------------------------------------


def test_test_env_seeds_no_channels(env: Env) -> None:
    # env=test tidak menyemai kanal (hanya dev/demo); daftar kosong dan dapat diisi admin.
    assert env.call("get", "/v1/channels/policy").json() == {"items": []}


def test_channel_policy_lifecycle(env: Env, tmp_path: Path) -> None:
    created = env.call(
        "put", "/v1/channels/policy/wa-tim", json={"active": True, "max_detail": "ringkas", "if_version": 0}
    )
    assert created.status_code == 200, created.text
    assert (created.json()["version"], created.json()["active"]) == (1, True)
    changed_ = env.call(
        "put",
        "/v1/channels/policy/wa-tim",
        json={"active": False, "max_detail": "ringkas", "min_band": "TINGGI", "if_version": 1},
    )
    assert (changed_.json()["version"], changed_.json()["min_band"]) == (2, "TINGGI")
    stale = env.call(
        "put", "/v1/channels/policy/wa-tim", json={"active": True, "max_detail": "ringkas", "if_version": 1}
    )
    assert (stale.status_code, _code(stale)) == (409, "VERSI_KONFLIK")
    listing = env.call("get", "/v1/channels/policy", env.h(ANALIS_ACT)).json()["items"]
    assert [(i["channel_id"], i["version"], i["active"]) for i in listing] == [("wa-tim", 2, False)]
    svc = env.client.get("/svc/v1/channels/policy", headers=env.h(None, subject="svc:notifier")).json()
    assert svc == {"items": listing}
    assert [d for d in changed(tmp_path)] == [
        {"scope": "channel_policy", "key": "wa-tim", "value_version": 1},
        {"scope": "channel_policy", "key": "wa-tim", "value_version": 2},
    ]
    assert env.audit.actions() == ["config.channel_policy.changed"] * 2


@pytest.mark.parametrize(
    "body",
    [
        {"active": True, "max_detail": "lengkap-sekali", "if_version": 0},
        {"active": True, "max_detail": "ringkas", "min_band": "ungu", "if_version": 0},
        {"active": "ya", "max_detail": "ringkas", "if_version": 0},
        {"max_detail": "ringkas", "if_version": 0},
    ],
)
def test_channel_policy_validation(env: Env, body: dict[str, Any]) -> None:
    resp = env.call("put", "/v1/channels/policy/wa-tim", json=body)
    assert resp.status_code in (400, 422) and _code(resp) == "VALIDASI_GAGAL"


def test_dev_env_seeds_default_channels(tmp_path: Path, key: SigningKey) -> None:
    assert Settings(env="dev").channels_to_seed() == ("email-tim", "telegram-tim", "wa-tim")
    assert Settings(env="test").channels_to_seed() == ()
    for env in _build(tmp_path, key, seed_channels=("email-tim", "wa-tim")):
        items = env.call("get", "/v1/channels/policy").json()["items"]
        assert [(i["channel_id"], i["version"], i["active"], i["max_detail"]) for i in items] == [
            ("email-tim", 1, True, "ringkas"),
            ("wa-tim", 1, True, "ringkas"),
        ]


# ---- katalog --------------------------------------------------------------------------------------------------


def _draft(env: Env, kind: str, content: dict[str, Any], act: dict[str, Any] = ANALIS_ACT) -> dict[str, Any]:
    resp = env.call("post", f"/v1/catalogs/{kind}/versions", env.h(act), json={"content": content})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_catalog_full_lifecycle_with_four_eyes(env: Env, tmp_path: Path) -> None:
    draft = _draft(env, "langkah", LANGKAH)
    assert (draft["version"], draft["status"], draft["created_by"], draft["reviewed_by"]) == (
        1,
        "draft",
        "usr_analis01",
        None,
    )
    assert draft["content_hash"] == catalogs.content_hash(LANGKAH)

    # belum ditinjau: tidak bisa diaktifkan
    early = env.call("post", "/v1/catalogs/langkah/versions/1/activate", env.admin(reauth=True))
    assert (early.status_code, _code(early)) == (409, "KATALOG_BELUM_DITINJAU")

    reviewed = env.call("post", "/v1/catalogs/langkah/versions/1/review")
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "ditinjau"
    assert reviewed.json()["reviewed_by"] == "usr_admin01"
    assert env.call("post", "/v1/catalogs/langkah/versions/1/review").status_code == 409  # bukan draf lagi

    active = env.call("post", "/v1/catalogs/langkah/versions/1/activate", env.admin(reauth=True))
    assert active.status_code == 200 and active.json()["status"] == "aktif" and active.json()["activated_at"]

    served = env.client.get("/svc/v1/catalogs/langkah/active", headers=env.h(None, subject="svc:triage")).json()
    assert served["catalog_version"] == 1 and served["content_hash"] == draft["content_hash"]
    assert served["langkah"] == LANGKAH["langkah"]
    assert changed(tmp_path) == [
        {"scope": "catalog", "kind": "langkah", "catalog_version": 1, "content_hash": draft["content_hash"]}
    ]
    assert env.audit.actions() == ["config.catalog.activated"]


def test_author_cannot_review_or_activate_own_version(env: Env) -> None:
    _draft(env, "langkah", LANGKAH, act=ADMIN_ACT)
    resp = env.call("post", "/v1/catalogs/langkah/versions/1/review")
    assert (resp.status_code, _code(resp)) == (403, "PENINJAU_SAMA_DENGAN_PENULIS")
    other = env.h(ADMIN2_ACT)
    assert env.call("post", "/v1/catalogs/langkah/versions/1/review", other).status_code == 200
    resp = env.call("post", "/v1/catalogs/langkah/versions/1/activate", env.admin(reauth=True))
    assert (resp.status_code, _code(resp)) == (403, "PENINJAU_SAMA_DENGAN_PENULIS")
    ok = env.call("post", "/v1/catalogs/langkah/versions/1/activate", env.h(ADMIN2_ACT, reauth=fresh()))
    assert ok.status_code == 200


def test_activation_requires_reauth_and_audit(env: Env) -> None:
    _draft(env, "langkah", LANGKAH)
    env.call("post", "/v1/catalogs/langkah/versions/1/review")
    resp = env.call("post", "/v1/catalogs/langkah/versions/1/activate", env.h(ADMIN_ACT))
    assert (resp.status_code, _code(resp)) == (403, "REAUTH_DIPERLUKAN")
    env.audit.down = True
    resp = env.call("post", "/v1/catalogs/langkah/versions/1/activate", env.admin(reauth=True))
    assert (resp.status_code, _code(resp)) == (503, "AUDIT_TIDAK_TERSEDIA")
    versions = env.call("get", "/v1/catalogs/langkah/versions").json()["items"]
    assert [v["status"] for v in versions] == ["ditinjau"]


def test_activating_archives_previous_version(env: Env) -> None:
    for step in ("S-a", "S-b"):
        doc = {"langkah": {step: {"judul": step, "risiko": "aman"}}}
        number = _draft(env, "langkah", doc)["version"]
        assert env.call("post", f"/v1/catalogs/langkah/versions/{number}/review").status_code == 200
        assert (
            env.call("post", f"/v1/catalogs/langkah/versions/{number}/activate", env.admin(reauth=True)).status_code
            == 200
        )
    statuses = [(v["version"], v["status"]) for v in env.call("get", "/v1/catalogs/langkah/versions").json()["items"]]
    assert statuses == [(1, "arsip"), (2, "aktif")]
    served = env.client.get("/svc/v1/catalogs/langkah/active", headers=env.h(None, subject="svc:triage")).json()
    assert list(served["langkah"]) == ["S-b"] and served["catalog_version"] == 2


def test_draft_validation_and_unknown_kind(env: Env) -> None:
    bad = env.call("post", "/v1/catalogs/langkah/versions", env.h(ANALIS_ACT), json={"content": {"langkah": {}}})
    assert (bad.status_code, _code(bad)) == (422, "VALIDASI_GAGAL")
    unknown = env.call("get", "/v1/catalogs/tidak_ada/versions")
    assert (unknown.status_code, _code(unknown)) == (404, "KATALOG_TIDAK_DITEMUKAN")
    missing = env.call("post", "/v1/catalogs/langkah/versions/9/review")
    assert (missing.status_code, _code(missing)) == (404, "KATALOG_TIDAK_DITEMUKAN")
    no_active = env.client.get("/svc/v1/catalogs/langkah/active", headers=env.h(None, subject="svc:triage"))
    assert (no_active.status_code, _code(no_active)) == (404, "KATALOG_TIDAK_DITEMUKAN")


def test_coverage_warns_about_missing_step_references(env: Env) -> None:
    for kind, doc in (("langkah", LANGKAH), ("pola_insiden", POLA)):
        number = _draft(env, kind, doc)["version"]
        env.call("post", f"/v1/catalogs/{kind}/versions/{number}/review")
        env.call("post", f"/v1/catalogs/{kind}/versions/{number}/activate", env.admin(reauth=True))
    coverage = env.call("get", "/v1/catalogs/pola_insiden/coverage", env.h(ANALIS_ACT)).json()
    assert coverage["complete"] is False and coverage["catalog_version"] == 1
    assert any("S-tidak-ada" in i for i in coverage["issues"])
    assert env.call("get", "/v1/catalogs/langkah/coverage").json()["complete"] is True


# ---- metrik dan kesehatan -------------------------------------------------------------------------------------


def _metric(env: Env, series: str) -> float:
    """Nilai satu deret metrik (global per proses, jadi uji membandingkan selisih)."""
    for line in env.client.get("/metrics").text.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_metrics_and_readiness(env: Env) -> None:
    changes = 'config_changes_total{scope="setting"}'
    active = 'config_catalog_active_version{kind="langkah"}'
    failures = "config_audit_failures_total"
    base_changes, base_failures = _metric(env, changes), _metric(env, failures)

    env.call("put", "/v1/settings/chat.allow_t2", json={"value": True, "if_version": 0})
    assert _metric(env, changes) == base_changes + 1
    _draft(env, "langkah", LANGKAH)
    env.call("post", "/v1/catalogs/langkah/versions/1/review")
    env.call("post", "/v1/catalogs/langkah/versions/1/activate", env.admin(reauth=True))
    assert _metric(env, active) == 1

    env.audit.down = True
    env.call("put", "/v1/settings/chat.allow_t2", json={"value": False, "if_version": 1})
    assert _metric(env, failures) == base_failures + 1
    assert _metric(env, changes) == base_changes + 1  # perubahan yang ditolak tidak dihitung
    assert CANARY not in env.client.get("/metrics").text
    assert env.client.get("/health").json()["service"] == "config"


def test_ready_is_down_before_database_is_ready(tmp_path: Path, key: SigningKey) -> None:
    # Basis data tidak dapat dibuka: /ready tetap down dan aksi tulis 503, bukan galat 500.
    bad = SecretStr(f"sqlite+aiosqlite:///{(tmp_path / 'tidak-ada' / 'x.db').as_posix()}")
    with TestClient(make_app(tmp_path, key, database_url=bad)) as client:
        assert client.get("/ready").status_code == 503
        resp = client.get("/v1/settings", headers=headers(key))
        assert resp.status_code == 503
