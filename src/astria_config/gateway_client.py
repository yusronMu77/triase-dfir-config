"""Penerusan rahasia kanal ke gateway: lewat memori, HMAC per permintaan, tanpa menyimpan nilainya di Core.

Kontrak sisi gateway (belum ada di gateway Tahap ini; `ditunda` di gateway/settings): `PUT /internal/v1/secrets/{key}`
dengan isi `{"value": "<rahasia>"}`, diautentikasi `X-Astria-Service: config` + HMAC (`astria_common.hmac_auth`).
"""

from __future__ import annotations

import httpx
import orjson
from astria_common.hmac_auth import signed_headers
from astria_common.http import make_client
from astria_common.log import get_logger

log = get_logger(__name__)


class GatewayUnavailable(Exception):
    """Gateway tidak terjangkau atau menolak; pesan tidak memuat nilai rahasia."""


class GatewayClient:
    def __init__(self, base_url: str, service_key: bytes, *, http: httpx.AsyncClient | None = None) -> None:
        self._key = service_key
        self._http = http or make_client(base_url, timeout=5.0)

    async def put_secret(self, key: str, value: str) -> None:
        path = f"/internal/v1/secrets/{key}"
        body = orjson.dumps({"value": value})
        headers = signed_headers("config", self._key, "PUT", path, body) | {"Content-Type": "application/json"}
        try:
            resp = await self._http.put(path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("gateway_tidak_terjangkau", error=type(exc).__name__)
            raise GatewayUnavailable(type(exc).__name__) from None
        if resp.status_code not in (200, 201, 204):
            log.warning("gateway_menolak_rahasia", status=resp.status_code)
            raise GatewayUnavailable(f"status {resp.status_code}")

    async def aclose(self) -> None:
        await self._http.aclose()
