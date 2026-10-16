"""convert_source: execute an approved Mapping deterministically.

Output layout (MSM raw layout, so build_db.py can read it with --extra-raw <out_dir>/<source_id>):
  <out_dir>/<source_id>/<machine_id>/dataset<N>/<session_id>_s0.wav   48 kHz / PCM_16 / mono
  <out_dir>/<source_id>/<machine_id>/dataset<N>/label.csv             12 MSM columns, labels 0/1/2 only
  <out_dir>/<source_id>/<machine_id>/dataset<N>/<session_id>_unlabeled.csv  start,end,cut_state of spans with label -1
                                                                      whose cut_state is cutting or unknown
                                                                      (only written when the session has such spans)
  <out_dir>/<source_id>/<machine_id>/dataset<N>/<session_id>_mtc.csv  unified controller columns (if any)
  <out_dir>/<source_id>/<machine_id>/dataset<N>/session.json          session_id lives here, not in the folder name
  <out_dir>/<source_id>/_manifest/{sources,sessions,events,provenance}.jsonl, context.csv and dataset_index.csv
Numbering of dataset<N>:
  grouping msm_session: N is the number of the source folder dataset<N> (MSM identity keeps the raw numbers).
  grouping per_file:    N = 1..n in sorted(session_id) order (opaque, does not reveal the label). Reproducible for the
                        same file set; adding or removing files renumbers, so the stable key is session_id.
  _manifest/dataset_index.csv (dataset, session_id, source_relative_path) records the assignment for both groupings.
Audio files under any path component starting with '_' or '.' are skipped (same rule as inspect_source).
"""
from __future__ import annotations

import csv
import datetime as dt
import glob
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Optional

from ..mapping_schema import Mapping
from .audio import normalize_audio, segment_stats

SPEC_PATH = Path(__file__).resolve().parents[1] / "spec" / "msm_spec.json"
LABEL_COLS = ["start", "end", "label", "direction", "N", "ap", "ae", "F", "Z", "D", "Tool", "Workpiece"]


MACHINE_SHORT_RE = re.compile(r"^[a-z0-9]+$")
DOMAIN_BY_FIELD = {"lab": "lab", "industry": "line", "unknown": "unknown"}
SOURCE_DATASET_RE = re.compile(r"dataset([1-9][0-9]*)")  # msm_session: the source folder name gives N
UNLABELED_COLS = ["start", "end", "cut_state"]
UNLABELED_CUT_STATES = ("cutting", "unknown")  # non_cutting stays a gap (no row anywhere)
UNLABELED_SUFFIX = "_unlabeled.csv"


def _skipped(rel: str) -> bool:
    """True if any path component starts with '_' or '.' (analysis folders, hidden files): not part of the dataset."""
    return any(part.startswith(("_", ".")) for part in rel.split("/"))


def _spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def machine_short_of(machine_id: str) -> str:
    """Viewer/build_db short machine name: machine_id lower-cased with everything outside [a-z0-9] removed."""
    return re.sub(r"[^a-z0-9]+", "", (machine_id or "").lower())


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_")


