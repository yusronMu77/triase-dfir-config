"""Jalur konsumen `/svc/v1` (jaringan core saja; tidak diteruskan reverse proxy)."""

from __future__ import annotations

from typing import Annotated, Any

from astria_common.auth import ServicePrincipal
from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import JSONResponse

from astria_config.auth import narrative_caller, svc_caller
from astria_config.service import ConfigService

router = APIRouter(prefix="/svc/v1", tags=["config-svc"])

KIND = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


def _svc(request: Request) -> ConfigService:
    return request.app.state.service


@router.get("/settings/effective", summary="Nilai efektif satu lingkup, tanpa rahasia")
async def effective(
    request: Request,
    scope: Annotated[str, Query(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)],
    _p: ServicePrincipal = Depends(svc_caller),  # noqa: B008
) -> dict[str, Any]:
    return await _svc(request).effective_settings(scope)


@router.get("/catalogs/{kind}/active", summary="Katalog aktif")
async def active_catalog(
    kind: KIND,
    request: Request,
    _p: ServicePrincipal = Depends(svc_caller),  # noqa: B008
) -> dict[str, Any]:
    return await _svc(request).active_catalog(kind)


@router.get("/channels/policy", summary="Kebijakan kanal")
async def channel_policy(
    request: Request,
    _p: ServicePrincipal = Depends(svc_caller),  # noqa: B008
) -> dict[str, Any]:
    return await _svc(request).list_policies()


@router.get("/secrets/model.api_key", summary="Kunci API model (hanya narrative)")
async def model_api_key(
    request: Request,
    principal: ServicePrincipal = Depends(narrative_caller),  # noqa: B008
) -> JSONResponse:
    result = await _svc(request).read_model_key(principal.service)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
