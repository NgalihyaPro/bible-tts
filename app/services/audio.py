"""Text chunking, WAV assembly and transcoding."""

import array
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

# Chapters are levelled once, with headroom. Piper normalises every synthesis
# call to full scale, so concatenated chunks otherwise sit at 0 dBFS; encoding
# that to AAC makes the decoder's reconstruction overshoot (measured up to
# +11 dBFS on real chapters) and every 16-bit player hard-clips it.
PEAK_DBFS = -3.0
# Appended before the fade: chapters often end mid-word, so fading the original
# tail would clip the final syllable.
TAIL_SILENCE_MS = 120
FADE_OUT_MS = 80
FADE_IN_MS = 10


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


def concat_wavs(
    wav_blobs: list[bytes],
    gap_ms: int = VERSE_GAP_MS,
    peak_dbfs: float | None = PEAK_DBFS,
) -> bytes:
    """Concatenate WAV blobs, inserting silence between them.

    All blobs must share a format, which holds because they come from one voice.

    Levels the result once and ends it on a fade. Pass peak_dbfs=None to skip
    that and return the raw concatenation.
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
    # Compute the gap in FRAMES, then convert to bytes. Deriving the byte count
    # directly (framerate * channels * sampwidth * gap_ms / 1000) gives 15435
    # for 22050Hz mono 16-bit at 350ms -- odd, so not a whole frame. That
    # shifted every following sample by one byte, pairing the low byte of one
    # sample with the high byte of the next, which sounds like broadband static.
    # It corrupted 95% of a generated corpus before being found.
    frame_bytes = params.nchannels * params.sampwidth
    gap_frames = int(params.framerate * gap_ms / 1000)
    silence = bytes(gap_frames * frame_bytes)
    assert len(silence) % frame_bytes == 0, "gap must be a whole number of frames"
    joined = silence.join(frames)

    if peak_dbfs is not None and params.sampwidth == 2:
        joined = _level_and_tail(joined, params.framerate, peak_dbfs)

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(joined)
    return out.getvalue()


def _level_and_tail(frames: bytes, framerate: int, peak_dbfs: float) -> bytes:
    """Scale to a single peak target, then append silence and fade out."""
    samples = array.array("h")
    samples.frombytes(frames)
    if not samples:
        return frames

    peak = max(abs(min(samples)), abs(max(samples)))
    if peak:
        scale = (10 ** (peak_dbfs / 20)) * 32767.0 / peak
        samples = array.array("h", (int(max(-32768, min(32767, s * scale))) for s in samples))

    samples.extend([0] * int(framerate * TAIL_SILENCE_MS / 1000))

    fade_out = int(framerate * FADE_OUT_MS / 1000)
    n = len(samples)
    for i in range(fade_out):
        samples[n - fade_out + i] = int(samples[n - fade_out + i] * (1.0 - i / fade_out))
    fade_in = int(framerate * FADE_IN_MS / 1000)
    for i in range(fade_in):
        samples[i] = int(samples[i] * (i / fade_in))

    return samples.tobytes()


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
