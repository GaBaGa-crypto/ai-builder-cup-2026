"""Step 1 inventory for the MSM dataset.

Reads every session folder under <raw>/<machine>/datasetN and writes:
  sessions.csv   one row per session (files present, durations, label mix, tool, remark, clipping)
  paths.csv      one row per labelled path/interval (label, duration, relative position, clipping)
  summary.md     the gate numbers the workflow needs

Usage: python inventory.py <raw_dir> <out_dir>
Only reads data. Writes nothing inside <raw_dir>.
"""
import csv
import os
import sys
from collections import Counter, defaultdict

import numpy as np

from msm_io import clip_ratio, iter_sessions, read_labels, read_summary, wav_info

RAW, OUT = sys.argv[1], sys.argv[2]
os.makedirs(OUT, exist_ok=True)

summary = read_summary(RAW)


session_rows, path_rows = [], []
for machine, ds, sdir in iter_sessions(RAW):
    files = os.listdir(sdir)
    has = {k: any(f.endswith(suf) for f in files) for k, suf in
           [("s0", "_s0.wav"), ("s1", "_s1.wav"), ("s2", "_s2.wav"), ("mtc", "_mtc.csv"), ("acc", "_acc.csv"),
            ("ct", "_ct.csv"), ("nc", ".nc"), ("mp4", ".mp4"), ("label", "label.csv")]}
    s0 = next((os.path.join(sdir, f) for f in files if f.endswith("_s0.wav")), None)
    sr = ch = sw = nfr = 0
    if s0:
        sr, ch, sw, nfr = wav_info(s0)
    dur = nfr / sr if sr else 0.0
    labels = read_labels(sdir)
    segs = [(float(r["start"]), float(r["end"])) for r in labels]
    clips = clip_ratio(s0, segs) if (s0 and segs) else [float("nan")] * len(segs)
    lab_counts = Counter(int(r["label"]) for r in labels)
    lab_secs = defaultdict(float)
    tools = Counter(r.get("Tool", "").strip() for r in labels)
    for i, r in enumerate(labels):
        lab = int(r["label"])
        s, e = segs[i]
        lab_secs[lab] += e - s
        path_rows.append(dict(
            machine=machine, dataset=ds, path_index=i, n_paths=len(labels), rel_index=round(i / max(1, len(labels) - 1), 3),
            start=s, end=e, dur=round(e - s, 3), label=lab, direction=r.get("direction", ""), N=r.get("N", ""),
            ap=r.get("ap", ""), ae=r.get("ae", ""), F=r.get("F", ""), Z=r.get("Z", ""), D=r.get("D", ""),
            tool=r.get("Tool", "").strip(), tool_defective="*" in r.get("Tool", ""), workpiece=r.get("Workpiece", ""),
            rel_start_in_session=round(s / dur, 3) if dur else "", clip_ratio=round(clips[i], 5)))
    kinds = sorted(lab_counts)
    mix = "none" if not kinds else (f"pure{kinds[0]}" if len(kinds) == 1 else "mixed:" + "".join(map(str, kinds)))
    sm = summary.get((machine, ds), {})
    session_rows.append(dict(
        machine=machine, dataset=ds, s0_sr=sr, s0_ch=ch, s0_bits=sw * 8, s0_sec=round(dur, 1),
        **{f"has_{k}": int(v) for k, v in has.items()},
        n_paths=len(labels), n0=lab_counts.get(0, 0), n1=lab_counts.get(1, 0), n2=lab_counts.get(2, 0),
        sec0=round(lab_secs.get(0, 0), 1), sec1=round(lab_secs.get(1, 0), 1), sec2=round(lab_secs.get(2, 0), 1),
        label_mix=mix, tools="|".join(f"{k}:{v}" for k, v in tools.items()),
        any_defective_tool=int(any("*" in t for t in tools)),
        workpiece=sm.get("workpiece", ""), remark=sm.get("remark", ""),
        max_clip_ratio=round(np.nanmax(clips), 5) if clips else ""))

