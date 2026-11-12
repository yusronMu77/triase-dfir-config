"""Konfigurasi config dari env `ASTRIA_*`."""

from pathlib import Path

from astria_common.settings import AstriaSettings


class Settings(AstriaSettings):
    service_name: str = "config"

    config_seed_file: Path | None = None
    """ASTRIA_CONFIG_SEED_FILE: JSON kebijakan kanal dan pengaturan dev, diterapkan idempoten saat start.
    Dilarang di `prod`; butuh ASTRIA_DATABASE_URL."""
