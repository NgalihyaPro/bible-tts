"""Liveness and readiness."""

import logging

from fastapi import APIRouter

from app.config import get_settings
from app.schemas import HealthResponse
from app.services.audio import ffmpeg_available
from app.services.cache import audio_cache
from app.services.generate import voice_for
from app.services.piper import piper_client

log = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Unauthenticated: this is what the container healthcheck and uptime probes hit.

    Reports degraded rather than failing when the engine is down, because chapter
    playback is served from cache and keeps working without it.
    """
    s = get_settings()
    default_voice = s.voices.get(s.default_language, "unknown")

    voices: list[str] = []
    piper_ok = False
    try:
        voices = await piper_client.voices()
        piper_ok = True
    except Exception as exc:  # noqa: BLE001
        log.warning("health: piper unreachable: %s", exc)

    problems = []
    if not piper_ok:
        problems.append("piper unreachable")
    if not ffmpeg_available():
        problems.append("ffmpeg missing")
    if piper_ok and default_voice not in voices:
        problems.append(f"default voice {default_voice} not loaded")

    return HealthResponse(
        status="ok" if not problems else "degraded",
        piper=piper_ok,
        default_voice=default_voice,
        voices_loaded=voices,
        audio_cached=audio_cache.count(),
        detail="; ".join(problems) or None,
    )


@router.get("/api/v1/voices")
async def voices() -> dict:
    """Configured language -> voice mapping, and what the engine actually has."""
    s = get_settings()
    try:
        loaded = await piper_client.voices()
    except Exception:  # noqa: BLE001
        loaded = []
    return {
        "configured": s.voices,
        "default_language": s.default_language,
        "default_voice": voice_for(s.default_language),
        "loaded": loaded,
        "revision": s.voice_revision,
    }
