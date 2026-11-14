"""Pagar `ASTRIA_ENV` pada jalur start sungguhan `astria-config` (infra#9).

Kebijakan (`astria_common.settings` dan `astria_common.envguard`): `ASTRIA_ENV` wajib dan hanya `dev`, `demo`, `prod`,
atau `test` (huruf kecil persis, tanpa nilai bawaan); konfigurasi yang bertentangan dengan lingkungannya ditolak.
Uji memanggil titik masuk console script dari image dan memastikan server tidak pernah dijalankan bila pagar menolak.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import astria_common.app
import pytest
from astria_common.envguard import EnvironmentGuardError
from pydantic import ValidationError

from astria_config import __main__ as entry
from astria_config.secrets import MasterKey

SERVICE = "config"
INVALID_ENV = [None, "", "staging", "production", "PROD", " prod"]


def _db(name: str) -> str:
    return f"postgresql+psycopg://astria:x@postgres:5432/{name}"


def valid_config(env: str, tmp_path: Path) -> dict[str, str]:
    """Konfigurasi sah minimum per lingkungan. `prod` wajib membawa kunci induk yang sudah ada."""
    key = tmp_path / "master.key"
    if env == "prod" and not key.exists():
        MasterKey.generate().save(key)
    return {
        "ASTRIA_DATABASE_URL": _db(f"{SERVICE}_{env}" if env != "prod" else SERVICE),
        "ASTRIA_MASTER_KEY_FILE": str(key),
    }


def start() -> None:
    entry.main()


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Ganti server dengan perekam: daftar lingkungan yang benar-benar sampai ke tahap membuka port."""
    calls: list[str] = []

    def fake_run(app: Any, settings: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(settings.env if settings is not None and hasattr(settings, "env") else "?")

    monkeypatch.setattr(astria_common.app, "run", fake_run)
    monkeypatch.setattr(entry, "run", fake_run, raising=False)
    monkeypatch.setattr(sys, "argv", ["astria-config"])

    monkeypatch.setenv("ASTRIA_SERVICE_KEY", "11" * 32)
    return calls


def _set(monkeypatch: pytest.MonkeyPatch, env: str | None, values: dict[str, str]) -> None:
    if env is None:
        monkeypatch.delenv("ASTRIA_ENV", raising=False)
    else:
        monkeypatch.setenv("ASTRIA_ENV", env)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("value", INVALID_ENV)
def test_missing_or_unsupported_env_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, served: list[str], value: str | None, tmp_path: Path
) -> None:
    _set(monkeypatch, value, valid_config("dev", tmp_path))
    with pytest.raises(ValidationError) as info:
        start()
    assert any(err["loc"] == ("env",) for err in info.value.errors())
    assert served == []
    assert not (tmp_path / "master.key").exists()  # kunci induk tidak dibuat


@pytest.mark.parametrize("env", ["dev", "demo", "prod", "test"])
def test_supported_env_starts(monkeypatch: pytest.MonkeyPatch, served: list[str], env: str, tmp_path: Path) -> None:
    _set(monkeypatch, env, valid_config(env, tmp_path))
    start()
    assert served == [env]


@pytest.mark.parametrize(
    ("env", "database"),
    [
        ("prod", f"{SERVICE}_dev"),
        ("prod", f"{SERVICE}_demo"),
        ("demo", f"{SERVICE}_dev"),
        ("demo", SERVICE),
        ("dev", f"{SERVICE}_demo"),
        ("dev", SERVICE),
    ],
)
def test_database_of_other_env_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, served: list[str], env: str, database: str, tmp_path: Path
) -> None:
    _set(monkeypatch, env, {**valid_config(env, tmp_path), "ASTRIA_DATABASE_URL": _db(database)})
    with pytest.raises(EnvironmentGuardError) as info:
        start()
    assert info.value.env == env
    assert served == []
    if env != "prod":
        assert not (tmp_path / "master.key").exists()  # kunci induk tidak dibuat


def test_prod_refuses_sqlite_before_touching_it(
    monkeypatch: pytest.MonkeyPatch, served: list[str], tmp_path: Path
) -> None:
    database = tmp_path / f"{SERVICE}.db"
    _set(
        monkeypatch,
        "prod",
        {**valid_config("prod", tmp_path), "ASTRIA_DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}"},
    )
    with pytest.raises(EnvironmentGuardError):
        start()
    assert served == []
    assert not database.exists()


def test_migrate_refuses_conflicting_env_before_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "config.db"
    monkeypatch.setattr(sys, "argv", ["astria-config", "migrate"])
    _set(monkeypatch, "prod", {"ASTRIA_DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}"})
    with pytest.raises(EnvironmentGuardError):
        entry.main()
    assert not database.exists()
