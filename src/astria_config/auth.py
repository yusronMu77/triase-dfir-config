"""Otorisasi jalur admin `/v1`: hanya `svc:api` dengan klaim `act`.

identity menerbitkan token dengan `act` hanya untuk `svc:api`, membaca ulang `act.roles` dari basis datanya, dan mengisi
`act.reauth_at` dari sesi. Karena itu config boleh mempercayai klaim tersebut; yang diperiksa di sini adalah peran dan
umur re-autentikasi per aksi.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from astria_common.auth import ServicePrincipal, require_service
from astria_common.errors import AstriaError
from fastapi import Depends, Request

from astria_config.metrics import AUTH_REJECTIONS

ADMIN = frozenset({"admin"})
ADMIN_OR_ANALIS = frozenset({"admin", "analis"})

_api_caller = require_service("svc:config", {"svc:api"})
# Pembaca jalur konsumen: layanan inti mana pun dengan token berumur pendek untuk svc:config.
svc_caller = require_service("svc:config")
narrative_caller = require_service("svc:config", {"svc:narrative"})


@dataclass(frozen=True)
class Actor:
    sub: str
    roles: frozenset[str]
    reauth_at: datetime | None

    def act_claim(self) -> dict[str, object]:
        return {"sub": self.sub, "roles": sorted(self.roles)}


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _reject(reason: str, code: str, message: str) -> AstriaError:
    AUTH_REJECTIONS.labels(reason).inc()
    return AstriaError(code, message, 403)


def actor_for(*, roles: frozenset[str], reauth: bool = False) -> Callable[..., Awaitable[Actor]]:
    """Dependensi: pengguna dari `act`, dengan salah satu peran `roles`; `reauth` menuntut login ulang segar."""

    async def dependency(request: Request, principal: ServicePrincipal = Depends(_api_caller)) -> Actor:  # noqa: B008
        act = principal.act
        sub = act.get("sub") if isinstance(act, dict) else None
        if not isinstance(sub, str):
            raise _reject("tanpa_act", "PERAN_TIDAK_CUKUP", "aksi ini memerlukan pengguna yang masuk")
        held = frozenset(r for r in (act.get("roles") or []) if isinstance(r, str))
        if not held & roles:
            raise _reject("peran", "PERAN_TIDAK_CUKUP", "peran Anda tidak boleh melakukan aksi ini")
        reauth_at = _parse_time(act.get("reauth_at"))
        if reauth:
            window = timedelta(minutes=request.app.state.settings.reauth_window_min)
            if reauth_at is None or datetime.now(UTC) - reauth_at > window:
                raise _reject("reauth", "REAUTH_DIPERLUKAN", "konfirmasi ulang kata sandi diperlukan")
        return Actor(sub=sub, roles=held, reauth_at=reauth_at)

    return dependency
