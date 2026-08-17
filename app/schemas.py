"""Request and response models."""

from enum import Enum

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    READY = "READY"
    GENERATING = "GENERATING"
    FAILED = "FAILED"


class TTSRequest(BaseModel):
    text: str = Field(min_length=1)
    language: str | None = None
    # Explicit piper voice name, overriding the language mapping.
    voice: str | None = None
    length_scale: float | None = Field(default=None, ge=0.5, le=2.0)


class HealthResponse(BaseModel):
    status: str
    piper: bool
    default_voice: str
    voices_loaded: list[str] = []
    audio_cached: int | None = None
    detail: str | None = None


class AudioStatusResponse(BaseModel):
    status: JobStatus
    language: str
    translation: str
    book: str
    chapter: int
    voice: str
    url: str | None = None
    size_bytes: int | None = None
    duration_s: float | None = None
    detail: str | None = None