def _field_values(mapping: Mapping, rel_path: str) -> dict:
    vals = {k: "" for k in LABEL_COLS[3:]}
    for fr in mapping.fields:
        if fr.applies_to and not re.search(fr.applies_to, rel_path):
            continue
        v = ""
        if fr.source == "constant" and fr.value is not None:
            v = fr.value
        elif fr.source == "path_regex" and fr.pattern:
            m = re.search(fr.pattern, rel_path)
            if m and m.groupdict().get("v") is not None:
                v = m.group("v")
        if v != "" and fr.msm_field not in ("Tool", "Workpiece", "direction"):
            try:
                v = float(v) * fr.factor
                if not math.isfinite(v):  # 'nan' / 'inf' in a path or constant: not a value, leave empty
                    raise ValueError(v)
                if fr.msm_field == "Z":
                    v = int(round(v))
                else:
                    v = round(v, 6)
                    v = int(v) if v == int(v) else v  # MSM writes 10000, not 10000.0
            except (TypeError, ValueError):
                v = ""
        vals[fr.msm_field] = v
    return vals


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def _unify_context(mtc_path: Path, spec: dict):
    """Return (header, rows, provenance dict) with unified column names; negative power -> blank."""
    with open(mtc_path, encoding="utf-8-sig") as fh:
        rd = csv.DictReader(fh)
        cols = rd.fieldnames or []
        kind = "msm_lab" if "Spindle_Speed" in cols else ("msm_vf10" if "s_speed" in cols else None)
        if kind is None:
            return None, [], {}
        cmap = spec["context_unified"]["known_sources"][kind]
        pmap = spec["provenance_only"]["known_sources"][kind]
        header = [c["name"] for c in spec["context_unified"]["columns"]]
        rows, prov = [], {k: set() for k in pmap}
        for r in rd:
            out = {}
            for h in header:
                src = cmap.get(h)
                v = r.get(src, "") if src else ""
                if h == "power_w" and v not in ("", None):
                    try:
                        v = "" if float(v) < 0 else v
                    except ValueError:
                        v = ""
                out[h] = v
            rows.append(out)
            for k, src in pmap.items():
                if r.get(src) not in (None, ""):
                    prov[k].add(r[src])
    return header, rows, {k: sorted(v)[:20] for k, v in prov.items()}


