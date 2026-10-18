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
import html
import os
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from http_range import send_bytes, send_file

COOKIE = "msm_viewer"
MAX_REQUEST_TARGET = 8192
MAX_QUERY_FIELDS = 16
PAYLOAD_RE = re.compile(r"[a-z0-9_]+")
AUDIO_RE = re.compile(r"/audio/([a-z0-9]+_[0-9a-f]{6})/(s[012])\.mp3")
REPLAY_RE = re.compile(r"/demo_replays/([a-z0-9_-]+)\.json")
DENIED = """<!doctype html><meta charset="utf-8"><title>需要存取碼</title>
<body style="font-family:system-ui,sans-serif;max-width:36rem;margin:4rem auto;padding:0 16px;line-height:1.6">
<h1 style="font-size:1.3rem">需要存取碼</h1><p>這是 MSM 切段驗證頁。請使用完整的分享網址（結尾是 <code>?k=…</code>）開啟。</p></body>""".encode()


AGENT_CONSOLE_URL = os.environ.get("AGENT_CONSOLE_URL", "https://msm-normalizer-agent-mdiyka74ta-de.a.run.app/dev-ui/")
_AGENT_CONSOLE_PARTS = urllib.parse.urlsplit(AGENT_CONSOLE_URL)
AGENT_CONSOLE_ORIGIN = (f"{_AGENT_CONSOLE_PARTS.scheme}://{_AGENT_CONSOLE_PARTS.netloc}"
                        if _AGENT_CONSOLE_PARTS.scheme == "https" and _AGENT_CONSOLE_PARTS.netloc else "")
NAV_CSS = ("<style>.msm-nav{position:sticky;top:0;z-index:50;display:flex;gap:6px;align-items:center;padding:6px 16px;"
           "background:#111827;border-bottom:1px solid #374151;font:14px system-ui,sans-serif}.msm-nav a{color:#e5e7eb;"
           "text-decoration:none;padding:6px 12px;border-radius:6px;border:1px solid transparent}.msm-nav a:hover{border-color:#6b7280}"
           ".msm-nav a.on{background:#2563eb;color:#fff}.msm-nav .sp{flex:1}.msm-nav small{color:#9ca3af}</style>")


def nav_html(active):
    """Top switcher shared by the viewer and the demo page (same origin, cookie carries the access code)."""
    agent_url = urllib.parse.urlsplit(AGENT_CONSOLE_URL)
    agent_href = AGENT_CONSOLE_URL if agent_url.scheme == "https" and agent_url.netloc else "#"
    def item(href, label, key, extra=""):
        cls = ' class="on"' if key == active else ""
        return f'<a href="{html.escape(href, quote=True)}"{cls}{extra}>{label}</a>'
    return (NAV_CSS + '<nav class="msm-nav">'
            + item("/", "切段驗證頁 / Session viewer", "viewer")
            + item("/demo", "一鍵示範頁 / One-click demo", "demo")
            + item(agent_href, "代理人主控台 / Agent console ↗", "agent", ' target="_blank" rel="noopener noreferrer"')
            + '<span class="sp"></span><small>MSM · AI Builder Cup 2026</small></nav>')


def with_nav(html_bytes, active):
    """Insert the nav right after <body ...>; if no <body> tag, return unchanged."""
    html = html_bytes.decode("utf-8")
    i = html.find("<body")
    if i >= 0:
        j = html.find(">", i)
        return (html[: j + 1] + nav_html(active) + html[j + 1 :]).encode("utf-8")
    k = html.find("</head>")  # pages without an explicit <body> (the viewer template)
    if k >= 0:
        return (html[: k + len("</head>")] + nav_html(active) + html[k + len("</head>") :]).encode("utf-8")
    return (nav_html(active) + html).encode("utf-8")


