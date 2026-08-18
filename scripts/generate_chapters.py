"""Bulk-generate chapter audio offline.

Runs on a developer machine, not the VPS: sustained throughput measured ~6.2x
realtime on a 15W laptop against 0.61x on the deployed 1-CPU container, so a
full translation is roughly ten hours here versus four days there. (A short
burst hits 16.8x, but the chip throttles under sustained load; size plans on the
sustained figure.)

Output lands in the exact cache layout the API serves from:

    {out}/{language}/{translation}/{voice}@{revision}/{book}/{chapter}.m4a

Resumable: existing files are skipped, so an interrupted run continues where it
stopped. Nothing here talks to the API or the VPS; upload is a separate step.

    python scripts/generate_chapters.py --voice voices/en/en_US-lessac-medium.onnx
    python scripts/generate_chapters.py --voice ... --books john,psalms --workers 4
"""

from __future__ import annotations

import argparse
import io
import json
import multiprocessing as mp
import os
import math
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# Reused so offline output is chunked and joined identically to the API's own
# fallback path -- otherwise pre-generated and on-demand audio would differ.
from app.services.audio import VERSE_GAP_MS, chunk_verses  # noqa: E402
from scripts.detect_noise import decode as _decode_f32, noisy_spans  # noqa: E402

SR = 22050

_voice = None
_syn_config = None


def _init_worker(voice_path: str, length_scale: float, ort_threads: int, speaker: int | None) -> None:
    """Load the model once per process rather than once per chapter.

    ort_threads caps onnxruntime's intra-op threads per worker. Measured on a
    2P+8E laptop, capping at 1 was slower than leaving it free, so the default
    is 0 (leave alone); the flag exists because the best value is hardware
    dependent. Must be set before onnxruntime is imported, which happens on the
    piper import below.
    """
    if ort_threads > 0:
        for var in ("OMP_NUM_THREADS", "ORT_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
            os.environ[var] = str(ort_threads)

    global _voice, _syn_config
    from piper import PiperVoice

    from piper import SynthesisConfig

    _voice = PiperVoice.load(voice_path)
    # normalize_audio=False is essential. Piper otherwise scales EVERY call to
    # full scale, so concatenated chunks all sit at 0 dBFS; encoding that to AAC
    # makes the decoder overshoot (measured up to +11 dBFS) and every 16-bit
    # player hard-clips it. Levels are set once per chapter instead, below.
    _syn_config = SynthesisConfig(
        length_scale=length_scale if length_scale != 1.0 else None,
        speaker_id=speaker,
        normalize_audio=False,
    )


def _synth(text: str) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        if _syn_config is not None:
            _voice.synthesize_wav(text, wf, syn_config=_syn_config)
        else:
            _voice.synthesize_wav(text, wf)
    return buf.getvalue()


PEAK_DBFS = -3.0        # headroom; the AAC encoder overshoots on transients
TAIL_SILENCE_S = 0.120  # appended before the fade, so speech is never clipped
FADE_OUT_S = 0.080
FADE_IN_S = 0.010


def _level_and_tail(pcm: np.ndarray, peak_dbfs: float) -> np.ndarray:
    """Normalise once across the whole chapter, then end it cleanly.

    Chapters frequently end mid-word, so silence is appended first and the fade
    only ever touches that silence.
    """
    peak = float(np.abs(pcm).max())
    if peak > 0:
        pcm = pcm * (10 ** (peak_dbfs / 20) * 32767.0 / peak)
    pcm = np.concatenate([pcm, np.zeros(int(SR * TAIL_SILENCE_S))])
    fo, fi = int(SR * FADE_OUT_S), int(SR * FADE_IN_S)
    pcm[-fo:] *= np.linspace(1.0, 0.0, fo)
    pcm[:fi] *= np.linspace(0.0, 1.0, fi)
    return pcm


def _encoded_peak(path: Path) -> float:
    """True peak of the encoded file, decoded as float.

    An int16 decode would clip the very overshoot we are checking for.
    """
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True,
    )
    a = np.frombuffer(r.stdout, dtype=np.float32)
    return float(np.abs(a).max()) if a.size else 0.0


