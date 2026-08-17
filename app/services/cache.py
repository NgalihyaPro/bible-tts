"""Audio cache addressing and lookup.

The cache key deliberately includes every input that can change the audio:
language, translation, book, chapter, voice and a voice revision. Two
translations of John 3, or the same chapter re-rendered with a different voice,
can therefore never collide.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.services.bible import validate_chapter, validate_slug

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheKey:
    language: str
    translation: str
    book: str
    chapter: int
    voice: str
    revision: str

    def as_str(self) -> str:
        return f"{self.language}/{self.translation}/{self.voice}@{self.revision}/{self.book}/{self.chapter}"


class AudioCache:
    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or get_settings().audio_dir).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def key(
        self,
        language: str,
        translation: str,
        book: str,
        chapter: int,
        voice: str,
    ) -> CacheKey:
        return CacheKey(
            language=validate_slug(language, "language"),
            translation=validate_slug(translation, "translation"),
            book=validate_slug(book, "book"),
            chapter=validate_chapter(chapter),
            # Voice names contain underscores and dots, so they get their own
            # narrow validation rather than the slug pattern.
            voice=_validate_voice(voice),
            revision=get_settings().voice_revision,
        )

    def path(self, key: CacheKey) -> Path:
        s = get_settings()
        p = (
            self._root
            / key.language
            / key.translation
            / f"{key.voice}@{key.revision}"
            / key.book
            / f"{key.chapter}.{s.audio_format}"
        ).resolve()
        # Final containment check. Every component is validated above; this
        # guarantees the invariant regardless of future changes upstream.
        if not str(p).startswith(str(self._root)):
            raise ValueError("cache path escapes audio root")
        return p

    def exists(self, key: CacheKey) -> bool:
        p = self.path(key)
        return p.is_file() and p.stat().st_size > 0

    def size(self, key: CacheKey) -> int | None:
        p = self.path(key)
        return p.stat().st_size if p.is_file() else None

    def count(self) -> int:
        s = get_settings()
        if not self._root.is_dir():
            return 0
        return sum(1 for _ in self._root.rglob(f"*.{s.audio_format}"))


_VOICE_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _validate_voice(voice: str) -> str:
    v = voice.strip()
    if not v or len(v) > 64 or not set(v) <= _VOICE_ALLOWED:
        raise ValueError(f"invalid voice name: {voice!r}")
    return v


audio_cache = AudioCache()
