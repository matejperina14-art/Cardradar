"""
CARD RADAR 7.0 – app.py
Spúšťa web a obsahuje všetky adresy (routy). Logika je v ostatných súboroch:
  logika.py   rozpoznávanie hľadania, filtre, sklad, ceny
  obchody.py  obchody, sťahovanie, katalógy, hľadanie, databáza
  strazca.py  strážca ceny a e-maily
  stranky.py  úvodná stránka, textové stránky, admin
  index.html  samotný web
  static/     logo a ikony

Premenné prostredia (Render → Environment):
  ADMIN_KEY      heslo k /admin/... stránkam
  PUBLIC_URL     hlavná adresa, napr. https://getcardradar.com
  DB_PATH        databáza na trvalom disku, napr. /var/data/cardradar.db
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM   e-maily strážcu
  ALLOW_INDEXING 1 = web môže byť v Google
  OPERATOR_NAME, CONTACT_EMAIL   do podmienok a ochrany údajov
"""

import gzip
import hashlib
import json
import os
import threading
import time
import urllib.parse
from collections import deque

from flask import Flask, Response, jsonify, redirect, request

import logika as L
import obchody as O
import strazca
import stranky as S

VERSION = "7.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=None)


# =========================================================
# OBMEDZENIE POČTU POŽIADAVIEK (na jednu IP za minútu)
# =========================================================

class RateLimiter:
    def __init__(self, limit, window=60):
        self.limit, self.window = limit, window
        self._hits, self._lock = {}, threading.Lock()

    def allow(self):
        key, now = client_ip(), time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 5000:
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                    self._hits.pop(k, None)
            return True


LIMIT_SEARCH = RateLimiter(30)
LIMIT_SUGGEST = RateLimiter(120)
LIMIT_IMAGES = RateLimiter(60)
LIMIT_HISTORY = RateLimiter(60)
LIMIT_ALERTS = RateLimiter(5, window=600)


def client_ip():
    """Skutočná IP návštevníka (web beží za Cloudflare a Renderom)."""
    for h in ("CF-Connecting-IP", "True-Client-IP"):
        if request.headers.get(h):
            return request.headers[h].strip()[:64]
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else "") or request.remote_addr or "?"


def too_many():
    return jsonify({"error": "Príliš veľa požiadaviek. Skús to o chvíľu."}), 429


def json_body():
    return request.get_json(silent=True) or {}


# =========================================================
# WEB
# =========================================================

_index = {"path": None, "mtime": None, "html": None, "etag": None}


def load_index():
    """index.html (vedľa app.py alebo v templates/), znovu sa načíta len pri zmene súboru."""
    for path in (os.path.join(BASE_DIR, "index.html"), os.path.join(BASE_DIR, "templates", "index.html")):
        if os.path.isfile(path):
            mtime = os.path.getmtime(path)
            if _index["path"] != path or _index["mtime"] != mtime:
                with open(path, "r", encoding="utf-8") as f:
                    html = f.read()
                _index.update(path=path, mtime=mtime, html=html,
                              etag=hashlib.md5(html.encode()).hexdigest()[:20])
            return _index["html"]
    return None


@app.get("/")
def home():
    html = load_index()
    if html is None:
        return Response("<h1>CardRadar</h1><p>index.html nebol nájdený.</p>", status=500, mimetype="text/html")
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    resp.set_etag(_index["etag"])
    return resp.make_conditional(request)   # opakovaná návšteva = len „304 Not Modified“


# =========================================================
# API PRE WEB
# =========================================================

@app.get("/api/config")
def api_config():
    resp = jsonify({
        "version": VERSION, "czk_per_eur": L.KURZ["CZK"], "alerts_enabled": strazca.ALERTS_ENABLED,
        "shops": [{"name": s["name"], "country": s["country"], "url": s["base_url"]} for s in O.active_shops()],
    })
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.get("/api/parse")
def api_parse():
    parsed = L.normalize_query(request.args.get("q", ""))
    parsed["type"] = L.classify_query(parsed)
    return jsonify(parsed)


@app.get("/api/search")
def api_search():
    original = L.clean_text(request.args.get("q", ""))[:150]
    if not original:
        return jsonify({"error": "Zadaj, čo chceš hľadať."}), 400
    if not LIMIT_SEARCH.allow():
        return too_many()
    parsed = L.normalize_query(original)
    normalized = parsed["normalized"] or original
    results, diagnostics = O.search_all(normalized)
    O.add_trends(results)
    # r=1 = stránka si potichu dopĺňa pomalé obchody, do „Najhľadanejšie“ sa to nepočíta
    O.save_history(results, log_query=None if request.args.get("r") else original)
    prices = [r["price_eur"] for r in results if r.get("price_eur")]
    payload = {
        "query": original, "normalized_query": normalized, "parsed": parsed, "results": results,
        "summary": {"count": len(results), "lowest_eur": min(prices) if prices else None},
        "czk_per_eur": L.KURZ["CZK"], "shops": O.shops_status(diagnostics),
        "query_lang": L.query_language(original),
    }
    if S.is_admin():
        payload["debug"] = diagnostics
    return jsonify(payload)


