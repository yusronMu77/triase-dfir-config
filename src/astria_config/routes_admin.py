"""Jalur admin `/v1` (kontrak: contracts/openapi/config-internal.yaml). Hanya dari `svc:api` dengan klaim `act`."""

from __future__ import annotations

from typing import Annotated, Any

from astria_contracts.openapi.config_internal import (
    CreateCatalogDraftRequest,
    PutChannelPolicyRequest,
    PutSecretRequest,
    PutSettingRequest,
)
from fastapi import APIRouter, Depends, Path, Request, Response

from astria_config.auth import ADMIN, ADMIN_OR_ANALIS, Actor, actor_for
from astria_config.service import ConfigService

router = APIRouter(prefix="/v1", tags=["config-admin"])

KEY = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$", max_length=100)]
GATEWAY_KEY = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_.-]*$", max_length=100)]
CHANNEL = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]*$", max_length=64)]
KIND = Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]
VERSION = Annotated[int, Path(ge=1)]

admin = Depends(actor_for(roles=ADMIN))
admin_reauth = Depends(actor_for(roles=ADMIN, reauth=True))
reader = Depends(actor_for(roles=ADMIN_OR_ANALIS))


def _svc(request: Request) -> ConfigService:
    return request.app.state.service


@router.get("/settings", summary="Pengaturan efektif beserta sumbernya")
async def list_settings(request: Request, _a: Actor = admin) -> dict[str, Any]:
    return await _svc(request).list_settings()


@router.put("/settings/{key}", summary="Ubah nilai runtime")
async def put_setting(key: KEY, body: PutSettingRequest, request: Request, actor: Actor = admin) -> dict[str, Any]:
    return await _svc(request).put_setting(actor, key, body.value, body.if_version, body.alasan)


@router.put("/secrets/{key}", summary="Tulis rahasia Core (tulis-saja)")
async def put_secret(key: KEY, body: PutSecretRequest, request: Request, actor: Actor = admin_reauth) -> dict[str, Any]:
    return await _svc(request).put_secret(actor, key, body.value)


@router.put("/secrets/gateway/{key}", status_code=204, summary="Teruskan rahasia kanal ke gateway")
async def forward_secret(
    key: GATEWAY_KEY, body: PutSecretRequest, request: Request, actor: Actor = admin_reauth
) -> Response:
    await _svc(request).forward_gateway_secret(actor, key, body.value)
    return Response(status_code=204)


@router.get("/channels/policy", summary="Kebijakan kanal")
async def list_policies(request: Request, _a: Actor = reader) -> dict[str, Any]:
    return await _svc(request).list_policies()


@router.put("/channels/policy/{channel_id}", summary="Ubah kebijakan kanal")
async def put_policy(
    channel_id: CHANNEL, body: PutChannelPolicyRequest, request: Request, actor: Actor = admin
) -> dict[str, Any]:
    return await _svc(request).put_policy(
        actor,
        channel_id,
        active=body.active,
        max_detail=body.max_detail,
        min_band=body.min_band,
        if_version=body.if_version,
    )


@router.get("/catalogs/{kind}/versions", summary="Versi katalog")
async def list_versions(kind: KIND, request: Request, _a: Actor = reader) -> dict[str, Any]:
    return await _svc(request).list_versions(kind)


@router.post("/catalogs/{kind}/versions", status_code=201, summary="Unggah draf katalog")
async def create_draft(
    kind: KIND, body: CreateCatalogDraftRequest, request: Request, actor: Actor = reader
) -> dict[str, Any]:
    return await _svc(request).create_draft(actor, kind, body.content, body.schema_version)


@router.post("/catalogs/{kind}/versions/{version}/review", summary="Tandai ditinjau (peninjau bukan penulis)")
async def review_version(kind: KIND, version: VERSION, request: Request, actor: Actor = admin) -> dict[str, Any]:
    return await _svc(request).review(actor, kind, version)


@router.post("/catalogs/{kind}/versions/{version}/activate", summary="Aktifkan versi yang sudah ditinjau")
async def activate_version(
    kind: KIND, version: VERSION, request: Request, actor: Actor = admin_reauth
) -> dict[str, Any]:
    return await _svc(request).activate(actor, kind, version)


@router.get("/catalogs/{kind}/coverage", summary="Cakupan katalog aktif")
async def coverage(kind: KIND, request: Request, _a: Actor = reader) -> dict[str, Any]:
    return await _svc(request).coverage(kind)
