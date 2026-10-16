"""Serve an exported viewer bundle (Cloud Run or local). Standard library only.

Spec: design/bdd-viewer-deploy.md.
Env / flags:
    BUNDLE_DIR    bundle folder from export_viewer_bundle.py (Cloud Run: the mounted bucket path)
    VIEWER_TOKEN  access code; visitors open https://<url>/?k=<code> once, then a cookie keeps them in
    PORT          listen port (Cloud Run sets it)
    python viewer_bundle_server.py [--bundle DIR] [--host 0.0.0.0] [--port 8080]
Without VIEWER_TOKEN the server refuses to start unless --no-token is given (local testing only).
"""
import argparse
import hashlib
import hmac
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from http_range import send_bytes, send_file

COOKIE = "msm_viewer"
PAYLOAD_RE = re.compile(r"[a-z0-9_]+")
AUDIO_RE = re.compile(r"/audio/([a-z0-9]+_[0-9a-f]{6})/(s[012])\.mp3")
DENIED = """<!doctype html><meta charset="utf-8"><title>需要存取碼</title>
<body style="font-family:system-ui,sans-serif;max-width:36rem;margin:4rem auto;padding:0 16px;line-height:1.6">
<h1 style="font-size:1.3rem">需要存取碼</h1><p>這是 MSM 切段驗證頁。請使用完整的分享網址（結尾是 <code>?k=…</code>）開啟。</p></body>""".encode()


def make_handler(bundle, token):
    bundle = os.path.realpath(bundle)
    cookie_val = hashlib.sha256(f"msm-viewer:{token}".encode()).hexdigest() if token else None

    def inside(*parts):
        p = os.path.realpath(os.path.join(bundle, *parts))
        return p if p.startswith(bundle + os.sep) and os.path.isfile(p) else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # 不記網址參數（存取碼在裡面）；只記錯誤
            if len(args) >= 2 and str(args[1]).startswith(("4", "5")):
                sys.stderr.write(f"{self.command} {urllib.parse.urlparse(self.path).path} {args[1]}\n")

        def authorized(self, u):
            if not token:
                return True
            jar = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
            if hmac.compare_digest(jar.get(COOKIE, ""), cookie_val):
                return True
            k = urllib.parse.parse_qs(u.query).get("k", [""])[0]
            if k and hmac.compare_digest(k, token):
                # 用 cookie 取代網址上的存取碼，並把 ?k= 從網址拿掉（瀏覽器會保留 #場次）
                self.send_response(303)
                self.send_header("Location", u.path or "/")
                self.send_header("Set-Cookie", f"{COOKIE}={cookie_val}; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            send_bytes(self, DENIED, "text/html; charset=utf-8", 401)
            return None

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/healthz":
                return send_bytes(self, b"ok", "text/plain")
            if not self.authorized(u):
                return
            # 頁面與 JSON 不快取（重新部署後立刻看到新版）；只有音檔走 send_file 的快取與 Range
            if u.path in ("/", "/index.html"):
                return send_bytes(self, open(inside("index.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/api/sessions":
                return send_bytes(self, open(inside("sessions.json"), "rb").read(), "application/json; charset=utf-8")
            if u.path == "/api/session":
                q = urllib.parse.parse_qs(u.query)
                m, ds = q.get("machine", [""])[0], q.get("ds", [""])[0]
                p = inside("payload", f"{m}-{ds}.json") if PAYLOAD_RE.fullmatch(m) and ds.isdigit() else None
                if not p:
                    return send_bytes(self, b"unknown session", "text/plain", 404)
                return send_bytes(self, open(p, "rb").read(), "application/json; charset=utf-8")
            mt = AUDIO_RE.fullmatch(u.path)
            p = inside("audio", mt.group(1), f"{mt.group(2)}.mp3") if mt else None
            if p:
                return send_file(self, p, "audio/mpeg")
            return send_bytes(self, b"not found", "text/plain", 404)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", default=os.environ.get("BUNDLE_DIR", "/bundle"))
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    ap.add_argument("--token-file", help="從檔案讀存取碼（本機測試用，避免存取碼出現在程序清單）")
    ap.add_argument("--no-token", action="store_true", help="本機測試用：不檢查存取碼")
    a = ap.parse_args()
    token = open(a.token_file).read().strip() if a.token_file else os.environ.get("VIEWER_TOKEN", "")
    if not token and not a.no_token:
        sys.exit("VIEWER_TOKEN 未設定：雲端部署必須設存取碼（本機測試請加 --no-token）")
    if not os.path.isfile(os.path.join(a.bundle, "sessions.json")):
        sys.exit(f"找不到資料包：{a.bundle}/sessions.json")
    print(f"viewer bundle server on {a.host}:{a.port}, bundle {a.bundle}, token {'on' if token else 'OFF'}", flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(a.bundle, token)).serve_forever()


if __name__ == "__main__":
    main()
