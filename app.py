"""
CARD RADAR 7.9 – app.py
Spúšťa web a obsahuje všetky adresy (routy). Logika je v ostatných súboroch:
  logika.py   rozpoznávanie hľadania, filtre, sklad, ceny
  obchody.py  obchody, sťahovanie, katalógy, hľadanie, databáza
  strazca.py  strážca ceny a e-maily
  stranky.py  úvodná stránka, textové stránky, admin
  index.html  samotný web
  static/     logo a ikony

Premenné prostredia (Render → Environment):
  ADMIN_KEY      heslo k /admin/... stránkam. Stačí raz otvoriť /admin/test?key=HESLO –
                 prehliadač si prihlásenie zapamätá na 30 dní a heslo z adresy zmizne.
                 Odhlásenie: /admin/logout
  PUBLIC_URL     hlavná adresa, napr. https://getcardradar.com
  DB_PATH        databáza na trvalom disku, napr. /var/data/cardradar.db
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM   e-maily strážcu
  ALLOW_INDEXING 1 = web môže byť v Google
  OPERATOR_NAME, CONTACT_EMAIL   do podmienok a ochrany údajov
"""

import gzip
import hashlib
import hmac
import html
import io
import json
import os
import threading
import time
import urllib.parse
from collections import deque

from flask import Flask, Response, jsonify, redirect, request

try:
    from PIL import Image   # Pillow: z loga vyrobí ikony v správnych veľkostiach (requirements.txt: Pillow)
except ImportError:
    Image = None
try:
    from pillow_heif import register_heif_opener   # fotky z iPhonu (HEIC), requirements.txt: pillow-heif
    register_heif_opener()
except Exception:
    pass

import logika as L
import obchody as O
import strazca
import stranky as S

VERSION = "7.9"
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


MAX_RESULTS = 600   # najviac ponúk v jednej odpovedi (najlacnejšie)
LIMIT_SEARCH = RateLimiter(30)
LIMIT_RETRY = RateLimiter(90)    # tiché dopĺňanie pomalých obchodov (r=1) sa nepočíta do LIMIT_SEARCH
LIMIT_SUGGEST = RateLimiter(120)
LIMIT_IMAGES = RateLimiter(60)
LIMIT_HISTORY = RateLimiter(60)
LIMIT_ALERTS = RateLimiter(5, window=600)
LIMIT_TOKEN = RateLimiter(20)   # potvrdenie / zrušenie strážcu (proti skúšaniu tokenov)
LIMIT_GO = RateLimiter(60)   # počítanie klikov (presmerovanie funguje vždy, nad limit sa len nezapočíta)


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


def kurz_info():
    """Aktuálny kurz pre web: 1 € = x Kč a dátum kurzu ECB."""
    return {"czk_per_eur": L.KURZ["CZK"], "czk_date": L.KURZ_INFO.get("date", ""),
            "czk_source": L.KURZ_INFO.get("source", "")}


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
                    page_html = f.read()
                _index.update(path=path, mtime=mtime, html=page_html,
                              etag=hashlib.md5(page_html.encode()).hexdigest()[:20])
            return _index["html"]
    return None


