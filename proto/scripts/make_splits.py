"""Step 3: session-level splits, normal-only training masks, and the pre-registration file.

Spec: design/bdd-step3-split.md.  Usage: python make_splits.py <db_dir> <prereg_json_out>
Writes <db_dir>/splits/{fold_sessions.csv, train_normal.csv, fold_composition.csv}
and <prereg_json_out> (with splits_sha256).
Reads only: main sessions/events, source dataset numbers, sealed labels. Never reads features or scores.
"""
import hashlib
import json
import os
import sqlite3
import sys

import pandas as pd

LAB = ["vm20i", "vmx30ui"]
VF10_REFERENCE_DATASETS = [2, 7, 12]  # 使用者 10/14 拍板：每批（1、3、6 月）最早一場全正常
CLIP_UNCLIPPED = 0.01  # 與第 1 步盤點同一定義：滿刻度樣本 ≤ 1% 算未削波

PREREG = {
    "version": "prereg-v1",
    "registered": "2026-10-14, before any feature or score exists",
    "decided_by": "user (budget, merge, hit rule, VF-10 reference); others from model_map v0.6 and design/11",
    "primary_contrast": "C1 vs B",
    "required_baselines": {"A": "context only", "T": "clip_ratio, rms_dbfs, dur", "M": "session and environment metadata"},
    "evaluation_slices": {
        "inferential": "VF-10 test sessions (5 with anomalies, 5 all-normal)",
        "exploratory": "VMX30Ui label-1 events with clip_ratio <= 0.01 (3 sessions; below the 4-session rule)",
        "demo_only": "VM20i (anomaly results labelled 'confounded by clipping')",
    },
    "windows": {"length_s": 2.0, "hop_s": 1.0},
    "event_auc_pooling": "mean of top-3 window scores (all windows if fewer than 3)",
    "false_alarm_budget": {"alarms_per_normal_cutting_hour": 5,
                           "denominator": "label-0 cutting time of the validation sessions",
                           "alarm_merge": "above-threshold windows whose starts are <= 5 s apart form one alarm",
                           "threshold_fit": "lab: inner leave-one-session-out on the fold's training sessions; VF-10: leave-one-reference-out"},
    "hit_rule": "an anomalous event counts as detected if any window inside it exceeds the threshold",
    "report_separately": ["lab vs line", "good-tool vs defective-tool label-1 recall", "alarm rate on label-2 events",
                          "normal events inside sessions that also contain anomalies (shortcut test)"],
    "uncertainty": {"method": "session-cluster bootstrap", "n": 2000, "seed": 20261014,
                    "p_values_only_if": ">= 4 sessions with anomalies in the slice"},
    "training_data": "label-0 cutting events of the fold's training/reference sessions only; same machine only",
    "alignment_note": "4 sessions with conf < 0.3 do not affect C1 (static context from label.csv); excluded only from research-version observer 2 and C2",
}


def sha256_of(paths):
    h = hashlib.sha256()
    for p in sorted(paths):
        h.update(os.path.basename(p).encode())
        h.update(open(p, "rb").read())
    return h.hexdigest()


def main():
    db, prereg_out = sys.argv[1], sys.argv[2]
    main_con = sqlite3.connect(os.path.join(db, "main.sqlite"))
    ses = pd.read_sql("select session_id, machine from sessions", main_con)
    ev = pd.read_sql("select event_id, session_id, cut_state, clip_ratio from events", main_con)
    ds = pd.read_sql("select session_id, dataset from session_source", sqlite3.connect(os.path.join(db, "source", "source.sqlite")))
    lab = pd.read_sql("select event_id, label_std, tool_defective from labels", sqlite3.connect(os.path.join(db, "sealed", "labels.sqlite")))
    ses = ses.merge(ds, on="session_id").sort_values(["machine", "dataset"]).reset_index(drop=True)
    ev = ev.merge(lab, on="event_id")

    folds = []  # (fold_id, session_id, role)
    for mach in LAB:
        ms = ses[ses.machine == mach]
        for r in ms.itertuples():
            fid = f"{mach}_loso_ds{r.dataset:02d}"
            folds.append((fid, r.session_id, "test"))
            folds += [(fid, o, "train") for o in ms.session_id if o != r.session_id]
    vf = ses[ses.machine == "vf10"]
    for r in vf.itertuples():
        folds.append(("vf10_main", r.session_id, "reference" if r.dataset in VF10_REFERENCE_DATASETS else "test"))
    fs = pd.DataFrame(folds, columns=["fold_id", "session_id", "role"])

    # 參考場次必須全正常（只斷言，不輸出標籤）
    ref = fs[fs.role == "reference"].session_id
    ref_labels = set(ev[(ev.session_id.isin(ref)) & (ev.cut_state == "cutting")].label_std)
    assert ref_labels == {0}, f"VF-10 reference sessions are not all-normal: {ref_labels}"

    # 只學正常的遮罩
    normal = ev[(ev.cut_state == "cutting") & (ev.label_std == 0)][["event_id", "session_id"]]
    tn = fs[fs.role.isin(["train", "reference"])].merge(normal, on="session_id")[["fold_id", "event_id"]]
    tn = tn.sort_values(["fold_id", "event_id"]).reset_index(drop=True)

    # 折的組成（只給第 7 步與報告）
    cut = ev[ev.cut_state == "cutting"]
    comp = []
    for (fid, sid) in fs[fs.role == "test"][["fold_id", "session_id"]].itertuples(index=False):
        c = cut[cut.session_id == sid]
        n = {k: int((c.label_std == k).sum()) for k in (0, 1, 2)}
        a = c[c.label_std == 1]
        comp.append(dict(fold_id=fid, test_session=sid, n0=n[0], n1=n[1], n2=n[2],
                         n1_unclipped=int((a.clip_ratio <= CLIP_UNCLIPPED).sum()),
                         n1_good_tool=int((a.tool_defective == 0).sum()), n1_defective_tool=int((a.tool_defective == 1).sum()),
                         pure_session=int(not (n[0] > 0 and n[1] > 0))))
    comp = pd.DataFrame(comp)

    out = os.path.join(db, "splits")
    os.makedirs(out, exist_ok=True)
    files = []
    for name, df in [("fold_sessions.csv", fs), ("train_normal.csv", tn), ("fold_composition.csv", comp)]:
        p = os.path.join(out, name)
        df.to_csv(p, index=False, lineterminator="\n")
        files.append(p)

    prereg = dict(PREREG, splits_files=[os.path.basename(p) for p in files], splits_sha256=sha256_of(files),
                  vf10_reference_datasets=VF10_REFERENCE_DATASETS)
    with open(prereg_out, "w", encoding="utf-8") as fh:
        json.dump(prereg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"folds: {fs.fold_id.nunique()}  train_normal rows: {len(tn)}  splits_sha256: {prereg['splits_sha256']}")


if __name__ == "__main__":
    main()
