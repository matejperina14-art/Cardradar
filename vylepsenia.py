"""
CARD RADAR – vylepšenia 6.20 (nad catalog_shops 6.18)

Opravy:
 - sety s množným číslom („30th Celebrations“ vs. „30th Celebration“) sa už nerozchádzajú
   – predtým hľadanie aj spájanie ponúk z rôznych obchodov tieto produkty vynechalo
 - strážca naskladnenia neposiela falošné e-maily, keď sa sklad na stránke nedá zistiť
 - opakované tiché dopytovanie stránky (pomalé obchody) už nenafukuje „Najhľadanejšie“
 - porovnanie ADMIN_KEY odolné voči časovému útoku
Zrýchlenie:
 - výsledky filtrov (merch, TCG, jazyk, normalizácia, spájanie) sa pamätajú,
   hľadanie v katalógoch je výrazne rýchlejšie
 - index.html s ETag: opakovaná návšteva stiahne len „304 Not Modified“
 - logo.svg bez 7 KB zbytočných metadát
 - SQLite v režime synchronous=NORMAL (rýchlejšie zápisy, s WAL bezpečné)
Bezpečnosť: X-Frame-Options, Permissions-Policy

INŠTALÁCIA – v app.py pod riadky
    import catalog_shops
    catalog_shops.install(globals())
pridaj:
    import vylepsenia
    vylepsenia.install(globals())
"""

import hashlib
import json
import os
import hmac
import re
import sqlite3
import time
from datetime import datetime, timezone
from functools import lru_cache

import catalog_shops as cs

G = {}
_orig_set_matches = None


# ---------- sety: jednotné a množné číslo ----------

def _stem(w):
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def _stem_words(text):
    return {_stem(w) for w in cs._fold_words(text)}


def set_matches_text(searchable, set_name):
    if _orig_set_matches(searchable, set_name):
        return True
    want = _stem_words(set_name)
    return bool(want) and want.issubset(_stem_words(searchable))


EXTRA_ALIASES = {
    "30th celebrations": "30th celebration",
    "30th anniversary celebration": "30th celebration",
    "30th anniversary celebrations": "30th celebration",
}


def _install_sets(g):
    global _orig_set_matches
    _orig_set_matches = cs.set_matches_text
    g["SET_ALIASES"].update(EXTRA_ALIASES)
    known = [s for s in g["KNOWN_SETS"] if s not in EXTRA_ALIASES]
    g["KNOWN_SETS"][:] = sorted(set(known), key=len, reverse=True)
    g["_SETS_RE"] = g["_compile_aliases"](g["SET_ALIASES"].items())
    g["_KNOWN_SETS_RE"] = g["_compile_aliases"]((c, c) for c in g["KNOWN_SETS"])
    cs.set_matches_text = set_matches_text           # používa catalog_shops (hľadanie, úvod)
    g["set_matches_text"] = cs.G["set_matches_text"] = set_matches_text


# ---------- pamäť pre drahé čisté funkcie ----------

def _cached_str(fn, size=20000):
    return lru_cache(maxsize=size)(fn)


def _install_caches(g):
    for name in ("merch_reason", "looks_like_tcg", "detect_language", "estimate_packs",
                 "group_key", "is_combo"):
        fast = _cached_str(g[name])
        g[name] = fast
        cs.G[name] = fast

    norm = lru_cache(maxsize=5000)(g["normalize_query"])

    def normalize_query(query):
        return dict(norm(query))   # kópia: volajúci si výsledok môže upraviť

    g["normalize_query"] = cs.G["normalize_query"] = normalize_query


# ---------- databáza ----------

