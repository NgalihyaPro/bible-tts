"""Text chunking, WAV assembly and transcoding."""

import asyncio
import io
import logging
import shutil
import wave
from pathlib import Path

log = logging.getLogger(__name__)

# Chunks are built from whole verses, so a chunk boundary is always a sentence
# boundary and the narration never breaks mid-clause. Sized to keep any single
# engine call well inside its timeout.
MAX_CHUNK_CHARS = 800
# Brief pause between verses; without it the reading runs together.
VERSE_GAP_MS = 350


def chunk_verses(verse_texts: list[str], max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Group verses into chunks without ever splitting one."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0

    for text in verse_texts:
        text = text.strip()
        if not text:
            continue
        # A single oversized verse becomes its own chunk rather than being cut.
        if current and length + len(text) + 1 > max_chars:
            chunks.append(" ".join(current))
            current, length = [], 0
        current.append(text)
        length += len(text) + 1

    if current:
        chunks.append(" ".join(current))
    return chunks


def concat_wavs(wav_blobs: list[bytes], gap_ms: int = VERSE_GAP_MS) -> bytes:
    """Concatenate WAV blobs, inserting silence between them.

    All blobs must share a format, which holds because they come from one voice.
    """
    if not wav_blobs:
        raise ValueError("no audio to concatenate")

    frames: list[bytes] = []
    params = None

    for blob in wav_blobs:
        with wave.open(io.BytesIO(blob)) as w:
            if params is None:
                params = w.getparams()
            elif (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (
                params.nchannels,
                params.sampwidth,
                params.framerate,
            ):
                raise ValueError("cannot concatenate WAVs with differing formats")
            frames.append(w.readframes(w.getnframes()))

    assert params is not None
    silence = b"\x00" * int(params.framerate * params.nchannels * params.sampwidth * gap_ms / 1000)
    joined = silence.join(frames)

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(joined)
    return out.getvalue()


def wav_duration(blob: bytes) -> float:
    with wave.open(io.BytesIO(blob)) as w:
        return w.getnframes() / w.getframerate()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def transcode(wav_bytes: bytes, dest: Path, bitrate: str = "48k") -> None:
    """WAV -> AAC in fMP4, written atomically.

    AAC-LC decodes in hardware on every Android version worth supporting and
    seeks cleanly over HTTP range requests. faststart moves the moov atom to the
    front so a player can begin without fetching the whole file.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "wav", "-i", "pipe:0",
        "-c:a", "aac", "-b:a", bitrate, "-ac", "1",
        "-movflags", "+faststart",
        "-f", "mp4", str(tmp),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate(wav_bytes)

    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace')[:400]}")

    # Atomic replace: a reader never observes a partially written file.
    tmp.replace(dest)
