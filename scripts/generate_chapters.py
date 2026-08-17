"""Bulk-generate chapter audio offline.

Runs on a developer machine, not the VPS: synthesis measured 16.8x realtime on a
laptop against 0.61x on the deployed 1-CPU container, so a full translation is a
few hours here versus days there.

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
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Reused so offline output is chunked and joined identically to the API's own
# fallback path -- otherwise pre-generated and on-demand audio would differ.
from app.services.audio import VERSE_GAP_MS, chunk_verses, concat_wavs  # noqa: E402

_voice = None
_syn_config = None


def _init_worker(voice_path: str, length_scale: float, ort_threads: int) -> None:
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

    _voice = PiperVoice.load(voice_path)
    if length_scale != 1.0:
        from piper import SynthesisConfig

        _syn_config = SynthesisConfig(length_scale=length_scale)


def _synth(text: str) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        if _syn_config is not None:
            _voice.synthesize_wav(text, wf, syn_config=_syn_config)
        else:
            _voice.synthesize_wav(text, wf)
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


def _render(task: tuple) -> tuple:
    """Render one chapter. Returns (book, chapter, audio_s, wall_s, error)."""
    book, chapter, verses, dest_str, bitrate = task
    dest = Path(dest_str)
    started = time.perf_counter()
    try:
        chunks = chunk_verses(verses)
        if not chunks:
            return (book, chapter, 0.0, 0.0, "no text")
        blobs = [_synth(c) for c in chunks]
        wav = concat_wavs(blobs, VERSE_GAP_MS)
        with wave.open(io.BytesIO(wav)) as w:
            audio_s = w.getnframes() / w.getframerate()
        _transcode(wav, dest, bitrate)
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
    ap.add_argument("--books", default=None, help="comma-separated slugs; default all")
    ap.add_argument("--chapters", default=None,
                    help="comma-separated chapter numbers, applied within --books; default all")
    ap.add_argument("--limit", type=int, default=None, help="stop after N chapters (for testing)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--force", action="store_true", help="re-render chapters that already exist")
    ap.add_argument("--ort-threads", type=int, default=0,
                    help="cap onnxruntime threads per worker; 0 leaves it to onnxruntime")
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

    print(f"voice      {voice_name}  (length_scale={args.length_scale})")
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
        initargs=(str(voice_path), args.length_scale, args.ort_threads),
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