@app.get("/api/suggestions")
def api_suggestions():
    q = L.clean_text(request.args.get("q", ""))[:80]
    if len(q) < 2:
        return jsonify({"query": q, "suggestions": []})
    if not LIMIT_SUGGEST.allow():
        return too_many()
    return jsonify({"query": q, "normalized_query": L.normalize_query(q)["normalized"],
                    "suggestions": O.suggestions(q)})


@app.post("/api/images")
def api_images():
    if not LIMIT_IMAGES.allow():
        return too_many()
    links = json_body().get("links") or []
    if not isinstance(links, list):
        return jsonify({"error": "links musí byť zoznam"}), 400
    return jsonify({"images": O.images_for(links)})


@app.get("/api/history")
def api_history():
    if not LIMIT_HISTORY.allow():
        return too_many()
    link = L.clean_text(request.args.get("link", ""))
    if not O.is_allowed_link(link):
        return jsonify({"error": "Neplatný odkaz."}), 400
    return jsonify(O.price_history(link))


@app.post("/api/latest")
def api_latest():
    if not LIMIT_HISTORY.allow():
        return too_many()
    links = json_body().get("links") or []
    return jsonify({"latest": O.latest_prices(links if isinstance(links, list) else [])})


@app.get("/api/home")
def api_home():
    resp = jsonify(S.home_data())
    resp.headers["Cache-Control"] = "public, max-age=120"
    return resp


# =========================================================
# STRÁŽCA CENY
# =========================================================

def site_url():
    return strazca.PUBLIC_URL or request.url_root.rstrip("/")


@app.post("/api/alerts")
def api_alerts():
    if not LIMIT_ALERTS.allow():
        return too_many()
    d = json_body()
    code, body = strazca.create_alert(d.get("email", ""), d.get("link", ""), d.get("title", ""),
                                      d.get("shop", ""), d.get("target"), site_url())
    return jsonify(body), code


@app.get("/alerts/confirm")
def alerts_confirm():
    token = L.clean_text(request.args.get("token", ""))
    row = strazca.confirm_alert(token)
    if not row:
        return S.simple_page("Odkaz neplatí", "Tento strážca už neexistuje alebo bol odkaz zmenený.")
    target, title = row
    import html
    stop = html.escape(f"{site_url()}/alerts/stop?token={urllib.parse.quote(token)}")
    return S.simple_page("Strážca je zapnutý 🔔",
                         f"Napíšeme ti, keď {html.escape(title or 'produkt')} klesne na "
                         f"{strazca.eur(target)} alebo menej.<br><br><a href='{stop}'>Zrušiť strážcu</a>")


@app.get("/alerts/stop")
def alerts_stop():
    if strazca.stop_alert(L.clean_text(request.args.get("token", ""))):
        return S.simple_page("Strážca zrušený", "Viac ti o tomto produkte písať nebudeme.")
    return S.simple_page("Hotovo", "Tento strážca už bol zrušený.")


# =========================================================
# TEXTOVÉ STRÁNKY A ADMIN
# =========================================================

app.add_url_rule("/podmienky", "terms", S.terms_page)
app.add_url_rule("/ochrana-udajov", "privacy", S.privacy_page)
app.add_url_rule("/pre-obchody", "for_shops", S.shops_page)
app.add_url_rule("/robots.txt", "robots", S.robots_txt)
app.add_url_rule("/admin/test", "admin_test", S.admin_test)
app.add_url_rule("/admin/obchody", "admin_obchody", S.admin_obchody, methods=["GET", "POST"])
app.add_url_rule("/admin/katalog", "admin_katalog", S.admin_katalog)


@app.get("/admin/cache")
def admin_cache():
    """Stav pamäte; s &clear=1 ju vymaže (napr. po zmene filtrov)."""
    if not S.is_admin():
        return jsonify({"error": "Nepovolené."}), 403
    if request.args.get("clear"):
        O.clear_caches()
        return jsonify({"status": "ok", "message": "Pamäť vymazaná."})
    return jsonify({"search_cache": len(O._cache), "image_cache": len(O.image_cache),
                    "suggestions": len(O.SUGGESTIONS)})


