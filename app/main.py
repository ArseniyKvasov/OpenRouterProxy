from contextlib import asynccontextmanager
import secrets
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.config import Settings, get_settings


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

SENSITIVE_REQUEST_HEADERS = {
    "authorization",
    "x-gateway-key",
    "x-gateway-secret",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings

    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.request_timeout_seconds,
            connect=settings.connect_timeout_seconds,
        ),
        follow_redirects=False,
    )

    yield

    await app.state.http_client.aclose()


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    configured_settings = settings or get_settings()

    app = FastAPI(
        title="OpenRouter Proxy",
        version="1.1.0",
        lifespan=lifespan,
    )

    app.state.settings = configured_settings

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route(
        "/openrouter/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def openrouter_proxy(path: str, request: Request):
        if not is_authorized(request, configured_settings.ai_gateway_key):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        body = await request.body()
        upstream_url = (
            f"{configured_settings.openrouter_base_url.rstrip('/')}/{path}"
        )

        client: httpx.AsyncClient = request.app.state.http_client

        try:
            upstream_request = client.build_request(
                method=request.method,
                url=upstream_url,
                params=request.query_params,
                headers=build_upstream_headers(
                    request,
                    configured_settings.openrouter_api_key,
                ),
                content=body,
            )

            upstream_response = await client.send(upstream_request)

        except httpx.HTTPError:
            return JSONResponse(
                {"error": "Upstream OpenRouter request failed"},
                status_code=502,
            )

        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=build_response_headers(upstream_response.headers),
            media_type=upstream_response.headers.get("content-type"),
        )

    @app.post("/telegram/sendMessage")
    async def telegram_send_message(request: Request):
        if not is_authorized(request, configured_settings.ai_gateway_key):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        telegram_token = str(
            getattr(configured_settings, "telegram_bot_token", "")
        ).strip()
        telegram_user_id = str(
            getattr(configured_settings, "telegram_user_id", "")
        ).strip()

        if not telegram_token or not telegram_user_id:
            return JSONResponse(
                {"error": "Telegram proxy is not configured"},
                status_code=503,
            )

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"error": "Request body must be valid JSON"},
                status_code=400,
            )

        if not isinstance(payload, dict):
            return JSONResponse(
                {"error": "Request body must be a JSON object"},
                status_code=400,
            )

        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                {"error": "Telegram message text is required"},
                status_code=400,
            )

        # Chat ID is always controlled by the proxy environment.
        payload["chat_id"] = telegram_user_id

        telegram_url = (
            f"https://api.telegram.org/bot{telegram_token}/sendMessage"
        )

        client: httpx.AsyncClient = request.app.state.http_client

        try:
            telegram_response = await client.post(
                telegram_url,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        except httpx.HTTPError:
            return JSONResponse(
                {"error": "Telegram upstream request failed"},
                status_code=502,
            )

        return Response(
            content=telegram_response.content,
            status_code=telegram_response.status_code,
            headers=build_response_headers(telegram_response.headers),
            media_type=telegram_response.headers.get("content-type"),
        )

    return app


def get_gateway_key(request: Request) -> str:
    return (
        request.headers.get("x-gateway-key")
        or request.headers.get("x-gateway-secret")
        or ""
    )


def is_authorized(request: Request, expected_key: str) -> bool:
    received_key = get_gateway_key(request)

    return bool(received_key) and bool(expected_key) and secrets.compare_digest(
        received_key,
        expected_key,
    )


def build_upstream_headers(
    request: Request,
    openrouter_api_key: str,
) -> dict[str, str]:
    headers: dict[str, str] = {}

    for name, value in request.headers.items():
        normalized_name = name.lower()

        if (
            normalized_name in HOP_BY_HOP_HEADERS
            or normalized_name in SENSITIVE_REQUEST_HEADERS
        ):
            continue

        headers[name] = value

    headers["Authorization"] = f"Bearer {openrouter_api_key}"
    return headers


def build_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
    }


app = create_app()