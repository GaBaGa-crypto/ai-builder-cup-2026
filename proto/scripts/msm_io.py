"""Shared readers for the MSM raw layout (<raw>/<machine>/datasetN/...).

Used by inventory.py (step 1) and build_db.py (step 2). Read-only: never writes inside raw.
"""
import csv
import glob
import os
import re
import wave

import numpy as np
import openpyxl
import pandas as pd


def read_summary(raw):
    """dataset_summary.xlsx -> {(machine, dataset): {workpiece, tool_summary, remark}}."""
    ws = openpyxl.load_workbook(os.path.join(raw, "dataset_summary.xlsx"), read_only=True).active
    summary = {}
    for r in list(ws.iter_rows(values_only=True))[1:]:
        if r[0]:
            summary[(str(r[0]).strip(), int(r[1]))] = dict(
                workpiece=r[2], tool_summary=str(r[3]).strip() if r[3] else "", remark=(str(r[4]).strip() if r[4] else ""))
    return summary


def iter_sessions(raw):
    """Yield (machine, dataset_number, session_dir) in machine order, then numeric dataset order."""
    for mdir in sorted(d for d in glob.glob(os.path.join(raw, "*")) if os.path.isdir(d)):
        machine = os.path.basename(mdir)
        for sdir in sorted(glob.glob(os.path.join(mdir, "dataset*")), key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0)):
            yield machine, int(re.sub(r"\D", "", os.path.basename(sdir))), sdir


def find(sdir, suffix):
    """First file in sdir ending with suffix (e.g. '_s0.wav'), or None."""
    return next((os.path.join(sdir, f) for f in sorted(os.listdir(sdir)) if f.endswith(suffix)), None)


def read_labels(sdir):
    """label.csv rows as dicts (empty list if absent)."""
    p = os.path.join(sdir, "label.csv")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def read_mtc(sdir):
    """mtc.csv as a DataFrame (None if absent); numeric columns coerced, text columns kept."""
    p = find(sdir, "_mtc.csv")
    if p is None:
        return None
    return pd.read_csv(p, encoding="utf-8-sig", low_memory=False)


def wav_info(path):
    w = wave.open(path)
    return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()


def read_wav_int16(path):
    """(samples as int16 array, sample rate)."""
    w = wave.open(path)
    sr, n = w.getframerate(), w.getnframes()
    return np.frombuffer(w.readframes(n), dtype=np.int16), sr


def clip_ratio(path, segs):
    """Fraction of int16 samples at full scale inside each (start, end) segment."""
    x, sr = read_wav_int16(path)
    return clip_ratio_array(x, sr, segs)


def clip_ratio_array(x, sr, segs):
    out = []
    for s, e in segs:
        y = x[int(s * sr):int(e * sr)]
        out.append(float(np.mean(np.abs(y.astype(np.int32)) >= 32767)) if len(y) else float("nan"))
    return out
