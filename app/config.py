"""Configuration, loaded from the environment (see .env.example)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Substrings that indicate an unreplaced placeholder rather than a real secret.
_PLACEHOLDER_MARKERS = ("set api_keys", "changeme", "change-me", "your-key", "yourkey", "example")
_MIN_KEY_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- service ---
    log_level: str = "INFO"

    # Comma-separated lists are kept as plain strings and split via the
    # properties below. pydantic-settings JSON-decodes list-typed fields before
    # any validator runs, so a bare `a,b` in the environment would raise.
    cors_origins: str = ""

    # --- auth ---
    # Required. Startup aborts if this is missing or looks like a placeholder,
    # so the service can never come up unauthenticated on a public domain.
    api_keys: str = ""
    # Escape hatch for local development and tests only. Never set in production.
    allow_insecure_no_auth: bool = False

    # --- piper engine ---
    # Reached over the internal Docker network; never exposed publicly.
    piper_url: str = "http://piper:5000"
    piper_timeout_s: float = 300.0

    # --- voices ---
    # Explicit voice selection: "{language}:{translation}" wins over "{language}".
    # Nothing is inferred from the text, so adding a language or giving one
    # translation its own narrator is a config change, not a code change.
    # Set as JSON in the environment, e.g.
    #   VOICES={"en":"en_US-libritts-high","en:kjv":"en_US-lessac-medium"}
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
        default_factory=lambda: {"en": "kjv", "sw": "suv"}
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

    def assert_auth_configured(self) -> None:
        """Abort startup unless real API keys are present.

        Compose's `${API_KEYS:?...}` guard is not enough: Coolify parses that
        syntax and uses the error message as a *default value*, so the service
        would happily start with the placeholder text as its key -- a key that
        is published in a public repository. Enforcement has to live here.
        """
        if self.allow_insecure_no_auth:
            return

        keys = self.api_key_list
        if not keys:
            raise RuntimeError(
                "API_KEYS is empty. Set it to one or more secret keys "
                "(openssl rand -hex 32), or set ALLOW_INSECURE_NO_AUTH=true for "
                "local development only."
            )

        for key in keys:
            lowered = key.lower()
            if any(marker in lowered for marker in _PLACEHOLDER_MARKERS):
                raise RuntimeError(
                    "API_KEYS still contains placeholder text. Replace it with a "
                    "generated secret (openssl rand -hex 32)."
                )
            if len(key) < _MIN_KEY_LENGTH:
                raise RuntimeError(
                    f"API_KEYS contains a key shorter than {_MIN_KEY_LENGTH} "
                    "characters. Use a generated secret (openssl rand -hex 32)."
                )


@lru_cache
def get_settings() -> Settings:
    return Settings()
