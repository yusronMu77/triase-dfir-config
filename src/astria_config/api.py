"""Jalur baca konsumen internal config (`/svc/v1`, note app-config 3.2). Wajib token layanan `svc:config`."""

from __future__ import annotations

from typing import Annotated, Any

from astria_common.auth import ServicePrincipal, require_service
from astria_common.errors import AstriaError
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from astria_config.store import SCOPE, ChannelPolicy, effective, list_policies

router = APIRouter(prefix="/svc/v1", tags=["config"])

POLICY_CALLERS = {"svc:api", "svc:notifier"}
SETTINGS_CALLERS = {
    "svc:api",
    "svc:ingest",
    "svc:narrative",
    "svc:notifier",
    "svc:scheduler",
    "svc:ticket",
    "svc:triage",
}


def _engine(request: Request) -> AsyncEngine:
    db = request.app.state.db
    if db is None or not db.is_ready:
        raise AstriaError("STORE_UNAVAILABLE", "penyimpanan konfigurasi belum siap", 503)
    return db.engine


@router.get("/channels/policy")
async def channel_policies(
    request: Request,
    _caller: Annotated[ServicePrincipal, Depends(require_service("svc:config", POLICY_CALLERS))],
) -> dict[str, list[ChannelPolicy]]:
    async with _engine(request).connect() as conn:
        return {"items": await list_policies(conn)}


@router.get("/settings/effective")
async def effective_settings(
    request: Request,
    scope: Annotated[str, Query(pattern=SCOPE)],
    _caller: Annotated[ServicePrincipal, Depends(require_service("svc:config", SETTINGS_CALLERS))],
) -> dict[str, Any]:
    async with _engine(request).connect() as conn:
        return {"scope": scope, "values": await effective(conn, scope)}
