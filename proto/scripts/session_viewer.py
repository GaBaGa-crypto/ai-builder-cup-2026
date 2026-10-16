"""Session segmentation check page: playable audio, waveform coloured by event, spindle speed, event table.

Spec: design/bdd-session-viewer.md.
Static export (one session, MP3s next to the page, e.g. for an artifact):
    python session_viewer.py <db_dir> <raw_dir> <machine e.g. imi_vm20i> <dataset N> <out_dir>
Interactive module for any session: see session_viewer_app.py (shares build_payload() and the template).
"""
import base64
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
import soundfile as sf

from msm_io import find, read_session_json, read_wav_int16

FINE_SAMPLES = 240      # 48 kHz 下每格 5 毫秒
COARSE_BUCKETS = 6000
MP3_LEVEL = 0.6         # 約 160 kbps；實測 ds6 s0 530 秒 → 10.6 MB
CHANNELS = ["s0", "s1", "s2"]
SHORT = {"imi_vm20i": "VM20i", "imi_vmx30ui": "VMX30Ui", "tmf_vf10": "VF-10"}
MIC = {"s0": "s0 空氣麥克風 Fifine K053", "s1": "s1 接觸式聽診器", "s2": "s2 接觸式聽診器"}
COOLANT_ZH = {"wet": "濕切", "dry": "乾切"}  # 冷卻條件：MSM 論文，實驗室濕切、產線乾切（build_db.py 的 COOLANT）
COOLANT_UNKNOWN = "未註明"  # 正規化來源沒講冷卻（session.json coolant=unknown）時顯示這個，不給假值
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "session_viewer_template.html")
PREREG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "design", "prereg-v1.json")


def vf10_reference_datasets():
    """VF-10 參考場次以預先登記為準；其餘 VF-10 場次是封存的測試集。"""
    try:
        return set(json.load(open(PREREG, encoding="utf-8"))["vf10_reference_datasets"])
    except (OSError, KeyError, ValueError):
        return {2, 7, 12}


def is_sealed(machine, ds, unseal_vf10=False):
    return machine == "tmf_vf10" and ds not in vf10_reference_datasets() and not unseal_vf10


def envelope(x, per):
    n = len(x) // per
    y = x[:n * per].reshape(n, per)
    mn = np.round(y.min(1) / 32768 * 127).astype(np.int8)
    mx = np.round(y.max(1) / 32768 * 127).astype(np.int8)
    return base64.b64encode(np.stack([mn, mx], 1).tobytes()).decode(), per


def connect(db_dir):
    return (sqlite3.connect(os.path.join(db_dir, "main.sqlite")),
            sqlite3.connect(os.path.join(db_dir, "source", "source.sqlite")),
            sqlite3.connect(os.path.join(db_dir, "sealed", "labels.sqlite")))


def wav_paths(raw, sdir):
    return {ch: p for ch in CHANNELS for p in [find(os.path.join(raw, sdir), f"_{ch}.wav")] if p}


def session_dir(root, sdir):
    """場次資料夾：建庫登記的根目錄（session_source.root）存在就接成絕對路徑，否則（舊庫沒有 root 欄、或庫搬到別台）
    沿用相對路徑由呼叫者的 raw 補。wav_paths(raw, 絕對路徑) 即該絕對路徑，所以既有呼叫者不必改。"""
    return os.path.join(root, sdir) if isinstance(root, str) and root and os.path.isdir(root) else sdir


def display_name(machine, meta):
    """機台顯示名：正規化場次取 session.json machine_name；MSM 原始資料（沒有 session.json）沿用 SHORT。"""
    return (meta or {}).get("machine_name") or SHORT.get(machine) or machine


def mic_name(ch, meta):
    if meta is None:
        return MIC[ch]
    mic = meta.get("mic_type")
    return f"{ch} 麥克風（來源未註明）" if not mic or mic == "unknown" else f"{ch} 麥克風（{mic}）"


BANDS = ["0-4k", "1-4k", "4-10k", "10-24k"]


def band_series(cache_dir, sid, ch, x, sr):
    """四頻帶每 0.1 秒一格的 dB（相對滿刻度），以 dB×10 的 int16 base64 送出；優先讀 band_trends.py 的快取。"""
    cf = os.path.join(cache_dir, f"{sid}_{ch}.npz")
    if os.path.exists(cf):
        z = np.load(cf)
        t, ms = z["t"], {b: z[b] for b in BANDS}
    else:
        from band_trends import band_ms  # 只有快取缺時才載入（會帶入 matplotlib）
        t, ms = band_ms(x, sr)
    enc = lambda v: base64.b64encode(np.round(10 * 10 * np.log10(np.asarray(v, np.float64) + 1e-12)).astype(np.int16).tobytes()).decode()
    return dict(t0=float(t[0]), hop=float(t[1] - t[0]), n=len(t), names=BANDS, db10={b: enc(ms[b]) for b in BANDS})


