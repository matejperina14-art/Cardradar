"""
CARD RADAR – rozšírenia 6.4
 - 6.4: katalógy sa po reštarte servera načítajú hneď a postupne
===========================
 - 6.3: rýchlejšie opakované hľadanie, správna cena pri zľavách, bezpečnostné opravy
 - obchody v katalógovom režime (iHRYsko, imago)
 - XML feedy (Heureka / Google), stačí vyplniť "feed" pri obchode
 - presnejšie hľadanie sealed produktov aj pri setoch, ktoré app.py nepozná
 - kurz CZK denne z Európskej centrálnej banky
 - stránky /podmienky a /ochrana-udajov + odkazy v päte webu
 - automatické mazanie starých strážcov ceny (GDPR)

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
import html as _html
import unicodedata
import xml.etree.ElementTree as ET
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
        # hlavná kategória + každý set zvlášť (hlavná kategória neukazuje všetko)
        "catalog": [
            "https://www.ihrysko.sk/pokemon-tcg-c17668",
            "https://www.ihrysko.sk/pokemon-delta-reign-c100387",
            "https://www.ihrysko.sk/pokemon-30th-celebrations-c100385",
            "https://www.ihrysko.sk/pokemon-pitch-black-c100378",
            "https://www.ihrysko.sk/pokemon-chaos-rising-c100377",
            "https://www.ihrysko.sk/pokemon-perfect-order-c100371",
            "https://www.ihrysko.sk/pokemon-ascended-heroes-c100365",
            "https://www.ihrysko.sk/pokemon-phantasmal-flames-c100358",
            "https://www.ihrysko.sk/pokemon-mega-evolution-c100354",
            "https://www.ihrysko.sk/pokemon-black-bolt-a-white-flare-sv-10-5-c100350",
            "https://www.ihrysko.sk/pokemon-destined-rivals-c100343",
            "https://www.ihrysko.sk/pokemon-journey-together-c100335",
            "https://www.ihrysko.sk/pokemon-prismatic-evolutions-c100327",
            "https://www.ihrysko.sk/pokemon-surging-sparks-c100321",
            "https://www.ihrysko.sk/pokemon-stellar-crown-c100318",
            "https://www.ihrysko.sk/pokemon-151-c100268",
        ],
        "max_pages": 8,
        # malé náhľady (xs) -> stredné (md)
        "image_replace": ("/xs/products/", "/md/products/"),
    },
    {
        "name": "imago", "country": "SK", "enabled": True,
        "base_url": "https://www.imago.sk/",
        "catalog": ["https://www.imago.sk/pokemon-kartove-hra"],
        "max_pages": 15,
    },
    {
        # web blokuje roboty (HTTP 403). Keď dostaneš adresu XML feedu,
        # vlož ju do "feed" a zmeň "enabled" na True.
        "name": "Herný svet", "country": "SK", "enabled": False,
        "base_url": "https://www.hernysvet.sk/",
        "feed": "",
        "catalog": ["https://www.hernysvet.sk/tema/pokemon"],
        "max_pages": 15,
    },
]

REFRESH_MIN = float(os.environ.get("CATALOG_REFRESH_MIN", "60"))
PAGE_DELAY = 1.0        # sekundy medzi stranami jedného obchodu (šetrne)
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
# Prevádzkovateľ (zobrazí sa v podmienkach a ochrane údajov) – nastav na Renderi
OPERATOR_NAME = os.environ.get("OPERATOR_NAME", "prevádzkovateľ CardRadar")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "") or os.environ.get("SMTP_FROM", "")
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FEED_MAX_BYTES = 150 * 1024 * 1024

EXTRA_NEW_SETS = [  # vložia sa pred "Ascended Heroes"
    {"name": "Pitch Black", "query": "pitch black"},
    {"name": "Chaos Rising", "query": "chaos rising"},
    {"name": "Perfect Order", "query": "perfect order"},
]

G = {}  # globálne premenné z app.py (nastaví install)

_COMING_RE = re.compile(r"o[čc]ak[áa]vame|o[čc]ek[áa]v[áa]me|pripravujeme|coming\s+soon", re.I)
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
        image = g["extract_image"](anchor, page_url)
        rep = shop.get("image_replace")
        if image and rep:
            image = image.replace(rep[0], rep[1])
        stock = g["detect_stock_el"](block)
        if not stock and _COMING_RE.search(block.get_text(" ", strip=True)):
            stock = "preorder"
        items.append({
            "link": href, "title": title, "price_eur": round(price, 2),
            "image": image, "stock": stock,
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
    if shop.get("feed"):
        return crawl_feed(shop)
    host = urllib.parse.urlparse(shop["base_url"]).netloc.lower()
    items, pages, errors = {}, 0, []
    start = time.monotonic()
    first_load = not _load_catalog(shop["name"])[0]   # po reštarte servera je katalóg prázdny

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
        if first_load and items:
            # priebežne uložiť, aby sa obchod dal hľadať hneď po prvej kategórii
            _save_catalog(shop["name"], list(items.values()))

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
    time.sleep(3)   # hneď po štarte: všetky obchody naraz, každý vo vlastnom vlákne
    while True:
        for shop in CATALOG_SHOPS:
            if not shop.get("enabled", True):
                continue
            try:
                _, updated = _load_catalog(shop["name"])
                if _is_stale(updated):
                    _crawl_bg(shop)
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
# XML FEED (Heureka / Google Merchant)
# =========================================================

def _local(tag):
    return tag.rsplit("}", 1)[-1].upper()


def _feed_stock(d):
    av = (d.get("AVAILABILITY") or "").lower().replace("_", " ")
    if av:
        if "out of stock" in av or "discontinued" in av:
            return "out"
        if "preorder" in av:
            return "preorder"
        if "backorder" in av:
            return "order"
        if "in stock" in av:
            return "in"
    dd = (d.get("DELIVERY_DATE") or "").strip()
    if dd == "0":
        return "in"
    if dd.isdigit():
        return "order"
    if dd:
        return "preorder"
    return ""


def crawl_feed(shop):
    """Prejde XML feed obchodu po kúskoch (aj veľký feed) a uloží TCG produkty."""
    g = G
    start = time.monotonic()
    host = urllib.parse.urlparse(shop["base_url"]).netloc.lower()
    items, errors, scanned = {}, [], 0
    try:
        resp = g["get_http_session"]().get(shop["feed"], timeout=(5, 90), stream=True)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        resp.raw.decode_content = True
        for _, el in ET.iterparse(resp.raw, events=("end",)):
            if _local(el.tag) not in ("SHOPITEM", "ITEM", "ENTRY"):
                continue
            scanned += 1
            d = {}
            for ch in el:
                d.setdefault(_local(ch.tag), (ch.text or "").strip())
            el.clear()
            title = g["clean_text"](d.get("PRODUCTNAME") or d.get("PRODUCT") or d.get("TITLE"))
            link = g["clean_link"](g["clean_text"](d.get("URL") or d.get("LINK")))
            if not title or not link or urllib.parse.urlparse(link).netloc.lower() != host:
                continue
            if g["is_merch"](title) or not g["looks_like_tcg"](title):
                continue
            raw = d.get("PRICE_VAT") or d.get("SALE_PRICE") or d.get("PRICE") or ""
            m = re.search(r"\d[\d\s.,]*", raw)
            price = g["_to_float"](m.group(0).strip()) if m else None
            if not price or price <= 0:
                continue
            if "CZK" in raw.upper() or shop.get("currency") == "CZK":
                price = price / g["CZK_PER_EUR"]
            items[link] = {"link": link, "title": title, "price_eur": round(price, 2),
                           "image": d.get("IMGURL") or d.get("IMAGE_LINK") or "",
                           "stock": _feed_stock(d)}
            if resp.raw.tell() > FEED_MAX_BYTES:
                errors.append({"url": shop["feed"], "error": "feed je príliš veľký, načítaná len časť"})
                break
    except Exception as e:
        errors.append({"url": shop.get("feed"), "error": str(e)[:200]})

    result = list(items.values())
    updated = ""
    if result:
        updated = _save_catalog(shop["name"], result)
        try:
            g["_save_history"]([dict(r, shop=shop["name"]) for r in result])
        except Exception:
            pass
    return {"shop": shop["name"], "source": "feed", "items": len(result),
            "scanned": scanned, "pages": 1, "errors": errors, "updated": updated,
            "elapsed_ms": round((time.monotonic() - start) * 1000), "sample": result[:10]}


# =========================================================
# PRESNEJŠIE HĽADANIE SEALED PRODUKTOV
# Ak set nie je v zozname (napr. úplne nový), app.py by vrátil všetky
# booster boxy. Teraz musia byť v názve aj ostatné hľadané slová.
# =========================================================

_GENERIC_WORDS = {
    "pokemon", "tcg", "booster", "boosters", "box", "boxy", "display", "elite", "trainer",
    "etb", "bundle", "pack", "packs", "blister", "tin", "tins", "mini", "collection",
    "premium", "kolekcia", "kolekce", "set", "edicia", "edice", "the", "and", "of",
    "en", "eng", "english", "anglicky", "anglicka", "anglicke", "card", "cards", "karty",
    "game", "hra", "balicek", "balicky", "sealed",
}


def _fold(text):
    t = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


def _fold_words(text):
    return set(re.findall(r"[a-z0-9]+", _fold(text)))


def _make_sealed_fix(original):
    def sealed_matches_query(title, extra_text, parsed):
        ok, reason = original(title, extra_text, parsed)
        if not ok or parsed.get("set_name"):
            return ok, reason
        want = {w for w in _fold_words(parsed.get("original", "")) - _GENERIC_WORDS
                if len(w) >= 3 or w.isdigit()}
        if want - _fold_words(title + " " + extra_text):
            return False, "words_not_found"
        return True, reason
    return sealed_matches_query


# =========================================================
# KURZ CZK Z ECB + UPRATOVANIE STRÁŽCOV
# =========================================================

def _update_czk():
    resp, _ = G["fetch"](ECB_URL, timeout=10)
    if not resp:
        return None
    m = re.search(r"currency=['\"]CZK['\"]\s+rate=['\"]([\d.]+)", resp.text)
    if not m:
        return None
    rate = float(m.group(1))
    if 15 < rate < 40:
        G["_g"]["CZK_PER_EUR"] = rate
        G["CZK_PER_EUR"] = rate
        return rate
    return None


def _cleanup_alerts():
    """Splnení strážcovia po 30 dňoch, nepotvrdení po 7 dňoch."""
    now = datetime.now(timezone.utc)
    conn = _db()
    try:
        conn.execute("DELETE FROM alerts WHERE notified IS NOT NULL AND notified < ?",
                     ((now - timedelta(days=30)).isoformat(),))
        conn.execute("DELETE FROM alerts WHERE confirmed = 0 AND created < ?",
                     ((now - timedelta(days=7)).isoformat(),))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def _daily_loop():
    time.sleep(5)
    while True:
        try:
            _update_czk()
        except Exception:
            pass
        try:
            _cleanup_alerts()
        except Exception:
            pass
        time.sleep(12 * 3600)


# =========================================================
# PODMIENKY A OCHRANA ÚDAJOV
# =========================================================

def _legal_page(title, body_html):
    from flask import Response
    home = G.get("PUBLIC_URL") or "/"
    contact = (f'<a href="mailto:{_html.escape(CONTACT_EMAIL)}">{_html.escape(CONTACT_EMAIL)}</a>'
               if CONTACT_EMAIL else "cez kontakt uvedený na webe")
    body_html = body_html.replace("{KONTAKT}", contact).replace(
        "{PREVADZKOVATEL}", _html.escape(OPERATOR_NAME))
    page = f"""<!doctype html><html lang="sk"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} – CardRadar</title>