def convert_source(source_dir: str, mapping_path: str, out_dir: str, limit: Optional[int] = None,
                   approved_by: str = "") -> dict:
    """Convert a source dataset into MSM-format sessions using an approved mapping file.

    Args:
        source_dir: Root folder of the external dataset.
        mapping_path: Path to the mapping JSON (see mapping_schema.Mapping).
        out_dir: Root folder for normalized output.
        limit: Optional max number of sessions (for dry runs).
        approved_by: Name of the person who approved the mapping (recorded in the output).

    Returns:
        Summary with counts per label, warnings, and manifest paths.
    """
    spec = _spec()
    mapping = Mapping.model_validate_json(Path(mapping_path).read_text(encoding="utf-8"))
    if mapping.unresolved and not approved_by:
        return {"error": "mapping has unresolved questions and no approver; resolve or approve first",
                "unresolved": mapping.unresolved}
    m = mapping.machine
    machine_short = machine_short_of(m.machine_id)
    if not MACHINE_SHORT_RE.fullmatch(machine_short):
        return {"error": f"machine.machine_id {m.machine_id!r} gives an empty machine_short; "
                         "it must contain at least one character in [a-z0-9]"}
    src_root, out_root = Path(source_dir), Path(out_dir) / mapping.source_id
    files = sorted(f for f in glob.glob(str(src_root / mapping.session_rule.audio_glob), recursive=True)
                   if not _skipped(Path(f).relative_to(src_root).as_posix()))
    if limit:
        files = files[:limit]
    if not files:
        return {"error": f"no audio matched {mapping.session_rule.audio_glob!r} under {src_root}"}

    # pass 1: collect every session id, then number dataset<N> folders
    #   msm_session: N = number of the source folder dataset<N>; per_file: N = rank in sorted(session_id)
    planned, source_n = {}, {}
    for f in files:
        fp = Path(f)
        rel = fp.relative_to(src_root).as_posix()
        if mapping.session_rule.grouping == "msm_session":
            session_id = _safe(f"{mapping.source_id}__{fp.parent.relative_to(src_root).as_posix()}")
            fm = SOURCE_DATASET_RE.fullmatch(fp.parent.name)
            if not fm:
                return {"error": f"grouping msm_session needs source folders named dataset<N> (N >= 1); got {rel!r}"}
            n_src = int(fm.group(1))
            clash = [s for s, n in source_n.items() if n == n_src]
            if clash:
                return {"error": f"source folders {planned[clash[0]][1]} and {rel} both give dataset{n_src}; "
                                 "map one machine per mapping"}
            source_n[session_id] = n_src
        else:
            # opaque id: source paths often encode the label (e.g. FRESH520/...A0001); the path stays in provenance
            session_id = f"{_safe(mapping.source_id)}__{hashlib.sha1(rel.encode('utf-8')).hexdigest()[:12]}"
        if session_id in planned:
            return {"error": f"two audio files map to session {session_id!r}: {planned[session_id][1]} and {rel}"}
        planned[session_id] = (fp, rel)
    if mapping.session_rule.grouping == "msm_session":
        plan = sorted((source_n[sid], sid, *planned[sid]) for sid in planned)
    else:
        plan = [(n, sid, *planned[sid]) for n, sid in enumerate(sorted(planned), start=1)]

    approved_at = dt.datetime.now().isoformat(timespec="seconds") if approved_by else None
    previous = None
    if out_root.exists() and any(out_root.iterdir()):
        # never mix runs and never delete: move the previous output aside
        previous = out_root.with_name(f"{out_root.name}.prev_{dt.datetime.now():%Y%m%d_%H%M%S}")
        out_root.rename(previous)
    sessions, events, provenance, context_rows, warnings = [], [], [], [], []
    rules = {r.native: r for r in mapping.label_rules}
    machine_name = m.display_name or m.model or m.machine_id
    remark = f"{mapping.source_name}; license {mapping.license or 'unknown'}"

    for dataset_n, session_id, fp, rel in plan:
        dst_dir = out_root / m.machine_id / f"dataset{dataset_n}"
        dst_wav = dst_dir / f"{session_id}_s0.wav"
        a = normalize_audio(str(fp), str(dst_wav), channel=mapping.audio.channel, gain=mapping.audio.gain)
        dur = a["duration_s"]
        label_rows, sess_events, unlabeled_rows = [], [], []

        if mapping.label_source == "msm_label_csv":
            lab_p = fp.parent / "label.csv"
            with open(lab_p, encoding="utf-8-sig") as fh:
                rd = csv.DictReader(fh)
                if rd.fieldnames != LABEL_COLS:
                    warnings.append(f"{rel}: label.csv header differs from MSM spec: {rd.fieldnames}")
                label_rows = list(rd)
            ev_type = "interval" if m.field == "industry" else "path"
            for r in label_rows:
                sess_events.append({"start_s": float(r["start"]), "end_s": float(r["end"]), "msm_label": int(r["label"]),
                                    "cut_state": "cutting", "event_type": ev_type, "label_native": r["label"],
                                    "anomaly_type": None, "mapping_confidence": "exact",
                                    **{k: r.get(k, "") for k in LABEL_COLS[3:]}})
            # gaps = non-cutting time (not normal)
            t, gaps = 0.0, []
            for r in sorted(label_rows, key=lambda r: float(r["start"])):
                if float(r["start"]) - t > 0.5:
                    gaps.append((t, float(r["start"])))
                t = max(t, float(r["end"]))
            if dur - t > 0.5:
                gaps.append((t, dur))
            for s, e in gaps:
                sess_events.append({"start_s": s, "end_s": e, "msm_label": -1, "cut_state": "non_cutting",
                                    "event_type": "non_cutting", "label_native": "", "anomaly_type": None,
                                    "mapping_confidence": "exact", **{k: "" for k in LABEL_COLS[3:]}})
            mtc = sorted(fp.parent.glob("*_mtc.csv"))
            if mtc:
                header, rows, prov = _unify_context(mtc[0], spec)
                if header:
                    with open(dst_dir / f"{session_id}_mtc.csv", "w", newline="", encoding="utf-8") as fh:
                        w = csv.DictWriter(fh, fieldnames=header)
                        w.writeheader()
                        w.writerows(rows)
                    context_rows.extend({"session_id": session_id, **r} for r in rows)
                    for k, vs in prov.items():
                        for v in vs:
                            provenance.append({"session_id": session_id, "field": k, "value": v})
            for nc in fp.parent.glob("*.nc"):
                first = nc.read_text(encoding="utf-8", errors="ignore").splitlines()[:1]
                provenance.append({"session_id": session_id, "field": "nc_program_text",
                                   "value": f"{nc.name}: {first[0] if first else ''}"})
            for r in label_rows:
                if r.get("Tool"):
                    provenance.append({"session_id": session_id, "field": "tool_code_raw", "value": r["Tool"]})
        else:
            token = None
            if mapping.label_pattern:
                mm = re.search(mapping.label_pattern, rel)
                token = mm.group("v") if mm and mm.groupdict().get("v") is not None else None
            rule = rules.get(token) if token is not None else None
            if rule is None:
                warnings.append(f"{rel}: no label rule for token {token!r}; event kept with label -1")
            fv = _field_values(mapping, rel)
            base = {"msm_label": rule.msm_label if rule else -1, "cut_state": rule.cut_state if rule else "unknown",
                    "anomaly_type": rule.anomaly_type if rule else None,
                    "mapping_confidence": rule.confidence if rule else "none", "event_type": "file"}
            # file-level rule, then interval labels override their spans (e.g. tool breakage onset found on a spectrogram)
            pieces = [(0.0, dur, base)]
            for il in sorted((i for i in mapping.interval_labels if i.path == rel), key=lambda i: i.start_s):
                s0, e0 = min(il.start_s, dur), min(il.end_s if il.end_s is not None else dur, dur)
                if e0 <= s0:
                    warnings.append(f"{rel}: interval {il.start_s}-{il.end_s} outside audio ({dur:.3f} s); ignored")
                    continue
                ov = {"msm_label": il.msm_label, "cut_state": il.cut_state, "anomaly_type": il.anomaly_type,
                      "mapping_confidence": il.confidence, "event_type": "interval"}
                new = []
                for a0, b0, lab0 in pieces:
                    if b0 <= s0 or a0 >= e0:
                        new.append((a0, b0, lab0))
                        continue
                    if a0 < s0:
                        new.append((a0, s0, lab0))
                    new.append((max(a0, s0), min(b0, e0), ov))
                    if b0 > e0:
                        new.append((e0, b0, lab0))
                pieces = new
            for a0, b0, lab0 in pieces:
                if b0 - a0 <= 1e-6:
                    continue
                ev_type = "non_cutting" if lab0["cut_state"] == "non_cutting" else lab0["event_type"]
                sess_events.append({"start_s": round(a0, 6), "end_s": round(b0, 6), **lab0, "event_type": ev_type,
                                    "label_native": token or "", **fv})
                if lab0["cut_state"] == "cutting" and lab0["msm_label"] in (0, 1, 2):
                    label_rows.append({"start": round(a0, 6), "end": round(b0, 6), "label": lab0["msm_label"], **fv})
                elif lab0["cut_state"] in UNLABELED_CUT_STATES:
                    # cutting without an MSM label (abstained / not mappable), or cut state unknown (no rule, or a rule
                    # saying unknown): not a label.csv row (labels stay 0/1/2) and not non-cutting time either, so it
                    # goes to the sidecar that build_db.py reads; an unknown cut state cannot carry a label -> -1
                    if lab0["msm_label"] != -1:
                        warnings.append(f"{rel}: cut_state unknown with label {lab0['msm_label']}; "
                                        "written to the sidecar as unknown, label -1")
                    unlabeled_rows.append({"start": round(a0, 6), "end": round(b0, 6), "cut_state": lab0["cut_state"]})

        provenance.append({"session_id": session_id, "field": "source_relative_path", "value": rel})
        dst_dir.mkdir(parents=True, exist_ok=True)
        with open(dst_dir / "label.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=LABEL_COLS, extrasaction="ignore")
            w.writeheader()
            w.writerows(label_rows)
        if unlabeled_rows:
            with open(dst_dir / f"{session_id}{UNLABELED_SUFFIX}", "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=UNLABELED_COLS)
                w.writeheader()
                w.writerows(unlabeled_rows)
        stats = segment_stats(str(dst_wav), [(e["start_s"], e["end_s"]) for e in sess_events])
        for i, (e, st) in enumerate(zip(sess_events, stats)):
            events.append({"event_id": f"{session_id}__e{i:04d}", "source_id": mapping.source_id,
                           "machine_id": m.machine_id, "session_id": session_id,
                           "dur_s": round(e["end_s"] - e["start_s"], 6), **e, **st})
        # workpiece: the mapping's Workpiece rule for this file, then any value written in label.csv rows
        wp = []
        for v in [_field_values(mapping, rel).get("Workpiece", "")] + [r.get("Workpiece", "") for r in label_rows]:
            v = str(v).strip()
            if v and v not in wp:
                wp.append(v)
        sess_meta = {"session_id": session_id, "source_id": mapping.source_id, "machine_id": m.machine_id,
                     "machine_short": machine_short, "machine_name": machine_name,
                     "dataset": f"dataset{dataset_n}", "process": m.process, "field": m.field,
                     "domain": DOMAIN_BY_FIELD[m.field], "coolant": m.coolant, "mic_type": mapping.audio.mic_type,
                     "workpiece": "; ".join(wp), "remark": remark,
                     "s0_path": dst_wav.relative_to(out_root).as_posix(), **a,
                     "n_label_rows": len(label_rows), "n_unlabeled_rows": len(unlabeled_rows), "n_events": len(sess_events),
                     "mapping_version": mapping.mapping_version, "spec_version": spec["spec_version"]}
        (dst_dir / "session.json").write_text(json.dumps(sess_meta, ensure_ascii=False, indent=1), encoding="utf-8")
        # remark (source name; license) is for the viewer only; the sources table already carries it
        sessions.append({k: v for k, v in sess_meta.items() if k != "remark"})

    # source-level remarks (MSM dataset_summary.xlsx) go to provenance only
    summ = src_root / "dataset_summary.xlsx"
    if summ.exists() and mapping.label_source == "msm_label_csv":
        import openpyxl
        ws = openpyxl.load_workbook(summ, read_only=True).active
        done = {s["session_id"] for s in sessions}
        for r in list(ws.iter_rows(values_only=True))[1:]:
            if r and r[0] and r[4]:
                sid = _safe(f"{mapping.source_id}__{r[0]}/dataset{int(r[1])}")
                if sid in done:
                    provenance.append({"session_id": sid, "field": "remark", "value": str(r[4]).strip()})

    man = out_root / "_manifest"
    source_row = {"source_id": mapping.source_id, "source_name": mapping.source_name, "url": mapping.url,
                  "license": mapping.license, "citation": mapping.citation, "mapping_version": mapping.mapping_version,
                  "approved_by": approved_by or mapping.approved_by, "approved_at": approved_at or mapping.approved_at,
                  "machine_id": m.machine_id, "n_sessions": len(sessions)}
    _write_jsonl(man / "sources.jsonl", [source_row])
    _write_jsonl(man / "sessions.jsonl", sessions)
    _write_jsonl(man / "events.jsonl", events)
    _write_jsonl(man / "provenance.jsonl", provenance)
    # N <-> session_id <-> source path; provenance-type manifest only (session.json never holds the source path)
    with open(man / "dataset_index.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["dataset", "session_id", "source_relative_path"])
        w.writerows((n, sid, rel) for n, sid, _fp, rel in plan)
    if context_rows:
        with open(man / "context.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(context_rows[0].keys()))
            w.writeheader()
            w.writerows(context_rows)
    (Path(out_root) / "_manifest" / "mapping_used.json").write_text(mapping.model_dump_json(indent=1), encoding="utf-8")

    from collections import Counter
    return {"source_id": mapping.source_id, "out_root": str(out_root), "n_sessions": len(sessions),
            "n_events": len(events), "events_by_label": dict(Counter(e["msm_label"] for e in events)),
            "events_by_cut_state": dict(Counter(e["cut_state"] for e in events)),
            "bandlimited_sessions": sum(1 for s in sessions if s.get("bandlimited")),
            "unlabeled_cutting_spans": sum(s["n_unlabeled_rows"] for s in sessions),  # sidecar rows (cutting + unknown)
            "unknown_cut_state_spans": sum(1 for e in events if e["cut_state"] == "unknown"),
            "dataset_numbering": ("source folder dataset<N>" if mapping.session_rule.grouping == "msm_session"
                                  else "sorted(session_id)"),
            "warnings": warnings[:50], "n_warnings": len(warnings), "manifest": str(man),
            "previous_output_moved_to": str(previous) if previous else None}
