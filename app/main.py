from contextlib import asynccontextmanager
import secrets
import ssl
import uuid
from typing import Optional, Tuple

import httpx
from starlette.background import BackgroundTask
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.corporate import (
    CorporateError,
    CorporateRoute,
    CorporateRouteResolver,
    EncryptedRouteCache,
    JwksVerifier,
    operation_for,
)


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
    "content-encoding",
}

SENSITIVE_REQUEST_HEADERS = {
    "authorization",
    "api-key",
    "x-gateway-key",
    "x-gateway-secret",
    "x-organization-id",
    "x-organization-context",
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
    app.state.config_http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.request_timeout_seconds,
            connect=settings.connect_timeout_seconds,
        ),
        follow_redirects=False,
        verify=build_config_ssl_context(settings),
    )

    redis_client: Redis | None = None
    route_cache: EncryptedRouteCache | None = None
    if settings.corporate_redis_url and settings.corporate_cache_encryption_key:
        redis_client = Redis.from_url(
            settings.corporate_redis_url,
            decode_responses=True,
        )
        route_cache = EncryptedRouteCache(
            redis_client,
            settings.corporate_cache_encryption_key,
            settings.corporate_route_cache_seconds,
        )

    app.state.corporate_cache = route_cache
    app.state.corporate_resolver = CorporateRouteResolver(
        settings,
        app.state.config_http_client,
        route_cache,
    )
    app.state.jwks_verifier = JwksVerifier(
        settings,
        app.state.config_http_client,
    )

    yield

    if redis_client is not None:
        await redis_client.aclose()
    await app.state.config_http_client.aclose()
    await app.state.http_client.aclose()


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    configured_settings = settings or get_settings()

    app = FastAPI(
        title="OpenRouter Proxy",
        version="1.2.0",
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
        organization_header_present = "x-organization-id" in request.headers
        context_header_present = "x-organization-context" in request.headers
        if organization_header_present or context_header_present:
            if not is_gateway_authorized(request, configured_settings):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if not organization_header_present or not context_header_present:
                return corporate_error_response("corporate_context_invalid", 403)
            return await proxy_corporate_request(path, request)

        credentials = get_matching_credentials(request, configured_settings)
        if credentials is None:
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
            )
        _, openrouter_api_key = credentials
        return await proxy_upstream_request(
            request,
            f"{configured_settings.openrouter_base_url.rstrip('/')}/{path}",
            openrouter_api_key,
            "bearer",
            legacy=True,
        )

    @app.post("/internal/corporate-cache/purge")
    async def purge_corporate_cache(request: Request):
        expected_key = configured_settings.corporate_cache_purge_key
        received_key = request.headers.get("x-internal-cache-key", "")
        if (
            not expected_key
            or not received_key
            or not secrets.compare_digest(received_key, expected_key)
        ):
            return JSONResponse({"error": "Unauthorized"}, status_code=401)

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "Request body must be valid JSON"}, status_code=400)

        if not isinstance(payload, dict) or not isinstance(payload.get("organization_id"), str):
            return JSONResponse({"error": "organization_id is required"}, status_code=400)
        operation = payload.get("operation")
        if operation is not None and operation not in {
            "chat_completions",
            "image_generation",
            "audio_speech",
            "audio_transcription",
        }:
            return JSONResponse({"error": "Unknown operation"}, status_code=400)

        cache: EncryptedRouteCache | None = request.app.state.corporate_cache
        if cache is None:
            return JSONResponse({"error": "Corporate cache is not configured"}, status_code=503)
        try:
            await cache.purge(payload["organization_id"], operation)
        except CorporateError:
            return corporate_error_response("corporate_upstream_unavailable", 503)
        return {"status": "purged"}

    @app.post("/telegram/sendMessage")
    async def telegram_send_message(request: Request):
        if get_matching_credentials(request, configured_settings) is None:
            return JSONResponse(
                {"error": "Unauthorized"},
                status_code=401,
            )

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

        # Chat ID is controlled by the proxy environment.
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
                    "Accept-Encoding": "identity",
                    "Content-Type": "application/json",
                },
            )

        except httpx.HTTPError as exc:
            return JSONResponse(
                {
                    "error": "Telegram upstream request failed",
                    "detail": str(exc),
                },
                status_code=502,
            )

        return Response(
            content=telegram_response.content,
            status_code=telegram_response.status_code,
            headers=build_response_headers(telegram_response.headers),
            media_type=telegram_response.headers.get("content-type"),
        )

    return app


