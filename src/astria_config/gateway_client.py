"""Penerusan rahasia kanal ke gateway: lewat memori, HMAC per permintaan, tanpa menyimpan nilainya di Core.

Kontrak sisi gateway (contracts/openapi/gateway-internal.yaml): `PUT /internal/v1/secrets/{key}`
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
    """Gateway tidak terjangkau, belum siap, atau menolak tanpa alasan yang dapat dipakai pemanggil (mis. 401)."""


# Penolakan gateway yang diteruskan ke admin dengan kode aslinya: kode -> (status HTTP, pesan tetap). Pesan dari gateway
# tidak diteruskan, jadi tidak ada jalan bagi nilai rahasia untuk bocor lewat galat.
FORWARDED_REJECTIONS: dict[str, tuple[int, str]] = {
    "GW_KUNCI_TIDAK_DIKENAL": (404, "gateway tidak mengenal kunci rahasia ini"),
    "GW_DIKUNCI_ENV": (409, "kunci rahasia ini dikunci lewat environment di gateway"),
    "GW_EVENT_INVALID": (422, "gateway menolak nilai rahasia (format tidak valid)"),
}


class GatewayRejected(Exception):
    """Gateway menolak rahasia dengan alasan yang boleh diteruskan ke admin."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status, self.message = FORWARDED_REJECTIONS[code]


def _rejection_code(resp: httpx.Response) -> str | None:
    """Kode galat gateway bila termasuk yang boleh diteruskan dan cocok dengan statusnya; selain itu None."""
    try:
        code = resp.json()["error"]["code"]
    except ValueError, KeyError, TypeError:
        return None
    known = FORWARDED_REJECTIONS.get(code) if isinstance(code, str) else None
    return code if known is not None and known[0] == resp.status_code else None


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
            code = _rejection_code(resp)
            if code is not None:
                raise GatewayRejected(code)
            raise GatewayUnavailable(f"status {resp.status_code}")

    async def aclose(self) -> None:
        await self._http.aclose()
