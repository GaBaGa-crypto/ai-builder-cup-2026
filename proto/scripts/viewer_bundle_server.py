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
DENIED = """<!doctype html><meta charset="utf-8"><title>需要存取碼 / Access code required</title>
<body style="font-family:system-ui,sans-serif;max-width:36rem;margin:4rem auto;padding:0 16px;line-height:1.6">
<h1 style="font-size:1.3rem">需要存取碼 / Access code required</h1>
<p>這是 MSM 切段驗證頁。請使用完整的分享網址（結尾是 <code>?k=…</code>）開啟。</p>
<p lang="en">This is the MSM session viewer. Please open it with the complete shared link (it ends in <code>?k=…</code>).</p></body>""".encode()


AGENT_CONSOLE_URL = os.environ.get("AGENT_CONSOLE_URL", "https://msm-normalizer-agent-mdiyka74ta-de.a.run.app/dev-ui/")
_AGENT_CONSOLE_PARTS = urllib.parse.urlsplit(AGENT_CONSOLE_URL)
AGENT_CONSOLE_ORIGIN = (f"{_AGENT_CONSOLE_PARTS.scheme}://{_AGENT_CONSOLE_PARTS.netloc}"
                        if _AGENT_CONSOLE_PARTS.scheme == "https" and _AGENT_CONSOLE_PARTS.netloc else "")
NAV_CSS = ("<style>.msm-nav{position:sticky;top:0;z-index:50;display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:6px 16px;"
           "background:#111827;border-bottom:1px solid #374151;font:14px system-ui,sans-serif}.msm-nav a{color:#e5e7eb;"
           "text-decoration:none;padding:6px 12px;border-radius:6px;border:1px solid transparent}.msm-nav a:hover{border-color:#6b7280}"
           ".msm-nav a.on{background:#2563eb;color:#fff}.msm-nav .sp{flex:1}.msm-nav small{color:#9ca3af}"
           ".msm-nav .msm-lang{display:inline-flex;flex-shrink:0;border:1px solid #4b5563;border-radius:6px;overflow:hidden;margin-right:10px}"
           ".msm-nav .msm-lang button{font:inherit;color:#e5e7eb;background:transparent;border:0;padding:5px 10px;cursor:pointer}"
           ".msm-nav .msm-lang button.on{background:#374151;color:#fff;font-weight:600}"
           "html[lang=en] [data-i18n-zh-only]{display:none}</style>")

