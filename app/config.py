"""Configuration, loaded from the environment (see .env.example)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- service ---
    log_level: str = "INFO"

    # Comma-separated lists are kept as plain strings and split via the
    # properties below. pydantic-settings JSON-decodes list-typed fields before
    # any validator runs, so a bare `a,b` in the environment would raise.
    cors_origins: str = ""

    # --- auth ---
    # Empty disables auth, which is only ever acceptable for local development.
    api_keys: str = ""

    # --- piper engine ---
    # Reached over the internal Docker network; never exposed publicly.
    piper_url: str = "http://piper:5000"
    piper_timeout_s: float = 300.0

    # --- voices ---
    # language -> piper voice name. Adding a language is a config change, not a
    # code change.
    voices: dict[str, str] = Field(
        default_factory=lambda: {
            "en": "en_US-lessac-medium",
            "sw": "sw_CD-lanfrica-medium",
        }
    )
    default_language: str = "en"
    # language -> translation used when a request omits ?translation=. Keeping
    # this explicit avoids silently narrating the wrong translation.
    default_translations: dict[str, str] = Field(
        default_factory=lambda: {"en": "kjv", "sw": "swh"}
    )
    # Bumping this invalidates every cached file without deleting anything, so a
    # voice change can never silently serve stale audio.
    voice_revision: str = "v1"
    # >1.0 slows narration. Piper defaults to ~224 wpm, faster than the 150-160
    # wpm typical of audiobooks.
    length_scale: float = 1.0

    # --- limits ---
    # ~600 chars is about 33s of work on the deployed 1-CPU engine, the practical
    # ceiling before Cloudflare's 100s proxy timeout gets uncomfortably close.
    max_tts_chars: int = 600
    max_request_bytes: int = 16 * 1024
    # The engine is single-threaded Flask on a 1-CPU cap: concurrent synthesis
    # serializes, so queueing would just produce timeouts. Refuse instead.
    max_concurrent_synthesis: int = 1
    rate_limit_requests: int = 60
    rate_limit_window_s: int = 60

    # --- storage ---
    audio_dir: Path = Path("/data/audio")
    bible_data_dir: Path = Path("/app/data/bible")
    # Delivered format. AAC in fMP4 decodes in hardware on every Android version
    # worth supporting and seeks cleanly over HTTP range requests.
    audio_format: str = "m4a"
    audio_bitrate: str = "48k"

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def api_key_list(self) -> list[str]:
        return self._split_csv(self.api_keys)

    @property
    def cors_origin_list(self) -> list[str]:
        return self._split_csv(self.cors_origins)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key_list)


@lru_cache
def get_settings() -> Settings:
    return Settings()
