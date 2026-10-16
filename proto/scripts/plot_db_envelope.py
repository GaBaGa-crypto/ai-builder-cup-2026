"""Per-session sound level plots: x = time (s), y = level (dBFS, 100 ms RMS), one panel per microphone.

Usage: python plot_db_envelope.py <db_dir> <raw_dir> <out_dir>
Writes <out_dir>/<machine>_ds<NN>.png, sessions.csv (conditions per session) and conditions.csv (per machine).
No labels are read or drawn (sealed until step 7). Lab sessions shade every cutting path in grey;
VF-10 events are labelled intervals whose boundaries would reveal anomalies, so VF-10 shows only
the controller's spindle-running band instead.
"""
import os
import sqlite3
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from msm_io import find, read_wav_int16

FRAME_S = 0.1
CHANNELS = [("s0", "Sound0 空氣麥克風"), ("s1", "Sound1 聽診器"), ("s2", "Sound2 聽診器")]
plt.rcParams["font.family"] = ["Noto Sans CJK TC", "Noto Sans CJK JP", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def level_dbfs(x, sr):
    hop = int(sr * FRAME_S)
    n = len(x) // hop
    y = (x[:n * hop].astype(np.float64) / 32768.0).reshape(n, hop)
    return (np.arange(n) + 0.5) * FRAME_S, 20 * np.log10(np.sqrt(np.mean(y ** 2, axis=1)) + 1e-5)


def rng(s):
    s = s.dropna()
    if s.empty:
        return ""
    lo, hi = s.min(), s.max()
    f = lambda v: f"{v:g}"
    return f(lo) if lo == hi else f"{f(lo)}–{f(hi)}"


def main():
    db, raw, out = sys.argv[1], sys.argv[2], sys.argv[3]
    os.makedirs(out, exist_ok=True)
    m = sqlite3.connect(os.path.join(db, "main.sqlite"))
    ses = pd.read_sql("select * from sessions", m).merge(
        pd.read_sql("select session_id, dataset, dir from session_source", sqlite3.connect(os.path.join(db, "source", "source.sqlite"))),
        on="session_id").sort_values(["machine", "dataset"])
    ev = pd.read_sql("select * from events where cut_state='cutting'", m)
    ctx = pd.read_sql("select session_id, t, spindle_speed_rpm from context_ts", m)

    rows = []
    for s in ses.itertuples():
        sdir = os.path.join(raw, s.dir)
        e = ev[ev.session_id == s.session_id]
        chans = [(k, name, find(sdir, f"_{k}.wav")) for k, name in CHANNELS]
        chans = [c for c in chans if c[2]]
        fig, axes = plt.subplots(len(chans), 1, figsize=(15, 2.3 * len(chans) + 0.9), sharex=True, squeeze=False)
        stats = {}
        for ax, (k, name, path) in zip(axes[:, 0], chans):
            x, sr = read_wav_int16(path)
            t, db_ = level_dbfs(x, sr)
            if s.domain == "lab":
                for r in e.itertuples():
                    ax.axvspan(r.start, r.end, color="0.82", lw=0, zorder=0)
            else:
                c = ctx[ctx.session_id == s.session_id].sort_values("t")
                run = c.spindle_speed_rpm.fillna(0).values > 0
                tt = np.append(c.t.values, c.t.values[-1] + 0.1)
                for i in np.flatnonzero(run):
                    ax.axvspan(tt[i], tt[i + 1], ymin=0, ymax=0.04, color="0.45", lw=0, zorder=0)
            ax.plot(t, db_, lw=0.5, color="#2b5c7a", zorder=2)
            ax.axhline(0, color="#ad3434", lw=0.6, ls="--", zorder=1)
            ax.set_ylim(-80, 3)
            ax.set_ylabel(f"{name}\ndBFS", fontsize=8)
            ax.grid(axis="y", color="0.9", lw=0.5)
            stats[k] = (np.percentile(db_, 50), np.percentile(db_, 99))
        axes[-1, 0].set_xlabel("時間（秒）")
        axes[-1, 0].set_xlim(0, s.s0_sec)
        shade = "灰底＝切削路徑" if s.domain == "lab" else "底部深灰細帶＝主軸運轉（控制器）"
        cond = (f"材料 {', '.join(sorted(e.material.unique()))}｜刀具 {', '.join(sorted(e.tool_class.unique()))}｜"
                f"N {rng(e.N)} rpm｜F {rng(e.F)} mm/s｜ap {rng(e.ap)} mm｜ae {rng(e.ae)} mm")
        fig.suptitle(f"{s.machine}  dataset{s.dataset}  ({s.session_id})  {s.s0_sec/60:.1f} 分鐘  ·  {shade}\n{cond}",
                     fontsize=9, x=0.01, ha="left")
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        fn = f"{s.machine}_ds{s.dataset:02d}.png"
        fig.savefig(os.path.join(out, fn), dpi=80)
        plt.close(fig)
        rows.append(dict(machine=s.machine, dataset=s.dataset, session_id=s.session_id, minutes=round(s.s0_sec / 60, 1),
                         channels="/".join(k for k, _, _ in chans), n_paths=len(e), material=", ".join(sorted(e.material.unique())),
                         tools=", ".join(sorted(e.tool_class.unique())), N_rpm=rng(e.N), F_mm_s=rng(e.F), ap_mm=rng(e.ap),
                         ae_mm=rng(e.ae), Z=rng(e.Z), D_mm=rng(e.D), direction="/".join(sorted(e.direction.unique())),
                         coolant=s.coolant, s0_median_dbfs=round(stats["s0"][0], 1), s0_p99_dbfs=round(stats["s0"][1], 1), png=fn))
        print(fn, flush=True)
    pd.DataFrame(rows).to_csv(os.path.join(out, "sessions.csv"), index=False)

    agg = []
    for mach, g in ev.merge(ses[["session_id", "machine"]], on="session_id").groupby("machine"):
        agg.append(dict(machine=mach, sessions=int((ses.machine == mach).sum()), paths=len(g),
                        condition_combos=len(g.groupby(["N", "F", "ap", "ae", "Z", "D", "tool_class", "material"])),
                        N_values=g.N.nunique(), N_rpm=rng(g.N), F_values=g.F.nunique(), F_mm_s=rng(g.F),
                        ap_mm=", ".join(f"{v:g}" for v in sorted(g.ap.unique())), ae_mm=", ".join(f"{v:g}" for v in sorted(g.ae.unique())),
                        Z=", ".join(f"{v:g}" for v in sorted(g.Z.unique())), D_mm=", ".join(f"{v:g}" for v in sorted(g.D.unique())),
                        tools=", ".join(sorted(g.tool_class.unique())), materials=", ".join(sorted(g.material.unique())),
                        directions="/".join(sorted(g.direction.unique()))))
    pd.DataFrame(agg).to_csv(os.path.join(out, "conditions.csv"), index=False)


if __name__ == "__main__":
    main()
