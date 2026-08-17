"""Bible text lookup.

Backed by JSON files today. The repository interface is what the API depends on,
so moving to Postgres (tables: translations, books, chapters, verses) means
adding an implementation here and nothing else.
"""

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import get_settings

log = logging.getLogger(__name__)

# Book slugs and translation ids are used to build filesystem paths, so they are
# whitelisted by pattern first and then checked against the loaded data. Nothing
# from the request is ever interpolated into a path unvalidated.
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
MAX_CHAPTER = 200


@dataclass(frozen=True)
class Verse:
    number: int
    text: str


@dataclass(frozen=True)
class Chapter:
    translation: str
    language: str
    book: str
    book_name: str
    number: int
    verses: list[Verse]

    @property
    def text(self) -> str:
        return " ".join(v.text.strip() for v in self.verses)


class BibleNotFound(LookupError):
    pass


class InvalidReference(ValueError):
    pass


def validate_slug(value: str, label: str) -> str:
    v = value.strip().lower()
    if not SLUG_RE.match(v):
        raise InvalidReference(f"invalid {label}: {value!r}")
    return v


def validate_chapter(value: int) -> int:
    if not 1 <= value <= MAX_CHAPTER:
        raise InvalidReference(f"chapter out of range: {value}")
    return value


class JsonBibleRepository:
    """Reads `{bible_data_dir}/{language}/{translation}.json`.

    Format:
        {
          "translation": "kjv",
          "language": "en",
          "name": "King James Version",
          "books": {
            "john": {"name": "John", "chapters": {"3": ["verse 1 text", ...]}}
          }
        }
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or get_settings().bible_data_dir

    @lru_cache(maxsize=32)  # noqa: B019 - repository is a process-lifetime singleton
    def _load(self, language: str, translation: str) -> dict:
        path = self._root / language / f"{translation}.json"
        # Resolve and confirm containment: belt and braces against traversal even
        # though both components are slug-validated.
        resolved = path.resolve()
        if not str(resolved).startswith(str(self._root.resolve())):
            raise InvalidReference("path escapes data directory")
        if not resolved.is_file():
            raise BibleNotFound(f"translation {translation!r} not available for {language!r}")
        return json.loads(resolved.read_text(encoding="utf-8"))

    def translations(self, language: str) -> list[str]:
        d = self._root / validate_slug(language, "language")
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

    def get_chapter(self, language: str, translation: str, book: str, chapter: int) -> Chapter:
        language = validate_slug(language, "language")
        translation = validate_slug(translation, "translation")
        book = validate_slug(book, "book")
        chapter = validate_chapter(chapter)

        data = self._load(language, translation)
        books = data.get("books", {})
        if book not in books:
            raise BibleNotFound(f"unknown book {book!r} in {translation}")

        entry = books[book]
        verses_raw = entry.get("chapters", {}).get(str(chapter))
        if not verses_raw:
            raise BibleNotFound(f"{book} {chapter} not found in {translation}")

        return Chapter(
            translation=translation,
            language=language,
            book=book,
            book_name=entry.get("name", book.title()),
            number=chapter,
            verses=[Verse(number=i + 1, text=t) for i, t in enumerate(verses_raw)],
        )


bible_repo = JsonBibleRepository()
