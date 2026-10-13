import httpx
import pytest

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings()


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
async def test_proxy_forwards_request_to_openrouter(settings: Settings):
    app = create_app(settings)
    upstream_response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=b'{"choices":[{"message":{"content":"Hello"}}]}',
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"),
    )

    mock_client = AsyncMock()
    mock_client.build_request = httpx.AsyncClient().build_request
    mock_client.send.return_value = upstream_response

    async with app.router.lifespan_context(app):
        app.state.http_client = mock_client
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/completions",
                headers={"Authorization": "Bearer user-key"},
                json={"model": "gpt-4", "messages": [{"role": "user", "content": "Hi"}]},
            )

    assert response.status_code == 200
    assert response.json() == {"choices": [{"message": {"content": "Hello"}}]}
    mock_client.send.assert_awaited_once()
    upstream_request = mock_client.send.await_args.args[0]
    assert str(upstream_request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert upstream_request.headers["host"] == "openrouter.ai"
    assert upstream_request.headers["authorization"] == "Bearer user-key"


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
                "/chat/completions",
                headers={"Authorization": "Bearer user-key"},
                content=b"{}",
            )

    assert response.status_code == 200
    assert response.content == b"data: hello\n\n"
    mock_client.send.assert_awaited_once()
    assert mock_client.send.await_args.kwargs["stream"] is True


@pytest.mark.asyncio
async def test_proxy_handles_upstream_error(settings: Settings):
    app = create_app(settings)

    mock_client = AsyncMock()
    mock_client.build_request = httpx.AsyncClient().build_request
    mock_client.send.side_effect = httpx.ConnectError("Connection refused")

    async with app.router.lifespan_context(app):
        app.state.http_client = mock_client
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/chat/completions",
                headers={"Authorization": "Bearer user-key"},
                content=b"{}",
            )

    assert response.status_code == 502
    assert "Upstream request failed" in response.text


from unittest.mock import AsyncMock