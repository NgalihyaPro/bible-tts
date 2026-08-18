"""Repair chapter audio that was encoded at 0 dBFS.

Piper normalises every synthesis call so its peak sits at full scale. Encoding
that to AAC makes the decoder's reconstruction overshoot -- measured up to
+8.8 dBFS -- and any player decoding to 16-bit hard-clips the result. The audio
itself is fine: Piper's WAV had 7 saturated samples in 895,488. The distortion
appears only at playback.

The encoded file still contains the overshoot, so a float decode recovers it
intact and no re-synthesis is needed. This lowers the gain until the true peak
fits under the rail, then ends the file cleanly.

Silence is appended BEFORE fading because chapters often end mid-speech; fading
the last 80ms of the original would clip the final word.

    python scripts/repair_audio.py --src audio/sw/... --dst audio_repaired/sw/...
"""

from __future__ import annotations

import argparse
import io
import multiprocessing as mp
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

SR = 22050
TARGET_DBFS = -2.0      # headroom so the re-encode cannot overshoot the rail
TAIL_SILENCE_S = 0.120  # appended, so the fade never touches speech
FADE_OUT_S = 0.080
FADE_IN_S = 0.010


def _ffmpeg() -> str:
    from shutil import which
    exe = which("ffmpeg")
    if exe:
        return exe
    raise SystemExit("ffmpeg not on PATH")


def decode_float(path: Path, ffmpeg: str) -> np.ndarray:
    """Decode to float32. Critical: an int16 decode would clip the overshoot
    we are trying to measure and remove."""
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[:200])
    return np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64)


def repair_one(job: tuple) -> tuple:
    src, dst, bitrate, ffmpeg = job
    src, dst = Path(src), Path(dst)
    try:
        x = decode_float(src, ffmpeg)
        if x.size == 0:
            return (str(src), None, None, "empty decode")

        peak = float(np.abs(x).max())
        if peak <= 0:
            return (str(src), None, None, "silent file")

        gain_db = TARGET_DBFS - 20 * np.log10(peak)
        y = x * (10 ** (gain_db / 20))

        y = np.concatenate([y, np.zeros(int(SR * TAIL_SILENCE_S))])
        fo = int(SR * FADE_OUT_S)
        fi = int(SR * FADE_IN_S)
        y[-fo:] *= np.linspace(1.0, 0.0, fo)
        y[:fi] *= np.linspace(0.0, 1.0, fi)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(y, -1.0, 1.0) * 32767).astype(np.int16).tobytes())

        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        p = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "wav", "-i", "pipe:0",
             "-c:a", "aac", "-b:a", bitrate, "-ac", "1", "-movflags", "+faststart",
             "-f", "mp4", str(tmp)],
            input=buf.getvalue(), capture_output=True,
        )
        if p.returncode != 0:
            tmp.unlink(missing_ok=True)
            return (str(src), None, None, p.stderr.decode(errors="replace")[:200])
        tmp.replace(dst)
        return (str(src), peak, gain_db, None)
    except Exception as exc:  # noqa: BLE001
        return (str(src), None, None, str(exc)[:200])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--bitrate", default="48k")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    ffmpeg = _ffmpeg()
    src_root, dst_root = Path(args.src), Path(args.dst)
    files = sorted(src_root.rglob("*.m4a"))
    if not files:
        print(f"no .m4a under {src_root}", file=sys.stderr)
        return 1

    jobs = [(str(p), str(dst_root / p.relative_to(src_root)), args.bitrate, ffmpeg) for p in files]
    print(f"repairing {len(jobs)} files")
    print(f"  {src_root}  ->  {dst_root}")
    print(f"  target peak {TARGET_DBFS} dBFS, +{TAIL_SILENCE_S*1000:.0f}ms silence, {FADE_OUT_S*1000:.0f}ms fade")

    ok = fail = 0
    peaks = []
    errors = []
    with mp.Pool(args.workers) as pool:
        for i, (src, peak, gain, err) in enumerate(pool.imap_unordered(repair_one, jobs), 1):
            if err:
                fail += 1
                errors.append(f"{src}: {err}")
            else:
                ok += 1
                peaks.append(peak)
            if i % 100 == 0 or i == len(jobs):
                print(f"  [{i}/{len(jobs)}] ok={ok} fail={fail}", flush=True)

    print(f"\nrepaired {ok}, failed {fail}")
    if peaks:
        a = np.array(peaks)
        print(f"original true peaks: median {np.median(a):.3f} ({20*np.log10(np.median(a)):+.2f} dBFS), "
              f"max {a.max():.3f} ({20*np.log10(a.max()):+.2f} dBFS)")
        print(f"files that were above full scale: {(a > 1.0).sum()} / {len(a)}")
    for e in errors[:20]:
        print(f"  FAIL {e}")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