def build_payload(db_dir, raw, machine, ds, audio_url, sealed, band_cache=None):
    """Everything the page needs for one session. audio_url(session_id, ch) -> URL string.

    When sealed, no event boundaries or labels leave this function (only audio, waveform, spindle).
    """
    band_cache = band_cache or os.path.join(db_dir, "..", "figures", "band_trends", "cache")
    m, src, lab = connect(db_dir)
    ss = pd.read_sql("select * from session_source where machine=? and dataset=?", src, params=[machine, ds]).iloc[0]
    sid = ss.session_id
    sdir = session_dir(ss.get("root"), ss.dir)
    meta = read_session_json(os.path.join(raw, sdir))
    ev = pd.read_sql("select * from events where session_id=? order by idx", m, params=[sid]).merge(
        pd.read_sql("select event_id, label_std from labels", lab), on="event_id")
    ctx = pd.read_sql("select t, spindle_speed_rpm from context_ts where session_id=? order by t", m, params=[sid])
    al = pd.read_sql("select * from alignment where session_id=?", m, params=[sid]).iloc[0]
    ses = pd.read_sql("select * from sessions where session_id=?", m, params=[sid]).iloc[0]

    chans = {}
    for ch, p in wav_paths(raw, sdir).items():
        x, sr = read_wav_int16(p)
        fine, fper = envelope(x, FINE_SAMPLES)
        coarse, cper = envelope(x, max(1, len(x) // COARSE_BUCKETS))
        chans[ch] = dict(name=mic_name(ch, meta), sr=sr, dur=len(x) / sr, fine=fine, fper=fper, coarse=coarse, cper=cper,
                         clip=float(np.mean(np.abs(x.astype(np.int32)) >= 32767)), url=audio_url(sid, ch),
                         bands=band_series(band_cache, sid, ch, x, sr))

    cut = ev[ev.cut_state == "cutting"]
    num = lambda v: None if pd.isna(v) or not np.isfinite(v) else float(v)  # 舊庫可能存了 inf：不送非法 JSON
    # 未標註切削（cutting 且 label_std=-1）額外帶 unlabeled=True；切削狀態未知（cut_state=unknown，標籤 -1）帶 cut=False＋
    # unlabeled=True＋unknown=True（不是非切削、也不是標籤 0）；其他事件的欄位與順序不變（MSM payload 逐位元同舊版）
    events = [] if sealed else [
        dict(i=int(r.idx), s=r.start, e=r.end, d=r.dur, cut=r.cut_state == "cutting", lab=int(r.label_std),
             N=num(r.N), F=num(r.F), ap=num(r.ap), ae=num(r.ae), Z=num(r.Z), D=num(r.D),
             dir={"DOWN": "順銑", "UP": "逆銑"}.get(r.direction, ""), tool=r.tool_class if isinstance(r.tool_class, str) else "",
             clip=round(r.clip_ratio, 4), cool=COOLANT_ZH.get(r.coolant, COOLANT_UNKNOWN), rms=round(r.rms_dbfs, 1),
             **({"unlabeled": True} if r.cut_state == "cutting" and int(r.label_std) == -1 else
                {"unlabeled": True, "unknown": True} if r.cut_state == "unknown" else {})) for r in ev.itertuples()]
    n_unk = int((ev.cut_state == "unknown").sum())
    n_unl = int((cut.label_std == -1).sum()) + n_unk  # 未標註＝切削標籤未定＋切削狀態未知
    info = dict(machine=display_name(machine, meta), machine_key=machine, dataset=ds, sid=sid, dur=float(ses.s0_sec),
                remark="（封存）" if sealed else (ss.remark or "（無備註）"), sealed=sealed,
                material=", ".join(sorted(cut.material.dropna().unique())), tools=", ".join(sorted(cut.tool_class.dropna().unique())),
                **({k: None for k in ("n0", "n1", "n2", "nnc")} if sealed else  # 封存場次連標籤計數都不送
                   dict(n0=int((cut.label_std == 0).sum()), n1=int((cut.label_std == 1).sum()), n2=int((cut.label_std == 2).sum()),
                        nnc=int((ev.cut_state == "non_cutting").sum()),
                        **({"n_unlabeled": n_unl} if n_unl else {}),  # 只在有未標註時才送（MSM 場次鍵集不變）
                        **({"n_unknown": n_unk} if n_unk else {}))),
                align=dict(head=num(al.offset_head_s), tail=num(al.offset_tail_s), conf=num(al.conf), anchor=al.anchor))
    spindle = dict(t=[round(v, 3) for v in ctx.t], v=[None if pd.isna(v) else float(v) for v in ctx.spindle_speed_rpm])
    return dict(info=info, events=events, chans=chans, spindle=spindle)


def session_listing(db_dir, unseal_vf10=False):
    """69 場的選單資料（機台、場次、長度、標籤計數、備註）；封存場次的計數與備註遮掉。回傳 (listing, session_source 表)。"""
    m, src, lab = connect(db_dir)
    has_root = any(r[1] == "root" for r in src.execute("pragma table_info(session_source)"))
    sessions = pd.read_sql(f"select session_id, machine, dataset, dir, remark{', root' if has_root else ''} from session_source", src)
    if has_root:  # 多根目錄的庫：dir 換成場次的絕對路徑（呼叫者 wav_paths(raw, r.dir) 照舊可用）
        sessions["dir"] = [session_dir(rt, d) for rt, d in zip(sessions.root, sessions.dir)]
    ses = pd.read_sql("select session_id, s0_sec from sessions", m)
    # 計數涵蓋 cutting 與 unknown（unknown 標籤必為 -1，算進 n_unlabeled）：n0+n1+n2+n_unlabeled＝sessions.n_cut
    ev_c = pd.read_sql("select event_id, session_id, cut_state from events where cut_state!='non_cutting'", m)
    n_unk = ev_c[ev_c.cut_state == "unknown"].groupby("session_id").size()
    cnt = ev_c[["event_id", "session_id"]].merge(
        pd.read_sql("select event_id, label_std from labels", lab), on="event_id").pivot_table(
        index="session_id", columns="label_std", values="event_id", aggfunc="count", fill_value=0)
    order = {"imi_vm20i": 0, "imi_vmx30ui": 1, "tmf_vf10": 2}  # MSM 三台在前，其他機台依名稱排在後
    listing = []
    for r in sessions.merge(ses, on="session_id").sort_values(["machine", "dataset"], key=lambda c: c.map(lambda v: f"{order.get(v, 9)}{v}") if c.name == "machine" else c).itertuples():
        sealed = is_sealed(r.machine, int(r.dataset), unseal_vf10)
        meta = read_session_json(r.dir) if os.path.isabs(r.dir) else None  # 相對路徑＝舊庫的 MSM 場次，沒有 session.json
        c = cnt.loc[r.session_id] if r.session_id in cnt.index else {}  # 整場沒有切削事件（只有表頭的 label.csv）時計數為 0
        nu = 0 if sealed else int(c.get(-1, 0))  # 未標註（cutting 且 -1，或 unknown）；只在 > 0 時送，MSM 清單鍵集不變
        nk = 0 if sealed else int(n_unk.get(r.session_id, 0))
        listing.append(dict(machine_key=r.machine, machine=display_name(r.machine, meta), dataset=int(r.dataset), sid=r.session_id,
                            dur=float(r.s0_sec), remark="" if sealed else (r.remark or ""), sealed=sealed,
                            **({k: None for k in ("n0", "n1", "n2")} if sealed else  # 封存場次不送標籤計數與備註
                               dict(n0=int(c.get(0, 0)), n1=int(c.get(1, 0)), n2=int(c.get(2, 0)))),
                            **({"n_unlabeled": nu} if nu else {}), **({"n_unknown": nk} if nk else {})))
    return listing, sessions


def write_mp3(x, sr, path):
    """int16 → MP3（soundfile 內建 Layer III；本機無 ffmpeg），並確認長度與原檔相差 < 0.1 秒。"""
    sf.write(path, x.astype(np.float32) / 32768.0, sr, format="MP3", subtype="MPEG_LAYER_III",
             compression_level=MP3_LEVEL, bitrate_mode="CONSTANT")
    d_mp3, d_wav = sf.info(path).duration, len(x) / sr
    assert abs(d_mp3 - d_wav) < 0.1, (path, d_mp3, d_wav)
    return d_mp3, d_wav


def to_json(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)  # NaN 不是合法 JSON


def render_page(title, payload_json):
    """payload_json='null' → 模組模式（頁面自己向 /api 取資料並顯示場次選單）。"""
    tpl = open(TEMPLATE, encoding="utf-8").read()
    return tpl.replace("__TITLE__", title).replace("__DATA__", payload_json.replace("</", "<\\/"))


def main():
    db_dir, raw, machine, ds, out = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
    os.makedirs(os.path.join(out, "audio"), exist_ok=True)
    payload = build_payload(db_dir, raw, machine, ds, lambda sid, ch: f"audio/{ch}.mp3", sealed=False)
    src = sqlite3.connect(os.path.join(db_dir, "source", "source.sqlite"))
    ss = pd.read_sql("select * from session_source where machine=? and dataset=?", src, params=[machine, ds]).iloc[0]
    sdir = session_dir(ss.get("root"), ss.dir)
    for ch, p in wav_paths(raw, sdir).items():
        x, sr = read_wav_int16(p)
        mp3 = os.path.join(out, "audio", f"{ch}.mp3")
        d_mp3, d_wav = write_mp3(x, sr, mp3)
        print(ch, f"mp3 {os.path.getsize(mp3)/1e6:.1f} MB, dur {d_mp3:.2f}/{d_wav:.2f}", flush=True)
    html = render_page(f"{payload['info']['machine']} ds{ds} 切段驗證", to_json(payload))
    open(os.path.join(out, "index.html"), "w", encoding="utf-8").write(html)
    print(f"saved {out}/index.html ({len(html)/1e6:.2f} MB), events {len(payload['events'])}")


if __name__ == "__main__":
    main()
