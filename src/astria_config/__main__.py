"""Jalankan layanan: `astria-config` atau `python -m astria_config`."""

from astria_common.app import run

from astria_config.main import build_app
from astria_config.settings import Settings


def main() -> None:
    settings = Settings()
    run(build_app(settings), settings)


if __name__ == "__main__":
    main()
