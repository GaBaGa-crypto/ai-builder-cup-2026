"""Step 2: build the three MSM databases (spec: design/bdd-step2-build-db.md).

Usage: python build_db.py <raw_dir> <out_dir> [--extra-raw DIR ...]
Every root (raw_dir and each --extra-raw DIR) has the layout <root>/<machine>/dataset<N>/. A dataset folder holding a
session.json (normalizer output) takes machine short name, domain, coolant, workpiece and remark from it instead of the
MSM dictionaries below; dataset_summary.xlsx is then optional. All roots are validated before anything is written.
Writes
  <out>/main.sqlite              events, sessions, context_ts, alignment; no labels, no source info
  <out>/source/source.sqlite     opaque-id map (+ root dir of each session), program names, NC text, remarks, raw tool strings (0700/0600)
  <out>/sealed/labels.sqlite     label_std, label_native, tool_defective per event (0700/0600)
  <out>/features_allowlist.json  the only columns a model or agent tool may read
Only reads the roots. Rebuilds the outputs from scratch on every run.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys

import numpy as np
import pandas as pd

from msm_io import (clip_ratio_array, find, iter_sessions, read_labels, read_mtc, read_session_json, read_summary,
                    read_unlabeled, read_wav_int16, wav_info)

SHORT = {"imi_vm20i": "vm20i", "imi_vmx30ui": "vmx30ui", "tmf_vf10": "vf10"}
DOMAIN = {"imi_vm20i": "lab", "imi_vmx30ui": "lab", "tmf_vf10": "line"}
COOLANT = {"lab": "wet", "line": "dry"}  # MSM 論文：實驗室濕切、產線乾切
SHORT_RE = re.compile(r"^[a-z0-9]+$")  # session_id 前綴；/audio/ 路徑守門只接受這種字元
DOMAINS, COOLANTS = {"lab", "line", "unknown"}, {"wet", "dry", "unknown"}  # session.json 合約（harness scenarios.md）
MIN_GAP_S = 0.5  # 未標時間至少這麼長才成為非切削事件
UNLABELED_CUT_STATES = {"cutting", "unknown"}  # 側檔 cut_state：切削但標籤未定／切削狀態未知；兩者標籤都是 -1
END_TOL_S = 0.05  # 側檔迄秒可超過音長的容許量（同 validate.py tolerance_s）

# 控制器欄位正規化：標準名 -> 各機台可能的原欄名
MTC_MAP = {
    "spindle_speed_rpm": ["Spindle_Speed", "s_speed"],
    "feed_rate": ["Feed_Rate"],
    "spindle_load": ["S_Axis_Load", "s_load"],
    "x_load": ["X_Axis_Load"], "y_load": ["Y_Axis_Load"], "z_load": ["Z_Axis_Load"],
    "power_w": ["watt"],
    "s_vrms": ["smvrms"], "x_vrms": ["xmvrms"], "y_vrms": ["ymvrms"], "z_vrms": ["zmvrms"],
}
EXEC_COLS = ["Execution", "execution"]
# 只進來源庫的控制器欄（會洩漏實驗設計或刀具身分）
MTC_SOURCE = {"program": ["Program_Name_Running", "Program_Name_Editing", "program"],
              "tool_number": ["Current_Tool", "tool_num"], "block": ["Block_Number", "block_num"]}

CONTEXT_STATIC = ["N", "F", "ap", "ae", "direction", "Z", "D", "material", "tool_class", "coolant"]
REACTION_TS = ["spindle_load", "x_load", "y_load", "z_load", "power_w", "s_vrms", "x_vrms", "y_vrms", "z_vrms"]
CONTEXT_TS = ["spindle_speed_rpm", "feed_rate", "executing"]
TRIVIAL = ["clip_ratio", "rms_dbfs", "dur"]
ID_TIME = ["event_id", "session_id", "idx", "start", "end", "domain", "cut_state", "machine", "t"]

ENV_HOP_S = 0.1   # 對齊用包絡的時間格
MAX_LAG_S = 6.0


def session_id(machine, ds, short=None):
    """短名＋SHA1(machine/datasetN) 前 6 碼；MSM 的雜湊輸入不變，正規化場次只換短名（session.json machine_short）。"""
    return f"{short or SHORT[machine]}_{hashlib.sha1(f'{machine}/dataset{ds}'.encode()).hexdigest()[:6]}"


def num_or_none(v, cast=float):
    """label.csv 數值格；空白、缺欄、nan／inf、非數字 -> None（來源未提供或不可用的工況留空，不得編造）。"""
    v = (v or "").strip()
    try:
        f = float(v)
    except ValueError:  # 空白或非數字
        return None
    return cast(f) if np.isfinite(f) else None


def text_or_none(v):
    return (v or "").strip() or None


def check_unlabeled(sdir, sid):
    """正規化側檔 <session_id>_unlabeled.csv 的建庫前檢查（validate.py 同一套規則）：起訖為有限數字、start < end、
    迄秒不超過音長＋0.05 秒、cut_state ∈ {cutting, unknown}、不與 label.csv 列或其他側檔列重疊。錯就 ValueError（指名場次與列）。"""
    rows = read_unlabeled(sdir)
    if not rows:
        return
    s0 = find(sdir, "_s0.wav")
    if s0 is not None:
        sr, _ch, _w, nframes = wav_info(s0)
        dur = nframes / sr
    else:
        dur = None  # 沒有 s0 由主迴圈報錯
    labelled = [(s, e) for r in read_labels(sdir) for s, e in [(num_or_none(r.get("start")), num_or_none(r.get("end")))]
                if s is not None and e is not None]
    where, spans = f"{sid} ({find(sdir, '_unlabeled.csv')})", []
    for n, r in enumerate(rows, 1):
        s, e = num_or_none(r.get("start")), num_or_none(r.get("end"))
        if s is None or e is None:
            raise ValueError(f"{where} row {n}: start/end must be finite numbers, got {r.get('start')!r}, {r.get('end')!r}")
        if (r.get("cut_state") or "").strip() not in UNLABELED_CUT_STATES:
            raise ValueError(f"{where} row {n}: cut_state must be one of {sorted(UNLABELED_CUT_STATES)}, got {r.get('cut_state')!r}")
        if not s < e:
            raise ValueError(f"{where} row {n}: start {s} >= end {e}")
        if dur is not None and e > dur + END_TOL_S:
            raise ValueError(f"{where} row {n}: end {e} beyond audio duration {dur:.3f} s")
        hit = next(((ls, le) for ls, le in labelled if s < le - 1e-6 and ls < e - 1e-6), None)
        if hit:
            raise ValueError(f"{where} row {n}: span {s}-{e} overlaps label.csv row {hit[0]}-{hit[1]}")
        hit = next(((pn, ps, pe) for pn, ps, pe in spans if s < pe - 1e-6 and ps < e - 1e-6), None)
        if hit:
            raise ValueError(f"{where} row {n}: span {s}-{e} overlaps sidecar row {hit[0]} ({hit[1]}-{hit[2]})")
        spans.append((n, s, e))


def plan_sessions(roots):
    """Every session of every root as (root, machine, ds, sdir, sid, meta, short, domain, coolant), validated before any
    audio is read or anything is written: missing root, bad session.json machine_short/domain/coolant, duplicate
    session_id or machine/dataset, bad <session_id>_unlabeled.csv (check_unlabeled)."""
    for root in roots:
        if not os.path.isdir(root):
            raise ValueError(f"root directory not found: {root}")
    plan, seen = [], {}
    for root in roots:
        for machine, ds, sdir in iter_sessions(root):
            meta = read_session_json(sdir)
            if meta is None:
                if machine not in SHORT:
                    raise ValueError(f"{sdir}: no session.json and '{machine}' is not an MSM machine {sorted(SHORT)}")
                short, dom = SHORT[machine], DOMAIN[machine]
                cool = COOLANT[dom]
            else:
                where = os.path.join(sdir, "session.json")
                short = meta.get("machine_short")
                if not isinstance(short, str) or not SHORT_RE.fullmatch(short):
                    raise ValueError(f"{where}: field 'machine_short' must match ^[a-z0-9]+$, got {short!r}")
                dom, cool = meta.get("domain") or "unknown", meta.get("coolant") or "unknown"
                if dom not in DOMAINS:
                    raise ValueError(f"{where}: field 'domain' must be one of {sorted(DOMAINS)}, got {dom!r}")
                if cool not in COOLANTS:
                    raise ValueError(f"{where}: field 'coolant' must be one of {sorted(COOLANTS)}, got {cool!r}")
            sid = session_id(machine, ds, short)
            for kind, key in (("session_id", sid), ("machine/dataset", (machine, ds))):
                # 驗證頁以 machine＋dataset 取場次、音檔以 session_id 取，兩者都必須唯一
                if key in seen:
                    raise ValueError(f"duplicate {kind} {key}: {seen[key]} and {sdir}")
                seen[key] = sdir
            check_unlabeled(sdir, sid)
            plan.append((root, machine, ds, sdir, sid, meta, short, dom, cool))
    return plan


def first_col(df, names):
    return next((n for n in names if n in df.columns), None)


def rms_dbfs(x, sr, s, e):
    y = x[int(s * sr):int(e * sr)].astype(np.float64) / 32768.0
    return float(20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-12)) if len(y) else float("nan")


def normalize_mtc(mtc):
    out = pd.DataFrame({"t": pd.to_numeric(mtc["time"], errors="coerce")})
    for std, names in MTC_MAP.items():
        c = first_col(mtc, names)
        out[std] = pd.to_numeric(mtc[c], errors="coerce") if c else np.nan
    out.loc[out["power_w"] < 0, "power_w"] = np.nan  # I37：負功率設缺值
    c = first_col(mtc, EXEC_COLS)
    out["executing"] = (mtc[c].astype(str).str.upper() == "ACTIVE").astype(int) if c else np.nan
    return out


ANCHORS = ["spindle_speed_rpm", "spindle_load"]  # 主軸啟停／換速（實驗室濕切鋁件負載近乎平坦），與負載變化（產線）


def estimate_alignment(x, sr, ctx):
    """Best of estimate_alignment_on() over ANCHORS (highest conf); records which anchor was used."""
    best = None
    for col in ANCHORS:
        r = estimate_alignment_on(x, sr, ctx, col)
        if best is None or r["conf"] > best["conf"]:
            best = dict(r, anchor=col)
    return best


def estimate_alignment_on(x, sr, ctx, col):
    """Cross-correlate audio log-RMS envelope with one controller column, separately in each half (I29).

    offset_s > 0 means the audio change happens offset_s seconds after the controller change.
    Records only; the time axis is not corrected here.
    """
    hop = int(sr * ENV_HOP_S)
    n = len(x) // hop
    env = np.log10(np.sqrt(np.mean((x[:n * hop].astype(np.float64) / 32768.0).reshape(n, hop) ** 2, axis=1)) + 1e-6)
    t = (np.arange(n) + 0.5) * ENV_HOP_S
    load = ctx[["t", col]].dropna().rename(columns={col: "v"})
    if len(load) < 10:
        return dict(offset_head_s=None, offset_tail_s=None, drift_s_per_h=None, conf=0.0, halves_used=0, at_lag_bound=0)
    # 控制器值保持到下一筆前（零階保持），不做線性內插
    ld = load["v"].values[np.clip(np.searchsorted(load["t"].values, t, side="right") - 1, 0, len(load) - 1)]
    L = int(MAX_LAG_S / ENV_HOP_S)
    res = []
    for a, b in [(0, n // 2), (n // 2, n)]:
        best = (np.nan, -np.inf)
        for lag in range(-L, L + 1):
            i0, i1 = max(a, a + lag), min(b, b + lag)  # 音訊索引 i 對應控制器索引 i - lag
            if i1 - i0 < 50:
                continue
            ea, lb = env[i0:i1], ld[i0 - lag:i1 - lag]
            if ea.std() == 0 or lb.std() == 0:
                continue
            r = float(np.corrcoef(ea, lb)[0, 1])
            if r > best[1]:
                best = (lag * ENV_HOP_S, r)
        res.append((best[0], best[1], (a + b) / 2 * ENV_HOP_S))
    (oh, rh, th), (ot, rt, tt) = res
    # 錨點在某半段完全不變＝該半段無資訊，不等於沒對齊；信心只取有資訊的半段
    rs = [r for r in (rh, rt) if np.isfinite(r)]
    drift = (ot - oh) / (tt - th) * 3600 if np.isfinite(oh) and np.isfinite(ot) else None
    return dict(offset_head_s=None if not np.isfinite(oh) else round(oh, 2),
                offset_tail_s=None if not np.isfinite(ot) else round(ot, 2),
                drift_s_per_h=None if drift is None else round(drift, 2),
                conf=round(min(rs), 4) if rs else 0.0, halves_used=len(rs),
                at_lag_bound=int(any(np.isfinite(o) and abs(o) >= MAX_LAG_S - 1e-9 for o in (oh, ot))))


def fresh_db(path, private):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    if private:
        os.chmod(d, 0o700)
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    if private:
        os.chmod(path, 0o600)
    return con


def main():
    ap = argparse.ArgumentParser(description="Build main / source / sealed databases from <root>/<machine>/dataset<N>/ roots.")
    ap.add_argument("raw_dir")
    ap.add_argument("out_dir")
    ap.add_argument("--extra-raw", nargs="+", action="extend", default=[], metavar="DIR",
                    help="additional roots with the same layout (e.g. normalizer output <out>/<source_id>)")
    a = ap.parse_args()
    raw, out = a.raw_dir, a.out_dir
    roots = [raw] + a.extra_raw
    try:
        plan = plan_sessions(roots)  # 先驗全部根目錄，錯了就不產生任何庫
    except ValueError as e:
        sys.exit(f"build_db: {e}")
    summaries = {root: read_summary(root) for root in roots}
    sessions, events, ctx_all, align = [], [], [], []
    src_sessions, src_events, labels = [], [], []

    for root, machine, ds, sdir, sid, meta, short, dom, cool in plan:
        s0 = find(sdir, "_s0.wav")
        x, sr = read_wav_int16(s0)
        dur = len(x) / sr
        rows = sorted(read_labels(sdir), key=lambda r: float(r["start"]))
        # 未標註（正規化側檔 <session_id>_unlabeled.csv：start,end,cut_state）：cut_state 照抄（cutting＝切削但標籤未定、
        # unknown＝切削狀態未知），標籤 -1，不當非切削空隙；MSM 原始資料沒有側檔 → rows 不變，三庫逐列同舊版
        unl = [dict(r, unlabeled=True) for r in read_unlabeled(sdir)]
        if unl:
            rows = sorted(rows + unl, key=lambda r: float(r["start"]))

        # 切削事件（label.csv 每列＋未標註切削）與其間的非切削事件
        evs, cur = [], 0.0
        material = next((text_or_none(r.get("Workpiece")) for r in rows if not r.get("unlabeled")), None)
        for r in rows:
            s, e = float(r["start"]), float(r["end"])
            if s - cur >= MIN_GAP_S:
                evs.append(dict(start=cur, end=s, cut_state="non_cutting", label_std=-1, label_native=None, tool_raw=None))
            if r.get("unlabeled"):  # 工況與刀具不編造：只有起訖，material 由場次預設補
                evs.append(dict(start=s, end=e, cut_state=r["cut_state"].strip(), label_std=-1, label_native=None, tool_raw=None))
                cur = max(cur, e)
                continue
            tool_raw = text_or_none(r.get("Tool"))  # 空白＝來源未提供：tool_defective 也留空
            evs.append(dict(start=s, end=e, cut_state="cutting", label_std=int(r["label"]), label_native=r["label"].strip(),
                            tool_raw=tool_raw, N=num_or_none(r.get("N")), F=num_or_none(r.get("F")), ap=num_or_none(r.get("ap")),
                            ae=num_or_none(r.get("ae")), direction=text_or_none(r.get("direction")), Z=num_or_none(r.get("Z"), int),
                            D=num_or_none(r.get("D")), material=text_or_none(r.get("Workpiece")),
                            tool_class=tool_raw.replace("*", "") if tool_raw else None))
            cur = max(cur, e)
        if dur - cur >= MIN_GAP_S:
            evs.append(dict(start=cur, end=dur, cut_state="non_cutting", label_std=-1, label_native=None, tool_raw=None))

        clips = clip_ratio_array(x, sr, [(v["start"], v["end"]) for v in evs])
        for k, (v, c) in enumerate(zip(evs, clips)):
            eid = f"{sid}_{k:04d}"
            events.append(dict(event_id=eid, session_id=sid, idx=k, start=round(v["start"], 3), end=round(v["end"], 3),
                               dur=round(v["end"] - v["start"], 3), domain=dom, cut_state=v["cut_state"],
                               **{f: v.get(f) for f in CONTEXT_STATIC if f not in ("coolant", "material")},
                               material=v.get("material", material), coolant=cool,
                               clip_ratio=round(c, 6), rms_dbfs=round(rms_dbfs(x, sr, v["start"], v["end"]), 3)))
            labels.append(dict(event_id=eid, label_std=v["label_std"], label_native=v["label_native"],
                               tool_defective=None if v["tool_raw"] is None else int("*" in v["tool_raw"])))
            if v["tool_raw"] is not None:
                src_events.append(dict(event_id=eid, tool_raw=v["tool_raw"]))

        mtc = read_mtc(sdir)
        if mtc is not None:
            ctx = normalize_mtc(mtc)
            ctx.insert(0, "session_id", sid)
            ctx_all.append(ctx)
            al = estimate_alignment(x, sr, ctx)
        else:
            al = dict(offset_head_s=None, offset_tail_s=None, drift_s_per_h=None, conf=0.0, halves_used=0, at_lag_bound=0, anchor=None)
        align.append(dict(session_id=sid, nominal_offset_s=0.0, **al))

        nc_files = sorted(glob.glob(os.path.join(sdir, "*.nc")))
        nc_text = "\n".join(f"### {os.path.basename(p)}\n" + open(p, encoding="utf-8", errors="replace").read() for p in nc_files)
        sm = dict(summaries[root].get((machine, ds), {}))
        if meta is not None:  # 正規化場次：工件與備註取自 session.json（該根目錄可以沒有 dataset_summary.xlsx）
            sm.update(workpiece=meta.get("workpiece") or "", remark=str(meta.get("remark") or ""))
        src = dict(session_id=sid, machine=machine, dataset=ds, dir=os.path.relpath(sdir, root),
                   files="|".join(sorted(f for f in os.listdir(sdir) if not f.endswith(".md5"))),
                   workpiece=str(sm.get("workpiece") or ""), tool_summary=sm.get("tool_summary", ""), remark=sm.get("remark", ""),
                   nc_files="|".join(os.path.basename(p) for p in nc_files), nc_text=nc_text, root=os.path.abspath(root))
        for key, names in MTC_SOURCE.items():
            c = first_col(mtc, names) if mtc is not None else None
            src[key] = "|".join(sorted(map(str, mtc[c].dropna().unique()))) if c else ""
        src_sessions.append(src)

        sessions.append(dict(session_id=sid, machine=short, domain=dom, coolant=cool,
                             s0_sec=round(dur, 3), sr=sr, has_s1=int(find(sdir, "_s1.wav") is not None),
                             has_s2=int(find(sdir, "_s2.wav") is not None), has_mtc=int(mtc is not None),
                             has_ct=int(find(sdir, "_ct.csv") is not None),
                             n_cut=sum(v["cut_state"] != "non_cutting" for v in evs),  # cutting＋unknown（MSM 只有 cutting）
                             n_noncut=sum(v["cut_state"] == "non_cutting" for v in evs)))
        print(f"{sid} {machine}/dataset{ds}: {len(evs)} events, align conf {al['conf']}", flush=True)

    con = fresh_db(os.path.join(out, "main.sqlite"), private=False)
    pd.DataFrame(sessions).to_sql("sessions", con, index=False)
    pd.DataFrame(events).to_sql("events", con, index=False)
    ctx_cols = ["session_id", "t", *MTC_MAP, "executing"]  # 全部場次都沒有控制器檔時仍建同欄位的空表
    (pd.concat(ctx_all, ignore_index=True) if ctx_all else pd.DataFrame(columns=ctx_cols)).to_sql("context_ts", con, index=False)
    pd.DataFrame(align).to_sql("alignment", con, index=False)
    con.execute("CREATE INDEX ix_ctx ON context_ts(session_id, t)")
    con.commit(); con.close()

    con = fresh_db(os.path.join(out, "source", "source.sqlite"), private=True)
    pd.DataFrame(src_sessions).to_sql("session_source", con, index=False)
    pd.DataFrame(src_events, columns=["event_id", "tool_raw"]).to_sql("event_source", con, index=False)  # 來源全無刀具字串時仍建表
    con.commit(); con.close()

    con = fresh_db(os.path.join(out, "sealed", "labels.sqlite"), private=True)
    pd.DataFrame(labels).to_sql("labels", con, index=False)
    con.commit(); con.close()

    allow = dict(
        spec="design/bdd-step2-build-db.md; design/04 §11.1",
        context_static=CONTEXT_STATIC, context_ts=CONTEXT_TS,
        reaction_ts=REACTION_TS, reaction_note="只給觀察者二與 C2；不得當工況輸入 A／C1",
        trivial_baseline_T=TRIVIAL, id_time=ID_TIME,
        never=["program", "nc_text", "remark", "tool_raw", "tool_number", "block", "tool_defective",
               "label_std", "label_native", "file names", "dataset number"])
    with open(os.path.join(out, "features_allowlist.json"), "w", encoding="utf-8") as fh:
        json.dump(allow, fh, ensure_ascii=False, indent=2)
    print(f"done: {len(sessions)} sessions, {len(events)} events -> {out}")


if __name__ == "__main__":
    main()