@app.get("/")
def home():
    page_html = load_index()
    if page_html is None:
        return Response("<h1>CardRadar</h1><p>index.html nebol nájdený.</p>", status=500, mimetype="text/html")
    resp = Response(page_html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    resp.set_etag(_index["etag"])
    return resp.make_conditional(request)   # opakovaná návšteva = len „304 Not Modified“


# =========================================================
# API PRE WEB
# =========================================================

@app.get("/api/config")
def api_config():
    O.ensure_czk()
    resp = jsonify({
        "version": VERSION, **kurz_info(), "alerts_enabled": strazca.ALERTS_ENABLED,
        "shops": [{"name": s["name"], "country": s["country"], "url": s["base_url"]} for s in O.active_shops()],
    })
    # kratšie, aby web nedržal starý kurz
    resp.headers["Cache-Control"] = "public, max-age=60"
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
    if not (LIMIT_RETRY if request.args.get("r") else LIMIT_SEARCH).allow():
        return too_many()
    parsed = L.normalize_query(original)
    normalized = parsed["normalized"] or original
    results, diagnostics = O.search_all(normalized)
    # r=1 = stránka si potichu dopĺňa pomalé obchody, do „Najhľadanejšie“ sa to nepočíta
    O.save_history(results, log_query=None if request.args.get("r") else original)
    # celkový počet aj s ponukami, ktoré katalógy pre veľké množstvo neposlali
    total = len(results) + sum(max(0, (d.get("total_matches") or 0) - (d.get("results") or 0)) for d in diagnostics)
    results = results[:MAX_RESULTS]   # „scarlet violet“ = tisíce ponúk; web by ich nevedel naraz zobraziť
    O.add_trends(results)
    prices = [r["price_eur"] for r in results if r.get("price_eur")]
    payload = {
        "query": original, "normalized_query": normalized, "parsed": parsed, "results": results,
        "summary": {"count": len(results), "total": total, "lowest_eur": min(prices) if prices else None},
        **kurz_info(), "shops": O.shops_status(diagnostics),
        "query_lang": L.query_language(original),
    }
    if request.args.get("debug") and S.is_admin():   # /?q=...&debug=1 len pre admina
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
    data = dict(S.home_data())
    data["kurz_czk"] = {"rate": L.KURZ["CZK"], "date": L.KURZ_INFO.get("date", "")}   # vždy aktuálny
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "public, max-age=120"
    return resp


# =========================================================
# STRÁŽCA CENY
# Potvrdenie aj zrušenie: odkaz z e-mailu (GET) len ukáže stránku s tlačidlom,
# zmena sa urobí až po kliknutí (POST). E-mailové služby a antivíry otvárajú
# odkazy v e-mailoch automaticky – inak by strážcu zapli alebo zrušili samy.
# =========================================================

def site_url():
    """Adresa webu do e-mailov a odkazov (za Renderom príde požiadavka ako http, preto https)."""
    return strazca._site(strazca.PUBLIC_URL or request.url_root)


def _token():
    return L.clean_text(request.values.get("token", ""))[:100]


def _token_page(title, text, action, token, button):
    """Stránka s jedným tlačidlom, ktoré pošle token cez POST."""
    body = (f"<div class='box'><h1>{html.escape(title)}</h1><p>{text}</p>"
            f"<form method='post' action='{action}'>"
            f"<input type='hidden' name='token' value='{html.escape(token)}'>"
            f"<button type='submit' style='font-size:16px;padding:12px 22px'>{html.escape(button)}</button>"
            f"</form></div>")
    return S.page(title, body, back=False)


def _alert_goal(target, title):
    name = html.escape(title or "produkt")
    return (f"bude <b>{name}</b> znova skladom" if strazca.is_stock_alert(target)
            else f"<b>{name}</b> klesne na {html.escape(strazca.eur(target))} alebo menej")


@app.post("/api/alerts")
def api_alerts():
    if not LIMIT_ALERTS.allow():
        return too_many()
    d = json_body()
    kind = "stock" if d.get("type") == "stock" else "price"   # type: "stock" = strážca naskladnenia
    code, body = strazca.create_alert(d.get("email", ""), d.get("link", ""), d.get("title", ""),
                                      d.get("shop", ""), d.get("target"), site_url(), kind)
    return jsonify(body), code


@app.route("/alerts/confirm", methods=["GET", "POST"])
def alerts_confirm():
    if not LIMIT_TOKEN.allow():
        return S.simple_page("Chvíľu počkaj", "Príliš veľa pokusov. Skús to o minútu.")
    token = _token()
    info = strazca.alert_by_token(token)
    if not info:
        return S.simple_page("Odkaz neplatí", "Tento strážca už neexistuje alebo bol odkaz zmenený.")
    target, title, confirmed = info
    stop = html.escape(f"{site_url()}/alerts/stop?token={urllib.parse.quote(token)}")

    if request.method == "POST" or confirmed:
        if not confirmed:
            strazca.confirm_alert(token)
        return S.simple_page("Strážca je zapnutý 🔔",
                             f"Napíšeme ti, keď {_alert_goal(target, title)}.<br><br>"
                             f"<a href='{stop}'>Zrušiť strážcu</a>")
    return _token_page("Potvrď strážcu", f"Napíšeme ti, keď {_alert_goal(target, title)}.",
                       "/alerts/confirm", token, "Potvrdiť strážcu")


@app.route("/alerts/stop", methods=["GET", "POST"])
def alerts_stop():
    if not LIMIT_TOKEN.allow():
        return S.simple_page("Chvíľu počkaj", "Príliš veľa pokusov. Skús to o minútu.")
    token = _token()
    # POST: tlačidlo na stránke alebo „Odhlásiť“ priamo v Gmaile / Apple Mail (List-Unsubscribe-Post)
    if request.method == "POST":
        if strazca.stop_alert(token):
            return S.simple_page("Strážca zrušený", "Viac ti o tomto produkte písať nebudeme.")
        return S.simple_page("Hotovo", "Tento strážca už bol zrušený.")
    info = strazca.alert_by_token(token)
    if not info:
        return S.simple_page("Hotovo", "Tento strážca už bol zrušený.")
    target, title, _ = info
    return _token_page("Zrušiť strážcu?", f"Prestaneme ti písať, keď {_alert_goal(target, title)}.",
                       "/alerts/stop", token, "Áno, zrušiť strážcu")


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
app.add_url_rule("/admin/report", "admin_report", S.admin_report)
app.add_url_rule("/sety", "sets", S.sets_page)
app.add_url_rule("/set/<slug>", "set_detail", S.set_page)
app.add_url_rule("/sitemap.xml", "sitemap", S.sitemap_xml)


@app.get("/go")
def go():
    """Odchod do obchodu: započíta klik a presmeruje (s utm alebo partnerským odkazom)."""
    link = L.clean_text(request.args.get("u", ""))
    if not O.is_allowed_link(link):
        return redirect("/", code=302)
    if LIMIT_GO.allow():
        O.count_click(link)
    resp = redirect(O.out_url(link), code=302)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@app.get("/admin/logout")
def admin_logout():
    resp = S.page("Odhlásené", "<div class='box'><h1>Odhlásené</h1><p>Admin prihlásenie je z tohto "
                               "prehliadača vymazané.</p></div>", back=True)
    resp.delete_cookie(S.ADMIN_COOKIE, path="/")
    return resp


@app.get("/admin/obchod")
def admin_obchod():
    """Diagnostika: /admin/obchod?name=all&q=pikachu – priamo vyskúša obchody (bez cache)
    a ukáže stav, HTTP kód, chybu, čas sťahovania a spracovania."""
    if not S.is_admin():
        return jsonify({"error": "Nepovolené."}), 403
    q = L.clean_text(request.args.get("q", "")) or "pikachu"
    norm = L.normalize_query(q)["normalized"] or q
    name = L.clean_text(request.args.get("name", "all")).lower()
    shops = [s for s in O.active_shops() if name == "all" or L.fold(s["name"]) == L.fold(name)]
    if not shops:
        return jsonify({"error": "Neznámy obchod.", "shops": [s["name"] for s in O.active_shops()]}), 400

    def one(shop):
        t0 = time.monotonic()
        extra = {}
        try:
            if O.is_catalog(shop):
                res, dbg = O.catalog_scrape(shop, norm)
                items, updated = O.load_catalog(shop["name"])
                extra = {"katalog_poloziek": len(items), "katalog_aktualizovany": updated}
                if shop.get("search_url"):   # porovnanie: koľko by našlo vyhľadávanie obchodu
                    sres, sdbg = O.scrape_search(shop, norm, 15, fetch_q=O.shop_query(norm))
                    extra.update(vyhladavanie_vysledkov=len(sres), vyhladavanie_status=sdbg.get("status"),
                                 vyhladavanie_stran=sdbg.get("pages"))
            else:
                res, dbg = O.scrape_search(shop, norm, 15, fetch_q=O.shop_query(norm))
                extra = {"poslane_do_obchodu": O.shop_query(norm), "stran": dbg.get("pages")}
        except Exception as e:
            res, dbg = [], {"status": "exception", "error": str(e)[:300]}
        return {"shop": shop["name"], "typ": "katalóg" if O.is_catalog(shop) else "vyhľadávanie", **extra,
                "status": dbg.get("status"), "http_status": dbg.get("http_status"), "error": dbg.get("error"),
                "spolu_ms": round((time.monotonic() - t0) * 1000), "stiahnutie_ms": dbg.get("fetch_ms"),
                "odkazov_na_stranke": dbg.get("links_scanned"), "vysledkov": len(res),
                "ukazka": [f"{r['title']} – {r['price_eur']} €" for r in res[:4]],
                "vyradene": [f"{d.get('title', '')[:60]} → {d.get('reason')}" for d in dbg.get("sample_decisions", [])
                             if d.get("decision") == "filtered"][:6]}

    from concurrent.futures import ThreadPoolExecutor as _TPE
    with _TPE(max_workers=8) as ex:
        out = list(ex.map(one, shops))
    return jsonify({"hladanie": norm, "pools": O.pool_stats(), "obchody": out})


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


@app.get("/admin/kurz")
def admin_kurz():
    """Okamžite načíta kurz z ECB a ukáže ho."""
    if not S.is_admin():
        return jsonify({"error": "Nepovolené."}), 403
    ok = O.update_czk()
    return jsonify({"updated": ok, **kurz_info()})


def _static_report():
    """Ktoré súbory sú v static/ (na kontrolu loga a ikon)."""
    if not os.path.isdir(STATIC_DIR):
        return {"static_exists": False, "static_files": []}
    files = sorted(os.listdir(STATIC_DIR))[:50]
    used = {}
    for role in ICON_ROLES:
        p = _auto_pick(role)
        used["cr-logo.png" if role == "logo" else role] = os.path.basename(p) if p else None
    return {"static_exists": True, "static_files": files,
            "icons_used": used,
            "static_missing": [f for f, v in used.items() if not v]}


@app.get("/health")
def health():
    """Verejne len stav (pre Render). Detail vidí prihlásený admin."""
    problems = []
    if load_index() is None:
        problems.append("index.html chýba")
    if O.db_problem():
        problems.append("databáza")
    basic = {"service": "CardRadar", "status": "ok" if not problems else "warning", "version": VERSION}
    if not S.is_admin():
        return jsonify(basic)
    return jsonify({
        **basic, "problems": problems,
        "index_exists": load_index() is not None, "html_parser": O.HTML_PARSER,
        "active_shops": [f"{s['name']} ({s['country']})" for s in O.active_shops()],
        "alerts_enabled": strazca.ALERTS_ENABLED, **kurz_info(),
        "db_path": O.DB_PATH, "db_persistent": O.db_persistent(),
        **({"db_warning": O.db_problem()} if O.db_problem() else {}),
        "pools": O.pool_stats(),
        **_static_report(), "icons": icon_report(),
    })


# =========================================================
# LOGO, IKONY, MOBILNÁ APLIKÁCIA (PWA)
# =========================================================

ICON_V = "6"   # zvýš, keď zmeníš ikony – prehliadače si ich stiahnu znova
_MIMES = {".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".ico": "image/x-icon",
          ".gif": "image/gif", ".avif": "image/avif",
          ".css": "text/css", ".js": "application/javascript", ".json": "application/json",
          ".woff2": "font/woff2", ".woff": "font/woff", ".txt": "text/plain"}
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


def _find_static(name):
    """Súbor v static/. Ak nesedí veľkosť písmen (Logo.PNG vs logo.png), nájde ho aj tak –
    na Windows to funguje, ale Render (Linux) veľké a malé písmená rozlišuje."""
    path = os.path.normpath(os.path.join(STATIC_DIR, name))
    if not path.startswith(STATIC_DIR + os.sep):
        return None
    if os.path.isfile(path):
        return path
    folder, base = os.path.split(path)
    if os.path.isdir(folder):
        for f in os.listdir(folder):
            if f.lower() == base.lower() and os.path.isfile(os.path.join(folder, f)):
                return os.path.join(folder, f)
    return None


# =========================================================
# IKONY Z LOGA
# Všetky ikony (záložka, iPhone, Android, nová karta) sa vyrábajú z static/cr-logo.png
# v presných veľkostiach a ako skutočné PNG / ICO – aj keď je logo veľké alebo v inom
# formáte (WebP, JPG). Bez Pillow sa pošle samotné logo.
# =========================================================

ICON_FALLBACK = "cr-logo.png"
LOGO_ALTERNATIVES = ["icon-512.png", "icon-192.png", "apple-touch-icon.png", "icon-maskable-512.png", "favicon-32.png"]
SAFE_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}   # tieto zobrazí každý prehliadač