# Shared client-side language engine (zh-Hant / en). Pages add their own tables via window.MSM_I18N_EXTRA.
# Order: URL ?lang=zh|en (whitelisted, then remembered) > localStorage msm_lang > navigator language (zh* -> zh, else en).
# In zh the DOM is never touched (the Chinese page is the submitted version). In en, text nodes and a few attributes are
# translated through exact / regex / phrase tables; a MutationObserver covers text the page builds later.
I18N_JS = r"""<script>(function(){
var CJK=/[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]/,HAN=/[\u4e00-\u9fff]/;
var q=/[?&]lang=(zh|en)(?:&|$)/.exec(location.search),lang=null;
if(q){lang=q[1];try{localStorage.setItem("msm_lang",lang);}catch(e){}}
if(!lang){try{var st=localStorage.getItem("msm_lang");if(st==="zh"||st==="en")lang=st;}catch(e){}}
if(!lang){var nl=(navigator.languages&&navigator.languages[0])||navigator.language||"en";lang=/^zh/i.test(nl)?"zh":"en";}
document.documentElement.lang=lang==="zh"?"zh-Hant":"en";
var hide=null;if(lang==="en"){hide=document.createElement("style");hide.textContent='html[lang="en"] body{visibility:hidden}';(document.head||document.documentElement).appendChild(hide);}
var PUNCT=[["（"," ("],["）",")"],["，",", "],["。",". "],["；","; "],["：",": "],["、",", "],["｜"," | "],["／","/"],["＋","+"],["＝","="],["「","\u201c"],["」","\u201d"]];
var ATTRS=["title","aria-label","placeholder","alt"];
var API={lang:lang,missing:{},exact:{},rules:[],phrases:[],heuristics:false,skip:"script,style,pre,code,textarea,[data-i18n-skip],[data-i18n-zh-only]"},merged=false,started=false;
API.exact["切段驗證頁 / Session viewer"]="Session viewer";API.exact["一鍵示範頁 / One-click demo"]="One-click demo";API.exact["代理人主控台 / Agent console ↗"]="Agent console ↗";
function merge(x){if(!x)return;var k;for(k in x.exact||{})API.exact[k]=x.exact[k];(x.rules||[]).forEach(function(r){API.rules.push(r);});for(k in x.phrases||{})API.phrases.push([k,x.phrases[k]]);API.phrases.sort(function(a,b){return b[0].length-a[0].length;});if(x.skip)API.skip+=","+x.skip;if(x.heuristics)API.heuristics=true;}
function pair(s){var i=s.indexOf(" / "),n=3;if(i<0){i=s.indexOf("）/ ");if(i>=0){i+=1;n=2;}}if(i<=0)return null;var L=s.slice(0,i),R=s.slice(i+n);if(!CJK.test(L)||CJK.test(R)||!R.trim())return null;return /^[^A-Za-z0-9\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]*/.exec(L)[0]+R;}
function tail(s){var i=s.length-1;while(i>=0&&!CJK.test(s.charAt(i)))i--;if(i<0)return null;var r=s.slice(i+1).replace(/^[\s.;:,)\u201d]+/,"");return r.length>=12&&/^[A-Za-z\u201c]/.test(r)?r:null;}
function tr(s){if(!CJK.test(s))return s;var m=/^(\s*)([\s\S]*?)(\s*)$/.exec(s),a=m[1],core=m[2],z=m[3],out=null,i;
if(Object.prototype.hasOwnProperty.call(API.exact,core))out=API.exact[core];
if(out==null){for(i=0;i<API.rules.length;i++){if(API.rules[i][0].test(core)){out=core.replace(API.rules[i][0],API.rules[i][1]);break;}}}
if(out==null&&API.heuristics)out=pair(core);
if(out==null)out=core;if(HAN.test(out)){for(i=0;i<API.phrases.length;i++){if(out.indexOf(API.phrases[i][0])>=0)out=out.split(API.phrases[i][0]).join(API.phrases[i][1]);}}
if(HAN.test(out)&&API.heuristics){var t=tail(out);if(t)out=t;}
if(CJK.test(out)){for(i=0;i<PUNCT.length;i++)out=out.split(PUNCT[i][0]).join(PUNCT[i][1]);out=out.replace(/\s{2,}/g," ").replace(/\(\s+/g,"(").replace(/\s+\)/g,")").replace(/\s+([,.;:])/g,"$1");}
if(HAN.test(out)&&!API.missing[core]){API.missing[core]=1;if(window.console)console.warn("[msm-i18n] untranslated:",core);}
return a+out+z;}
function skip(el){return !!(el&&el.closest&&el.closest(API.skip));}
function attrs(el){for(var i=0;i<ATTRS.length;i++){if(el.hasAttribute(ATTRS[i])){var v=el.getAttribute(ATTRS[i]),w=tr(v);if(w!==v)el.setAttribute(ATTRS[i],w);}}}
function text(n){var v=tr(n.data);if(v!==n.data)n.data=v;}
function node(n){if(n.nodeType===3){var p=n.parentNode;if(p&&p.nodeType===1&&!skip(p))text(n);return;}if(n.nodeType!==1||skip(n))return;attrs(n);
var w=document.createTreeWalker(n,5,null,false),c;while((c=w.nextNode())){if(c.nodeType===1){if(!skip(c))attrs(c);}else if(c.parentNode&&!skip(c.parentNode))text(c);}}
function unhide(){if(hide&&hide.parentNode)hide.parentNode.removeChild(hide);hide=null;}if(hide)setTimeout(unhide,3000);
function start(){if(started)return;started=true;try{node(document.documentElement);}finally{unhide();}
new MutationObserver(function(ms){for(var i=0;i<ms.length;i++){var m=ms[i];if(m.type==="childList"){for(var j=0;j<m.addedNodes.length;j++)node(m.addedNodes[j]);}else if(m.type==="characterData"){node(m.target);}else if(m.target.nodeType===1&&!skip(m.target)){attrs(m.target);}}})
.observe(document.documentElement,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:ATTRS});}
function mark(){var b=document.querySelectorAll(".msm-lang button[data-lang]");for(var i=0;i<b.length;i++){var on=b[i].getAttribute("data-lang")===lang;b[i].classList.toggle("on",on);b[i].setAttribute("aria-pressed",on?"true":"false");}}
API.set=function(l){if((l!=="zh"&&l!=="en")||l===lang)return;var stored=true;try{localStorage.setItem("msm_lang",l);}catch(e){stored=false;}
try{var u=new URL(location.href);if(stored)u.searchParams.delete("lang");else u.searchParams.set("lang",l);history.replaceState(null,"",u.href);}catch(e){}
if(l==="en"){lang=API.lang="en";document.documentElement.lang="en";if(!merged){merge(window.MSM_I18N_EXTRA);merged=true;}start();mark();}else{location.reload();}};
function ready(){if(!merged){merge(window.MSM_I18N_EXTRA);merged=true;}mark();document.addEventListener("click",function(e){var t=e.target&&e.target.closest&&e.target.closest(".msm-lang button[data-lang]");if(t)API.set(t.getAttribute("data-lang"));});if(lang==="en")start();}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",ready);else ready();
window.MSM_I18N=API;})();</script>"""


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
            + '<span class="sp"></span>'
            + '<span class="msm-lang" role="group" aria-label="Language / 語言" data-i18n-skip>'
            + '<button type="button" data-lang="zh" lang="zh-Hant" aria-pressed="false">中文</button>'
            + '<button type="button" data-lang="en" lang="en" aria-pressed="false">EN</button></span>'
            + '<small>MSM · AI Builder Cup 2026</small></nav>' + I18N_JS)


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
                lang = query.get("lang", [""])[0]  # keep a whitelisted ?lang= so a share link can force the language
                self.send_header("Location", redirect + ("?lang=" + lang if lang in ("zh", "en") else ""))
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