@app.get("/health")
def health():
    return jsonify({
        "service": "CardRadar", "status": "ok", "version": VERSION,
        "index_exists": load_index() is not None, "html_parser": O.HTML_PARSER,
        "active_shops": [f"{s['name']} ({s['country']})" for s in O.active_shops()],
        "alerts_enabled": strazca.ALERTS_ENABLED, "czk_per_eur": L.KURZ["CZK"],
        "db_path": O.DB_PATH, "db_persistent": O.db_persistent(),
        **({"db_warning": O.DB_WARNING} if O.DB_WARNING else {}),
    })


# =========================================================
# LOGO, IKONY, MOBILNÁ APLIKÁCIA (PWA)
# =========================================================

ICON_V = "3"   # zvýš, keď zmeníš ikony – prehliadače si ich stiahnu znova
_MIMES = {".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".ico": "image/x-icon"}
_static_mem = {}


def _strip_png(data):
    """PNG bez metadát (menší súbor, obrázok rovnaký)."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return data
    out, i = [data[:8]], 8
    try:
        while i < len(data):
            length = int.from_bytes(data[i:i + 4], "big")
            ctype = data[i + 4:i + 8]
            if ctype not in (b"caBX", b"tEXt", b"iTXt", b"zTXt", b"eXIf"):
                out.append(data[i:i + 12 + length])
            i += 12 + length
            if ctype == b"IEND":
                break
    except Exception:
        return data
    return b"".join(out)


@app.get("/static/<path:name>")
def static_files(name):
    path = os.path.normpath(os.path.join(STATIC_DIR, name))
    mime = _MIMES.get(os.path.splitext(path)[1].lower())
    if not mime or not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
        return Response("Nenájdené", status=404, mimetype="text/plain")
    mtime = os.path.getmtime(path)
    hit = _static_mem.get(path)
    if not hit or hit[0] != mtime:
        with open(path, "rb") as f:
            data = f.read()
        hit = _static_mem[path] = (mtime, _strip_png(data) if mime == "image/png" else data)
    resp = Response(hit[1], mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.get("/favicon.ico")
def favicon():
    return static_files("favicon-32.png")


@app.get("/manifest.webmanifest")
def manifest():
    v = "?v=" + ICON_V
    data = {
        "name": "CardRadar – ceny Pokémon kariet", "short_name": "CardRadar",
        "description": "Porovnanie cien Pokémon kariet, ETB a booster boxov.",
        "start_url": "/?source=pwa", "scope": "/", "display": "standalone",
        "background_color": "#0a1422", "theme_color": "#101b44", "lang": "sk",
        "icons": [
            {"src": "/static/icon-192.png" + v, "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png" + v, "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-maskable-512.png" + v, "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    resp = Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# Offline kópia hlavnej stránky; ceny (/api/) vždy čerstvé zo siete.
SERVICE_WORKER = """
const CACHE = 'cardradar-v10';
const SHELL = ['/', '/static/icon-192.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/admin/')) return;
  if (e.request.mode === 'navigate') {
    if (url.pathname !== '/') return;
    e.respondWith(fetch(e.request).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put('/', copy)); }
      return r;
    }).catch(() => caches.match('/')));
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.match(e.request).then(m => m || fetch(e.request).then(r => {
      const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); return r;
    })));
  }
});
"""


@app.get("/sw.js")
def service_worker():
    resp = Response(SERVICE_WORKER, mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


# =========================================================
# PRED A PO KAŽDEJ POŽIADAVKE
# =========================================================

@app.before_request
def main_domain():
    """onrender.com a www. presmeruje na PUBLIC_URL (cesta aj parametre ostanú)."""
    public = strazca.PUBLIC_URL
    if not public or request.path == "/health":   # /health nechávame pre kontroly Renderu
        return None
    main = urllib.parse.urlparse(public).netloc.lower()
    host = request.host.split(":")[0].lower()
    if main and host != main and (host.endswith(".onrender.com") or host == "www." + main):
        return redirect(public + request.full_path.rstrip("?"), code=308)
    return None


@app.after_request
def finalize(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if not S.ALLOW_INDEXING:
        resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    # gzip pre texty a JSON
    if (resp.status_code == 200 and not resp.direct_passthrough
            and "Content-Encoding" not in resp.headers
            and "gzip" in request.headers.get("Accept-Encoding", "").lower()
            and resp.mimetype in ("application/json", "text/html", "application/javascript",
                                  "application/manifest+json", "image/svg+xml", "text/plain")):
        data = resp.get_data()
        if len(data) >= 1024:
            resp.set_data(gzip.compress(data, compresslevel=5))
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers.add("Vary", "Accept-Encoding")
    return resp


# =========================================================
# ŠTART
# =========================================================

O.init_db()
O.start_background()
S.start_background()
strazca.start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), threaded=True)
