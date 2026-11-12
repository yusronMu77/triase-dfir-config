"""Smoke: aplikasi hidup, pagar lingkungan, dan kunci induk tidak dibuat sebelum pagar lolos."""

from pathlib import Path

import pytest
from astria_common.auth import SigningKey
from astria_common.envguard import EnvironmentGuardError
from fastapi.testclient import TestClient
from helpers import make_app, settings_for, wait_ready
from pydantic import SecretStr

from astria_config.main import build_app
from astria_config.secrets import MasterKeyError
from astria_config.settings import Settings


def test_health_ready_and_metrics(tmp_path: Path, key: SigningKey) -> None:
    with TestClient(make_app(tmp_path, key)) as client:
        health = client.get("/health").json()
        assert (health["service"], health["env"]) == ("config", "test")
        wait_ready(client)
        assert "astria_service_info" in client.get("/metrics").text
    assert (tmp_path / "master.key").exists()


def test_refuses_to_start_with_non_dev_database_in_dev(tmp_path: Path) -> None:
    url = SecretStr("postgresql+psycopg://u:p@postgres:5432/astria")
    master = tmp_path / "master.key"
    with pytest.raises(EnvironmentGuardError):
        build_app(Settings(env="dev", database_url=url, master_key_file=master))
    assert not master.exists()  # pagar lingkungan jalan sebelum kunci induk dibuat


def test_prod_refuses_sqlite(tmp_path: Path) -> None:
    with pytest.raises(EnvironmentGuardError):
        build_app(settings_for(tmp_path, env="prod"))
    assert not (tmp_path / "master.key").exists()


def test_prod_requires_existing_master_key(tmp_path: Path) -> None:
    # Pagar prod (PostgreSQL non-dev, dst.) dipenuhi lewat env sungguhan di CI; di sini cukup kontrak kunci induk:
    from astria_config.secrets import MasterKey

    with pytest.raises(MasterKeyError):
        MasterKey.load_or_create(tmp_path / "belum-ada.key", create=False)