async def proxy_corporate_request(path: str, request: Request):
    organization_id = request.headers.get("x-organization-id", "").strip()
    context = request.headers.get("x-organization-context", "")
    operation = operation_for(request.method, path)
    request_id = str(uuid.uuid4())

    if not organization_id:
        return corporate_error_response("corporate_context_invalid", 403, request_id)
    if operation is None:
        return corporate_error_response("operation_not_configured", 404, request_id)

    try:
        verifier: JwksVerifier = request.app.state.jwks_verifier
        await verifier.validate(context, organization_id)
        resolver: CorporateRouteResolver = request.app.state.corporate_resolver
        route = await resolver.resolve(organization_id, operation)
    except CorporateError as exc:
        status_code = {
            "corporate_context_invalid": 403,
            "organization_mismatch": 403,
            "operation_not_configured": 404,
            "corporate_upstream_unavailable": 503,
        }.get(exc.code, 403)
        return corporate_error_response(
            exc.code,
            status_code,
            request_id,
        )

    return await proxy_upstream_request(
        request,
        f"{route.base_url}/{path.strip('/')}",
        route.api_key,
        route.auth_scheme,
        request_id=request_id,
    )


async def proxy_upstream_request(
    request: Request,
    upstream_url: str,
    api_key: str,
    auth_scheme: str,
    *,
    legacy: bool = False,
    request_id: str | None = None,
):
    client: httpx.AsyncClient = request.app.state.http_client

    try:
        upstream_request = client.build_request(
            method=request.method,
            url=upstream_url,
            params=request.query_params,
            headers=build_upstream_headers(request, api_key, auth_scheme),
            content=await request.body(),
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        if legacy:
            return JSONResponse(
                {"error": "Upstream OpenRouter request failed"},
                status_code=502,
            )
        return corporate_error_response(
            "corporate_upstream_failed", 502, request_id
        )

    response_headers = build_response_headers(upstream_response.headers)
    media_type = upstream_response.headers.get("content-type")
    if media_type and media_type.lower().split(";", 1)[0] == "text/event-stream":
        return StreamingResponse(
            upstream_response.aiter_raw(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=media_type,
            background=BackgroundTask(upstream_response.aclose),
        )

    try:
        content = await upstream_response.aread()
    except httpx.HTTPError:
        if legacy:
            return JSONResponse(
                {"error": "Upstream OpenRouter request failed"},
                status_code=502,
            )
        return corporate_error_response(
            "corporate_upstream_failed", 502, request_id
        )
    finally:
        await upstream_response.aclose()
    return Response(
        content=content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=media_type,
    )


def corporate_error_response(
    code: str,
    status_code: int,
    request_id: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": code,
            "request_id": request_id or str(uuid.uuid4()),
        },
        status_code=status_code,
    )


def build_config_ssl_context(settings: Settings) -> ssl.SSLContext:
    context = ssl.create_default_context(
        cafile=settings.corporate_config_ca_file or None
    )
    if (
        settings.corporate_config_client_cert_file
        and settings.corporate_config_client_key_file
    ):
        context.load_cert_chain(
            certfile=settings.corporate_config_client_cert_file,
            keyfile=settings.corporate_config_client_key_file,
        )
    return context


def get_gateway_key(request: Request) -> str:
    # Check custom headers first
    key = (
        request.headers.get("x-gateway-key")
        or request.headers.get("x-gateway-secret")
        or ""
    )
    if key:
        return key
    # Check Authorization: Bearer <token>
    auth = request.headers.get("Authorization")
    if auth:
        parts = auth.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
    return ""


def is_authorized(request: Request, expected_key: str) -> bool:
    received_key = get_gateway_key(request)

    return (
        bool(received_key)
        and bool(expected_key)
        and secrets.compare_digest(received_key, expected_key)
    )


def is_gateway_authorized(request: Request, settings: Settings) -> bool:
    received_key = get_gateway_key(request)
    if not received_key:
        return False
    return any(
        gateway_key and secrets.compare_digest(received_key, gateway_key)
        for gateway_key in (settings.ai_gateway_key, settings.ai_gateway_key_2)
    )


def get_matching_credentials(
    request: Request,
    settings: Settings,
) -> Optional[Tuple[str, str]]:
    received_key = get_gateway_key(request)

    if not received_key:
        return None

    credential_pairs = (
        (settings.ai_gateway_key, settings.openrouter_api_key),
        (settings.ai_gateway_key_2, settings.openrouter_api_key_2),
    )

    for gateway_key, openrouter_api_key in credential_pairs:
        if (
            gateway_key
            and openrouter_api_key
            and secrets.compare_digest(received_key, gateway_key)
        ):
            return gateway_key, openrouter_api_key

    return None


def build_upstream_headers(
    request: Request,
    api_key: str,
    auth_scheme: str = "bearer",
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

    if auth_scheme == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif auth_scheme == "api_key":
        headers["api-key"] = api_key
    else:
        raise ValueError("Unsupported upstream authorization scheme")

    # Do not allow OpenRouter/Cloudflare compression to create
    # Content-Encoding mismatches after httpx processes the response.
    headers["Accept-Encoding"] = "identity"

    return headers


def build_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
    }


app = create_app()
