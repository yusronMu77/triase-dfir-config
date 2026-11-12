"""Rakit aplikasi config: lapisan nilai, kunci induk, basis data, outbox, audit sinkron, dan router.

Start ditolak (sebelum port dibuka) bila berkas konfigurasi, variabel env, atau kunci induk tidak valid.
Dev memakai `uvicorn astria_config.main:build_app --factory --reload`; image memakai `astria-config`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping

from astria_common import envguard
from astria_common.app import create_app
from astria_common.audit_client import AuditClient
from astria_common.auth import ServiceTokenClient, ServiceTokenVerifier
from astria_common.component import Check
from astria_common.db import Database
from astria_common.errors import AstriaError, error_body
from astria_common.log import get_logger
from astria_common.service import standard_runtime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError

from astria_config import routes_admin, routes_svc
from astria_config.db import metadata
from astria_config.gateway_client import GatewayClient
from astria_config.layers import ENV_PREFIX, Layers
from astria_config.secrets import MasterKey
from astria_config.service import AuditSink, ConfigService, SecretForwarder, UnavailableAudit
from astria_config.settings import Settings

log = get_logger(__name__)


class Bootstrap:
    """Komponen: menyiapkan data awal (kebijakan kanal, gauge katalog) setelah DB siap; /ready `down` sebelum itu."""

    name = "config"
    required = True

    def __init__(self, settings: Settings, runtime_db: Database, service: ConfigService) -> None:
        self._settings = settings
        self._db = runtime_db
        self._service = service
        self._done = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._error: str | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="config-bootstrap")

    async def _run(self) -> None:
        await self._db.wait_ready()
        while True:
            try:
                created = await self._service.seed_policies(self._settings.channels_to_seed())
                await self._service.refresh_catalog_gauge()
                if created:
                    log.info("kebijakan_kanal_disemai", jumlah=created)
                self._error = None
                self._done.set()
                return
            except Exception as exc:  # noqa: BLE001 - dicoba ulang; status terlihat di /ready
                self._error = type(exc).__name__
                log.warning("bootstrap_config_gagal", error=self._error)
                await asyncio.sleep(2)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def check(self) -> Check:
        if not self._done.is_set():
            return Check("down", f"menyiapkan data awal ({self._error or 'menunggu basis data'})")
        return Check("ok")


def build_app(
    settings: Settings | None = None,
    *,
    audit: AuditSink | None = None,
    gateway: SecretForwarder | None = None,
    token_verifier: ServiceTokenVerifier | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    envguard.enforce(settings)  # sebelum menyentuh berkas atau kunci induk
    env = {k: v for k, v in (environ if environ is not None else os.environ).items() if k.startswith(ENV_PREFIX)}
    layers = Layers.from_settings(settings.config_file, env)  # ConfigError = gagal start
    master = MasterKey.load_or_create(settings.master_key_file, create=settings.env != "prod")

    runtime = standard_runtime(settings, metadata=[metadata])
    db = runtime.require_db()

    key = settings.service_key_bytes()
    if audit is None:
        if key is not None:
            audit = AuditClient(
                ServiceTokenClient(settings.service_name, key, settings.identity_url), settings.audit_url
            )
        else:
            audit = UnavailableAudit()
    if gateway is None and key is not None and settings.gateway_url:
        gateway = GatewayClient(settings.gateway_url, key)

    service = ConfigService(db=db, layers=layers, master=master, audit=audit, gateway=gateway)
    runtime.add(Bootstrap(settings, db, service))

    app = create_app(
        settings,
        components=runtime.components,
        routers=[routes_admin.router, routes_svc.router],
        token_verifier=token_verifier,
    )
    app.state.service = service

    @app.exception_handler(DBAPIError)
    @app.exception_handler(InterfaceError)
    async def _store_down(_req: Request, exc: Exception) -> JSONResponse:
        log.error("penyimpanan_galat", error=type(exc).__name__)
        err = AstriaError("PENYIMPANAN_TIDAK_TERSEDIA", "penyimpanan tidak tersedia", 503)
        return JSONResponse(error_body(err.code, err.message), status_code=503)

    return app
