"""Client for the Piper engine's HTTP API (internal network only)."""

import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class PiperError(RuntimeError):
    pass


class PiperClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        s = get_settings()
        self._base = (base_url or s.piper_url).rstrip("/")
        self._timeout = timeout or s.piper_timeout_s
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(base_url=self._base, timeout=self._timeout)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise PiperError("Piper client not started")
        return self._client

    async def voices(self) -> list[str]:
        resp = await self.client.get("/voices")
        resp.raise_for_status()
        return list(resp.json().keys())

    async def is_alive(self) -> bool:
        try:
            await self.voices()
            return True
        except Exception as exc:  # noqa: BLE001 - health must never raise
            log.warning("piper health probe failed: %s", exc)
            return False

    async def synthesize(
        self,
        text: str,
        voice: str,
        length_scale: float | None = None,
    ) -> bytes:
        """Synthesize one chunk of text, returning WAV bytes.

        Note the engine loads a voice on first use, so the first call for a given
        voice is markedly slower than subsequent ones.
        """
        payload: dict[str, object] = {"text": text, "voice": voice}
        if length_scale is not None:
            payload["length_scale"] = length_scale

        try:
            resp = await self.client.post("/synthesize", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise PiperError(f"piper returned {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.HTTPError as exc:
            raise PiperError(f"piper unreachable at {self._base}: {exc}") from exc

        if not resp.content.startswith(b"RIFF"):
            raise PiperError("piper did not return WAV data")
        return resp.content


piper_client = PiperClient()
