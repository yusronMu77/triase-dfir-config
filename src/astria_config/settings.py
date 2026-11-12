"""Konfigurasi config dari env `ASTRIA_*`.

Variabel `ASTRIA_CFG_<KUNCI>` (mis. `ASTRIA_CFG_CHAT__ALLOW_T2=true`) bukan field di sini: itu lapisan env terkunci
untuk nilai pengaturan dan dibaca oleh `astria_config.layers`.
"""

from pathlib import Path

from astria_common.settings import AstriaSettings

DEV_CHANNELS = ("email-tim", "telegram-tim", "wa-tim")


class Settings(AstriaSettings):
    service_name: str = "config"

    config_file: Path | None = None
    """Berkas YAML lapisan berkas (opsional). Divalidasi saat start; kunci tak dikenal atau rahasia = gagal start."""

    master_key_file: Path = Path("/var/lib/astria/config/master.key")
    """Kunci induk AES-GCM (32 byte, base64). Dibuat otomatis (0600) di dev dan test; `prod` wajib sudah ada."""

    reauth_window_min: int = 10
    """Umur maksimum `act.reauth_at` untuk rahasia dan aktivasi katalog."""

    gateway_url: str = "http://gateway:8080"
    """Tujuan penerusan rahasia kanal. Hanya dari env; tidak pernah dari nilai pengaturan."""

    seed_channels: tuple[str, ...] | None = None
    """Kanal yang disemai kebijakan awalnya (aktif, ringkas) bila tabel kosong. Kosong = bawaan per lingkungan
    (dev dan demo: kanal dev; test dan prod: tidak ada)."""

    def channels_to_seed(self) -> tuple[str, ...]:
        if self.seed_channels is not None:
            return self.seed_channels
        return DEV_CHANNELS if self.env in {"dev", "demo"} else ()
