"""Find chapters containing synthesised noise instead of speech.

During bulk generation some chapters came out with seconds-long spans of
broadband noise where speech should be. Re-synthesising the same text produces
clean audio, so the cause is a transient failure at generation time, not the
text or the model.

Speech is strongly tonal, so its spectral flatness sits near 0.0001; the corrupt
spans measure around 0.5, close to white noise. That separation is wide enough
to detect reliably.

    python scripts/scan_corruption.py --root audio_repaired/sw/... --workers 8
"""

from __future__ import annotations

import argparse
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

SR = 22050
N = 1024
HOP = 2048          # coarse: corrupt spans last seconds, not milliseconds
FLATNESS_THRESHOLD = 0.12
LOUD_THRESHOLD = 0.02


def scan(path: Path) -> dict:
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
        capture_output=True,
    )
    x = np.frombuffer(r.stdout, dtype=np.float32).astype(np.float32)
    if x.size < N * 4:
        return {"file": str(path), "error": "too short or undecodable"}

    n_frames = (len(x) - N) // HOP
    idx = np.arange(N)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = x[idx] * np.hanning(N).astype(np.float32)

    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2 + 1e-12
    spec = spec[:, 1:]
    flatness = np.exp(np.mean(np.log(spec), axis=1)) / np.mean(spec, axis=1)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))

    loud = rms > LOUD_THRESHOLD
    if loud.sum() == 0:
        return {"file": str(path), "noise_frac": 0.0, "noise_seconds": 0.0, "duration": len(x) / SR}

    noisy = (flatness > FLATNESS_THRESHOLD) & loud
    return {
        "file": str(path),
        "duration": len(x) / SR,
        "noise_frac": float(noisy.sum() / loud.sum()),
        "noise_seconds": float(noisy.sum() * HOP / SR),
        "median_flatness": float(np.median(flatness[loud])),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--threshold", type=float, default=0.05,
                    help="flag a chapter when this fraction of loud frames is noise-like")
    ap.add_argument("--out", default=None, help="write the flagged list as JSON")
    args = ap.parse_args()

    files = sorted(Path(args.root).rglob("*.m4a"))
    print(f"scanning {len(files)} files")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        results = list(ex.map(scan, files))

    errors = [r for r in results if "error" in r]
    ok = [r for r in results if "error" not in r]
    flagged = sorted((r for r in ok if r["noise_frac"] >= args.threshold),
                     key=lambda r: -r["noise_frac"])

    total_noise = sum(r["noise_seconds"] for r in ok)
    print(f"\n  clean            : {len(ok) - len(flagged)}")
    print(f"  FLAGGED corrupt  : {len(flagged)}  ({len(flagged)/max(len(ok),1)*100:.1f}%)")
    print(f"  undecodable      : {len(errors)}")
    print(f"  total noise audio: {total_noise/60:.1f} min")

    if flagged:
        print(f"\n  worst 25:")
        for r in flagged[:25]:
            p = Path(r["file"])
            print(f"    {p.parent.name}/{p.stem:<5} {r['noise_frac']*100:5.1f}% noise  "
                  f"{r['noise_seconds']:6.1f}s of {r['duration']:6.1f}s")

    if args.out:
        Path(args.out).write_text(json.dumps(flagged, indent=2), encoding="utf-8")
        print(f"\n  wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
