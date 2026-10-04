"""
CARD RADAR – obchody v katalógovom režime (6.1)
================================================
iHRYsko, imago a Herný svet nemajú vyhľadávanie, ktoré by sa dalo spoľahlivo
čítať, preto ich CardRadar raz za hodinu prejde cez kategóriu Pokémon TCG
(aj ďalšie strany), uloží produkty do databázy a pri hľadaní filtruje lokálne.
Výhoda: hľadanie je okamžité a obchody dostanú len pár požiadaviek za hodinu.

INŠTALÁCIA (jediná zmena v app.py):
    na úplný koniec app.py, TESNE PRED riadok  if __name__ == "__main__":
    pridaj tieto dva riadky:

        import catalog_shops
        catalog_shops.install(globals())

    a tento súbor ulož vedľa app.py.

TEST:
    /api/debug/catalog                      -> stav všetkých katalógov
    /api/debug/catalog?name=imago&refresh=1 -> prejde obchod hneď a ukáže vzorku
    (s ADMIN_KEY pridaj &key=...)

Premenné prostredia (nepovinné):
    CATALOG_REFRESH_MIN  – ako často obnoviť katalóg (predvolene 60 minút)
"""

import copy
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup

try:
    import fcntl
except ImportError:
    fcntl = None

# =========================================================
# KONFIGURÁCIA
#   catalog    zoznam URL kategórií, ktoré sa prechádzajú (aj ďalšie strany)
#   max_pages  koľko strán najviac na jednu kategóriu
# =========================================================

CATALOG_SHOPS = [
    {
        "name": "iHRYsko", "country": "SK", "enabled": True,
        "base_url": "https://www.ihrysko.sk/",
        "catalog": ["https://www.ihrysko.sk/pokemon-tcg-c17668"],
        "max_pages": 15,
    },
    {
        "name": "imago", "country": "SK", "enabled": True,
        "base_url": "https://www.imago.sk/",
        "catalog": ["https://www.imago.sk/pokemon-kartove-hra"],
        "max_pages": 15,
    },
    {
        "name": "Herný svet", "country": "SK", "enabled": True,
        "base_url": "https://www.hernysvet.sk/",
        "catalog": ["https://www.hernysvet.sk/tema/pokemon"],
        "max_pages": 15,
    },
]

REFRESH_MIN = float(os.environ.get("CATALOG_REFRESH_MIN", "60"))
PAGE_DELAY = 1.5        # sekundy medzi stranami jedného obchodu (šetrne)
MEM_TTL = 60            # sekundy, kým sa katalóg znova načíta z DB

# Nové sety Mega Evolution, ktoré v app.py chýbali
EXTRA_ALIASES = {
    "me01": "mega evolution", "me1": "mega evolution",
    "me02": "phantasmal flames", "me2": "phantasmal flames",
    "me2.5": "ascended heroes", "me 2.5": "ascended heroes",
    "me03": "perfect order", "me3": "perfect order",
    "me04": "chaos rising", "me4": "chaos rising",
    "me05": "pitch black", "me5": "pitch black",
    "pitch": "pitch black", "chaos": "chaos rising",
}
EXTRA_SETS = {"pitch black", "chaos rising", "perfect order"}
EXTRA_NEW_SETS = [  # vložia sa pred "Ascended Heroes"
    {"name": "Pitch Black", "query": "pitch black"},
    {"name": "Chaos Rising", "query": "chaos rising"},
    {"name": "Perfect Order", "query": "perfect order"},
]

G = {}  # globálne premenné z app.py (nastaví install)

_PLACEHOLDER_RE = re.compile(r"loading|placeholder|blank|spacer|lazy[-_]?load|1x1|pixel\.", re.I)
_STRIKE_SELECTOR = ("del, s, strike, [class*='old'], [class*='before'], "
                    "[class*='original'], [class*='crossed'], [class*='strike']")

_mem = {}                 # shop -> (monotonic, items, updated_iso)
_mem_lock = threading.Lock()
_crawling = set()
_crawl_lock = threading.Lock()
_lock_file = None


# =========================================================
# DATABÁZA
# =========================================================

def _db():
    conn = sqlite3.connect(G["DB_PATH"], timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    conn = _db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS catalog_items (
                shop TEXT NOT NULL, link TEXT NOT NULL, title TEXT,
                price_eur REAL, image TEXT, stock TEXT,
                PRIMARY KEY (shop, link)
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        conn.commit()
    finally:
        conn.close()


def _save_catalog(shop_name, items):
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute("DELETE FROM catalog_items WHERE shop = ?", (shop_name,))
        conn.executemany(
            "INSERT OR REPLACE INTO catalog_items (shop, link, title, price_eur, image, stock) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(shop_name, i["link"], i["title"], i["price_eur"], i["image"], i["stock"])
             for i in items])
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
                     ("catalog:" + shop_name, now))
        conn.commit()
    finally:
        conn.close()
    with _mem_lock:
        _mem.pop(shop_name, None)
    return now


