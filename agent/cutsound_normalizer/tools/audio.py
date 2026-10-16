"""Audio normalization to the MSM s0 format: 48 kHz, 16-bit PCM, mono. Level is preserved by default."""
from __future__ import annotations

import hashlib
import math
import shutil
from fractions import Fraction
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

TARGET_SR = 48000
FULL_SCALE = 0.999  # |x| at or above this (float, -1..1) counts as clipped


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _db(x: float) -> float:
    return round(20 * math.log10(x), 2) if x > 0 else -math.inf


def normalize_audio(src: str, dst: str, channel: str = "mean", gain: str = "none") -> dict:
    """Convert one audio file to MSM s0 format and report what changed.

    Args:
        src: Source audio path (any format libsndfile reads).
        dst: Output WAV path.
        channel: 'mean' | 'first' | 'second' when the source has more than one channel.
        gain: 'none' keeps level; 'peak' scales the peak to -1 dBFS.

    Returns:
        dict with native format, conversions applied, clipping and level statistics, output sha256.
    """
    src_p, dst_p = Path(src), Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    info = sf.info(str(src_p))
    native = {"native_sample_rate": info.samplerate, "native_channels": info.channels,
              "native_subtype": info.subtype, "duration_s": round(info.duration, 6)}

    # Exact copy when already compliant and no gain change: guarantees bit identity.
    if (info.samplerate == TARGET_SR and info.channels == 1 and info.subtype == "PCM_16"
            and gain == "none" and src_p.suffix.lower() == ".wav"):
        shutil.copyfile(src_p, dst_p)
        x, _ = sf.read(str(src_p), dtype="int16")
        xf = x.astype(np.float64) / 32768.0
        clip = float(np.mean(np.abs(x.astype(np.int32)) >= 32767)) if len(x) else 0.0
        return {**native, "conversions": ["copy (already 48k/16-bit/mono)"], "bandlimited": False,
                "channel_rule": "n/a", "gain_rule": gain, "clip_ratio_native": round(clip, 6),
                "peak_dbfs": _db(float(np.max(np.abs(xf))) if len(xf) else 0.0),
                "rms_dbfs": _db(float(np.sqrt(np.mean(xf ** 2))) if len(xf) else 0.0),
                "int16_clipped_by_conversion": 0, "sha256": _sha256(dst_p)}

    x, sr = sf.read(str(src_p), dtype="float64", always_2d=True)
    conversions = []
    clip_native = float(np.mean(np.any(np.abs(x) >= FULL_SCALE, axis=1))) if len(x) else 0.0
    channel_corr = None
    if x.shape[1] > 1:
        if x.shape[1] == 2 and len(x) > 1 and np.std(x[:, 0]) > 0 and np.std(x[:, 1]) > 0:
            channel_corr = round(float(np.corrcoef(x[:, 0], x[:, 1])[0, 1]), 4)
        if channel == "first":
            y = x[:, 0]
        elif channel == "second":
            y = x[:, 1]
        else:
            y = x.mean(axis=1)
        conversions.append(f"{x.shape[1]}ch -> mono ({channel})")
    else:
        y = x[:, 0]

    bandlimited = False
    if sr != TARGET_SR:
        frac = Fraction(TARGET_SR, sr).limit_denominator(1000)
        y = resample_poly(y, frac.numerator, frac.denominator)
        conversions.append(f"resample {sr} -> {TARGET_SR} Hz (polyphase {frac.numerator}/{frac.denominator})")
        bandlimited = sr < TARGET_SR

    if gain == "peak":
        pk = float(np.max(np.abs(y))) if len(y) else 0.0
        if pk > 0:
            y = y * (10 ** (-1 / 20) / pk)
            conversions.append("peak normalize to -1 dBFS")

    if info.subtype != "PCM_16":
        conversions.append(f"{info.subtype} -> PCM_16")
    q = np.round(y * 32768.0)
    clipped_by_conv = int(np.sum((q > 32767) | (q < -32768)))
    q = np.clip(q, -32768, 32767).astype(np.int16)
    sf.write(str(dst_p), q, TARGET_SR, subtype="PCM_16")
    yf = q.astype(np.float64) / 32768.0
    return {**native, "conversions": conversions, "bandlimited": bandlimited, "channel_rule": channel,
            "channel_corr": channel_corr, "gain_rule": gain, "clip_ratio_native": round(clip_native, 6),
            "peak_dbfs": _db(float(np.max(np.abs(yf))) if len(yf) else 0.0),
            "rms_dbfs": _db(float(np.sqrt(np.mean(yf ** 2))) if len(yf) else 0.0),
            "int16_clipped_by_conversion": clipped_by_conv, "sha256": _sha256(dst_p)}


def segment_stats(wav_path: str, segments: list) -> list:
    """Clip ratio, RMS and peak (dBFS) for each (start_s, end_s) segment of a normalized s0 file."""
    x, sr = sf.read(str(wav_path), dtype="int16")
    out = []
    for s, e in segments:
        seg = x[int(max(0.0, s) * sr):int(max(0.0, e) * sr)]
        if len(seg) == 0:
            out.append({"clip_ratio": None, "rms_dbfs": None, "peak_dbfs": None})
            continue
        f = seg.astype(np.float64) / 32768.0
        out.append({"clip_ratio": round(float(np.mean(np.abs(seg.astype(np.int32)) >= 32767)), 6),
                    "rms_dbfs": _db(float(np.sqrt(np.mean(f ** 2)))), "peak_dbfs": _db(float(np.max(np.abs(f))))})
    return out
