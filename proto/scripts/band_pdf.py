"""Four-band PDF report: information first (machines, conditions, 175-file list), then one page of band plots per file.

Spec: design/bdd-band-trends.md (v3).
Usage: python band_pdf.py <db_dir> <raw_dir> <band_out_dir> <per_file_xlsx> <conditions_csv> <pdf_out_dir>
The file list is read from the per-file Excel so the two documents always agree. No labels;
VF-10 test sessions show the controller spindle band instead of labelled intervals.
"""
import html
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import openpyxl
import pandas as pd

from band_trends import BANDS, CH_NAME, HOP_S, SHORT, VF10_REF, db, load
from per_file_report import MIC, TYPE

# 四個頻帶各一個色相（色盲可分：藍、綠、橙、紫紅）
BAND_COLOR = {"0-4k": "#1f5f8b", "1-4k": "#2e8b57", "4-10k": "#d07a12", "10-24k": "#a23b72"}
BAND_LABEL = {"0-4k": "0–4 kHz", "1-4k": "1–4 kHz", "4-10k": "4–10 kHz", "10-24k": "10–24 kHz"}
E = html.escape


def read_excel_lists(xlsx):
    wb = openpyxl.load_workbook(xlsx, read_only=True)
    out = []
    for ws in wb.worksheets:
        rows = ws.iter_rows(min_row=5, values_only=True)
        hdr = list(next(rows))
        for r in rows:
            if not r[0]:
                break
            out.append(dict(zip(hdr, r), 機台=ws.title))
    return pd.DataFrame(out)


def plot_bands(s, t, ms, cut, ctx, path):
    k = int(round(1 / HOP_S)); nb = len(t) // k
    tt = t[:nb * k].reshape(nb, k).mean(1)
    fig, axes = plt.subplots(4, 1, figsize=(13, 7.0), sharex=True)
    for ax, (name, *_) in zip(axes, BANDS):
        y = db(ms[name][:nb * k].reshape(nb, k).mean(1))
        if s.domain == "lab":
            for r in cut.itertuples():
                ax.axvspan(r.start, r.end, color="0.9", lw=0, zorder=0)
        else:
            c = ctx.sort_values("t")
            run = c.spindle_speed_rpm.fillna(0).values > 0
            te = np.append(c.t.values, c.t.values[-1] + 0.1)
            for i in np.flatnonzero(run):
                ax.axvspan(te[i], te[i + 1], ymin=0, ymax=0.05, color="0.55", lw=0, zorder=0)
        ax.plot(tt, y, lw=0.8, color=BAND_COLOR[name], zorder=2)
        lo, hi = np.percentile(y, 1), y.max()
        ax.set_ylim(lo - 4, hi + 3)
        ax.set_ylabel("dB", fontsize=8)
        ax.text(0.005, 0.93, BAND_LABEL[name], transform=ax.transAxes, va="top", fontsize=10, fontweight="bold",
                color=BAND_COLOR[name], bbox=dict(facecolor="white", edgecolor="none", alpha=0.8, pad=1.5))
        ax.grid(axis="y", color="0.9", lw=0.5)
        ax.tick_params(labelsize=8)
    axes[-1].set_xlabel("時間（秒）；每秒能量平均", fontsize=9)
    axes[-1].set_xlim(0, s.s0_sec)
    fig.tight_layout(h_pad=0.4)
    fig.savefig(path, dpi=90)
    plt.close(fig)