def _load_catalog(shop_name):
    with _mem_lock:
        hit = _mem.get(shop_name)
        if hit and time.monotonic() - hit[0] < MEM_TTL:
            return hit[1], hit[2]
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT link, title, price_eur, image, stock FROM catalog_items WHERE shop = ?",
            (shop_name,)).fetchall()
        meta = conn.execute("SELECT v FROM meta WHERE k = ?", ("catalog:" + shop_name,)).fetchone()
    except Exception:
        rows, meta = [], None
    finally:
        conn.close()
    items = [{"link": r[0], "title": r[1], "price_eur": r[2], "image": r[3] or "",
              "stock": r[4] or ""} for r in rows]
    updated = meta[0] if meta else ""
    with _mem_lock:
        _mem[shop_name] = (time.monotonic(), items, updated)
    return items, updated


def _is_stale(updated, factor=1.0):
    if not updated:
        return True
    try:
        t = datetime.fromisoformat(updated)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - t > timedelta(minutes=REFRESH_MIN * factor)


# =========================================================
# PARSOVANIE STRÁNKY KATEGÓRIE
# =========================================================

def img_url(image, base_url):
    """Ako img_url v app.py, ale preskočí zástupné obrázky (loading.gif...)."""
    clean_text, absolute_url = G["clean_text"], G["absolute_url"]
    for attr in G["IMG_ATTRS"]:
        value = clean_text(image.get(attr, ""))
        if value and not value.startswith("data:image/") and not _PLACEHOLDER_RE.search(value):
            absolute = absolute_url(base_url, value)
            if absolute:
                return absolute
    for attr in ("srcset", "data-srcset"):
        candidates = []
        for part in clean_text(image.get(attr, "")).split(","):
            pieces = clean_text(part).split()
            if not pieces or _PLACEHOLDER_RE.search(pieces[0]):
                continue
            width = 0
            if len(pieces) > 1:
                m = re.search(r"(\d+)w", pieces[1])
                width = int(m.group(1)) if m else 0
            absolute = absolute_url(base_url, pieces[0])
            if absolute:
                candidates.append((width, absolute))
        if candidates:
            return max(candidates, key=lambda c: c[0])[1]
    return ""


def _tile_price(block_el):
    """Cena z dlaždice bez prečiarknutej (pôvodnej) ceny."""
    if block_el is None:
        return None
    try:
        el = copy.copy(block_el)
        for old in el.select(_STRIKE_SELECTOR):
            old.decompose()
        price = G["parse_price"](el.get_text(" ", strip=True))
    except Exception:
        price = None
    if price is None:
        price = G["parse_price"](block_el.get_text(" ", strip=True))
    return price


def _other_links(block, href, page_url):
    """Koľko iných odkazov je v dlaždici (veľa = nie je to jeden produkt)."""
    own = href.lower().rstrip("/")
    other = set()
    for a in block.find_all("a", href=True):
        h = G["absolute_url"](page_url, a["href"])
        if h:
            h = G["clean_link"](h).lower().rstrip("/")
            if h != own:
                other.add(h)
    return len(other)


def _parse_listing(shop, html, page_url):
    g = G
    soup = BeautifulSoup(html, g["HTML_PARSER"])
    host = urllib.parse.urlparse(shop["base_url"]).netloc.lower()

    by_href = {}
    for a in soup.find_all("a", href=True):
        href = g["clean_link"](g["absolute_url"](page_url, a["href"]))
        if not href:
            continue
        p = urllib.parse.urlparse(href)
        if p.netloc.lower() != host or p.path in ("", "/"):
            continue
        by_href.setdefault(href.lower().rstrip("/"), (href, []))[1].append(a)

    items = []
    for href, anchors in by_href.values():
        # z viacerých odkazov na ten istý produkt vyber najkratší "TCG" názov
        named = []
        for a in anchors:
            t = g["extract_title"](a)
            if 6 <= len(t) <= 200 and g["looks_like_tcg"](t) and not g["is_merch"](t):
                named.append((len(t), t, a))
        if not named:
            continue
        _, title, anchor = min(named, key=lambda x: x[0])

        block = g["find_product_block_el"](anchor)
        if block is None or _other_links(block, href, page_url) > 3:
            continue  # nie je to dlaždica produktu (menu, zoznam, päta...)
        price = _tile_price(block)
        if not price or price <= 0:
            continue
        items.append({
            "link": href, "title": title, "price_eur": round(price, 2),
            "image": g["extract_image"](anchor, page_url),
            "stock": g["detect_stock_el"](block),
        })
    return items, soup


