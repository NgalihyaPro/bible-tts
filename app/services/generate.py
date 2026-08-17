"""Chapter generation: text -> chunks -> engine -> concatenated audio -> cache.

This is the fallback path. In normal operation chapter audio is pre-generated
offline and this never runs, because on the deployed engine a single average
chapter takes minutes.
"""

import logging

from app.config import get_settings
from app.schemas import JobStatus
from app.services.audio import chunk_verses, concat_wavs, transcode, wav_duration
from app.services.bible import Chapter, bible_repo
from app.services.cache import CacheKey, audio_cache
from app.services.jobs import get_synthesis_semaphore, job_registry
from app.services.piper import piper_client

log = logging.getLogger(__name__)


def voice_for_language(language: str) -> str:
    s = get_settings()
    voice = s.voices.get(language)
    if not voice:
        raise LookupError(f"no voice configured for language {language!r}")
    return voice


async def synthesize_text(text: str, voice: str, length_scale: float | None = None) -> bytes:
    """Synthesize a single short passage, bounded by the engine semaphore."""
    s = get_settings()
    sem = get_synthesis_semaphore(s.max_concurrent_synthesis)
    if sem.locked():
        raise BusyError("engine busy")
    async with sem:
        return await piper_client.synthesize(text, voice, length_scale or s.length_scale)


class BusyError(RuntimeError):
    pass


async def generate_chapter(key: CacheKey, chapter: Chapter) -> None:
    """Render a chapter into the cache. Intended to run as a background task.

    Chunks on verse boundaries so the narration never breaks mid-sentence, then
    concatenates with a short pause between verses so it reads continuously.
    """
    s = get_settings()
    job_key = key.as_str()
    sem = get_synthesis_semaphore(s.max_concurrent_synthesis)

    try:
        chunks = chunk_verses([v.text for v in chapter.verses])
        if not chunks:
            raise ValueError("chapter has no text")

        log.info("generating %s: %d verses in %d chunks", job_key, len(chapter.verses), len(chunks))

        blobs: list[bytes] = []
        async with sem:
            for i, chunk in enumerate(chunks, 1):
                blobs.append(await piper_client.synthesize(chunk, key.voice, s.length_scale))
                log.debug("%s chunk %d/%d done", job_key, i, len(chunks))

        wav = concat_wavs(blobs)
        dest = audio_cache.path(key)
        await transcode(wav, dest, s.audio_bitrate)

        log.info(
            "generated %s: %.1fs audio, %d bytes",
            job_key,
            wav_duration(wav),
            dest.stat().st_size,
        )
        await job_registry.finish(job_key, JobStatus.READY)

    except Exception as exc:  # noqa: BLE001 - background task must record failure
        log.exception("generation failed for %s", job_key)
        await job_registry.finish(job_key, JobStatus.FAILED, str(exc)[:300])


async def load_chapter(language: str, translation: str, book: str, chapter: int) -> Chapter:
    return bible_repo.get_chapter(language, translation, book, chapter)
