"""Acceptance checks for step 2 (design/bdd-step2-build-db.md, scenarios S1-S14).

Usage: python check_db.py <db_dir> <inventory_dir> [<second_db_dir_for_S11>]
Independent of build_db.py: re-derives expectations from the inventory outputs and the
databases themselves. Prints PASS/FAIL per scenario; exit code 1 if any FAIL.
Multi-root databases (build_db.py --extra-raw): the MSM-specific expectations (inventory counts, clip AUC, VF-10,
negative watt) are checked on the MSM sessions only; normalized sessions are checked against their own folders
(label.csv rows + <session_id>_unlabeled.csv rows by cut_state cutting/unknown, session.json machine_short).
"""
import csv
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
# MSM 場次＝來源機台資料夾是 MSM 三台且資料夾沒有 session.json；其餘是正規化場次（--extra-raw）
has_root = "root" in ss.columns


def sdir_of(r):
    return os.path.join(r.root, r.dir) if has_root and isinstance(r.root, str) and r.root else os.path.join(DB, "..", "raw", r.dir)


ss["is_msm"] = [r.machine in mshort and not os.path.exists(os.path.join(sdir_of(r), "session.json")) for r in ss.itertuples()]
msm_ids = set(ss.loc[ss.is_msm, "session_id"])
ev_msm = ev[ev.session_id.isin(msm_ids)]


def read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


# S1 事件分域
cut = ev[ev.cut_state == "cutting"]
cut_msm = cut[cut.session_id.isin(msm_ids)]
want = paths.groupby("machine").size().rename(index=mshort)
got = cut_msm.merge(ses[["session_id", "machine"]], on="session_id").groupby("machine").size()
dom_ok = set(ev_msm.loc[ev_msm.session_id.str.startswith("vf10"), "domain"]) == {"line"} and \
    set(ev_msm.loc[~ev_msm.session_id.str.startswith("vf10"), "domain"]) == {"lab"}
check("S1 cutting events per machine == label rows", want.sort_index().equals(got.sort_index()) and dom_ok,
      f"want {dict(want)} got {dict(got)}")
# 正規化場次（逐場）：cutting 事件數＝label.csv 列數＋側檔 cut_state=cutting 列數；unknown 事件數＝側檔 unknown 列數
n_cut_got = cut.groupby("session_id").size()
n_unk_got = ev[ev.cut_state == "unknown"].groupby("session_id").size()
bad_norm, n_label_rows, n_unl_rows, n_unk_rows = [], 0, 0, 0
for r in ss[~ss.is_msm].itertuples():
    d = sdir_of(r)
    nl = len(read_rows(os.path.join(d, "label.csv")))
    side = [x for f in os.listdir(d) if f.endswith("_unlabeled.csv") for x in read_rows(os.path.join(d, f))]
    nu, nk = sum(x.get("cut_state") == "cutting" for x in side), sum(x.get("cut_state") == "unknown" for x in side)
    n_label_rows += nl
    n_unl_rows += nu
    n_unk_rows += nk
    got = (int(n_cut_got.get(r.session_id, 0)), int(n_unk_got.get(r.session_id, 0)))
    if got != (nl + nu, nk):
        bad_norm.append((r.session_id, got, nl, nu, nk))
if (~ss.is_msm).any():
    check("S1 normalized sessions: cutting events == label.csv rows + sidecar cutting rows, unknown == sidecar unknown rows",
          not bad_norm, f"{int((~ss.is_msm).sum())} sessions, {n_label_rows} label rows + {n_unl_rows} sidecar cutting rows, "
          f"{n_unk_rows} sidecar unknown rows; bad {bad_norm[:3]}")

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
cm = cut_msm.merge(key, on="session_id").merge(paths, left_on=["machine", "dataset", "start", "end"],
                                         right_on=["machine", "dataset", "start", "end"], suffixes=("", "_inv"))
diff = (cm.clip_ratio - cm.clip_ratio_inv).abs().max()
check("S6 clip_ratio == inventory paths.csv", len(cm) == len(cut_msm) and diff < 1e-5, f"matched {len(cm)}/{len(cut_msm)}, max diff {diff:.2e}")
aucs = {}
for mach, g in m[(m.cut_state == "cutting") & m.session_id.isin(msm_ids)].merge(ses[["session_id", "machine"]], on="session_id").groupby("machine"):
    g = g[g.label_std.isin([0, 1])]
    aucs[mach] = round(roc_auc_score(g.label_std == 1, g.clip_ratio), 3)