# Rola -> (presný názov, požadovaný rozmer, priehľadné pozadie?)
ICON_ROLES = {
    "logo":                  ("cr-logo.png", None, True),
    "favicon-32.png":        ("favicon-32.png", 32, None),
    "apple-touch-icon.png":  ("apple-touch-icon.png", 180, None),
    "icon-192.png":          ("icon-192.png", 192, None),
    "icon-512.png":          ("icon-512.png", 512, True),
    "icon-maskable-512.png": ("icon-maskable-512.png", 512, False),
}
_scan = {"key": None, "images": []}


def _image_info(path):
    """(šírka, výška, priehľadný roh?) – PNG aj bez Pillow, ostatné cez Pillow."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
        w = h = None
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
        transparent = None
        if Image is not None:
            with Image.open(path) as im:
                w, h = im.size
                rgba = im.convert("RGBA")
                transparent = rgba.getpixel((0, 0))[3] < 128
        return (w, h, transparent) if w and h else None
    except Exception:
        return None


def _static_images():
    """Obrázky v static/ s rozmermi (pamätá si ich, kým sa priečinok nezmení)."""
    if not os.path.isdir(STATIC_DIR):
        return []
    files = sorted(f for f in os.listdir(STATIC_DIR)
                   if os.path.splitext(f)[1].lower() in (".png", ".webp", ".jpg", ".jpeg", ".gif"))
    key = tuple((f, os.path.getmtime(os.path.join(STATIC_DIR, f))) for f in files)
    if key != _scan["key"]:
        imgs = []
        for f in files:
            info = _image_info(os.path.join(STATIC_DIR, f))
            if info:
                imgs.append((f,) + info)
        _scan.update(key=key, images=imgs)
    return _scan["images"]


def _auto_pick(role):
    """Súbor pre rolu: presný názov, inak obrázok so správnym rozmerom (názov nevadí)."""
    exact, size, transparent = ICON_ROLES[role]
    p = _find_static(exact)
    if p:
        return p
    squares = [i for i in _static_images() if i[1] == i[2]]
    if role == "logo":
        named = [i for i in squares if "logo" in i[0].lower()]
        cands = named or [i for i in squares if i[3]] or squares
        cands = sorted(cands, key=lambda i: -i[1])          # najväčšie
    else:
        cands = [i for i in squares if i[1] == size]
        if transparent is not None and len(cands) > 1:
            cands = sorted(cands, key=lambda i: i[3] is not transparent)
    return os.path.join(STATIC_DIR, cands[0][0]) if cands else None


def _logo_source():
    """Súbor s logom: cr-logo.png, inak súbor s „logo“ v názve, inak niektorá ikona.
    Web tak ukáže logo, aj keď sa súbor v GitHube premenuje."""
    p = _auto_pick("logo")
    if p:
        return p
    if os.path.isdir(STATIC_DIR):
        for f in sorted(os.listdir(STATIC_DIR)):
            if "logo" in f.lower() and os.path.splitext(f)[1].lower() in _MIMES:
                return os.path.join(STATIC_DIR, f)
    for name in LOGO_ALTERNATIVES:
        p = _find_static(name)
        if p:
            return p
    return None


ICON_SIZES = {"favicon-16.png": 16, "favicon-32.png": 32, "favicon-48.png": 48,
              "apple-touch-icon.png": 180, "apple-touch-icon-precomposed.png": 180,
              "icon-192.png": 192, "icon-512.png": 512, "icon-maskable-512.png": 512}
ICON_BG = (16, 27, 68, 255)   # tmavomodrá ako hlavička webu (iPhone nevie priehľadné ikony)
_icon_mem = {}


def _magic_mime(data):
    """Skutočný formát obrázka podľa obsahu, nie podľa koncovky súboru."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return None