def main():
    db_dir, raw, band_out, xlsx, cond_csv, out = sys.argv[1:7]
    cache, pngd = os.path.join(band_out, "cache"), os.path.join(out, "png")
    os.makedirs(pngd, exist_ok=True)
    m = sqlite3.connect(os.path.join(db_dir, "main.sqlite"))
    ses = pd.read_sql("select * from sessions", m).merge(
        pd.read_sql("select session_id, dataset, dir from session_source", sqlite3.connect(os.path.join(db_dir, "source", "source.sqlite"))),
        on="session_id").set_index("session_id")
    ev = pd.read_sql("select * from events where cut_state='cutting'", m)
    ctx_all = pd.read_sql("select session_id, t, spindle_speed_rpm from context_ts", m)
    L = read_excel_lists(xlsx)
    L.insert(0, "編號", range(1, len(L) + 1))

    figs = []
    for r in L.to_dict("records"):
        s = ses.loc[r["session_id"]]
        t, ms = load(SimpleNamespace(session_id=r["session_id"], dir=s.dir), raw, cache)[r["聲道"]]
        png = os.path.join(pngd, f"{r['編號']:03d}_{r['機台']}_{r['場次']}_{r['聲道']}.png")
        if not os.path.exists(png):
            plot_bands(s, t, ms, ev[ev.session_id == r["session_id"]], ctx_all[ctx_all.session_id == r["session_id"]], png)
        figs.append((r, os.path.basename(png), s.domain == "line" and int(s.dataset) not in VF10_REF))
        print(os.path.basename(png), flush=True)

    C = pd.read_csv(cond_csv).set_index("machine")
    mk = {"VM20i": "vm20i", "VMX30Ui": "vmx30ui", "VF-10": "vf10"}  # conditions.csv 用主庫的機台短名
    cnt = L.groupby("機台").size()
    cond_rows = "".join(
        f"<tr><th>{mname}</th><td>{E(TYPE[k_])}</td><td class='n'>{int(C.loc[mk[mname], 'sessions'])}</td><td class='n'>{int(cnt[mname])}</td>"
        f"<td class='n'>{int(C.loc[mk[mname], 'paths'])}</td><td class='n'>{int(C.loc[mk[mname], 'condition_combos'])}</td>"
        f"<td class='n'>{C.loc[mk[mname], 'N_rpm']}</td><td class='n'>{C.loc[mk[mname], 'F_mm_s']}</td>"
        f"<td class='n'>{E(str(C.loc[mk[mname], 'ap_mm']))}</td><td class='n'>{E(str(C.loc[mk[mname], 'ae_mm']))}</td>"
        f"<td class='n'>{E(str(C.loc[mk[mname], 'Z']))}</td><td class='n'>{E(str(C.loc[mk[mname], 'D_mm']))}</td>"
        f"<td>{E(C.loc[mk[mname], 'tools'])}</td><td>{E(C.loc[mk[mname], 'materials'])}</td></tr>"
        for k_, mname in [("vm20i", "VM20i"), ("vmx30ui", "VMX30Ui"), ("vf10", "VF-10")])
    cols = ["編號", "機台", "場次", "聲道", "麥克風", "冷卻", "長度秒", "路徑數", "材料", "刀具類別", "N rpm", "F mm/s", "ap mm", "ae mm",
            "Z 刃", "D mm", "方向", "全頻 dB", "0-4k dB", "1-4k dB", "4-10k dB", "10-24k dB"]
    fmt = lambda v: "" if v is None or (isinstance(v, float) and np.isnan(v)) else (f"{v:g}" if isinstance(v, float) else str(v))
    num = lambda c: c in ("編號", "長度秒", "路徑數") or "dB" in c
    list_rows = "".join("<tr>" + "".join(f"<td{' class=n' if num(c) else ''}>{E(fmt(r[c]))}</td>" for c in cols) + "</tr>"
                        for r in L[cols].to_dict("records"))
    pages = "".join(
        f"<section class='fig'><div class='cap'><span class='no'>#{r['編號']:03d}</span><strong>{E(r['機台'])} {E(str(r['場次']))} · {E(CH_NAME[r['聲道']])}</strong>"
        f"<span>{E(r['麥克風'])}｜{E(r['冷卻'])}｜{fmt(r['長度秒'])} 秒｜路徑 {r['路徑數']}</span></div>"
        f"<div class='cond'>材料 {E(str(r['材料']))}｜刀具 {E(str(r['刀具類別']))}｜N {E(fmt(r['N rpm']))} rpm｜F {E(fmt(r['F mm/s']))} mm/s｜"
        f"ap {E(fmt(r['ap mm']))} mm｜ae {E(fmt(r['ae mm']))} mm｜Z {E(fmt(r['Z 刃']))}｜D {E(fmt(r['D mm']))} mm｜{E(str(r['方向']))}"
        f"{'｜<em>測試場次：底部灰帶＝主軸運轉，不畫標註區間</em>' if test else ('｜灰底＝切削路徑' if r['機台'] != 'VF-10' else '｜參考場次：底部灰帶＝主軸運轉')}</div>"
        f"<img src='png/{png}' alt='{E(r['機台'])} {E(str(r['場次']))} {E(r['聲道'])} 四頻帶音量對時間'></section>"
        for r, png, test in figs)
    legend = "".join(f"<span class='sw' style='background:{BAND_COLOR[b]}'></span>{BAND_LABEL[b]}　" for b, *_ in BANDS)
    doc = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><title>MSM 四頻帶聲音報告</title><style>
