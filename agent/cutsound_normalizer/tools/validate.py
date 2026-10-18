"""validate_normalized: check a converted source against the MSM spec. Returns errors and warnings, never fixes."""
from __future__ import annotations

import csv
import json
import math
import os
import re
from pathlib import Path

import soundfile as sf

from ..proposals import work_dir
from ._paths import resolve_under_roots
from .convert import (LABEL_COLS, MACHINE_SHORT_RE, SOURCE_DATASET_RE, UNLABELED_COLS, UNLABELED_CUT_STATES,
                       UNLABELED_SUFFIX)

LEAK_FIELDS = {"program_name", "block_number", "tool_number", "part_count", "remark", "nc_program_text", "Program_Name_Running"}
DATASET_RE = re.compile(r"dataset([1-9][0-9]*)")
SOURCE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
NEW_KEYS = ("machine_short", "machine_name", "domain", "coolant", "mic_type", "workpiece", "remark")


def validate_normalized(out_dir: str, source_id: str, tolerance_s: float = 0.05) -> dict:
    """Validate every session folder of one converted source.

    Args:
        out_dir: Root output folder used by convert_source.
        source_id: Source to validate.
        tolerance_s: Allowed overshoot of label end time beyond audio duration.

    Returns:
        dict with n_sessions, n_errors, errors (first 50), n_warnings, warnings (first 50), checks performed.
    """
    if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
        return {"error": "invalid source_id: must match ^[a-z0-9_-]{1,64}$"}
    try:
        safe_out_dir = resolve_under_roots(
            out_dir, (work_dir(), os.environ.get("NORMALIZER_DATA_ROOT", "")))
    except ValueError as e:
        return {"error": str(e)}
    root = safe_out_dir / source_id
    errors, warnings = [], []
    sessions = sorted(p for p in root.glob("*/*") if (p / "session.json").exists())
    # native label tokens (e.g. FRESH, BROKEN) must not appear in ids or session metadata used for analysis
    mp = root / "_manifest" / "mapping_used.json"
    tokens, grouping = [], "per_file"
    if mp.exists():
        used = json.loads(mp.read_text(encoding="utf-8"))
        tokens = [r["native"] for r in used.get("label_rules", []) if len(r["native"]) >= 3]
        grouping = (used.get("session_rule") or {}).get("grouping", "per_file")
    seen_ids, by_machine = {}, {}
    for sd in sessions:
        try:
            meta = json.loads((sd / "session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            errors.append(f"{sd.parent.name}/{sd.name}: session.json unreadable ({e})")
            continue
        sid = str(meta.get("session_id") or "")
        where = f"{sd.parent.name}/{sd.name}"
        if not sid:
            errors.append(f"{where}: session.json has no session_id")
            continue
        if sid in seen_ids:
            errors.append(f"{sid}: session_id appears in both {seen_ids[sid]} and {where}")
        seen_ids[sid] = where
        fm = DATASET_RE.fullmatch(sd.name)
        if not fm:
            errors.append(f"{sid}: folder {where} is not named dataset<N>")
        else:
            by_machine.setdefault(sd.parent.name, []).append((int(fm.group(1)), sid))
        for k in NEW_KEYS:
            if k not in meta:
                errors.append(f"{sid}: session.json missing {k}")
        if "machine_short" in meta and not MACHINE_SHORT_RE.fullmatch(str(meta["machine_short"])):
            errors.append(f"{sid}: machine_short {meta['machine_short']!r} must match [a-z0-9]+")
        if meta.get("domain", "unknown") not in ("lab", "line", "unknown"):
            errors.append(f"{sid}: domain {meta['domain']!r} not lab/line/unknown")
        if meta.get("coolant", "unknown") not in ("wet", "dry", "unknown"):
            errors.append(f"{sid}: coolant {meta['coolant']!r} not wet/dry/unknown")
        wav = sd / f"{sid}_s0.wav"
        if not wav.exists():
            errors.append(f"{sid}: missing s0 wav")
            continue
        i = sf.info(str(wav))
        if (i.samplerate, i.channels, i.subtype) != (48000, 1, "PCM_16"):
            errors.append(f"{sid}: audio format {i.samplerate}/{i.channels}/{i.subtype} != 48000/1/PCM_16")
        meta_text = " ".join([sid] + [str(v) for v in meta.values() if isinstance(v, str)]).upper()
        hit = [t for t in tokens if t.upper() in meta_text]
        if hit:
            errors.append(f"{sid}: native label token {hit} visible in session id/metadata (label leak)")
        for k in ("native_sample_rate", "clip_ratio_native", "rms_dbfs", "sha256", "source_id"):
            if k not in meta:
                errors.append(f"{sid}: session.json missing {k}")
        if meta.get("native_sample_rate", 48000) < 48000 and not meta.get("bandlimited"):
            errors.append(f"{sid}: upsampled from {meta.get('native_sample_rate')} but bandlimited flag not set")
        lab = sd / "label.csv"
        if not lab.exists():
            errors.append(f"{sid}: missing label.csv")
            continue
        with open(lab, encoding="utf-8-sig") as fh:
            rd = csv.DictReader(fh)
            if rd.fieldnames != LABEL_COLS:
                errors.append(f"{sid}: label.csv header {rd.fieldnames} != MSM {LABEL_COLS}")
            rows = list(rd)
        labelled = []
        prev_end = -1.0
        for n, r in enumerate(rows, 1):
            try:
                s, e, lb = float(r["start"]), float(r["end"]), int(r["label"])
            except (TypeError, ValueError):
                errors.append(f"{sid} row {n}: start/end/label not numeric")
                continue
            if lb not in (0, 1, 2):
                errors.append(f"{sid} row {n}: label {lb} not in 0/1/2")
            if not s < e:
                errors.append(f"{sid} row {n}: start >= end")
            if e > i.duration + tolerance_s:
                errors.append(f"{sid} row {n}: end {e} beyond audio duration {i.duration:.3f}")
            if s < prev_end - 1e-6:
                warnings.append(f"{sid} row {n}: overlaps previous segment")
            prev_end = max(prev_end, e)
            labelled.append((s, e))
            if r.get("direction", "") not in ("", "UP", "DOWN"):
                errors.append(f"{sid} row {n}: direction {r['direction']!r}")
            for k in ("N", "ap", "ae", "F", "D"):
                if r.get(k, "") != "":
                    try:
                        if not math.isfinite(float(r[k])):
                            errors.append(f"{sid} row {n}: {k} not a finite number ({r[k]!r})")
                        elif float(r[k]) < 0:
                            errors.append(f"{sid} row {n}: {k} negative")
                    except ValueError:
                        errors.append(f"{sid} row {n}: {k} not numeric ({r[k]!r})")
        # spans without an MSM label (cut_state cutting or unknown): sidecar start,end,cut_state; written only when
        # non-empty; never overlaps label.csv or another sidecar row (same checks as build_db.py check_unlabeled)
        unl = sd / f"{sid}{UNLABELED_SUFFIX}"
        if unl.exists():
            with open(unl, encoding="utf-8-sig") as fh:
                rd = csv.DictReader(fh)
                if rd.fieldnames != UNLABELED_COLS:
                    errors.append(f"{sid}: {unl.name} header {rd.fieldnames} != {UNLABELED_COLS}")
                urows = list(rd)
            if not urows:
                errors.append(f"{sid}: {unl.name} has no rows (it must only exist when there are unlabeled cutting spans)")
            spans = []
            for n, r in enumerate(urows, 1):
                try:
                    s, e = float(r["start"]), float(r["end"])
                except (TypeError, ValueError):
                    errors.append(f"{sid} unlabeled row {n}: start/end not numeric ({r.get('start')!r}, {r.get('end')!r})")
                    continue
                if not (math.isfinite(s) and math.isfinite(e)):
                    errors.append(f"{sid} unlabeled row {n}: start/end not a finite number ({r.get('start')!r}, {r.get('end')!r})")
                    continue
                if r.get("cut_state") not in UNLABELED_CUT_STATES:
                    errors.append(f"{sid} unlabeled row {n}: cut_state {r.get('cut_state')!r} not in {list(UNLABELED_CUT_STATES)}")
                if not s < e:
                    errors.append(f"{sid} unlabeled row {n}: start >= end")
                if e > i.duration + tolerance_s:
                    errors.append(f"{sid} unlabeled row {n}: end {e} beyond audio duration {i.duration:.3f}")
                if any(s < le - 1e-6 and ls < e - 1e-6 for ls, le in labelled):
                    errors.append(f"{sid} unlabeled row {n}: overlaps a label.csv row")
                if any(s < pe - 1e-6 and ps < e - 1e-6 for _pn, ps, pe in spans):
                    errors.append(f"{sid} unlabeled row {n}: overlaps another unlabeled row")
                spans.append((n, s, e))
            if meta.get("n_unlabeled_rows", len(urows)) != len(urows):
                errors.append(f"{sid}: session.json n_unlabeled_rows {meta.get('n_unlabeled_rows')} != {len(urows)} rows")
        elif meta.get("n_unlabeled_rows"):
            errors.append(f"{sid}: session.json n_unlabeled_rows={meta['n_unlabeled_rows']} but {unl.name} is missing")
        if meta.get("int16_clipped_by_conversion", 0) > 0:
            warnings.append(f"{sid}: {meta['int16_clipped_by_conversion']} samples clipped during int16 conversion")

    man = root / "_manifest"
    # dataset_index.csv: N <-> session_id <-> source path, one row per session folder
    index = {}
    if (man / "dataset_index.csv").exists():
        with open(man / "dataset_index.csv", encoding="utf-8-sig") as fh:
            rd = csv.DictReader(fh)
            if rd.fieldnames != ["dataset", "session_id", "source_relative_path"]:
                errors.append(f"dataset_index.csv header {rd.fieldnames} != dataset,session_id,source_relative_path")
            for r in rd:
                index[r.get("session_id")] = r
        folder = {sid: n for items in by_machine.values() for n, sid in items}
        if set(index) != set(folder):
            errors.append(f"dataset_index.csv lists {len(index)} sessions, folders hold {len(folder)} "
                          f"(differ: {sorted(set(index) ^ set(folder))[:3]})")
        for sid, n in folder.items():
            if sid in index and str(index[sid].get("dataset")) != str(n):
                errors.append(f"{sid}: dataset_index.csv says dataset{index[sid].get('dataset')}, folder is dataset{n}")
    else:
        errors.append("manifest/dataset_index.csv missing")
    # dataset<N>: msm_session keeps the source folder number; per_file is 1..n in sorted(session_id) order
    for machine, items in by_machine.items():
        items.sort()
        if grouping == "msm_session":
            for n, sid in items:
                src = (index.get(sid) or {}).get("source_relative_path", "")
                parts = src.split("/")
                fm = SOURCE_DATASET_RE.fullmatch(parts[-2]) if len(parts) >= 2 else None
                if not fm or int(fm.group(1)) != n:
                    errors.append(f"{sid}: folder dataset{n} does not keep the source folder number ({src!r})")
        elif [n for n, _ in items] != list(range(1, len(items) + 1)):
            errors.append(f"{machine}: dataset numbers are not 1..{len(items)} without gaps")
        elif [s for _, s in items] != sorted(s for _, s in items):
            errors.append(f"{machine}: dataset<N> order does not follow sorted session_id")

    if (man / "events.jsonl").exists():
        with open(man / "events.jsonl", encoding="utf-8") as fh:
            for line in fh:
                ev = json.loads(line)
                leaked = LEAK_FIELDS & set(ev)
                if leaked:
                    errors.append(f"event {ev.get('event_id')}: leak-prone fields in events table {sorted(leaked)}")
                    break
    else:
        errors.append("manifest/events.jsonl missing")
    return {"source_id": source_id, "n_sessions": len(sessions), "n_errors": len(errors), "errors": errors[:50],
            "n_warnings": len(warnings), "warnings": warnings[:50],
            "checks": ["audio 48k/mono/PCM_16", "session.json keys", "bandlimited flag", "label.csv header",
                       "label values 0/1/2", "start<end", "end within duration", "direction values",
                       "non-negative numeric conditions", "no leak-prone fields in events",
                       "no native label token in session id/metadata",
                       "dataset<N>: msm_session keeps the source number, per_file follows sorted session_id",
                       "dataset_index.csv matches the folders",
                       "unlabeled sidecar finite start<end, cut_state cutting/unknown, in range, no overlap",
                       "viewer keys in session.json",
                       "machine_short [a-z0-9]+", "unique session_id"]}