def _to_wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.clip(pcm, -32768, 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def _transcode(wav_bytes: bytes, dest: Path, bitrate: str) -> None:
    """WAV -> AAC in fMP4, written atomically so a crash cannot leave a partial
    file that later looks like a cache hit."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "wav", "-i", "pipe:0",
            "-c:a", "aac", "-b:a", bitrate, "-ac", "1",
            "-movflags", "+faststart",
            "-f", "mp4", str(tmp),
        ],
        input=wav_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:300]}")
    tmp.replace(dest)


def _validate(dest: Path, expected_s: float) -> str | None:
    """Return a reason string when the encoded chapter is unacceptable.

    Guards the two failures seen in production: byte-misaligned concatenation
    producing broadband noise, and encoding at 0 dBFS producing clipping on
    playback.
    """
    x = _decode_f32(dest)
    if x.size == 0:
        return "undecodable"

    dur = len(x) / SR
    if abs(dur - expected_s) > 1.0:
        return f"duration {dur:.1f}s != expected {expected_s:.1f}s"

    peak = float(np.abs(x).max())
    if peak > 1.0:
        return f"peak {peak:.3f} above full scale"

    _, noisy_s, _ = noisy_spans(x)
    if noisy_s > 0.5:
        return f"{noisy_s:.1f}s of noise detected"
    return None


def _render(task: tuple) -> tuple:
    """Render one chapter. Returns (book, chapter, audio_s, wall_s, error)."""
    book, chapter, verses, dest_str, bitrate = task
    dest = Path(dest_str)
    started = time.perf_counter()
    try:
        chunks = chunk_verses(verses)
        if not chunks:
            return (book, chapter, 0.0, 0.0, "no text")

        gap = np.zeros(int(SR * VERSE_GAP_MS / 1000))  # samples, not bytes
        parts: list[np.ndarray] = []
        for c in chunks:
            with wave.open(io.BytesIO(_synth(c))) as w:
                seg = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64)
            if parts:
                parts.append(gap)
            parts.append(seg)
        pcm = np.concatenate(parts)
        audio_s = len(pcm) / SR

        # The encoder overshoots by a content-dependent amount, so verify the
        # result and back off until it genuinely fits under full scale.
        target = PEAK_DBFS
        for _ in range(4):
            _transcode(_to_wav(_level_and_tail(pcm.copy(), target)), dest, bitrate)
            peak = _encoded_peak(dest)
            if peak <= 1.0:
                break
            target -= 20 * math.log10(peak) + 1.0

        problem = _validate(dest, audio_s + TAIL_SILENCE_S)
        if problem:
            dest.unlink(missing_ok=True)
            return (book, chapter, 0.0, time.perf_counter() - started, f"validation: {problem}")
        return (book, chapter, audio_s, time.perf_counter() - started, None)
    except Exception as exc:  # noqa: BLE001 - one bad chapter must not kill the run
        return (book, chapter, 0.0, time.perf_counter() - started, str(exc)[:200])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--voice", required=True, help="path to a .onnx voice model")
    ap.add_argument("--voice-name", default=None,
                    help="name used in the cache path; defaults to the model filename stem")
    ap.add_argument("--bible", default=str(ROOT / "data/bible/en/kjv.json"))
    ap.add_argument("--out", default=str(ROOT / "audio"))
    ap.add_argument("--language", default="en")
    ap.add_argument("--translation", default=None, help="defaults to the value in the bible file")
    ap.add_argument("--revision", default="v1", help="must match VOICE_REVISION on the server")
    ap.add_argument("--length-scale", type=float, default=1.0, help=">1.0 slows narration")
    ap.add_argument("--bitrate", default="48k")
    ap.add_argument("--peak-dbfs", type=float, default=PEAK_DBFS,
                    help="chapter peak before encoding; lowered automatically if the encoder overshoots")
    ap.add_argument("--books", default=None, help="comma-separated slugs; default all")
    ap.add_argument("--chapters", default=None,
                    help="comma-separated chapter numbers, applied within --books; default all")
    ap.add_argument("--limit", type=int, default=None, help="stop after N chapters (for testing)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--force", action="store_true", help="re-render chapters that already exist")
    ap.add_argument("--ort-threads", type=int, default=0,
                    help="cap onnxruntime threads per worker; 0 leaves it to onnxruntime")
    ap.add_argument("--speaker", type=int, default=None,
                    help="speaker id for multi-speaker voices; becomes part of the cache path")
    args = ap.parse_args()

    voice_path = Path(args.voice).resolve()
    if not voice_path.is_file():
        print(f"voice model not found: {voice_path}", file=sys.stderr)
        return 1

    import shutil
    if not shutil.which("ffmpeg"):
        print("ffmpeg not on PATH. Install it (winget install Gyan.FFmpeg) and reopen the shell.",
              file=sys.stderr)
        return 1

    data = json.loads(Path(args.bible).read_text(encoding="utf-8"))
    translation = args.translation or data.get("translation", "unknown")
    voice_name = args.voice_name or voice_path.stem
    if args.speaker is not None and not args.voice_name:
        # Different speakers are different voices, so they must not share a
        # cache path or one would silently serve the other's audio.
        voice_name = f"{voice_name}-s{args.speaker}"
    out_root = Path(args.out) / args.language / translation / f"{voice_name}@{args.revision}"

    wanted = {b.strip() for b in args.books.split(",")} if args.books else None
    wanted_ch = {int(c) for c in args.chapters.split(",")} if args.chapters else None

    tasks, skipped = [], 0
    for slug, book in data["books"].items():
        if wanted and slug not in wanted:
            continue
        for chapter_no, verses in sorted(book["chapters"].items(), key=lambda kv: int(kv[0])):
            if wanted_ch and int(chapter_no) not in wanted_ch:
                continue
            dest = out_root / slug / f"{chapter_no}.m4a"
            if dest.is_file() and dest.stat().st_size > 0 and not args.force:
                skipped += 1
                continue
            tasks.append((slug, int(chapter_no), verses, str(dest), args.bitrate))

    tasks.sort(key=lambda t: (t[0], t[1]))
    if args.limit:
        tasks = tasks[: args.limit]

    print(f"voice      {voice_name}  (length_scale={args.length_scale}, speaker={args.speaker})")
    print(f"output     {out_root}")
    print(f"chapters   {len(tasks)} to render, {skipped} already present")
    print(f"workers    {args.workers} (ort_threads={args.ort_threads or 'auto'})")
    if not tasks:
        print("nothing to do")
        return 0

    started = time.perf_counter()
    done = failed = 0
    total_audio = 0.0
    errors: list[str] = []

    with mp.Pool(
        processes=args.workers,
        initializer=_init_worker,
        initargs=(str(voice_path), args.length_scale, args.ort_threads, args.speaker),
    ) as pool:
        for book, chapter, audio_s, wall_s, err in pool.imap_unordered(_render, tasks, chunksize=1):
            done += 1
            if err:
                failed += 1
                errors.append(f"{book} {chapter}: {err}")
                print(f"  FAIL {book} {chapter}: {err}")
                continue
            total_audio += audio_s
            elapsed = time.perf_counter() - started
            rate = done / elapsed
            remaining = (len(tasks) - done) / rate if rate else 0
            print(
                f"  [{done}/{len(tasks)}] {book} {chapter}  "
                f"{audio_s / 60:.1f}min audio in {wall_s:.1f}s  "
                f"| {total_audio / elapsed:.1f}x realtime  "
                f"| eta {remaining / 60:.0f}min",
                flush=True,
            )

    elapsed = time.perf_counter() - started
    print(f"\nrendered {done - failed}/{len(tasks)} chapters in {elapsed / 60:.1f} min")
    print(f"total audio {total_audio / 3600:.1f} h  ({total_audio / elapsed:.1f}x realtime aggregate)")
    if failed:
        print(f"\n{failed} failed:")
        for e in errors[:20]:
            print(f"  {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
