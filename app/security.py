"""API key authentication and in-process rate limiting."""

import secrets
import time
from collections import defaultdict, deque

from fastapi import Header, HTTPException, Request, status

from app.config import get_settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    """Validate the X-API-Key header.

    compare_digest keeps the comparison constant-time, so a caller cannot probe
    for a valid key by measuring how long a rejection takes.
    """
    settings = get_settings()
    if not settings.auth_enabled:
        return "anonymous"

    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-API-Key header",
        )

    for known in settings.api_key_list:
        if secrets.compare_digest(x_api_key, known):
            return x_api_key

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


class SlidingWindowLimiter:
    """Fixed-capacity sliding window, keyed per caller.

    In-process and therefore per-container: with a single API replica that is
    exact, and with several it becomes approximate. Swap in Redis before scaling
    the API horizontally.
    """

    def __init__(self, limit: int, window_s: int) -> None:
        self._limit = limit
        self._window = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        hits = self._hits[key]
        cutoff = now - self._window
        while hits and hits[0] < cutoff:
            hits.popleft()

        if len(hits) >= self._limit:
            retry_after = max(1, int(hits[0] + self._window - now))
            return False, retry_after

        hits.append(now)
        return True, 0


_limiter: SlidingWindowLimiter | None = None


def get_limiter() -> SlidingWindowLimiter:
    global _limiter
    if _limiter is None:
        s = get_settings()
        _limiter = SlidingWindowLimiter(s.rate_limit_requests, s.rate_limit_window_s)
    return _limiter


async def rate_limit(request: Request, api_key: str = "anonymous") -> None:
    """Rate limit by API key, falling back to client IP.

    Behind Cloudflare and Coolify's proxy the socket peer is a proxy, so prefer
    the forwarded client address when present.
    """
    key = api_key
    if key == "anonymous":
        forwarded = request.headers.get("x-forwarded-for", "")
        key = forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")

    allowed, retry_after = get_limiter().check(key)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )
