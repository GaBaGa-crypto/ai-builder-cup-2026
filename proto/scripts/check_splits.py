"""Acceptance checks for step 3 (design/bdd-step3-split.md, S1-S7).

Usage: python check_splits.py <db_dir> <prereg_json> <make_splits.py> [<second_splits_dir_for_S6>]
Re-derives every expectation from the databases; shares no logic with make_splits.py.
"""
import ast
import hashlib
import json
import os
import re
import sqlite3
import sys

import pandas as pd

DB, PREREG, SRC = sys.argv[1], sys.argv[2], sys.argv[3]
SPL2 = sys.argv[4] if len(sys.argv) > 4 else None
SPL = os.path.join(DB, "splits")
fails = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        fails.append(name)


fs = pd.read_csv(os.path.join(SPL, "fold_sessions.csv"))
tn = pd.read_csv(os.path.join(SPL, "train_normal.csv"))
comp = pd.read_csv(os.path.join(SPL, "fold_composition.csv"))
ses = pd.read_sql("select session_id, machine from sessions", sqlite3.connect(os.path.join(DB, "main.sqlite")))
ev = pd.read_sql("select event_id, session_id, cut_state, clip_ratio from events", sqlite3.connect(os.path.join(DB, "main.sqlite")))
src = pd.read_sql("select session_id, dataset from session_source", sqlite3.connect(os.path.join(DB, "source", "source.sqlite")))
lab = pd.read_sql("select * from labels", sqlite3.connect(os.path.join(DB, "sealed", "labels.sqlite")))
ev = ev.merge(lab, on="event_id")
mach = dict(zip(ses.session_id, ses.machine))

# S1 實驗室留一場次
labf = fs[fs.fold_id != "vf10_main"]
tests = labf[labf.role == "test"]
ok_one_test = (tests.groupby("fold_id").size() == 1).all() and labf.fold_id.nunique() == 56
lab_sessions = set(ses[ses.machine.isin(["vm20i", "vmx30ui"])].session_id)
ok_each_once = sorted(tests.session_id) == sorted(lab_sessions)
overlap = labf.groupby("fold_id").apply(lambda g: len(set(g[g.role == "test"].session_id) & set(g[g.role == "train"].session_id)), include_groups=False).sum()
same_machine = all(len({mach[s] for s in g.session_id}) == 1 for _, g in labf.groupby("fold_id"))
full_train = all(len(g[g.role == "train"]) == (ses.machine == mach[g[g.role == "test"].session_id.iloc[0]]).sum() - 1 for _, g in labf.groupby("fold_id"))
check("S1 56 LOSO folds, each lab session tested once, train∩test=∅, same machine, train = all others",
      ok_one_test and ok_each_once and overlap == 0 and same_machine and full_train)

# S2 VF-10
vf = fs[fs.fold_id == "vf10_main"].merge(src, on="session_id")
ref_ds = sorted(vf[vf.role == "reference"].dataset)
test_ids = set(vf[vf.role == "test"].session_id)
cut = ev[ev.cut_state == "cutting"]
anom_sessions = set(cut[cut.label_std == 1].session_id)
check("S2 VF-10 reference = ds2/ds7/ds12, 10 test sessions (5 with anomalies + 5 all-normal)",
      ref_ds == [2, 7, 12] and len(test_ids) == 10 and len(test_ids & anom_sessions) == 5, f"ref {ref_ds}")
check("S2 VF-10 reference sessions all label 0",
      set(cut[cut.session_id.isin(vf[vf.role == 'reference'].session_id)].label_std) == {0})

# S3 遮罩
m = tn.merge(ev, on="event_id", how="left").merge(fs, on=["fold_id", "session_id"], how="left")
check("S3 every mask event: cutting, label 0, in a train/reference session of its fold",
      m.cut_state.eq("cutting").all() and m.label_std.eq(0).all() and m.role.isin(["train", "reference"]).all(), f"rows {len(tn)}")
expect = 0
for fid, g in fs.groupby("fold_id"):
    tr = g[g.role.isin(["train", "reference"])].session_id
    expect += int(((cut.session_id.isin(tr)) & (cut.label_std == 0)).sum())
check("S3 masks contain every eligible normal event (no silent drops)", expect == len(tn), f"expect {expect} got {len(tn)}")

# S4 組成表與 pure 標記
mix = cut.groupby("session_id").label_std.agg(lambda x: set(x))
want_pure = {s: int(not ({0, 1} <= mix[s])) for s in tests.session_id}
got_pure = dict(zip(comp.test_session, comp.pure_session))
pc = comp.merge(ses, left_on="test_session", right_on="session_id").groupby("machine").pure_session.sum().to_dict()
check("S4 pure_session flags correct; VM20i 26, VMX30Ui 15", all(got_pure[s] == want_pure[s] for s in want_pure) and pc.get("vm20i") == 26 and pc.get("vmx30ui") == 15, str(pc))
unc = cut[(cut.label_std == 1) & (cut.clip_ratio <= 0.01)]
vx_unc = comp.merge(ses, left_on="test_session", right_on="session_id").query("machine=='vmx30ui'").n1_unclipped.sum()
check("S4 VMX30Ui unclipped label-1 = 50 events from 3 sessions", vx_unc == 50 and unc[unc.session_id.map(mach) == "vmx30ui"].session_id.nunique() == 3)

# S5 預先登記
pr = json.load(open(PREREG, encoding="utf-8"))
files = [os.path.join(SPL, f) for f in pr["splits_files"]]
h = hashlib.sha256()
for p in sorted(files):
    h.update(os.path.basename(p).encode()); h.update(open(p, "rb").read())
need = pr["primary_contrast"] == "C1 vs B" and pr["false_alarm_budget"]["alarms_per_normal_cutting_hour"] == 5 \
    and pr["windows"] == {"length_s": 2.0, "hop_s": 1.0} and "any window" in pr["hit_rule"] and pr["vf10_reference_datasets"] == [2, 7, 12]
check("S5 prereg fields and splits_sha256 match files", need and h.hexdigest() == pr["splits_sha256"])

# S6 可重現、無結果依賴
tree = ast.parse(open(SRC, encoding="utf-8").read())
tree.body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))  # 模組說明
             and not (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "PREREG" for t in n.targets))]  # 登記文字
code = ast.unparse(tree)
hit = re.findall(r"feature|score|embed|model|\.npy|\.pt\b", code, re.I)
check("S6 make_splits.py code (minus docstring/PREREG text) reads no features/scores/models", not hit, str(hit))
if SPL2:
    same = all(open(os.path.join(SPL, f), "rb").read() == open(os.path.join(SPL2, f), "rb").read() for f in pr["splits_files"])
    check("S6 rerun gives byte-identical split files", same)
else:
    print("SKIP S6 rerun")

# S7 VF-10 測試事件不在任何遮罩
vf_test_events = set(ev[ev.session_id.isin(test_ids)].event_id)
check("S7 no VF-10 test event in any mask", not (vf_test_events & set(tn.event_id)))

print(f"\n{'ALL PASS' if not fails else 'FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