check("S6 clip AUC reproduces 0.991/0.859/0.571", aucs == {"vm20i": 0.991, "vmx30ui": 0.859, "vf10": 0.571}, str(aucs))
check("S6 rms_dbfs finite for all events", np.isfinite(ev.rms_dbfs).all())

# S7 對齊
al = pd.read_sql("select * from alignment", main)
low = al[al.conf < 0.3]
check("S7 alignment row for every session", set(al.session_id) == set(ses.session_id),
      f"{len(al)} rows; conf<0.3: {len(low)} sessions, first {list(low.session_id)[:6]}")

# S8 負功率
ctx = pd.read_sql("select session_id, power_w from context_ts", main)
check("S8 no negative power_w", int((ctx.power_w < 0).sum()) == 0)
raw_neg = 0
for r in ss[(ss.machine == "imi_vmx30ui") & ss.is_msm].itertuples():
    d = sdir_of(r)  # 庫有 root 欄就用登記的根目錄，否則沿用 <db>/../raw
    f = [x for x in os.listdir(d) if x.endswith("_mtc.csv")][0]
    w = pd.to_numeric(pd.read_csv(os.path.join(d, f), usecols=["watt"]).watt, errors="coerce")
    raw_neg += int((w < 0).sum())
check("S8 raw negative watt count == 579", raw_neg == 579, f"raw {raw_neg}")

# S9 控制器時序欄位
want_ts = ["session_id", "t", "spindle_speed_rpm", "feed_rate", "spindle_load", "x_load", "y_load", "z_load", "power_w",
           "s_vrms", "x_vrms", "y_vrms", "z_vrms", "executing"]
check("S9 context_ts columns normalized", sorted(cols["context_ts"]) == sorted(want_ts), str(cols["context_ts"]))
vf = pd.read_sql("select * from context_ts where session_id like 'vf10%'", main)
check("S9 VF-10 has spindle speed/load, no feed/power", vf.spindle_load.notna().all() and vf.feed_rate.isna().all() and vf.power_w.isna().all())

# S10 不透明編號：MSM 用固定短名；正規化場次用該資料夾 session.json 的 machine_short（多根目錄的庫也能驗）
def short_of(r):
    if r.is_msm:
        return mshort[r.machine]
    with open(os.path.join(sdir_of(r), "session.json"), encoding="utf-8") as fh:
        return json.load(fh)["machine_short"]


recomputed = {f"{short_of(r)}_{hashlib.sha1(f'{r.machine}/dataset{r.dataset}'.encode()).hexdigest()[:6]}" for r in ss.itertuples()}
check("S10 opaque session ids reproducible, unique", recomputed == set(ses.session_id) and ses.session_id.is_unique,
      f"{len(recomputed)} recomputed ({int(ss.is_msm.sum())} MSM, {int((~ss.is_msm).sum())} normalized)")

# S13 多根目錄（--extra-raw）：每場 session_source.root + dir 指到存在的場次資料夾
if "root" in ss.columns:
    miss = [r.session_id for r in ss.itertuples() if not os.path.isdir(os.path.join(r.root, r.dir))]
    check("S13 session_source.root/dir resolves to every session folder", not miss, f"{ss.root.nunique()} root(s); missing {miss[:3]}")
else:
    print("SKIP S13 (db has no session_source.root column)")

# S14 標籤值域：切削事件 ∈ {0,1,2,-1}（-1＝未標註切削，只來自正規化側檔）；切削狀態未知全為 -1（也只來自側檔）；
# 非切削全為 -1；cut_state 只有這三種
cut_l, nc_l, unk_l = m[m.cut_state == "cutting"], m[m.cut_state == "non_cutting"], m[m.cut_state == "unknown"]
cut_unl = cut_l[cut_l.label_std == -1]
check("S14 cutting labels in {0,1,2,-1}, unknown all -1, non_cutting all -1, -1 cutting/unknown == sidecar rows",
      set(cut_l.label_std.unique()) <= {0, 1, 2, -1} and (nc_l.label_std == -1).all() and (unk_l.label_std == -1).all()
      and set(m.cut_state.unique()) <= {"cutting", "non_cutting", "unknown"}
      and len(cut_unl) == n_unl_rows and len(unk_l) == n_unk_rows
      and not cut_unl.session_id.isin(msm_ids).any() and not unk_l.session_id.isin(msm_ids).any(),
      f"cutting labels {sorted(int(v) for v in cut_l.label_std.unique())}, unlabeled cutting {len(cut_unl)} "
      f"(sidecar cutting rows {n_unl_rows}), unknown {len(unk_l)} labels {sorted(int(v) for v in unk_l.label_std.unique())} "
      f"(sidecar unknown rows {n_unk_rows}), non_cutting {len(nc_l)}")


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
