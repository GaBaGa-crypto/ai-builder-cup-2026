"""Alignment figure: Sound0 spectrogram + MTConnect spindle/feed + label.csv segments on one timeline.
Usage: python align_plot.py <session_dir> <out_png>
session_dir must contain *_s0.wav, *_mtc.csv, label.csv (MSM layout).
"""
import sys, glob, os, csv, wave
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from scipy.signal import stft

sess, out = sys.argv[1], sys.argv[2]
wav = glob.glob(os.path.join(sess, "*_s0.wav"))[0]
mtc = glob.glob(os.path.join(sess, "*_mtc.csv"))
lab = os.path.join(sess, "label.csv")

w = wave.open(wav); sr = w.getframerate(); n = w.getnframes()
x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
dur = n / sr
f, t, Z = stft(x, fs=sr, nperseg=2048, noverlap=1024)
S = 20 * np.log10(np.abs(Z) + 1e-9)
fmax = 12000; m = f <= fmax

rows = list(csv.DictReader(open(lab, encoding="utf-8-sig")))
labels = [(float(r["start"]), float(r["end"]), int(r["label"]), r) for r in rows]
colors = {0: "tab:green", 1: "tab:red", 2: "tab:orange"}

nrow = 3 if mtc else 2
fig, ax = plt.subplots(nrow, 1, figsize=(16, 9), sharex=True,
                       gridspec_kw={"height_ratios": [3, 1, 1][:nrow]})
ax[0].pcolormesh(t, f[m] / 1000, S[m], shading="auto", cmap="magma",
                 vmin=np.percentile(S[m], 5), vmax=np.percentile(S[m], 99.5))
ax[0].set_ylabel("kHz"); ax[0].set_title(f"Sound0 spectrogram  {os.path.basename(wav)}  ({sr} Hz, {dur:.0f} s)")
for s, e, l, r in labels:
    for a in ax:
        a.axvspan(s, e, color=colors.get(l, "gray"), alpha=0.25 if a is ax[0] else 0.35, lw=0)
    N = float(r.get("N", 0)); Zf = float(r.get("Z", 0))
    if N and Zf:
        tpf = N * Zf / 60.0
        ax[0].hlines([tpf / 1000 * k for k in (1, 2, 3)], s, e, colors="cyan", lw=0.6, alpha=0.8)
    ax[0].text((s + e) / 2, fmax / 1000 * 0.95, f"L{l}", ha="center", va="top", fontsize=8, color="white")

if mtc:
    mt = list(csv.DictReader(open(mtc[0], encoding="utf-8-sig")))
    tt = np.array([float(r["time"]) for r in mt])
    def col(name):
        v = []
        for r in mt:
            try: v.append(float(r.get(name, "nan")))
            except ValueError: v.append(np.nan)
        return np.array(v)
    ax[1].step(tt, col("Spindle_Speed"), where="post", color="k"); ax[1].set_ylabel("Spindle rpm")
    ax[2].step(tt, col("Feed_Rate"), where="post", color="tab:blue"); ax[2].set_ylabel("Feed")
    ax2b = ax[2].twinx(); ax2b.step(tt, col("S_Axis_Load"), where="post", color="tab:purple", alpha=0.6); ax2b.set_ylabel("Spindle load", color="tab:purple")
else:
    rms = np.sqrt(np.convolve(x**2, np.ones(4800)/4800, mode="same"))
    ax[1].plot(np.arange(len(rms))/sr, 20*np.log10(rms+1e-9), color="k", lw=0.5); ax[1].set_ylabel("RMS dB")
ax[-1].set_xlabel("time (s)"); ax[-1].set_xlim(0, dur)
from matplotlib.patches import Patch
ax[0].legend(handles=[Patch(color=colors[k], alpha=0.5, label=f"label {k}") for k in sorted(colors)] +
             [plt.Line2D([], [], color="cyan", lw=1, label="tooth-passing freq x1..3")], loc="upper right", fontsize=8)
fig.tight_layout(); fig.savefig(out, dpi=110)
labelled = sum(e - s for s, e, _, _ in labels)
print(f"saved {out}; duration {dur:.1f}s; labelled {labelled:.1f}s in {len(labels)} paths; labels {sorted(set(l for _,_,l,_ in labels))}; mtc rows {len(mt) if mtc else 0}")
