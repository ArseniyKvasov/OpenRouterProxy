from unittest.mock import AsyncMock

import httpx
import pytest

from app.config import Settings
from app.main import build_upstream_headers, create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        ai_gateway_key="gateway-secret",
        openrouter_api_key="openrouter-secret",
    )


@pytest.mark.asyncio
async def test_health(settings: Settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://test",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_proxy_rejects_missing_gateway_key(settings: Settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings)),
        base_url="http://test",
    ) as client:
        response = await client.post("/openrouter/chat/completions", json={})

    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}


def test_upstream_headers_replace_client_authorization(settings: Settings):
    request = httpx.Request(
        "POST",
        "http://test/openrouter/chat/completions",
        headers={
            "Authorization": "Bearer client-token",
            "X-Gateway-Key": settings.ai_gateway_key,
            "Content-Type": "application/json",
            "X-Custom": "preserved",
        },
    )

    headers = build_upstream_headers(httpx.Request("POST", request.url, headers=request.headers), settings.openrouter_api_key)

    assert headers["Authorization"] == "Bearer openrouter-secret"
    assert headers["content-type"] == "application/json"
    assert headers["x-custom"] == "preserved"
    assert "x-gateway-key" not in {name.lower() for name in headers}


@pytest.mark.asyncio
async def test_proxy_streams_upstream_response(settings: Settings):
    app = create_app(settings)
    upstream_response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "connection": "close"},
        content=b"data: hello\n\n",
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
    )

    async def stream_response():
        yield b"data: hello\n\n"

    upstream_response.aiter_raw = stream_response

    mock_client = AsyncMock()
    mock_client.build_request = httpx.AsyncClient().build_request
    mock_client.send.return_value = upstream_response

    async with app.router.lifespan_context(app):
        app.state.http_client = mock_client
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/openrouter/chat/completions",
                headers={"x-gateway-key": settings.ai_gateway_key},
                content=b"{}",
            )

    assert response.status_code == 200
    assert response.content == b"data: hello\n\n"
    mock_client.send.assert_awaited_once()
