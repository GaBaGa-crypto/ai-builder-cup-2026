"""Acceptance checks for step 2 (design/bdd-step2-build-db.md, scenarios S1-S12).

Usage: python check_db.py <db_dir> <inventory_dir> [<second_db_dir_for_S11>]
Independent of build_db.py: re-derives expectations from the inventory outputs and the
databases themselves. Prints PASS/FAIL per scenario; exit code 1 if any FAIL.
"""
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

DB, INV = sys.argv[1], sys.argv[2]
DB2 = sys.argv[3] if len(sys.argv) > 3 else None
main = sqlite3.connect(os.path.join(DB, "main.sqlite"))
src = sqlite3.connect(os.path.join(DB, "source", "source.sqlite"))
lab = sqlite3.connect(os.path.join(DB, "sealed", "labels.sqlite"))
fails = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        fails.append(name)


ev = pd.read_sql("select * from events", main)
ses = pd.read_sql("select * from sessions", main)
L = pd.read_sql("select * from labels", lab)
ss = pd.read_sql("select * from session_source", src)
es = pd.read_sql("select * from event_source", src)
paths = pd.read_csv(os.path.join(INV, "paths.csv"))
mshort = {"imi_vm20i": "vm20i", "imi_vmx30ui": "vmx30ui", "tmf_vf10": "vf10"}

# S1 事件分域
cut = ev[ev.cut_state == "cutting"]
want = paths.groupby("machine").size().rename(index=mshort)
got = cut.merge(ses[["session_id", "machine"]], on="session_id").groupby("machine").size()
dom_ok = set(ev.loc[ev.session_id.str.startswith("vf10"), "domain"]) == {"line"} and \
    set(ev.loc[~ev.session_id.str.startswith("vf10"), "domain"]) == {"lab"}
check("S1 cutting events per machine == label rows", want.sort_index().equals(got.sort_index()) and dom_ok,
      f"want {dict(want)} got {dict(got)}")

# S2 非切削
m = ev.merge(L, on="event_id")
nc = m[m.cut_state == "non_cutting"]
cov = ev.groupby("session_id").agg(total=("dur", "sum"), n=("dur", "size")).join(ses.set_index("session_id").s0_sec)
cov_ok = ((cov.total <= cov.s0_sec + 1e-3) & (cov.total >= cov.s0_sec - 0.5 * (cov.n + 1))).all()
check("S2 non_cutting labels all -1, none 0", (nc.label_std == -1).all() and len(nc) > 0, f"n={len(nc)}")
check("S2 events cover s0 within gap tolerance", bool(cov_ok))

# S3 主庫零標籤欄
bad_col = re.compile(r"label|defect|anomaly|remark|program|(^|_)nc(_|$)|tool_raw|block|current_tool|tool_num", re.I)
cols = {t: [r[1] for r in main.execute(f"pragma table_info({t})")] for (t,) in main.execute("select name from sqlite_master where type='table'")}
hits = [(t, c) for t, cs in cols.items() for c in cs if bad_col.search(c)]
check("S3 no forbidden column names in main", not hits, str(hits))

# S4 洩漏字串掃描
sensitive = set()
for col in ["dir", "files", "remark", "nc_files", "program", "tool_summary"]:
    for v in ss[col].dropna().astype(str):
        sensitive.update(p.strip() for p in re.split(r"[|\n]", v) if len(p.strip()) >= 4)
for v in ss.nc_text:
    sensitive.update(l.strip() for l in v.splitlines() if l.strip().startswith("N") and "(" in l and len(l.strip()) >= 6)
sensitive.update(t for t in es.tool_raw if "*" in t)
texts = []
for t, cs in cols.items():
    for c in cs:
        for (v,) in main.execute(f"select distinct {c} from {t} where typeof({c})='text'"):
            texts.append((t, c, v))
leak = [(t, c, v, s) for t, c, v in texts for s in sensitive if s and s in v]
pat = re.compile(r"\*|EXP|defect|dataset|\d{8}_\d{6}", re.I)
leak += [(t, c, v, "pattern") for t, c, v in texts if pat.search(v)]
check("S4 no source strings / leak patterns in main text cells", not leak, f"{len(sensitive)} sensitive strings scanned; hits {leak[:3]}")

# S5 允許欄
allow = json.load(open(os.path.join(DB, "features_allowlist.json"), encoding="utf-8"))
ok_cols = set(allow["context_static"] + allow["context_ts"] + allow["reaction_ts"] + allow["trivial_baseline_T"] + allow["id_time"])
extra = {c for c in cols["events"] + cols["context_ts"] if c not in ok_cols}
check("S5 events/context_ts columns within allowlist", not extra, str(sorted(extra)))