for name, rows in [("sessions.csv", session_rows), ("paths.csv", path_rows)]:
    with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

# ---- summary --------------------------------------------------------------
L = []
L.append("# MSM step-1 inventory\n")
L.append(f"sessions: {len(session_rows)}; labelled paths/intervals: {len(path_rows)}\n")
by_m = defaultdict(list)
for r in session_rows:
    by_m[r["machine"]].append(r)
L.append("## Per machine\n")
L.append("| machine | sessions | s0 hours | paths | label0 | label1 | label2 | sessions with label1 | mixed sessions | pure1 | pure2 |")
L.append("|---|---|---|---|---|---|---|---|---|---|---|")
for m, rs in by_m.items():
    L.append(f"| {m} | {len(rs)} | {sum(r['s0_sec'] for r in rs)/3600:.2f} | {sum(r['n_paths'] for r in rs)} | "
             f"{sum(r['n0'] for r in rs)} | {sum(r['n1'] for r in rs)} | {sum(r['n2'] for r in rs)} | "
             f"{sum(1 for r in rs if r['n1'])} | {sum(1 for r in rs if r['label_mix'].startswith('mixed'))} | "
             f"{sum(1 for r in rs if r['label_mix']=='pure1')} | {sum(1 for r in rs if r['label_mix']=='pure2')} |")
L.append("\n## Label x defective tool (paths, sessions)\n")
L.append("| machine | label | good tool paths | good tool sessions | defective tool paths | defective tool sessions |")
L.append("|---|---|---|---|---|---|")
for m in by_m:
    for lab in (0, 1, 2):
        g = [p for p in path_rows if p["machine"] == m and p["label"] == lab and not p["tool_defective"]]
        d = [p for p in path_rows if p["machine"] == m and p["label"] == lab and p["tool_defective"]]
        L.append(f"| {m} | {lab} | {len(g)} | {len({p['dataset'] for p in g})} | {len(d)} | {len({p['dataset'] for p in d})} |")
L.append("\n## Path duration (s) by machine and label: median [min, max]\n")
for m in by_m:
    for lab in (0, 1, 2):
        ds_ = [p["dur"] for p in path_rows if p["machine"] == m and p["label"] == lab]
        if ds_:
            L.append(f"- {m} label{lab}: n={len(ds_)}, median {np.median(ds_):.2f} [{min(ds_):.2f}, {max(ds_):.2f}]")
L.append("\n## Relative position of label-1 paths within their session (0=first, 1=last)\n")
for m in by_m:
    rel = [p["rel_index"] for p in path_rows if p["machine"] == m and p["label"] == 1 and p["n_paths"] > 1]
    if rel:
        L.append(f"- {m}: n={len(rel)}, median {np.median(rel):.2f}, share in last third {np.mean(np.array(rel) >= 2/3):.2f}")
L.append("\n## Clipping (fraction of full-scale samples) by machine and label\n")
for m in by_m:
    for lab in (0, 1, 2):
        c = [p["clip_ratio"] for p in path_rows if p["machine"] == m and p["label"] == lab and p["clip_ratio"] == p["clip_ratio"]]
        if c:
            c = np.array(c)
            L.append(f"- {m} label{lab}: paths with >1% clipped samples {np.mean(c > 0.01):.2%} (n={len(c)}), max {c.max():.2%}")
L.append("\n## Distinct Tool values\n")
L.append(", ".join(f"{k} ({v})" for k, v in Counter(p["tool"] for p in path_rows).most_common()))
L.append("\n## File availability\n")
for m, rs in by_m.items():
    L.append(f"- {m}: " + ", ".join(f"{k}={sum(r['has_'+k] for r in rs)}" for k in ["s0", "s1", "s2", "mtc", "acc", "ct", "nc", "mp4", "label"]))
with open(os.path.join(OUT, "summary.md"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(L) + "\n")
print("\n".join(L))
