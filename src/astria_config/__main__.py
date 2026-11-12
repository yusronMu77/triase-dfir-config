"""Jalankan layanan: `astria-config` atau `python -m astria_config`.

`astria-config migrate` menerapkan migrasi skema lalu berhenti.
"""

import sys

from astria_common.app import run

from astria_config.main import build_app
from astria_config.migrate import ensure_schema
from astria_config.settings import Settings

_ACTIONS = {
    "baru": "skema dibuat dari nol (upgrade head)",
    "stamp": "tabel sudah ada tanpa riwayat: diberi stamp head, tabel tidak diubah",
    "ada": "riwayat sudah ada: upgrade head (no-op bila sudah di head)",
}


def migrate() -> int:
    settings = Settings()
    if settings.database_url is None:
        print("ASTRIA_DATABASE_URL wajib diisi", file=sys.stderr)
        return 2
    action = ensure_schema(settings.database_url, settings.db_schema)
    print(f"migrasi selesai: {_ACTIONS[action]}")
    return 0


def main() -> None:
    args = sys.argv[1:]
    if args == ["migrate"]:
        raise SystemExit(migrate())
    if args:
        print("pemakaian: astria-config [migrate]", file=sys.stderr)
        raise SystemExit(2)
    settings = Settings()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
