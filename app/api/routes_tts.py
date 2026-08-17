"""Arbitrary-text synthesis.

Not the chapter path. This exists for short passages: a single verse, a search
result, a UI string. It is capped tightly because the engine runs on a 1-CPU
container behind a proxy that times out at 100 seconds.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.config import get_settings
from app.schemas import TTSRequest
from app.security import rate_limit, require_api_key
from app.services.generate import BusyError, synthesize_text, voice_for_language
from app.services.piper import PiperError

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["tts"])


@router.post("/tts")
async def tts(
    payload: TTSRequest,
    request: Request,
    api_key: str = Depends(require_api_key),
) -> Response:
    await rate_limit(request, api_key)
    s = get_settings()

    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")
    if len(text) > s.max_tts_chars:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"text exceeds {s.max_tts_chars} characters. "
                "Use the chapter endpoint for long passages."
            ),
        )

    if payload.voice:
        voice = payload.voice
    else:
        language = payload.language or s.default_language
        try:
            voice = voice_for_language(language)
        except LookupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        wav = await synthesize_text(text, voice, payload.length_scale)
    except BusyError as exc:
        # Refusing beats queueing: the engine serializes work, so a queued
        # request would sit until the proxy times it out anyway.
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="synthesis engine busy, retry shortly",
            headers={"Retry-After": "10"},
        ) from exc
    except PiperError as exc:
        log.error("synthesis failed: %s", exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"Cache-Control": "no-store", "Content-Disposition": 'inline; filename="speech.wav"'},
    )
