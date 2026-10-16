"""Shared readers for the MSM raw layout (<raw>/<machine>/datasetN/...).

Used by inventory.py (step 1) and build_db.py (step 2). Read-only: never writes inside raw.
"""
import csv
import glob
import json
import os
import re
import wave

import numpy as np
import openpyxl
import pandas as pd


def read_summary(raw):
    """dataset_summary.xlsx -> {(machine, dataset): {workpiece, tool_summary, remark}}; {} if the root has none (normalized roots)."""
    p = os.path.join(raw, "dataset_summary.xlsx")
    if not os.path.exists(p):
        return {}
    ws = openpyxl.load_workbook(p, read_only=True).active
    summary = {}
    for r in list(ws.iter_rows(values_only=True))[1:]:
        if r[0]:
            summary[(str(r[0]).strip(), int(r[1]))] = dict(
                workpiece=r[2], tool_summary=str(r[3]).strip() if r[3] else "", remark=(str(r[4]).strip() if r[4] else ""))
    return summary


DATASET_DIR_RE = re.compile(r"dataset([0-9]+)")


def iter_sessions(raw):
    """Yield (machine, dataset_number, session_dir) in machine order, then numeric dataset order.
    Only folders named exactly dataset<N> count (a normalized root also holds _manifest/dataset_index.csv)."""
    for mdir in sorted(d for d in glob.glob(os.path.join(raw, "*")) if os.path.isdir(d)):
        machine = os.path.basename(mdir)
        found = [(int(m.group(1)), p) for p in glob.glob(os.path.join(mdir, "dataset*"))
                 for m in [DATASET_DIR_RE.fullmatch(os.path.basename(p))] if m and os.path.isdir(p)]
        for n, sdir in sorted(found):
            yield machine, n, sdir


def read_session_json(sdir):
    """session.json written by the normalizer (machine_short, machine_name, domain, coolant, mic_type, ...), or None if absent."""
    p = os.path.join(sdir, "session.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            meta = json.load(fh)
    except ValueError as e:
        raise ValueError(f"{p}: not valid JSON ({e})") from None
    if not isinstance(meta, dict):
        raise ValueError(f"{p}: must hold a JSON object, got {type(meta).__name__}")
    return meta


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


def read_unlabeled(sdir):
    """<session_id>_unlabeled.csv rows (start, end, cut_state) written by the normalizer for spans with label -1 whose
    cut_state is cutting or unknown; empty list if absent (MSM raw never has it). build_db.check_unlabeled validates them."""
    p = find(sdir, "_unlabeled.csv")
    if p is None:
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
