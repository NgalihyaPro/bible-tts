"""Chapter audio: the endpoint the mobile app actually calls."""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.ranged import ranged_file_response
from app.schemas import AudioStatusResponse, JobStatus
from app.security import rate_limit, require_api_key
from app.services.bible import BibleNotFound, InvalidReference, bible_repo
from app.services.cache import CacheKey, audio_cache
from app.services.generate import generate_chapter, load_chapter, voice_for
from app.services.jobs import job_registry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/bible", tags=["bible"])


def _resolve(language: str, translation: str | None, book: str, chapter: int) -> tuple[CacheKey, str]:
    s = get_settings()

    # Translation is resolved first: the voice may be chosen per translation.
    trans = translation or s.default_translations.get(language)
    if not trans:
        available = bible_repo.translations(language)
        if not available:
            raise HTTPException(status_code=404, detail=f"no translations available for {language!r}")
        trans = available[0]

    try:
        voice = voice_for(language, trans)
    except LookupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        key = audio_cache.key(language, trans, book, chapter, voice)
    except (InvalidReference, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return key, trans


def _status_payload(key: CacheKey, job_status: JobStatus, **extra) -> AudioStatusResponse:
    return AudioStatusResponse(
        status=job_status,
        language=key.language,
        translation=key.translation,
        book=key.book,
        chapter=key.chapter,
        voice=key.voice,
        **extra,
    )


@router.get("/audio/status/{language}/{book}/{chapter}", response_model=AudioStatusResponse)
async def audio_status(
    language: str,
    book: str,
    chapter: int,
    request: Request,
    translation: str | None = Query(default=None),
    api_key: str = Depends(require_api_key),
) -> AudioStatusResponse:
    """Let the client decide between showing a play button and a spinner."""
    await rate_limit(request, api_key)
    key, _ = _resolve(language, translation, book, chapter)

    if audio_cache.exists(key):
        return _status_payload(
            key,
            JobStatus.READY,
            url=f"/api/v1/bible/audio/{key.language}/{key.book}/{key.chapter}?translation={key.translation}",
            size_bytes=audio_cache.size(key),
        )

    job = await job_registry.get(key.as_str())
    if job is None:
        # Never requested. Not an error: the client should request the audio
        # endpoint, which starts generation.
        return _status_payload(key, JobStatus.FAILED, detail="not generated yet")
    return _status_payload(key, job.status, detail=job.error)


@router.get("/audio/{language}/{book}/{chapter}")
async def chapter_audio(
    language: str,
    book: str,
    chapter: int,
    request: Request,
    background: BackgroundTasks,
    translation: str | None = Query(default=None),
    api_key: str = Depends(require_api_key),
) -> Response:
    """Serve cached chapter audio, or start generating it.

    Cache hit is the normal case: chapters are pre-generated offline, so this is
    a file read. On a miss, generation starts in the background and the caller
    gets 202 immediately -- synthesis takes minutes on the deployed engine, far
    past the proxy's 100s timeout, so blocking here would only ever produce 524s.
    """
    await rate_limit(request, api_key)
    key, _ = _resolve(language, translation, book, chapter)

    if audio_cache.exists(key):
        return ranged_file_response(audio_cache.path(key), request)

    job_key = key.as_str()
    job, is_owner = await job_registry.claim(job_key)

    if not is_owner:
        # Someone else is already generating this exact chapter. Deduplicated:
        # the herd waits on one job rather than starting several.
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=_status_payload(key, JobStatus.GENERATING).model_dump(),
            headers={"Retry-After": "15"},
        )

    try:
        chapter_obj = await load_chapter(key.language, key.translation, key.book, key.chapter)
    except BibleNotFound as exc:
        await job_registry.forget(job_key)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidReference as exc:
        await job_registry.forget(job_key)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    background.add_task(generate_chapter, key, chapter_obj)
    log.info("queued generation for %s (%d verses)", job_key, len(chapter_obj.verses))

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=_status_payload(key, JobStatus.GENERATING, detail="generation started").model_dump(),
        headers={"Retry-After": "15"},
    )


@router.get("/text/{language}/{book}/{chapter}")
async def chapter_text(
    language: str,
    book: str,
    chapter: int,
    request: Request,
    translation: str | None = Query(default=None),
    api_key: str = Depends(require_api_key),
) -> dict:
    """The verses behind a chapter, useful for debugging a bad rendering."""
    await rate_limit(request, api_key)
    key, trans = _resolve(language, translation, book, chapter)
    try:
        ch = await load_chapter(key.language, trans, key.book, key.chapter)
    except BibleNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return {
        "language": ch.language,
        "translation": ch.translation,
        "book": ch.book,
        "book_name": ch.book_name,
        "chapter": ch.number,
        "verse_count": len(ch.verses),
        "verses": [{"number": v.number, "text": v.text} for v in ch.verses],
    }
