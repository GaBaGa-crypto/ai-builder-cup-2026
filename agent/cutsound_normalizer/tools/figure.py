"""render_session_figure: the viewer-style evidence figure (waveform + 4-band dB on the 0.1 s grid) as a PNG.

For the person who approves a proposal: the same picture the verification page shows, with the agent's
candidate times marked. The band computation is a copy of `band_ms` in proto/scripts/band_trends.py (Hann-windowed
4096-point FFT every 0.1 s, Parseval per band; bands 0-4k / 1-4k / 4-10k / 10-24k Hz). It is copied, not imported:
the agent package must not depend on proto/. Float input (soundfile, channel 0) replaces the int16 reader.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

import numpy as np
import soundfile as sf

from ._paths import redact_data_root, resolve_under_data_root

NFFT, HOP_S = 4096, 0.1
BANDS = [("0-4k", 0, 4000), ("1-4k", 1000, 4000), ("4-10k", 4000, 10000), ("10-24k", 10000, 24001)]
COLORS = {"0-4k": "#2b5c7a", "1-4k": "#7fb3d5", "4-10k": "#c47f1a", "10-24k": "#8a3b6e"}
MAX_SECONDS = 600.0          # longest window drawn in one figure

FIG_DIR: Optional[Path] = None   # set by the headless runner; default <NORMALIZER_WORK_DIR>/figures


def _fig_dir() -> Path:
    return FIG_DIR if FIG_DIR is not None else Path(os.environ.get("NORMALIZER_WORK_DIR", "work")) / "figures"


def band_ms(x: np.ndarray, sr: int):
    """Per-frame mean-square per band (copy of proto/scripts/band_trends.band_ms; x is float in [-1, 1])."""
    hop = int(sr * HOP_S)
    n = max(0, (len(x) - NFFT) // hop + 1)
    win = np.hanning(NFFT).astype(np.float32)
    f = np.fft.rfftfreq(NFFT, 1 / sr)
    wk = np.full(len(f), 2.0); wk[0] = 1.0; wk[-1] = 1.0  # one-sided spectrum: DC and Nyquist not doubled
    norm = NFFT * float(np.sum(win.astype(np.float64) ** 2))
    masks = {name: (f >= lo) & (f < hi) for name, lo, hi in BANDS}
    out = {name: np.empty(n) for name, *_ in BANDS}
    out["total"] = np.empty(n)
    xf = x.astype(np.float32)
    for a in range(0, n, 2000):
        b = min(n, a + 2000)
        idx = (np.arange(a, b) * hop)[:, None] + np.arange(NFFT)[None, :]
        p = np.abs(np.fft.rfft(xf[idx] * win, axis=1)) ** 2 * wk / norm
        for name, m in masks.items():
            out[name][a:b] = p[:, m].sum(axis=1)
        out["total"][a:b] = p.sum(axis=1)
    t = np.arange(n) * HOP_S + NFFT / 2 / sr
    return t, out


def _db(ms):
    return 10 * np.log10(np.asarray(ms, dtype=np.float64) + 1e-12)


def render_session_figure(wav_path: str, t0_s: Optional[float] = None, t1_s: Optional[float] = None,
                          marks_s: Optional[List[float]] = None, title: Optional[str] = None) -> dict:
    """Draw the verification-page figure for one WAV file or a window of it and save it as a PNG.

    Top: waveform (channel 0). Bottom: band energy in dB re full scale on the 0.1 s grid for 0-4k, 1-4k, 4-10k
    and 10-24k Hz (the viewer's four bands). Vertical lines mark candidate times. For human review only: the
    figure is not evidence by itself; cite tool numbers.

    Args:
        wav_path: Path to a WAV file.
        t0_s: Window start in file-local seconds (None = file start).
        t1_s: Window end in file-local seconds (None = file end; at most 600 s are drawn).
        marks_s: File-local times to mark (e.g. change-point candidates).
        title: Optional figure title.

    Returns:
        dict with status ('ok' or 'error'), png_path, t0_s, t1_s, duration_s, bands_hz, marks_s.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        safe_wav_path = resolve_under_data_root(wav_path)
        info = sf.info(str(safe_wav_path))
        sr, dur = int(info.samplerate), float(info.duration)
        a_s = 0.0 if t0_s is None else min(max(float(t0_s), 0.0), dur)
        b_s = dur if t1_s is None else min(max(float(t1_s), 0.0), dur)
        b_s = min(b_s, a_s + MAX_SECONDS)
        if b_s <= a_s:
            return {"status": "error", "error": f"empty window {a_s}-{b_s} s (file {dur:.3f} s)"}
        x, _ = sf.read(str(safe_wav_path), start=int(round(a_s * sr)), stop=int(round(b_s * sr)), dtype="float32",
                       always_2d=True)
        x = x[:, 0]
        tw = a_s + np.arange(len(x)) / sr
        t, ms = band_ms(x, sr)
        t = t + a_s
        marks = [float(m) for m in (marks_s or []) if a_s <= float(m) <= b_s]

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 5.2), sharex=True,
                                       gridspec_kw={"height_ratios": [1, 1.6]})
        step = max(1, len(x) // 20000)            # min/max decimation keeps peaks visible
        if step > 1:
            n = len(x) // step
            xr = x[: n * step].reshape(n, step)
            tt = tw[: n * step: step]
            ax1.fill_between(tt, xr.min(1), xr.max(1), color="0.35", lw=0)
        else:
            ax1.plot(tw, x, lw=0.4, color="0.35")
        ax1.set_ylabel("waveform\n(full scale)", fontsize=8)
        ax1.set_ylim(-1.05, 1.05)
        lo = []
        for name, *_ in BANDS:
            y = _db(ms[name]) if len(t) else np.array([])
            ax2.plot(t, y, lw=0.8, color=COLORS[name], label=f"{name} Hz")
            if len(y):
                lo.append(np.percentile(y, 1))
        if lo:
            ax2.set_ylim(min(lo) - 5, 2)
        ax2.set_ylabel("band energy\ndB re full scale (0.1 s)", fontsize=8)
        ax2.set_xlabel("time in file (s)")
        ax2.legend(loc="upper right", fontsize=8, ncol=4, frameon=False)
        ax2.grid(axis="y", color="0.92", lw=0.5)
        for m in marks:
            for ax in (ax1, ax2):
                ax.axvline(m, color="#c0392b", lw=1.0, ls="--")
            ax1.text(m, 1.0, f" {m:.2f} s", color="#c0392b", fontsize=8, va="top")
        ax2.set_xlim(a_s, b_s)
        fig.suptitle(title or f"{safe_wav_path.name}  {a_s:.2f}-{b_s:.2f} s", fontsize=9, x=0.01, ha="left")
        fig.tight_layout(rect=(0, 0, 1, 0.96))
        d = _fig_dir()
        d.mkdir(parents=True, exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", safe_wav_path.stem)
        png = d / f"{stem}_{a_s:.2f}-{b_s:.2f}s.png"
        fig.savefig(png, dpi=80)
        plt.close(fig)
        return {"status": "ok", "png_path": str(png), "t0_s": round(a_s, 3), "t1_s": round(b_s, 3),
                "duration_s": round(dur, 3), "bands_hz": [[lo_, hi_] for _, lo_, hi_ in BANDS], "marks_s": marks}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "tool": "render_session_figure",
                "error": redact_data_root(f"{type(e).__name__}: {e}")}