@page {{ size: A4 landscape; margin: 12mm 12mm 12mm 12mm; }}
body {{ font-family: "Noto Sans CJK TC", "Noto Sans TC", sans-serif; color: #1c2321; font-size: 9.5pt; line-height: 1.5; margin: 0; }}
h1 {{ font-family: "Noto Serif CJK TC", serif; font-size: 22pt; margin: 0 0 4mm; }}
h2 {{ font-size: 13pt; margin: 6mm 0 2mm; border-bottom: 1.2pt solid #1c2321; padding-bottom: 1mm; }}
.lead {{ max-width: 230mm; color: #444; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border-bottom: 0.4pt solid #c9d0cd; padding: 1.2mm 1.6mm; text-align: left; vertical-align: top; }}
thead th {{ background: #e6ecef; font-size: 8pt; }}
td.n, .n {{ font-variant-numeric: tabular-nums; text-align: right; }}
table.list {{ font-size: 7pt; }} table.list td, table.list th {{ padding: 0.6mm 1.1mm; }}
table.list thead {{ display: table-header-group; }} table.list tr {{ break-inside: avoid; }}
.defs {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2mm 8mm; }}
.defs div {{ border-left: 2pt solid #1f5f8b; padding-left: 3mm; }}
.sw {{ display: inline-block; width: 9mm; height: 2.4mm; vertical-align: middle; margin-right: 1.5mm; border-radius: 0.5mm; }}
.part {{ break-before: page; }}
.fig {{ break-before: page; }}
.cap {{ display: flex; gap: 4mm; align-items: baseline; font-size: 11pt; }}
.cap .no {{ font-family: monospace; color: #666; }} .cap span:last-child {{ color: #555; font-size: 9pt; }}
.cond {{ font-size: 8.5pt; color: #333; margin: 1mm 0 2mm; }}
.fig img {{ width: 100%; max-height: 165mm; object-fit: contain; display: block; }}
em {{ font-style: normal; color: #8a5a00; }}
</style></head><body>
<h1>MSM 四頻帶聲音報告</h1>
<p class="lead">Purdue MSM 多感測器銑削資料集，175 個音檔（3 台機台、69 個錄音場次）。第一部分是機台與工況資訊，第二部分是每個音檔一頁的四頻帶音量圖：0–4 kHz、1–4 kHz、4–10 kHz、10–24 kHz，每個頻帶一種顏色。2026-10-15 於 GB10 產生。</p>
<h2>第一部分　資訊</h2>
<div class="defs">
<div><strong>筆數</strong>　音檔 175（VM20i 111、VMX30Ui 38、VF-10 26）；錄音場次 69，共 8.21 小時；切削路徑 2,575。</div>
<div><strong>頻帶顏色</strong>　{legend}</div>
<div><strong>dB</strong>　相對滿刻度（0 dB＝錄音能記錄的最大值）。麥克風未校正，不是聲壓 dB SPL，只能做相對比較。</div>
<div><strong>計算</strong>　每 0.1 秒一格，4096 點 Hann 窗 FFT（解析 11.7 Hz），換算各頻帶能量；圖上每秒取能量平均。0–4 kHz 包含 1–4 kHz。</div>
<div><strong>底色</strong>　實驗室兩台：淺灰底＝label.csv 的切削路徑（不分正常異常）。VF-10：圖底部灰帶＝控制器記錄的主軸運轉時段。</div>
<div><strong>不含標籤</strong>　本報告不標正常或異常。VF-10 的 10 個測試場次（ds2、ds7、ds12 以外）依 2026-10-14 預先登記封存，不畫標註區間。</div>
</div>
<h2>三台機台與工況範圍</h2>
<table><thead><tr><th>機台</th><th>型式</th><th>場次</th><th>音檔</th><th>路徑</th><th>工況組合</th><th>N rpm</th><th>F mm/s</th><th>ap mm</th><th>ae mm</th><th>Z 刃</th><th>D mm</th><th>刀具類別</th><th>材料</th></tr></thead><tbody>{cond_rows}</tbody></table>
<p class="lead" style="margin-top:2mm">工況單位已對照控制器紀錄確認：N 與主軸轉速一致（rpm），F 與進給一致（mm/s）；ap、ae、D 為 mm。實驗室濕切，產線乾切。</p>
<h2 class="part">175 個音檔的機台與工況（與 Excel〈MSM_每音檔聲音與工況〉各頁總表相同）</h2>
<table class="list"><thead><tr>{''.join(f'<th>{E(c)}</th>' for c in cols)}</tr></thead><tbody>{list_rows}</tbody></table>
<h2 class="part">第二部分　圖檔</h2>
<p class="lead">以下每頁一個音檔，順序與上表編號相同。每頁四列子圖由上而下為 0–4、1–4、4–10、10–24 kHz；橫軸為時間（秒），縱軸為 dB。各列縱軸範圍各自調整，不同列之間請看數字比較高低。</p>
{pages}
</body></html>"""
    hp = os.path.abspath(os.path.join(out, "report.html"))  # file:// 網址必須是絕對路徑
    open(hp, "w", encoding="utf-8").write(doc)
    pdf = os.path.abspath(os.path.join(out, "MSM_四頻帶聲音報告.pdf"))
    subprocess.run(["chromium", "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={pdf}", f"file://{hp}"], check=True, capture_output=True, timeout=1200)
    print("saved", pdf)


if __name__ == "__main__":
    main()