def _make_icon(name):
    """(dáta, mime) ikony vyrobenej z loga, alebo None."""
    src = _logo_source()
    if not src or Image is None:
        return None
    key = (name, src, os.path.getmtime(src))
    if key in _icon_mem:
        return _icon_mem[key]
    out = None
    try:
        with Image.open(src) as im:
            im.load()
            logo = im.convert("RGBA")
        bbox = logo.getbbox()   # odstráni prázdny priehľadný okraj okolo loga
        if bbox:
            logo = logo.crop(bbox)
        buf = io.BytesIO()
        if name == "logo":
            if max(logo.size) > 512:
                logo.thumbnail((512, 512), Image.LANCZOS)
            logo.save(buf, format="PNG", optimize=True)
            out = (buf.getvalue(), "image/png")
        elif name == "favicon.ico":
            canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
            inner = logo.copy()
            inner.thumbnail((256, 256), Image.LANCZOS)
            canvas.alpha_composite(inner, ((256 - inner.width) // 2, (256 - inner.height) // 2))
            canvas.save(buf, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])
            out = (buf.getvalue(), "image/x-icon")
        else:
            size = ICON_SIZES[name]
            solid = name.startswith("apple-touch") or "maskable" in name
            pad = int(size * (0.14 if "maskable" in name else 0.08 if solid else 0))
            canvas = Image.new("RGBA", (size, size), ICON_BG if solid else (0, 0, 0, 0))
            inner = logo.copy()
            inner.thumbnail((size - 2 * pad, size - 2 * pad), Image.LANCZOS)
            canvas.alpha_composite(inner, ((size - inner.width) // 2, (size - inner.height) // 2))
            if solid:
                canvas = canvas.convert("RGB")
            canvas.save(buf, format="PNG", optimize=True)
            out = (buf.getvalue(), "image/png")
    except Exception as e:
        print(f"[CardRadar] Ikona {name} sa nedala vyrobiť z loga: {e}", flush=True)
    _icon_mem[key] = out
    return out


def _icon_response(data, mime):
    resp = Response(data, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def icon_report():
    """Pre /health: stav loga a ikon."""
    src = _logo_source()
    files = sorted(os.listdir(STATIC_DIR)) if os.path.isdir(STATIC_DIR) else []
    if not src:
        return {"logo": "CHÝBA – v static/ nie je cr-logo.png ani iný obrázok loga", "static": files}
    with open(src, "rb") as f:
        head = f.read(16)
    info = {"logo": os.path.basename(src), "logo_kb": round(os.path.getsize(src) / 1024),
            "logo_format": _magic_mime(head) or "neznámy (prehliadač ho nemusí vedieť zobraziť)",
            "icons_from_logo": Image is not None}
    if os.path.basename(src) != ICON_FALLBACK:
        info["poznamka"] = f"logo nájdené podľa rozmeru: {os.path.basename(src)}"
    if Image is not None:
        try:
            with Image.open(src) as im:
                info["logo_px"] = f"{im.width}x{im.height}"
        except Exception as e:
            info["logo_error"] = str(e)[:120]
    else:
        info["tip"] = "Pridaj riadok Pillow do requirements.txt – ikony budú v presných veľkostiach."
    return info


@app.get("/static/<path:name>")
def static_files(name):
    base = os.path.basename(name).lower()
    if base in ICON_ROLES and not _find_static(name):
        found = _auto_pick(base)                         # napr. IMG_0857.png má 32×32 -> favicon
        if found:
            name = os.path.relpath(found, STATIC_DIR)
    if base in ICON_SIZES and not _find_static(name):   # vlastná ikona v static/ má prednosť
        made = _make_icon(base)
        if made:
            return _icon_response(*made)
    if base == ICON_FALLBACK:
        src = _logo_source()
        if src:
            with open(src, "rb") as f:
                kind = _magic_mime(f.read(16))
            if kind not in SAFE_IMAGES:
                made = _make_icon("logo")   # HEIC / AVIF -> PNG
                if made:
                    return _icon_response(*made)
            name = os.path.relpath(src, STATIC_DIR)
    path = _find_static(name)
    if not path and base in ICON_SIZES:
        path = _find_static(ICON_FALLBACK)
    mime = _MIMES.get(os.path.splitext(path or name)[1].lower())
    if not mime or not path:
        resp = Response("Nenájdené", status=404, mimetype="text/plain")
        resp.headers["Cache-Control"] = "no-store"   # chýbajúci súbor si nikto nesmie zapamätať
        return resp
    mtime = os.path.getmtime(path)
    hit = _static_mem.get(path)
    if not hit or hit[0] != mtime:
        with open(path, "rb") as f:
            data = f.read()
        hit = _static_mem[path] = (mtime, _strip_png(data) if mime == "image/png" else data)
    if mime.startswith("image/"):
        mime = _magic_mime(hit[1][:16]) or mime   # WebP / JPG s koncovkou .png
    resp = Response(hit[1], mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/favicon.ico")
def favicon():
    made = _make_icon("favicon.ico")
    return _icon_response(*made) if made else static_files("favicon-32.png")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def apple_icon():
    return static_files("apple-touch-icon.png")   # iPhone sa pýta aj priamo na tieto adresy


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
# Obrázky zo static/: najprv sieť (nové logo sa ukáže hneď), cache len keď je offline.
# Ukladajú sa len úspešné odpovede – predtým sa uložila aj chyba 404 a logo potom chýbalo navždy.
SERVICE_WORKER = """
const CACHE = 'cardradar-v14';
self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.add('/')).catch(() => {}).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
function save(req, r) {
  if (r && r.ok && r.type === 'basic') { const copy = r.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
  return r;
}
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/admin/')) return;
  if (e.request.mode === 'navigate') {
    if (url.pathname !== '/') return;
    e.respondWith(fetch(e.request).then(r => save('/', r)).catch(() => caches.match('/')));
    return;
  }
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(fetch(e.request).then(r => r.ok ? save(e.request, r) : caches.match(e.request).then(m => m || r))
      .catch(() => caches.match(e.request)));
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


@app.before_request
def admin_login():
    """/admin/...?key=HESLO: uloží prihlásenie do cookie a presmeruje na adresu BEZ hesla
    (heslo tak neostane v histórii prehliadača, v záložkách ani v logoch)."""
    if not (request.path.startswith("/admin/") or request.path == "/health") or request.method != "GET":
        return None
    key = request.args.get("key", "")
    if not key or not S.ADMIN_KEY or not hmac.compare_digest(key.encode(), S.ADMIN_KEY.encode()):
        return None
    args = [(k, v) for k, v in request.args.items(multi=True) if k != "key"]
    resp = redirect(request.path + ("?" + urllib.parse.urlencode(args) if args else ""), code=303)
    local = request.host.split(":")[0] in ("localhost", "127.0.0.1")
    resp.set_cookie(S.ADMIN_COOKIE, S.admin_cookie_value(), max_age=30 * 86400, path="/",
                    secure=not local, httponly=True, samesite="Lax")
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
                                  "application/manifest+json", "image/svg+xml", "text/plain", "text/css")):
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
if O.db_problem():
    print("[CardRadar] VAROVANIE: " + O.db_problem(), flush=True)
O.start_background()
S.start_background()
strazca.start_background()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), threaded=True)