<style>
body{{margin:0;background:#0f172a;color:#e2e8f0;font:16px/1.6 -apple-system,Segoe UI,sans-serif}}
main{{max-width:720px;margin:0 auto;padding:28px 20px 60px}}
h1{{color:#facc15;font-size:1.6rem}} h2{{font-size:1.1rem;margin-top:1.6em;color:#f8fafc}}
a{{color:#facc15}} p,li{{color:#cbd5e1}} .back{{display:inline-block;margin-bottom:12px}}
</style><main><a class="back" href="{home}">← Späť na CardRadar</a><h1>{title}</h1>{body_html}
<p style="margin-top:2em;font-size:.85rem;color:#64748b">Posledná aktualizácia: október 2026</p></main>"""
    return Response(page, mimetype="text/html")


TERMS_HTML = """
<p>CardRadar je bezplatný porovnávač cien Pokémon TCG produktov, ktorý prevádzkuje {PREVADZKOVATEL}.</p>
<h2>Čo CardRadar robí</h2>
<p>Zobrazuje ceny a dostupnosť produktov z verejne dostupných stránok a feedov internetových obchodov.
CardRadar nič nepredáva. Nákup prebieha vždy priamo v obchode a riadi sa jeho obchodnými podmienkami.</p>
<h2>Presnosť údajov</h2>
<p>Ceny a sklad sa aktualizujú automaticky, no môžu byť oneskorené alebo nepresné. Pred nákupom
si vždy over cenu a dostupnosť v obchode. Prepočet z CZK na EUR je orientačný (denný kurz ECB).</p>
<h2>Odkazy na obchody</h2>
<p>Niektoré odkazy môžu byť partnerské. Ak cez ne nakúpiš, CardRadar môže dostať províziu.
Cenu pre teba to nemení.</p>
<h2>Strážca ceny</h2>
<p>Služba je bezplatná a bez záruky, že e-mail príde vždy včas. Kedykoľvek ju zrušíš odkazom v e-maile.</p>
<h2>Ochranné známky</h2>
<p>Pokémon a súvisiace názvy sú ochranné známky ich vlastníkov. CardRadar nie je s nimi spojený.</p>
<h2>Kontakt</h2><p>{KONTAKT}</p>
"""

PRIVACY_HTML = """
<p>Prevádzkovateľ: {PREVADZKOVATEL}, kontakt: {KONTAKT}.</p>
<h2>Aké údaje spracúvame</h2>
<ul>
<li><b>Strážca ceny:</b> e-mail, sledovaný produkt a cieľová cena. Účel: poslať ti upozornenie,
o ktoré si požiadal (právny základ: tvoj súhlas potvrdený kliknutím v e-maile).</li>
<li><b>Hľadané výrazy:</b> ukladáme len text hľadania a počet za deň, bez väzby na teba,
aby sme ukázali obľúbené hľadania.</li>
<li><b>IP adresa:</b> drží sa len v pamäti servera asi minútu, na ochranu pred zneužitím
(obmedzenie počtu požiadaviek). Neukladá sa.</li>
<li><b>Obľúbené produkty</b> sa ukladajú iba v tvojom prehliadači, nie na serveri.</li>
</ul>
<h2>Ako dlho</h2>
<p>Nepotvrdený strážca sa zmaže po 7 dňoch, splnený 30 dní po odoslaní upozornenia.
Aktívny strážca trvá, kým ho nezrušíš odkazom v e-maile.</p>
<h2>Kto k údajom má prístup</h2>
<p>Web beží na serveroch spoločnosti Render (USA). E-maily sa odosielajú cez poskytovateľa
e-mailovej služby. Údaje nepredávame ani nezdieľame na reklamné účely.</p>
<h2>Cookies</h2>
<p>CardRadar nepoužíva reklamné ani sledovacie cookies. Prehliadač si ukladá len súbory
potrebné na rýchlejšie načítanie a obľúbené produkty.</p>
<h2>Tvoje práva</h2>
<p>Máš právo na prístup k údajom, ich opravu, vymazanie a odvolanie súhlasu. Stačí napísať na
kontakt vyššie. Sťažnosť môžeš podať na Úrad na ochranu osobných údajov SR (dataprotection.gov.sk).</p>
"""

FOOTER_HTML = ('<footer style="text-align:center;padding:24px 12px 40px;font-size:13px;'
               'opacity:.7"><a href="/podmienky" style="color:inherit">Podmienky</a> · '
               '<a href="/ochrana-udajov" style="color:inherit">Ochrana údajov</a></footer>')


def _inject_footer(resp):
    from flask import request
    try:
        if (request.path == "/" and resp.status_code == 200 and resp.mimetype == "text/html"
                and "Content-Encoding" not in resp.headers and not resp.direct_passthrough):
            body = resp.get_data(as_text=True)
            if "/ochrana-udajov" not in body and "</body>" in body:
                resp.set_data(body.replace("</body>", FOOTER_HTML + "</body>", 1))
    except Exception:
        pass
    return resp


# =========================================================
# OPRAVY A ZRÝCHLENIE 6.3 (opravy pre app.py, bez úpravy app.py)
# =========================================================
from concurrent.futures import ThreadPoolExecutor as _TPE

FAST_TIMEOUT = 6            # max. sekúnd čakania na jeden obchod (bolo 8)
SWR_FRESH = 600             # 10 min: výsledok je čerstvý
SWR_STALE = 6 * 3600        # do 6 h: ukáže sa hneď a na pozadí sa obnoví
SWR_MAX = 800
_swr = {}
_swr_lock = threading.Lock()
_swr_busy = set()
_swr_pool = _TPE(max_workers=4, thread_name_prefix="swr")


def _swr_key(shop, query):
    return shop["name"].lower() + "|" + G["clean_text"](query).lower()


def _swr_put(key, results, debug):
    with _swr_lock:
        if key not in _swr and len(_swr) >= SWR_MAX:
            _swr.pop(min(_swr, key=lambda k: _swr[k][0]), None)
        _swr[key] = (time.monotonic(), copy.deepcopy(results), copy.deepcopy(debug))


def _make_shop_search(original):
    """Opakované hľadanie je okamžité: starší výsledok sa ukáže hneď
    a čerstvý sa stiahne na pozadí (stale-while-revalidate)."""
    def run(shop, query, timeout):
        res, dbg = original(shop, query, return_debug=True, cache_result=True, timeout=timeout)
        if dbg.get("status") in G["CACHEABLE_STATUSES"]:
            _swr_put(_swr_key(shop, query), res, dbg)
        return res, dbg

    def refresh(shop, query, key):
        try:
            run(shop, query, FAST_TIMEOUT)
        except Exception:
            pass
        finally:
            with _swr_lock:
                _swr_busy.discard(key)

    def shop_search(shop, query, return_debug=False, cache_result=True, timeout=None):
        timeout = timeout or FAST_TIMEOUT
        if not cache_result:
            res, dbg = original(shop, query, return_debug=True, cache_result=False, timeout=timeout)
            return (res, dbg) if return_debug else res
        key = _swr_key(shop, query)
        with _swr_lock:
            ent = _swr.get(key)
        if ent:
            age = time.monotonic() - ent[0]
            if age < SWR_STALE:
                if age >= SWR_FRESH:
                    with _swr_lock:
                        start = key not in _swr_busy
                        _swr_busy.add(key)
                    if start:
                        _swr_pool.submit(refresh, shop, query, key)
                res, dbg = copy.deepcopy(ent[1]), copy.deepcopy(ent[2])
                dbg["cache"] = "stale" if age >= SWR_FRESH else "hit"
                G["fill_images_from_cache"](res)
                return (res, dbg) if return_debug else res
        res, dbg = run(shop, query, timeout)
        return (res, dbg) if return_debug else res
    return shop_search


_STRIKE_APP = _STRIKE_SELECTOR + ", [class*='standard'], [class*='compare'], [class*='regular']"


def _make_block_fix(original):
    """Dlaždica bez prečiarknutej ceny – pri zľave sa brala pôvodná (vyššia) cena."""
    def find_product_block_el(anchor):
        el = original(anchor)
        if el is None:
            return el
        try:
            if not el.select(_STRIKE_APP):
                return el
            c = copy.copy(el)
            for old in c.select(_STRIKE_APP):
                old.decompose()
            if G["parse_price"](c.get_text(" ", strip=True)) is None:
                return el
            return c
        except Exception:
            return el
    return find_product_block_el


def client_ip():
    """Skutočná IP návštevníka (Render beží za Cloudflare)."""
    from flask import request
    for h in ("CF-Connecting-IP", "True-Client-IP"):
        v = request.headers.get(h, "").strip()
        if v:
            return v[:64]
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else "") or request.remote_addr or "?"


def debug_allowed():
    """Debug len s ADMIN_KEY. Bez kľúča iba lokálne na vlastnom počítači."""
    from flask import request
    if G["_g"]["ADMIN_KEY"]:
        return G["is_admin"]()
    return request.remote_addr in ("127.0.0.1", "::1") and not request.headers.get("X-Forwarded-For")


def alerts_confirm():
    """Rovnaké ako v app.py, ale názov produktu je ošetrený (XSS)."""
    from flask import request
    token = G["clean_text"](request.args.get("token", ""))
    conn = _db()
    try:
        row = conn.execute("SELECT id, target, title FROM alerts WHERE token = ?", (token,)).fetchone()
        if row:
            conn.execute("UPDATE alerts SET confirmed = 1 WHERE id = ?", (row[0],))
            conn.commit()
    finally:
        conn.close()
    page = G["_simple_page"]
    if not row:
        return page("Odkaz neplatí", "Tento strážca už neexistuje alebo bol odkaz zmenený.")
    base = G["_g"]["PUBLIC_URL"] or request.url_root.rstrip("/")
    stop = _html.escape(f"{base}/alerts/stop?token={urllib.parse.quote(token)}")
    title = _html.escape(row[2] or "produkt")
    return page("Strážca je zapnutý 🔔",
                f"Napíšeme ti, keď {title} klesne na {row[1]:.2f} € alebo menej."
                f"<br><br><a href='{stop}' style='color:#94a3b8'>Zrušiť strážcu</a>")


def manifest():
    from flask import Response
    import json as _json
    data = {
        "name": "CardRadar – ceny Pokémon kariet", "short_name": "CardRadar",
        "description": "Porovnanie cien Pokémon kariet, ETB a booster boxov.",
        "start_url": "/?source=pwa", "scope": "/", "display": "standalone",
        "background_color": "#0d1530", "theme_color": "#0d1530", "lang": "sk",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    resp = Response(_json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def _install_fixes(g):
    g["SEARCH_TIMEOUT"] = FAST_TIMEOUT
    for name, fn in (("client_ip", client_ip), ("debug_allowed", debug_allowed)):
        g[name] = fn
        G[name] = fn
    fixed_block = _make_block_fix(g["find_product_block_el"])
    g["find_product_block_el"] = fixed_block
    G["find_product_block_el"] = fixed_block
    fast = _make_shop_search(g["shop_search"])
    g["shop_search"] = fast
    G["shop_search"] = fast
    app = g["app"]
    if "alerts_confirm" in app.view_functions:
        app.view_functions["alerts_confirm"] = alerts_confirm
    if "manifest" in app.view_functions:
        app.view_functions["manifest"] = manifest


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
        if shop.get("catalog") or shop.get("feed"):
            return catalog_scrape(shop, query)
        return original(shop, query, timeout)

    g["_scrape"] = _scrape

    # 6) presnejšie hľadanie sealed produktov (aj pre pôvodné obchody)
    fixed = _make_sealed_fix(g["sealed_matches_query"])
    g["sealed_matches_query"] = fixed
    G["sealed_matches_query"] = fixed

    # 7) databáza, stránky, obnova na pozadí
    _init_db()
    app = g["app"]
    app.add_url_rule("/api/debug/catalog", "api_debug_catalog", api_debug_catalog)
    app.add_url_rule("/podmienky", "terms_page",
                     lambda: _legal_page("Podmienky používania", TERMS_HTML))
    app.add_url_rule("/ochrana-udajov", "privacy_page",
                     lambda: _legal_page("Ochrana osobných údajov", PRIVACY_HTML))
    app.after_request(_inject_footer)   # beží pred gzipom v app.py
    threading.Thread(target=_daily_loop, daemon=True, name="daily").start()
    _start_worker()

    # 8) opravy a zrýchlenie app.py (6.3)
    _install_fixes(g)
