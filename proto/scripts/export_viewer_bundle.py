"""Export the session viewer as a static bundle for Cloud Run (spec: design/bdd-viewer-deploy.md).

Usage: python export_viewer_bundle.py [--msm DIR] [--out DIR] [--jobs 8]
Writes <out>/index.html, sessions.json, payload/<machine>-<ds>.json, audio/<session_id>/<ch>.mp3, manifest.json.
VF-10 test sessions are exported sealed (no events, counts or remarks). No database, label store or WAV goes into the bundle.
Re-running skips MP3s that already exist with the right duration.
"""
import argparse
import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor

import soundfile as sf

from msm_io import read_wav_int16
from session_viewer import build_payload, is_sealed, render_page, session_listing, to_json, wav_paths, write_mp3


def encode(job):
    wav, mp3 = job
    if os.path.exists(mp3):
        d_wav = sf.info(wav).duration
        if abs(sf.info(mp3).duration - d_wav) < 0.1:
            return mp3, "skip"
    x, sr = read_wav_int16(wav)
    write_mp3(x, sr, mp3 + ".part")
    os.replace(mp3 + ".part", mp3)
    return mp3, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--msm", default=os.path.expanduser("~/m143359/金屬中心/datasets/MSM"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args()
    db_dir, raw = os.path.join(a.msm, "db"), os.path.join(a.msm, "raw")
    out = a.out or os.path.join(a.msm, "viewer_bundle")
    os.makedirs(os.path.join(out, "payload"), exist_ok=True)

    listing, sessions = session_listing(db_dir, unseal_vf10=False)
    jobs = []
    for r in sessions.itertuples():
        os.makedirs(os.path.join(out, "audio", r.session_id), exist_ok=True)
        for ch, wav in wav_paths(raw, r.dir).items():
            jobs.append((wav, os.path.join(out, "audio", r.session_id, f"{ch}.mp3")))
    with ProcessPoolExecutor(a.jobs) as ex:
        for i, (mp3, st) in enumerate(ex.map(encode, jobs), 1):
            print(f"[{i}/{len(jobs)}] {st} {os.path.relpath(mp3, out)}", flush=True)

    for s in listing:
        p = build_payload(db_dir, raw, s["machine_key"], s["dataset"], lambda sid, ch: f"audio/{sid}/{ch}.mp3",
                          sealed=is_sealed(s["machine_key"], s["dataset"]))
        with open(os.path.join(out, "payload", f"{s['machine_key']}-{s['dataset']}.json"), "w", encoding="utf-8") as fh:
            fh.write(to_json(p))
    with open(os.path.join(out, "sessions.json"), "w", encoding="utf-8") as fh:
        json.dump(listing, fh, ensure_ascii=False)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(render_page("MSM 切段驗證", "null"))

    files = []
    for dp, _, fs in os.walk(out):
        for f in sorted(fs):
            if f == "manifest.json" or f.endswith(".part"):
                continue
            path = os.path.join(dp, f)
            h = hashlib.sha256(open(path, "rb").read()).hexdigest()
            files.append(dict(path=os.path.relpath(path, out), bytes=os.path.getsize(path), sha256=h))
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(sessions=len(listing), sealed=sum(s["sealed"] for s in listing), files=sorted(files, key=lambda x: x["path"])), fh, indent=1)
    print(f"bundle {out}: {len(files)} files, {sum(f['bytes'] for f in files)/1e9:.2f} GB")


if __name__ == "__main__":
    main()