def db_connect():
    conn = sqlite3.connect(G["_g"]["DB_PATH"], timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# ---------- štatistika hľadaní bez opakovaní ----------

def _make_save_history(orig):
    def save_history(query, results):
        try:
            from flask import request
            retry = bool(request.args.get("r"))
        except Exception:
            retry = False
        if not retry:
            return orig(query, results)
        if results:   # ceny uložíme, hľadanie do štatistiky nie
            G["_g"]["BG_EXECUTOR"].submit(G["_g"]["_save_history"], [dict(r) for r in results])
    return save_history


# ---------- admin ----------

def is_admin():
    from flask import request
    key = G["_g"]["ADMIN_KEY"]
    if not key:
        return False
    return hmac.compare_digest(request.args.get("key", "").encode(), key.encode())


# ---------- strážca ceny ----------

def _should_notify(price, stock, target):
    if not price or price > target or stock == "out":
        return False
    if stock in ("in", "preorder", "order"):
        return True
    # sklad sa nedal zistiť: upozorníme len pri skutočnom poklese pod cieľ,
    # inak by strážca naskladnenia (cieľ = aktuálna cena) poslal e-mail hneď
    return price < target - 0.005


def check_alerts_once():
    g = G["_g"]
    conn = cs._db()
    try:
        alerts = conn.execute(
            "SELECT id, email, link, title, shop, target, token, site FROM alerts "
            "WHERE confirmed = 1 AND notified IS NULL").fetchall()
    finally:
        conn.close()

    offers = {}
    for link in {a[2] for a in alerts}:
        try:
            offers[link] = g["fetch_product_offer"](link)
        except Exception:
            offers[link] = (None, "")
        time.sleep(1)

    now = datetime.now(timezone.utc).isoformat()
    conn = cs._db()
    try:
        for aid, email, link, title, shop, target, token, site in alerts:
            price, stock = offers.get(link, (None, ""))
            conn.execute("UPDATE alerts SET last_price = ?, last_checked = ? WHERE id = ?",
                         (price, now, aid))
            if price:
                conn.execute("""
                    INSERT INTO price_daily (link, day, shop, title, price_eur, stock)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(link, day) DO UPDATE SET price_eur = excluded.price_eur,
                        stock = excluded.stock
                """, (link, g["today_str"](), shop, title, price, stock))
            if _should_notify(price, stock, target):
                try:
                    cs._drop_mail(email, title, shop, link, target, price, token,
                                  g["PUBLIC_URL"] or site or "")
                    conn.execute("UPDATE alerts SET notified = ? WHERE id = ?", (now, aid))
                except Exception:
                    pass
        conn.commit()
    finally:
        conn.close()


# ---------- vlastné logo a ikony (6.20) ----------
# Súbory nahraté do priečinka static/ (icon-192.png, favicon-32.png...) majú
# prednosť pred ikonami zabudovanými v app.py. Nové logo = len nahrať súbory.

ICON_V = "2"   # zvýš, keď zmeníš ikony, aby si ich prehliadače stiahli znova


def _install_static_overrides(g):
    folder = os.path.join(g["BASE_DIR"], "static")
    if not os.path.isdir(folder):
        return
    mimes = {".png": "image/png", ".svg": "image/svg+xml", ".webp": "image/webp",
             ".jpg": "image/jpeg", ".ico": "image/x-icon"}
    for name in os.listdir(folder):
        mime = mimes.get(os.path.splitext(name)[1].lower())
        path = os.path.join(folder, name)
        if mime and os.path.isfile(path):
            with open(path, "rb") as f:
                g["EMBEDDED_STATIC"][name] = (f.read(), mime)


def manifest():
    from flask import Response
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


_NEON_HEADER = ("header a{font-weight:800;font-size:21px;letter-spacing:-.01em;text-decoration:none;"
                "color:#38d8ff;text-shadow:0 0 4px rgba(56,216,255,.55),0 0 14px rgba(56,216,255,.45)}")


def _install_branding(g):
    _install_static_overrides(g)
    app = g["app"]
    if "manifest" in app.view_functions:
        app.view_functions["manifest"] = manifest
    # nová verzia offline pamäte, aby sa staré ikony v telefónoch vymenili
    cs.SERVICE_WORKER_FIX = re.sub(r"cardradar-v\d+", "cardradar-v9", cs.SERVICE_WORKER_FIX)
    # neónový názov aj na stránkach Podmienky, Ochrana údajov, Pre obchody
    cs._PAGE_CSS = re.sub(r"header a\{[^}]*\}", _NEON_HEADER, cs._PAGE_CSS, count=1)


# ---------- web ----------

def _wrap_home(app):
    orig = app.view_functions.get("home")
    if not orig:
        return

    def home():
        from flask import request
        resp = orig()
        if resp.status_code == 200:
            resp.set_etag(hashlib.md5(resp.get_data()).hexdigest()[:20])
            resp.make_conditional(request)
        return resp

    app.view_functions["home"] = home


def _wrap_health(app):
    orig = app.view_functions.get("health")
    if not orig:
        return

    def health():
        from flask import jsonify
        resp = orig()
        try:
            data = resp.get_json() or {}
        except Exception:
            return resp
        data["improvements"] = "6.20"
        return jsonify(data)

    app.view_functions["health"] = health


def _security_headers(resp):
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return resp


def install(g):
    G.update(g)
    G["_g"] = g

    _install_sets(g)
    _install_caches(g)

    g["db_connect"] = db_connect
    cs._db = db_connect

    g["save_history"] = _make_save_history(g["save_history"])
    g["is_admin"] = cs.G["is_admin"] = is_admin
    g["check_alerts_once"] = check_alerts_once

    _install_branding(g)

    data, mime = g["EMBEDDED_STATIC"]["logo.svg"]
    g["EMBEDDED_STATIC"]["logo.svg"] = (
        re.sub(rb"<metadata>.*?</metadata>", b"", data, flags=re.S), mime)

    app = g["app"]
    _wrap_home(app)
    _wrap_health(app)
    app.after_request(_security_headers)
