"""Rakit aplikasi config: komponen standar, seed dev opsional, dan jalur baca `/svc/v1`.

Dev memakai `uvicorn astria_config.main:build_app --factory --reload`; image memakai `astria-config`.
"""

from astria_common.app import create_app
from astria_common.auth import ServiceTokenVerifier
from astria_common.envguard import EnvironmentGuardError
from astria_common.service import standard_runtime
from fastapi import FastAPI

from astria_config import api
from astria_config.settings import Settings
from astria_config.store import Seeder, load_seed, metadata


def build_app(settings: Settings | None = None, *, token_verifier: ServiceTokenVerifier | None = None) -> FastAPI:
    settings = settings or Settings()
    if settings.config_seed_file is not None and settings.env == "prod":
        raise EnvironmentGuardError(settings.env, ["berkas seed dev (ASTRIA_CONFIG_SEED_FILE) dilarang di produksi"])
    runtime = standard_runtime(settings, metadata=[metadata])
    if settings.config_seed_file is not None:
        runtime.add(Seeder(runtime.require_db(), load_seed(settings.config_seed_file)))
    app = create_app(settings, components=runtime.components, routers=[api.router], token_verifier=token_verifier)
    app.state.db = runtime.db
    return app
