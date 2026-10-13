from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

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
    "content-encoding",
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


def create_app(settings: Settings | None = None) -> FastAPI:
    configured_settings = settings or get_settings()

    app = FastAPI(
        title="OpenRouter Proxy",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.state.settings = configured_settings

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_request(path: str, request: Request):
        upstream_url = f"{configured_settings.openrouter_base_url.rstrip('/')}/{path}"
        if request.query_params:
            upstream_url += f"?{request.query_params}"

        client: httpx.AsyncClient = request.app.state.http_client

        headers = {}
        for name, value in request.headers.items():
            if name.lower() not in HOP_BY_HOP_HEADERS:
                headers[name] = value

        headers["Host"] = "openrouter.ai"

        try:
            upstream_request = client.build_request(
                method=request.method,
                url=upstream_url,
                headers=headers,
                content=await request.body(),
            )
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            return Response(
                content=f"Upstream request failed: {exc}",
                status_code=502,
            )

        response_headers = {
            name: value
            for name, value in upstream_response.headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
        }

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
        except httpx.HTTPError as exc:
            return Response(
                content=f"Upstream read failed: {exc}",
                status_code=502,
            )
        finally:
            await upstream_response.aclose()

        return Response(
            content=content,
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=media_type,
        )

    return app


app = create_app()