def make_handler(bundle, token):
    bundle = os.path.realpath(bundle)
    cookie_val = hashlib.sha256(f"msm-viewer:{token}".encode()).hexdigest() if token else None

    def inside(*parts):
        p = os.path.realpath(os.path.join(bundle, *parts))
        return p if p.startswith(bundle + os.sep) and os.path.isfile(p) else None

    class Handler(BaseHTTPRequestHandler):
        server_version = "MSMViewer"
        sys_version = ""

        def end_headers(self):
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                             "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                             f"font-src https://fonts.gstatic.com; connect-src 'self' {AGENT_CONSOLE_ORIGIN}; "
                             "media-src 'self'; "
                             "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
                             "form-action 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            super().end_headers()

        def log_message(self, fmt, *args):  # 不記網址參數（存取碼在裡面）；只記錯誤
            if len(args) >= 2 and str(args[1]).startswith(("4", "5")):
                sys.stderr.write(f"{self.command} {urllib.parse.urlparse(self.path).path} {args[1]}\n")

        def query(self, u):
            if len(u.query) > MAX_REQUEST_TARGET or re.search(r"%(?![0-9a-fA-F]{2})", u.query):
                raise ValueError("invalid query")
            return urllib.parse.parse_qs(u.query, max_num_fields=MAX_QUERY_FIELDS)

        def authorized(self, u, query):
            if not token:
                return True
            jar = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
            if hmac.compare_digest(jar.get(COOKIE, ""), cookie_val):
                return True
            k = query.get("k", [""])[0]
            if k and hmac.compare_digest(k, token):
                # 用 cookie 取代網址上的存取碼，並把 ?k= 從網址拿掉（瀏覽器會保留 #場次）
                self.send_response(303)
                redirect = u.path if (u.path in ("/", "/index.html", "/demo", "/demo.html")
                                           and "\\" not in u.path and not u.path.startswith("//")) else "/"
                self.send_header("Location", redirect)
                self.send_header("Set-Cookie", f"{COOKIE}={cookie_val}; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            send_bytes(self, DENIED, "text/html; charset=utf-8", 401)
            return None

        def do_HEAD(self):
            self.do_GET()

        def do_OPTIONS(self):
            return send_bytes(self, b"method not allowed", "text/plain; charset=utf-8", 405,
                              (("Allow", "GET, HEAD"),))

        def do_GET(self):
            if len(self.path) > MAX_REQUEST_TARGET:
                return send_bytes(self, b"request target too long", "text/plain; charset=utf-8", 414)
            try:
                u = urllib.parse.urlparse(self.path)
                query = self.query(u)
            except ValueError:
                return send_bytes(self, b"bad request", "text/plain; charset=utf-8", 400)
            if u.path == "/healthz":
                return send_bytes(self, b"ok", "text/plain")
            if not self.authorized(u, query):
                return
            # 頁面與 JSON 不快取（重新部署後立刻看到新版）；只有音檔走 send_file 的快取與 Range
            if u.path in ("/", "/index.html"):
                return send_bytes(self, with_nav(open(inside("index.html"), "rb").read(), "viewer"), "text/html; charset=utf-8")
            if u.path in ("/demo", "/demo.html") and inside("demo_page.html"):
                return send_bytes(self, with_nav(open(inside("demo_page.html"), "rb").read(), "demo"), "text/html; charset=utf-8")
            mr = REPLAY_RE.fullmatch(u.path)
            if mr and inside("demo_replays", mr.group(1) + ".json"):
                return send_bytes(self, open(inside("demo_replays", mr.group(1) + ".json"), "rb").read(), "application/json; charset=utf-8")
            if u.path == "/api/sessions":
                return send_bytes(self, open(inside("sessions.json"), "rb").read(), "application/json; charset=utf-8")
            if u.path == "/api/session":
                m, ds = query.get("machine", [""])[0], query.get("ds", [""])[0]
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
    token = (open(a.token_file).read() if a.token_file else os.environ.get("VIEWER_TOKEN", "")).strip()
    if not token and not a.no_token:
        sys.exit("VIEWER_TOKEN 未設定：雲端部署必須設存取碼（本機測試請加 --no-token）")
    if not os.path.isfile(os.path.join(a.bundle, "sessions.json")):
        sys.exit(f"找不到資料包：{a.bundle}/sessions.json")
    print(f"viewer bundle server on {a.host}:{a.port}, bundle {a.bundle}, token {'on' if token else 'OFF'}", flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(a.bundle, token)).serve_forever()


if __name__ == "__main__":
    main()
