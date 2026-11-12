"""GatewayClient: penolakan gateway yang boleh diteruskan ke admin, dan yang tetap 503."""

from __future__ import annotations

import httpx
import pytest

from astria_config.gateway_client import GatewayClient, GatewayRejected, GatewayUnavailable

pytestmark = pytest.mark.anyio

KEY = bytes.fromhex("11" * 32)
SECRET = "123456789:AAH-uji-bukan-token-asli-xxxxxxxxxxxxxxx"  # noqa: S105  # gitleaks:allow


def client(status: int, body: dict | str | None) -> GatewayClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if body is None:
            return httpx.Response(status)
        if isinstance(body, str):
            return httpx.Response(status, content=body.encode())
        return httpx.Response(status, json=body)

    http = httpx.AsyncClient(base_url="http://gateway.test", transport=httpx.MockTransport(handler))
    return GatewayClient("http://gateway.test", KEY, http=http)


def err(code: str) -> dict:
    return {"error": {"code": code, "message": f"pesan gateway memuat {SECRET}", "trace_id": "t"}}


async def test_success_204() -> None:
    await client(204, None).put_secret("telegram.bot_token", SECRET)


@pytest.mark.parametrize(
    ("status", "code"),
    [(404, "GW_KUNCI_TIDAK_DIKENAL"), (409, "GW_DIKUNCI_ENV"), (422, "GW_EVENT_INVALID")],
)
async def test_forwarded_rejections(status: int, code: str) -> None:
    with pytest.raises(GatewayRejected) as exc:
        await client(status, err(code)).put_secret("telegram.bot_token", SECRET)
    assert (exc.value.code, exc.value.status) == (code, status)
    assert SECRET not in exc.value.message  # pesan tetap, bukan pesan gateway


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, err("GW_AUTH_FAILED")),  # gagal autentikasi: bukan salah admin
        (503, err("GW_NOT_READY")),
        (500, "<html>boom</html>"),
        (404, err("GW_CHANNEL_UNKNOWN")),  # kode tidak ada di daftar teruskan
        (409, err("GW_KUNCI_TIDAK_DIKENAL")),  # kode dan status tidak cocok
        (422, "bukan json"),
        (422, {"error": "bukan objek"}),
    ],
)
async def test_other_failures_stay_unavailable(status: int, body: dict | str) -> None:
    with pytest.raises(GatewayUnavailable):
        await client(status, body).put_secret("telegram.bot_token", SECRET)
