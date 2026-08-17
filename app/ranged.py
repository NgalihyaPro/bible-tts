"""HTTP range-request support for audio files.

Implemented explicitly rather than relying on the framework, because seeking is
a core feature of the player: Media3/ExoPlayer issues a Range request to jump to
a position, and a server that answers 200 with the whole body instead of 206
forces a full re-download on every seek.
"""

import re
from pathlib import Path

from fastapi import HTTPException, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
CHUNK = 64 * 1024

MEDIA_TYPES = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}


def media_type_for(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _iter_range(path: Path, start: int, end: int):
    with path.open("rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            data = f.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def ranged_file_response(path: Path, request: Request, cache_seconds: int = 86400) -> Response:
    """Serve a file, honouring a single Range header.

    Multipart ranges are not supported; no audio player requests them.
    """
    if not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="audio not found")

    size = path.stat().st_size
    media_type = media_type_for(path)
    # Chapter audio is immutable for a given cache key, so it is safe to cache
    # aggressively: a new voice produces a new key, not a new body at this URL.
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": f"public, max-age={cache_seconds}, immutable",
    }

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=media_type, headers=headers)

    match = RANGE_RE.match(range_header.strip())
    if not match:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{size}"},
            detail="malformed Range header",
        )

    start_s, end_s = match.groups()
    if start_s:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    elif end_s:
        # Suffix form: "bytes=-500" means the final 500 bytes.
        start = max(0, size - int(end_s))
        end = size - 1
    else:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{size}"},
            detail="malformed Range header",
        )

    end = min(end, size - 1)
    if start > end or start >= size:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{size}"},
            detail="range not satisfiable",
        )

    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        }
    )
    return StreamingResponse(
        _iter_range(path, start, end),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers=headers,
    )