def _next_page(soup, page_url, n, host):
    absolute_url = G["absolute_url"]
    el = soup.select_one('link[rel~="next"], a[rel~="next"]')
    if el is not None and el.get("href"):
        u = absolute_url(page_url, el["href"])
        if urllib.parse.urlparse(u).netloc.lower() == host:
            return u
    pat = re.compile(r"(?:[?&](?:page|strana|stranka|p|pg)=|/strana-|/page[/-]?)"
                     + str(n + 1) + r"(?!\d)", re.I)
    for a in soup.find_all("a", href=True):
        if pat.search(a["href"]):
            u = absolute_url(page_url, a["href"])
            if urllib.parse.urlparse(u).netloc.lower() == host:
                return u
    return None


# =========================================================
# PRECHÁDZANIE OBCHODU
# =========================================================

def crawl_shop(shop):
    host = urllib.parse.urlparse(shop["base_url"]).netloc.lower()
    items, pages, errors = {}, 0, []
    start = time.monotonic()

    for url in shop["catalog"]:
        n, visited = 1, set()
        while url and n <= shop.get("max_pages", 10) and url not in visited:
            visited.add(url)
            resp, dbg = G["fetch"](url, timeout=15)
            if not resp:
                errors.append({"url": url, "error": dbg.get("error") or dbg.get("status")})
                break
            found, soup = _parse_listing(shop, resp.text, url)
            pages += 1
            new = 0
            for it in found:
                if it["link"] not in items:
                    items[it["link"]] = it
                    new += 1
            if new == 0 and n > 1:
                break
            url = _next_page(soup, url, n, host)
            n += 1
            if url:
                time.sleep(PAGE_DELAY)

    result = list(items.values())
    updated = ""
    if result:  # pri chybe necháme starý katalóg
        updated = _save_catalog(shop["name"], result)
        try:
            G["_save_history"]([dict(r, shop=shop["name"]) for r in result])
        except Exception:
            pass
    return {"shop": shop["name"], "items": len(result), "pages": pages,
            "errors": errors, "updated": updated,
            "elapsed_ms": round((time.monotonic() - start) * 1000),
            "sample": result[:10]}


def _crawl_bg(shop):
    with _crawl_lock:
        if shop["name"] in _crawling:
            return
        _crawling.add(shop["name"])

    def run():
        try:
            crawl_shop(shop)
        except Exception:
            pass
        finally:
            with _crawl_lock:
                _crawling.discard(shop["name"])

    threading.Thread(target=run, daemon=True, name="catalog-" + shop["name"]).start()


def _loop():
    time.sleep(20)
    while True:
        for shop in CATALOG_SHOPS:
            if not shop.get("enabled", True):
                continue
            try:
                _, updated = _load_catalog(shop["name"])
                if _is_stale(updated):
                    crawl_shop(shop)
            except Exception:
                pass
        time.sleep(300)


def _start_worker():
    global _lock_file
    if fcntl is not None:
        try:
            _lock_file = open(os.path.join(G["BASE_DIR"], ".catalog.lock"), "w")
            fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # iný proces už katalógy obnovuje
    threading.Thread(target=_loop, daemon=True, name="catalog").start()


# =========================================================
# HĽADANIE V KATALÓGU (náhrada _scrape pre tieto obchody)
# =========================================================

def catalog_scrape(shop, query):
    g = G
    start = time.monotonic()
    debug = {
        "shop": shop["name"], "query": query, "url": "catalog", "status": "starting",
        "http_status": None, "results": 0, "links_scanned": 0, "unique_links": 0,
        "price_found": 0, "merch_filtered": 0, "match_filtered": 0,
        "language_filtered": 0, "accepted": 0, "images_found": 0,
        "images_missing": 0, "elapsed_ms": 0, "cache": "miss", "error": "",
        "sample_decisions": [],
    }
    items, updated = _load_catalog(shop["name"])
    if not items or _is_stale(updated, factor=3):
        _crawl_bg(shop)
    if not items:
        debug["status"] = "catalog_loading"
        debug["error"] = "Katalóg sa práve načítava, skús o minútu."
        debug["elapsed_ms"] = round((time.monotonic() - start) * 1000)
        return [], debug

    parsed = g["normalize_query"](query)
    kind = g["classify_query"](parsed)
    results = []
    debug["links_scanned"] = debug["unique_links"] = len(items)

    for it in items:
        title = it["title"]
        if kind == "card":
            ok, reason = g["card_matches_query"](title, "", parsed, loose_set=shop.get("loose_set", False))
        else:
            ok, reason = g["sealed_matches_query"](title, "", parsed)
        if not ok:
            debug["match_filtered"] += 1
            continue
        lang = g["detect_language"](title)
        price = it["price_eur"]
        packs = g["estimate_packs"](title, lang)
        results.append({
            "title": title, "shop": shop["name"], "country": shop["country"],
            "condition": "Nové", "language": lang, "price_eur": round(price, 2),
            "link": it["link"], "image": it["image"], "stock": it["stock"],
            "packs": packs,
            "price_per_pack": round(price / packs, 2) if packs and packs > 1 else None,
            "group": g["group_key"](title, lang),
        })

    results.sort(key=lambda r: r["price_eur"])
    debug.update(price_found=len(results), accepted=len(results), results=len(results),
                 images_found=sum(1 for r in results if r["image"]),
                 status="ok" if results else "no_results",
                 elapsed_ms=round((time.monotonic() - start) * 1000))
    debug["images_missing"] = len(results) - debug["images_found"]
    return results, debug


