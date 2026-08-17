"""Bible TTS API."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import routes_bible, routes_health, routes_tts
from app.config import get_settings
from app.services.audio import ffmpeg_available
from app.services.cache import audio_cache
from app.services.piper import piper_client

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    logging.basicConfig(
        level=getattr(logging, s.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    audio_cache.root.mkdir(parents=True, exist_ok=True)
    await piper_client.start()

    log.info("piper=%s audio_dir=%s ffmpeg=%s", s.piper_url, audio_cache.root, ffmpeg_available())
    if not s.auth_enabled:
        log.warning("API_KEYS is empty - authentication is DISABLED")
    if not ffmpeg_available():
        log.warning("ffmpeg not found - chapter generation will fail")

    try:
        yield
    finally:
        await piper_client.close()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="Bible TTS API",
        version="0.1.0",
        description="Chapter narration for the Bible app, backed by a self-hosted Piper engine.",
        lifespan=lifespan,
    )

    if s.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=s.cors_origin_list,
            allow_methods=["GET", "POST"],
            allow_headers=["X-API-Key", "Content-Type", "Range"],
            # Media3 needs these to seek when a web client is involved.
            expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
        )

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        """Reject oversized bodies before they are read into memory."""
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > s.max_request_bytes:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"detail": f"request body exceeds {s.max_request_bytes} bytes"},
            )
        return await call_next(request)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        # Log the detail, return a generic message: internals must not leak to
        # a public client.
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal error"})

    app.include_router(routes_health.router)
    app.include_router(routes_tts.router)
    app.include_router(routes_bible.router)
    return app


app = create_app()
