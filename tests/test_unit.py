"""Uji unit tanpa basis data: registri, lapisan nilai, kripto rahasia, hash kanonik, dan lint katalog."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from astria_config import catalogs, registry
from astria_config.layers import ConfigError, Layers, env_name, load_file_values
from astria_config.secrets import MasterKey, MasterKeyError, Sealed, SecretUnreadable

# ---- registri -----------------------------------------------------------------------------------------------


def test_registry_has_contract_keys() -> None:
    names = {k.name for k in registry.KEYS}
    assert {
        "model.chain",
        "model.allowlist_hosts",
        "model.api_key",
        "model.timeout_seconds",
        "score.band",
        "ingest.allowed_roots",
        "schedule.digest",
        "schedule.shift",
        "api.public_base_url",
        "chat.allow_t2",
        "retention.raw_days",
    } <= names
    assert registry.get("model.api_key").secret  # type: ignore[union-attr]
    assert registry.get("tidak.ada") is None
    assert registry.get("model.timeout_seconds").scope == "model"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("model.timeout_seconds", 0),
        ("model.timeout_seconds", 121),
        ("model.timeout_seconds", True),
        ("model.timeout_seconds", "20"),
        ("chat.allow_t2", "ya"),
        ("score.band", {"kritis": 10, "tinggi": 50, "sedang": 30}),
        ("score.band", {"kritis": 70}),
        ("schedule.digest", "bukan cron"),
        ("api.public_base_url", "ftp://x"),
        ("api.public_base_url", "http://x.example"),
        ("retention.raw_days", 0),
        ("model.allowlist_hosts", ["http://bukan-host"]),
        ("model.chain", ["tidak-dikenal"]),
        ("ingest.allowed_roots", "bukan-daftar"),
    ],
)
def test_registry_rejects_invalid_values(key: str, value: object) -> None:
    with pytest.raises(ValueError, match=r".+"):
        registry.REGISTRY[key].validate(value)


def test_registry_error_does_not_echo_value() -> None:
    with pytest.raises(ValueError) as caught:
        registry.REGISTRY["api.public_base_url"].validate("ftp://rahasia-sekali.example")
    assert "rahasia-sekali" not in str(caught.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("model.timeout_seconds", 45),
        ("chat.allow_t2", True),
        ("score.band", {"kritis": 80, "tinggi": 60, "sedang": 40}),
        ("schedule.digest", "30 6 * * 1-5"),
        ("api.public_base_url", "https://astria.example"),
        ("api.public_base_url", ""),
        ("retention.raw_days", 90),
        ("retention.raw_days", None),
    ],
)
def test_registry_accepts_valid_values(key: str, value: object) -> None:
    assert registry.REGISTRY[key].validate(value) == value


# ---- lapisan nilai -------------------------------------------------------------------------------------------


def test_env_name_maps_dot_to_double_underscore() -> None:
    assert env_name("chat.allow_t2") == "ASTRIA_CFG_CHAT__ALLOW_T2"
    assert env_name("model.timeout_seconds") == "ASTRIA_CFG_MODEL__TIMEOUT_SECONDS"


def test_layer_precedence_default_file_database_env() -> None:
    spec = registry.REGISTRY["model.timeout_seconds"]
    row = {"value": 60, "version": 3, "updated_by": "usr_x", "updated_at": None}

    default = Layers().resolve(spec, None)
    assert (default.value, default.source, default.locked) == (20, "bawaan", False)

    file_layer = Layers({"model.timeout_seconds": 30}).resolve(spec, None)
    assert (file_layer.value, file_layer.source) == (30, "berkas")

    database = Layers({"model.timeout_seconds": 30}).resolve(spec, row)
    assert (database.value, database.source, database.version) == (60, "database", 3)

    env = Layers({"model.timeout_seconds": 30}, {"ASTRIA_CFG_MODEL__TIMEOUT_SECONDS": "90"})
    resolved = env.resolve(spec, row)
    assert (resolved.value, resolved.source, resolved.locked) == (90, "env", True)
    assert env.locked("model.timeout_seconds")


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        ("chat.allow_t2", "true", True),
        ("chat.allow_t2", "0", False),
        ("model.timeout_seconds", " 45 ", 45),
        ("score.band", '{"kritis": 80, "tinggi": 60, "sedang": 40}', {"kritis": 80, "tinggi": 60, "sedang": 40}),
        ("model.allowlist_hosts", '["api.example.com"]', ["api.example.com"]),
        ("api.public_base_url", "https://astria.example", "https://astria.example"),
    ],
)
def test_env_values_are_parsed_by_type(key: str, raw: str, expected: object) -> None:
    layers = Layers(None, {env_name(key): raw})
    assert layers.resolve(registry.REGISTRY[key]).value == expected


@pytest.mark.parametrize(
    "environ",
    [
        {"ASTRIA_CFG_CHAT__ALLOW_T2": "mungkin"},
        {"ASTRIA_CFG_MODEL__TIMEOUT_SECONDS": "banyak"},
        {"ASTRIA_CFG_MODEL__TIMEOUT_SECONDS": "9999"},
        {"ASTRIA_CFG_SCORE__BAND": "bukan json"},
        {"ASTRIA_CFG_MODEL__API_KEY": "sk-rahasia"},
        {"ASTRIA_CFG_KUNCI__ASING": "1"},
    ],
)
def test_invalid_env_refuses_start(environ: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as caught:
        Layers(None, environ)
    assert "sk-rahasia" not in str(caught.value)


def test_file_is_parsed_and_validated(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("model:\n  timeout_seconds: 30\nchat:\n  allow_t2: true\n", encoding="utf-8")
    assert load_file_values(path) == {"model.timeout_seconds": 30, "chat.allow_t2": True}


@pytest.mark.parametrize(
    "content",
    [
        "model:\n  timeout_seconds: 9999\n",
        "model:\n  tidak_ada: 1\n",
        "model:\n  api_key: sk-rahasia\n",
        "- daftar\n",
        "model: 3\n",
        "model: [\n",
    ],
)
def test_bad_file_refuses_start(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_file_values(path)
    assert "sk-rahasia" not in str(caught.value)


def test_missing_file_refuses_start(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_file_values(tmp_path / "tidak-ada.yaml")


def test_empty_file_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("", encoding="utf-8")
    assert load_file_values(path) == {}


# ---- rahasia -----------------------------------------------------------------------------------------------


def test_seal_roundtrip_and_unique_nonce() -> None:
    master = MasterKey.generate()
    first = master.seal("model.api_key", 1, "sk-nilai-uji")
    second = master.seal("model.api_key", 1, "sk-nilai-uji")
    assert master.open("model.api_key", 1, first) == "sk-nilai-uji"
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert len(first.nonce) == 12
    assert b"sk-nilai-uji" not in first.ciphertext


def test_ciphertext_is_bound_to_name_version_and_key() -> None:
    master = MasterKey.generate()
    sealed = master.seal("model.api_key", 2, "sk-nilai-uji")
    with pytest.raises(SecretUnreadable):
        master.open("model.lain", 2, sealed)  # nama lain: HKDF dan AAD berbeda
    with pytest.raises(SecretUnreadable):
        master.open("model.api_key", 3, sealed)  # versi nilai lain: AAD berbeda
    with pytest.raises(SecretUnreadable):
        MasterKey.generate().open("model.api_key", 2, sealed)  # kunci induk lain
    with pytest.raises(SecretUnreadable):
        master.open("model.api_key", 2, Sealed(sealed.ciphertext, sealed.nonce, 2))  # versi kunci data lain


def test_tampered_ciphertext_is_rejected() -> None:
    master = MasterKey.generate()
    sealed = master.seal("model.api_key", 1, "sk-nilai-uji")
    flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
    with pytest.raises(SecretUnreadable):
        master.open("model.api_key", 1, Sealed(flipped, sealed.nonce, sealed.key_version))


def test_master_key_file_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "master.key"
    with pytest.raises(MasterKeyError):
        MasterKey.load_or_create(path, create=False)
    created = MasterKey.load_or_create(path, create=True)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = MasterKey.load_or_create(path, create=False)
    sealed = created.seal("model.api_key", 1, "x")
    assert loaded.open("model.api_key", 1, sealed) == "x"
    with pytest.raises(FileExistsError):
        created.save(path)  # tidak menimpa
    assert "tersembunyi" in repr(created)


@pytest.mark.parametrize("content", ["bukan base64!!", "AAAA"])
def test_bad_master_key_file(tmp_path: Path, content: str) -> None:
    path = tmp_path / "master.key"
    path.write_text(content, encoding="ascii")
    with pytest.raises(MasterKeyError):
        MasterKey.load(path)


# ---- hash kanonik dan katalog ------------------------------------------------------------------------------


def test_content_hash_is_canonical() -> None:
    a = {"b": 1, "a": {"y": [1, 2], "x": "é"}}
    b = json.loads('{"a": {"x": "é", "y": [1, 2]}, "b": 1}')
    assert catalogs.content_hash(a) == catalogs.content_hash(b)
    assert catalogs.content_hash(a).startswith("sha256:") and len(catalogs.content_hash(a)) == 71
    assert catalogs.content_hash({"a": 1}) != catalogs.content_hash({"a": 2})
    assert catalogs.canonical({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    with pytest.raises(ValueError, match=r".+"):
        catalogs.content_hash({"x": float("nan")})


VALID: dict[str, Any] = {
    "alert_penjelasan": {"entries": {"4625": {"judul": "Logon gagal", "penjelasan": "Banyak percobaan gagal"}}},
    "pola_insiden": {"pola": {"brute": {"judul": "Brute force", "langkah": ["S-isolasi"]}}},
    "temuan_korelasi": {"temuan": {"k1": {"judul": "Korelasi"}}},
    "langkah": {"langkah": {"S-isolasi": {"judul": "Isolasi", "risiko": "aman"}}},
    "sla": {"band": {"kritis": {"tanggapan_awal_menit": 15}}, "jam_kerja": {"mulai": "08:00"}},
    "bobot_skor": {"bobot": {"severity": {"high": 30, "low": 5}, "pengali": 1.5}, "ambang": {"kritis": 70}},
    "aturan_android": {"aturan": {"a1": {"aksi": "tampil"}}},
    "aturan_peran": {"peran": {"admin": {"izin": ["x"]}}},
    "aset": {"aset": {"WS-ADM-04": {"kritikalitas": "tinggi"}}},
}


@pytest.mark.parametrize("kind", catalogs.KINDS)
def test_every_kind_has_a_valid_example(kind: str) -> None:
    assert catalogs.validate_document(kind, VALID[kind]) == []


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        ("alert_penjelasan", {"entries": {"4625": {"judul": "tanpa penjelasan"}}}),
        ("alert_penjelasan", {"entries": {}}),
        ("alert_penjelasan", {"entries": {"4625": {"judul": "a", "penjelasan": "b"}}, "lain": 1}),
        ("pola_insiden", {"pola": {"p": {"judul": "x", "langkah": ["isolasi-tanpa-awalan"]}}}),
        ("langkah", {"langkah": {"S-x": {"judul": "x", "risiko": "berbahaya"}}}),
        ("sla", {"band": {"kritis": {"tanggapan_awal_menit": 0}}}),
        ("sla", {"band": {"kritis": {"tanggapan_awal_menit": True}}}),
        ("bobot_skor", {"bobot": {"x": -1}}),
        ("aset", {"aset": {"a": {"kritikalitas": "sangat"}}}),
        ("aturan_peran", {"peran": {"admin": "bukan objek"}}),
        ("aturan_android", []),
        ("temuan_korelasi", {"temuan": {"!!": {"judul": "x"}}}),
    ],
)
def test_invalid_documents_have_errors(kind: str, document: object) -> None:
    assert catalogs.validate_document(kind, document)


def test_unknown_kind_and_non_finite_numbers() -> None:
    assert catalogs.validate_document("tidak-ada", {})
    assert catalogs.validate_document("bobot_skor", {"bobot": {"x": float("inf")}})


def test_validation_errors_do_not_quote_content() -> None:
    doc = {"entries": {"4625": {"judul": "  ", "penjelasan": "RAHASIA-ISI"}}}
    assert all("RAHASIA-ISI" not in e for e in catalogs.validate_document("alert_penjelasan", doc))


def test_lint_references_warns_on_missing_steps() -> None:
    pola = {"pola": {"p1": {"judul": "x", "langkah": ["S-isolasi", "S-hilang"]}}}
    issues = catalogs.lint_references("pola_insiden", pola, VALID["langkah"])
    assert len(issues) == 1 and "S-hilang" in issues[0] and "p1" in issues[0]
    assert catalogs.lint_references("pola_insiden", VALID["pola_insiden"], VALID["langkah"]) == []
    assert catalogs.lint_references("pola_insiden", pola, None)  # langkah belum aktif: peringatan
    assert catalogs.lint_references("sla", VALID["sla"], None) == []


def test_unwritable_master_key_location_has_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(self: MasterKey, path: Path) -> None:
        raise PermissionError(13, "ditolak")

    monkeypatch.setattr(MasterKey, "save", deny)
    with pytest.raises(MasterKeyError, match="volume") as caught:
        MasterKey.load_or_create(tmp_path / "master.key", create=True)
    assert "PermissionError" in str(caught.value)


def test_master_key_created_by_another_replica_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "master.key"
    winner = MasterKey.generate()
    real_save = MasterKey.save

    def race(self: MasterKey, target: Path) -> None:
        winner_copy = real_save(winner, target)  # replika lain menulis lebih dulu
        real_save(self, target)  # ... lalu penulisan ini bentrok (O_EXCL)
        return winner_copy

    monkeypatch.setattr(MasterKey, "save", race)
    loaded = MasterKey.load_or_create(path, create=True)
    sealed = winner.seal("model.api_key", 1, "x")
    assert loaded.open("model.api_key", 1, sealed) == "x"