# S6 削波與 inventory 一致、AUC 重現
key = ss[["session_id", "machine", "dataset"]]
cm = cut.merge(key, on="session_id").merge(paths, left_on=["machine", "dataset", "start", "end"],
                                         right_on=["machine", "dataset", "start", "end"], suffixes=("", "_inv"))
diff = (cm.clip_ratio - cm.clip_ratio_inv).abs().max()
check("S6 clip_ratio == inventory paths.csv", len(cm) == len(cut) and diff < 1e-5, f"matched {len(cm)}/{len(cut)}, max diff {diff:.2e}")
aucs = {}
for mach, g in m[m.cut_state == "cutting"].merge(ses[["session_id", "machine"]], on="session_id").groupby("machine"):
    g = g[g.label_std.isin([0, 1])]
    aucs[mach] = round(roc_auc_score(g.label_std == 1, g.clip_ratio), 3)
check("S6 clip AUC reproduces 0.991/0.859/0.571", aucs == {"vm20i": 0.991, "vmx30ui": 0.859, "vf10": 0.571}, str(aucs))
check("S6 rms_dbfs finite for all events", np.isfinite(ev.rms_dbfs).all())

# S7 對齊
al = pd.read_sql("select * from alignment", main)
low = al[al.conf < 0.3]
check("S7 alignment row for every session", set(al.session_id) == set(ses.session_id), f"{len(al)} rows; conf<0.3: {list(low.session_id)}")

# S8 負功率
ctx = pd.read_sql("select session_id, power_w from context_ts", main)
check("S8 no negative power_w", int((ctx.power_w < 0).sum()) == 0)
raw_neg = 0
for d in ss[ss.machine == "imi_vmx30ui"].dir:
    f = [x for x in os.listdir(os.path.join(DB, "..", "raw", d)) if x.endswith("_mtc.csv")][0]
    w = pd.to_numeric(pd.read_csv(os.path.join(DB, "..", "raw", d, f), usecols=["watt"]).watt, errors="coerce")
    raw_neg += int((w < 0).sum())
check("S8 raw negative watt count == 579", raw_neg == 579, f"raw {raw_neg}")

# S9 控制器時序欄位
want_ts = ["session_id", "t", "spindle_speed_rpm", "feed_rate", "spindle_load", "x_load", "y_load", "z_load", "power_w",
           "s_vrms", "x_vrms", "y_vrms", "z_vrms", "executing"]
check("S9 context_ts columns normalized", sorted(cols["context_ts"]) == sorted(want_ts), str(cols["context_ts"]))
vf = pd.read_sql("select * from context_ts where session_id like 'vf10%'", main)
check("S9 VF-10 has spindle speed/load, no feed/power", vf.spindle_load.notna().all() and vf.feed_rate.isna().all() and vf.power_w.isna().all())

# S10 不透明編號
recomputed = {f"{mshort[r.machine]}_{hashlib.sha1(f'{r.machine}/dataset{r.dataset}'.encode()).hexdigest()[:6]}" for r in ss.itertuples()}
check("S10 opaque session ids reproducible, unique", recomputed == set(ses.session_id) and ses.session_id.is_unique)


def content_hash(path):
    c = sqlite3.connect(path)
    h = hashlib.sha256()
    for (t,) in c.execute("select name from sqlite_master where type='table' order by name"):
        for row in c.execute(f"select * from {t} order by 1, 2"):
            h.update(repr(row).encode())
    return h.hexdigest()


# S11 可重現
if DB2:
    same = all(content_hash(os.path.join(DB, p)) == content_hash(os.path.join(DB2, p))
               for p in ["main.sqlite", "source/source.sqlite", "sealed/labels.sqlite"])
    check("S11 two builds give identical table contents", same)
else:
    print("SKIP S11 (no second db dir)")

# S12 權限
perm = lambda p: stat.S_IMODE(os.stat(os.path.join(DB, p)).st_mode)
check("S12 private dirs 0700, files 0600",
      perm("source") == 0o700 and perm("sealed") == 0o700 and perm("source/source.sqlite") == 0o600 and perm("sealed/labels.sqlite") == 0o600,
      f"{oct(perm('source'))} {oct(perm('sealed'))} {oct(perm('source/source.sqlite'))} {oct(perm('sealed/labels.sqlite'))}")

print(f"\n{'ALL PASS' if not fails else 'FAILED: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
