"""Session viewer module: pick any MSM session (machine + dataset) and verify its segmentation by ear and eye.

Spec: design/bdd-session-viewer.md (v2, v3). Runs on this machine only (binds 127.0.0.1) and streams the original WAVs.
Usage:
    python session_viewer_app.py [--msm DIR] [--port 8765] [--unseal-vf10] [--open]
Then open http://127.0.0.1:8765/  (a specific session: http://127.0.0.1:8765/#imi_vm20i-9)
VF-10 test sessions are sealed by default (audio, waveform and spindle only); --unseal-vf10 lifts that.
For the URL-reachable copy see export_viewer_bundle.py + viewer_bundle_server.py (Cloud Run).
"""
import argparse
import functools
import json
import os
import re
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from http_range import send_bytes, send_file
from session_viewer import build_payload, is_sealed, render_page, session_listing, to_json, wav_paths


def make_handler(db_dir, raw, unseal):
    listing, sessions = session_listing(db_dir, unseal)
    # 允許讀的音檔：只有資料庫登記的場次 × s0/s1/s2（不接受任意路徑）
    audio_files = {(r.session_id, ch): p for r in sessions.itertuples() for ch, p in wav_paths(raw, r.dir).items()}
    known = {(r.machine, int(r.dataset)) for r in sessions.itertuples()}

    @functools.lru_cache(maxsize=6)
    def payload(machine, ds):
        return to_json(build_payload(db_dir, raw, machine, ds, lambda sid, ch: f"audio/{sid}/{ch}.wav",
                                     sealed=is_sealed(machine, ds, unseal))).encode()

    page = render_page("MSM 切段驗證", "null").encode()
    sessions_json = json.dumps(listing, ensure_ascii=False).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 安靜：只記錯誤
            if args and str(args[1]).startswith(("4", "5")):
                super().log_message(fmt, *args)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return send_bytes(self, page, "text/html; charset=utf-8")
            if u.path == "/api/sessions":
                return send_bytes(self, sessions_json, "application/json; charset=utf-8")
            if u.path == "/api/session":
                q = urllib.parse.parse_qs(u.query)
                try:
                    key = (q["machine"][0], int(q["ds"][0]))
                except (KeyError, ValueError, IndexError):
                    return send_bytes(self, b"bad request", "text/plain", 400)
                if key not in known:
                    return send_bytes(self, b"unknown session", "text/plain", 404)
                return send_bytes(self, payload(*key), "application/json; charset=utf-8")
            mt = re.fullmatch(r"/audio/([a-z0-9]+_[0-9a-f]{6})/(s[012])\.wav", u.path)
            if mt and (mt.group(1), mt.group(2)) in audio_files:
                return send_file(self, audio_files[(mt.group(1), mt.group(2))], "audio/wav")
            return send_bytes(self, b"not found", "text/plain", 404)

    return Handler, len(listing), sum(s["sealed"] for s in listing)


def main():
    ap = argparse.ArgumentParser(description="MSM 切段驗證模組")
    ap.add_argument("--msm", default=os.path.expanduser("~/m143359/金屬中心/datasets/MSM"), help="含 db/ 與 raw/ 的 MSM 資料夾")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--unseal-vf10", action="store_true", help="顯示 VF-10 測試場次的切段與標籤（會破封存，請另記偏離）")
    ap.add_argument("--open", action="store_true", help="啟動後自動開瀏覽器")
    a = ap.parse_args()
    handler, n, n_sealed = make_handler(os.path.join(a.msm, "db"), os.path.join(a.msm, "raw"), a.unseal_vf10)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"MSM 切段驗證模組：{n} 個場次（封存 {n_sealed} 個）→ {url}  （Ctrl+C 結束）", flush=True)
    if a.open:
        webbrowser.open(url)
    ThreadingHTTPServer(("127.0.0.1", a.port), handler).serve_forever()


if __name__ == "__main__":
    main()
