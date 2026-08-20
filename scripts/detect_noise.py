"""Detect spans where synthesis produced noise instead of speech.

Discriminator: fraction of frame energy above 6.8 kHz. This voice puts ~96% of
its energy below 1.3 kHz and essentially none above 6.8 kHz, while the corrupt
spans spread energy evenly to 11 kHz. The separation is roughly 0% against 35%,
so the threshold is not delicate.

Spectral flatness was tried first and proved unreliable -- it also flags quiet
frames and codec noise-fill. Band energy does not.

Those percentages are for sw_CD-lanfrica-medium and do not transfer to every
voice. en_US-kristin-medium is brighter, and its sibilants reach 54-75% of frame
energy above 6.8 kHz for as long as 0.47s -- indistinguishable from corruption
frame by frame. Duration is what separates them, so callers validating a bright
voice should raise min_span_s rather than trust the default.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

SR = 22050
N = 1024
HOP = 1024
HF_HZ = 6800
HF_BIN = int(HF_HZ / (SR / N))
HF_FRACTION_THRESHOLD = 0.10   # clean ~0.00, corrupt ~0.35
SILENCE_RMS = 0.005


def decode(path: Path, ss: float | None = None, dur: float | None = None) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error"]
    if ss is not None:
        cmd += ["-ss", str(ss), "-t", str(dur)]
    cmd += ["-i", str(path), "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True).stdout, dtype=np.float32).astype(np.float32)


def hf_profile(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return (hf_fraction, rms) per frame."""
    nf = (len(x) - N) // HOP
    if nf < 1:
        return np.zeros(0), np.zeros(0)
    idx = np.arange(N)[None, :] + HOP * np.arange(nf)[:, None]
    fr = x[idx] * np.hanning(N).astype(np.float32)
    spec = np.abs(np.fft.rfft(fr, axis=1)) ** 2
    total = spec[:, 1:].sum(axis=1) + 1e-20
    hf = spec[:, HF_BIN:].sum(axis=1)
    rms = np.sqrt(np.mean(fr.astype(np.float64) ** 2, axis=1))
    return hf / total, rms


def noisy_spans(x: np.ndarray, min_span_s: float = 0.35) -> tuple[list[tuple[float, float]], float, int]:
    """Contiguous spans of noise. Returns (spans, noisy_seconds, audible_frames)."""
    hf, rms = hf_profile(x)
    if hf.size == 0:
        return [], 0.0, 0
    audible = rms > SILENCE_RMS
    noisy = (hf > HF_FRACTION_THRESHOLD) & audible

    spans: list[tuple[float, float]] = []
    i = 0
    while i < len(noisy):
        if noisy[i]:
            j = i
            while j + 1 < len(noisy) and noisy[j + 1]:
                j += 1
            t0, t1 = i * HOP / SR, (j * HOP + N) / SR
            if t1 - t0 >= min_span_s:
                spans.append((round(t0, 2), round(t1, 2)))
            i = j + 1
        else:
            i += 1
    return spans, float(sum(b - a for a, b in spans)), int(audible.sum())