# =========================================================
# DEBUG ENDPOINT
# =========================================================

def api_debug_catalog():
    from flask import jsonify, request
    if not G["debug_allowed"]():
        return jsonify({"error": "Nepovolené."}), 403
    name = G["clean_text"](request.args.get("name", "")).lower()
    if name:
        shop = next((s for s in CATALOG_SHOPS if s["name"].lower() == name), None)
        if not shop:
            return jsonify({"error": "Neznámy obchod.",
                            "shops": [s["name"] for s in CATALOG_SHOPS]}), 400
        if request.args.get("refresh"):
            return jsonify(crawl_shop(shop))
        items, updated = _load_catalog(shop["name"])
        return jsonify({"shop": shop["name"], "items": len(items),
                        "updated": updated, "sample": items[:20]})
    out = []
    for s in CATALOG_SHOPS:
        items, updated = _load_catalog(s["name"])
        out.append({"shop": s["name"], "enabled": s.get("enabled", True),
                    "items": len(items), "updated": updated,
                    "crawling": s["name"] in _crawling})
    return jsonify({"refresh_min": REFRESH_MIN, "catalogs": out})


# =========================================================
# INŠTALÁCIA DO app.py
# =========================================================

def install(g):
    G.update(g)
    G["_g"] = g

    # 1) nové sety + skratky ME01–ME05
    g["SET_ALIASES"].update(EXTRA_ALIASES)
    sets = set(g["KNOWN_SETS"]) | set(EXTRA_ALIASES.values()) | EXTRA_SETS
    g["KNOWN_SETS"][:] = sorted(sets, key=len, reverse=True)
    g["_SETS_RE"] = g["_compile_aliases"](g["SET_ALIASES"].items())
    g["_KNOWN_SETS_RE"] = g["_compile_aliases"]((c, c) for c in g["KNOWN_SETS"])
    g["TCG_SET_NAMES"].update(sets)
    g["_TCG_SET_RE"] = re.compile(
        r"\b(?:" + "|".join(re.escape(s) for s in sorted(g["TCG_SET_NAMES"], key=len, reverse=True))
        + r")\b", re.I)
    known = {s["query"] for s in g["NEW_SETS"]}
    pos = next((i for i, s in enumerate(g["NEW_SETS"]) if s["query"] == "ascended heroes"),
               len(g["NEW_SETS"]))
    for s in reversed(EXTRA_NEW_SETS):
        if s["query"] not in known:
            g["NEW_SETS"].insert(pos, s)

    # 2) sledovací parameter z Heureky (hgtid) preč z odkazov
    g["_TRACKING_PARAM_RE"] = re.compile(
        r"^(?:_pos|_sid|_ss|_psq|_fid|_v|utm_\w+|fbclid|gclid|srsltid|ref|variant_id|hgtid|hgid)$",
        re.I)

    # 3) obrázky: preskočiť loading.gif a podobné
    g["img_url"] = img_url
    G["img_url"] = img_url

    # 4) obchody
    for shop in CATALOG_SHOPS:
        if any(s["name"] == shop["name"] for s in g["SHOPS"]):
            continue
        g["SHOPS"].append(shop)
        if shop.get("enabled", True):
            g["ACTIVE_SHOPS"].append(shop)
        g["ALLOWED_HOSTS"].add(urllib.parse.urlparse(shop["base_url"]).netloc.lower())

    # 5) hľadanie: katalógové obchody idú cez databázu
    original = g["_scrape"]

    def _scrape(shop, query, timeout):
        if shop.get("catalog"):
            return catalog_scrape(shop, query)
        return original(shop, query, timeout)

    g["_scrape"] = _scrape

    # 6) databáza, endpoint, obnova na pozadí
    _init_db()
    g["app"].add_url_rule("/api/debug/catalog", "api_debug_catalog", api_debug_catalog)
    _start_worker()
