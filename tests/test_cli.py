"""CLI `config`: validate, catalog lint/hash/seed, dan rotate-master (SQLite)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from astria_common.auth import SigningKey
from fastapi.testclient import TestClient
from helpers import LANGKAH, fresh, headers, make_app, wait_ready

from astria_config import catalogs, cli
from astria_config.secrets import MasterKey

CANARY = "nilai-uji-tidak-boleh-bocor-3"


def run(*argv: str) -> int:
    with pytest.raises(SystemExit) as done:
        cli.main(list(argv))
    return int(done.value.code or 0)


def query(db: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


def write(path: Path, content: object) -> Path:
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def test_validate_accepts_and_rejects(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = write(tmp_path / "ok.yaml", "model:\n  timeout_seconds: 30\n")
    assert run("validate", "--file", str(good)) == 0
    assert "valid: 1 kunci" in capsys.readouterr().out
    bad = write(tmp_path / "bad.yaml", "model:\n  api_key: sk-rahasia\n")
    assert run("validate", "--file", str(bad)) == 1
    err = capsys.readouterr().err
    assert "TIDAK VALID" in err and "sk-rahasia" not in err


def test_validate_checks_env_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    good = write(tmp_path / "ok.yaml", "")
    monkeypatch.setenv("ASTRIA_CFG_CHAT__ALLOW_T2", "mungkin")
    assert run("validate", "--file", str(good)) == 1
    assert "ASTRIA_CFG_CHAT__ALLOW_T2" in capsys.readouterr().err


def test_catalog_lint(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("catalog", "lint", "langkah", str(write(tmp_path / "a.json", LANGKAH))) == 0
    assert "valid: langkah" in capsys.readouterr().out
    bad = write(tmp_path / "b.json", {"langkah": {"S-x": {"judul": "x", "risiko": "salah"}}})
    assert run("catalog", "lint", "langkah", str(bad)) == 1
    assert "risiko" in capsys.readouterr().err


def test_catalog_lint_unreadable_file_exits_nonzero(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as done:
        cli.main(["catalog", "lint", "langkah", str(write(tmp_path / "c.json", "bukan json"))])
    assert done.value.code not in (0, None)


def test_catalog_hash_matches_service_hash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path / "a.json", LANGKAH)
    assert run("catalog", "hash", str(path)) == 0
    assert capsys.readouterr().out.strip() == catalogs.content_hash(LANGKAH)


def _db_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **extra: str) -> Path:
    monkeypatch.setenv("ASTRIA_ENV", "test")
    monkeypatch.setenv("ASTRIA_DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'config.db').as_posix()}")
    monkeypatch.setenv("ASTRIA_MASTER_KEY_FILE", str(tmp_path / "master.key"))
    for k, v in extra.items():
        monkeypatch.setenv(k, v)
    return tmp_path / "config.db"


def test_catalog_seed_activates_next_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _db_env(monkeypatch, tmp_path)
    path = write(tmp_path / "langkah.json", LANGKAH)
    assert run("catalog", "seed", "langkah", str(path), "--by", "seed-dev") == 0
    assert "versi 1 aktif" in capsys.readouterr().out
    other = write(tmp_path / "l2.json", {"langkah": {"S-b": {"judul": "b", "risiko": "aman"}}})
    assert run("catalog", "seed", "langkah", str(other), "--by", "seed-dev") == 0
    rows = (
        sqlite3.connect(db)
        .execute("SELECT version, status, created_by FROM catalog_version ORDER BY version")
        .fetchall()
    )
    assert rows == [(1, "arsip", "seed-dev"), (2, "aktif", "seed-dev")]
    events = query(db, "SELECT count(*) FROM outbox WHERE event_type = 'config.changed'")[0][0]
    assert events == 2


def test_catalog_seed_rejects_invalid_and_prod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _db_env(monkeypatch, tmp_path)
    bad = write(tmp_path / "b.json", {"langkah": {}})
    assert run("catalog", "seed", "langkah", str(bad), "--by", "x") == 1
    assert "tidak valid" in capsys.readouterr().err
    monkeypatch.setenv("ASTRIA_ENV", "prod")
    good = write(tmp_path / "g.json", LANGKAH)
    assert run("catalog", "seed", "langkah", str(good), "--by", "x") == 2
    assert "prod" in capsys.readouterr().err


def test_rotate_master_reencrypts_all_secrets(tmp_path: Path, key: SigningKey, monkeypatch: pytest.MonkeyPatch) -> None:
    with TestClient(make_app(tmp_path, key)) as client:
        wait_ready(client)
        resp = client.put("/v1/secrets/model.api_key", json={"value": CANARY}, headers=headers(key, reauth=fresh(1)))
        assert resp.status_code == 200
    db = _db_env(monkeypatch, tmp_path)
    old_ct = query(db, "SELECT ciphertext, key_version FROM setting WHERE key='model.api_key'")[0]

    new_key = tmp_path / "new.key"
    assert run("rotate-master", "--new-key-file", str(new_key)) == 0
    new_ct = query(db, "SELECT ciphertext, key_version FROM setting WHERE key='model.api_key'")[0]
    assert new_ct[0] != old_ct[0] and new_ct[1] == old_ct[1] + 1

    # Kunci lama tak lagi membuka; kunci baru membuka. Berkas lama tidak ditimpa.
    row = query(db, "SELECT nonce, version FROM setting WHERE key='model.api_key'")[0]
    from astria_config.secrets import Sealed, SecretUnreadable

    sealed = Sealed(new_ct[0], row[0], new_ct[1])
    with pytest.raises(SecretUnreadable):
        MasterKey.load(tmp_path / "master.key").open("model.api_key", row[1], sealed)
    assert MasterKey.load(new_key).open("model.api_key", row[1], sealed) == CANARY

    (tmp_path / "master.key").write_bytes(new_key.read_bytes())  # operator mengganti kunci aktif
    with TestClient(make_app(tmp_path, key)) as client:
        wait_ready(client)
        got = client.get("/svc/v1/secrets/model.api_key", headers=headers(key, None, subject="svc:narrative"))
        assert got.json()["value"] == CANARY


def test_rotate_master_refuses_same_file_and_missing_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _db_env(monkeypatch, tmp_path)
    same = tmp_path / "master.key"
    assert run("rotate-master", "--new-key-file", str(same)) == 2
    assert run("rotate-master", "--new-key-file", str(tmp_path / "new.key")) == 1  # kunci lama tidak ada
    assert "kunci induk" in capsys.readouterr().err
