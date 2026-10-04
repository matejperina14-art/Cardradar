import os
import re
import sqlite3
import threading
import time
import copy
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response

# =========================================================
# CARD RADAR 5.23 - CardyX
# =========================================================

VERSION = "5.23"
app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cardradar.db")
CZK_PER_EUR = 24.4618
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

CACHE_TTL = 120
CACHE_MAX_ITEMS = 50
SUGGESTION_CACHE_TTL = 300
SUGGESTION_CACHE_MAX_ITEMS = 200
IMAGE_CACHE_TTL = 3600
IMAGE_CACHE_MAX_ITEMS = 500
SEARCH_TIMEOUT = 10
SUGGESTION_TIMEOUT = 4
IMAGE_FETCH_WORKERS = 8
IMAGE_FETCH_TIMEOUT = 3
MIN_LOCAL_SUGGESTIONS = 4  # ak je menej, doplníme z CardyX

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,sk-SK;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

_local = threading.local()


def get_http_session():
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.session = s
    return s


# =========================================================
# TEXT HELPERS
# =========================================================

def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


normalize_spaces = clean_text


def words(text):
    return set(re.findall(r"[a-z0-9]+", clean_text(text).lower()))


def text_contains_word(text, word):
    if not text or not word:
        return False
    return re.search(r"\b" + re.escape(word) + r"\b", text, re.I) is not None


# =========================================================
# TTL CACHE (jedna trieda pre všetky cache)
# =========================================================

class TTLCache:
    def __init__(self, ttl, max_items):
        self.ttl = ttl
        self.max_items = max_items
        self._d = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            item = self._d.get(key)
            if item is None:
                return None
            if time.monotonic() - item[0] > self.ttl:
                self._d.pop(key, None)
                return None
            return copy.deepcopy(item[1])

    def set(self, key, value):
        with self._lock:
            if key not in self._d and len(self._d) >= self.max_items:
                oldest = min(self._d, key=lambda k: self._d[k][0])
                self._d.pop(oldest, None)
            self._d[key] = (time.monotonic(), copy.deepcopy(value))

    def clear(self):
        with self._lock:
            self._d.clear()

    def stats(self):
        now = time.monotonic()
        with self._lock:
            active = sum(1 for t, _ in self._d.values() if now - t <= self.ttl)
            return len(self._d), active


search_cache = TTLCache(CACHE_TTL, CACHE_MAX_ITEMS)
suggestion_cache = TTLCache(SUGGESTION_CACHE_TTL, SUGGESTION_CACHE_MAX_ITEMS)
image_cache = TTLCache(IMAGE_CACHE_TTL, IMAGE_CACHE_MAX_ITEMS)

# Autocomplete katalóg (posledných 500 reálnych kariet)
SUGGESTION_CATALOG = {}
_catalog_lock = threading.Lock()


# =========================================================
# DATABASE
# =========================================================

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT, shop TEXT, title TEXT,
            price_eur REAL, link TEXT, checked_at TEXT
        )
    """)
    conn.commit()
    conn.close()


init_db()


def save_history(query, results):
    if not results:
        return
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (query, r.get("shop", ""), r.get("title", ""),
             r.get("price_eur", 0), r.get("link", ""), now)
            for r in results
        ]
        conn.executemany(
            "INSERT INTO price_history "
            "(query, shop, title, price_eur, link, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


def find_index():
    for sub in ("templates", "Templates", ""):
        path = os.path.join(BASE_DIR, sub, "index.html")
        if os.path.isfile(path):
            return path
    return None


# =========================================================
# ALIASES
# =========================================================

SET_ALIASES = {
    "sv8": "surging sparks", "sv8a": "terastal festival",
    "sv9": "journey together", "sv9a": "destined rivals",
    "sv10": "destined rivals", "sv10.5": "destined rivals",
    "sv11": "black bolt white flare", "sv6": "twilight masquerade",
    "sv7": "stellar crown", "sv5": "temporal forces",
    "sv4": "paradox rift", "sv3": "obsidian flames",
    "sv2": "paldea evolved", "sv1": "scarlet violet base",
    "151": "pokemon 151", "pokemon151": "pokemon 151",
    "pokemon 151": "pokemon 151",
    "prismatic": "prismatic evolutions",
    "prismatic evo": "prismatic evolutions",
    "surging": "surging sparks", "sparks": "surging sparks",
    "destined": "destined rivals", "journey": "journey together",
    "terastal": "terastal festival",
    "phantasmal": "phantasmal flames",
    "phantasmal flames": "phantasmal flames",
    "mega brave": "mega evolution mega brave",
    "mega evolution": "mega evolution",
}

KNOWN_SETS = sorted(
    set(SET_ALIASES.values()) | {
        "surging sparks", "pokemon 151", "prismatic evolutions",
        "terastal festival", "destined rivals", "journey together",
        "twilight masquerade", "stellar crown", "temporal forces",
        "obsidian flames", "mega evolution", "phantasmal flames",
    },
    key=len, reverse=True,
)

POKEMON_ALIASES = {
    "pikachu": "Pikachu", "pika": "Pikachu",
    "charizard": "Charizard", "char": "Charizard",
    "umbreon": "Umbreon", "eevee": "Eevee", "mew": "Mew",
    "mewtwo": "Mewtwo", "gengar": "Gengar", "lucario": "Lucario",
    "greninja": "Greninja", "rayquaza": "Rayquaza",
    "gardevoir": "Gardevoir", "dragonite": "Dragonite",
    "gyarados": "Gyarados", "blastoise": "Blastoise",
    "venusaur": "Venusaur", "lugia": "Lugia", "ho-oh": "Ho-Oh",
    "hooh": "Ho-Oh", "arceus": "Arceus", "dialga": "Dialga",
    "palkia": "Palkia", "zekrom": "Zekrom", "reshiram": "Reshiram",
    "celebi": "Celebi", "jolteon": "Jolteon", "vaporeon": "Vaporeon",
    "flareon": "Flareon", "espeon": "Espeon", "sylveon": "Sylveon",
    "leafeon": "Leafeon", "glaceon": "Glaceon",
}

PRODUCT_PATTERNS = [
    ("elite trainer box", r"\belite\s+trainer\s+box\b"),
    ("elite trainer box", r"\betb\b"),
    ("booster box", r"\bbooster\s*box\b"),
    ("booster bundle", r"\bbooster\s*bundle\b"),
    ("collection box", r"\bcollection\s+box\b"),
    ("premium collection", r"\bpremium\s+collection\b"),
    ("blister", r"\bblister(?:\s+pack)?\b"),
    ("tin", r"\btins?\b"),
]

_SETS_SORTED = sorted(SET_ALIASES.items(), key=lambda x: len(x[0]), reverse=True)
_POKEMON_SORTED = sorted(POKEMON_ALIASES.items(), key=lambda x: len(x[0]), reverse=True)


def _extract(q, candidates):
    """Nájde prvý (alias, canonical) v texte, vráti (canonical, q_bez_aliasu)."""
    for alias, canonical in candidates:
        pattern = r"\b" + re.escape(alias.lower()) + r"\b"
        if re.search(pattern, q):
            return canonical, re.sub(pattern, " ", q)
    return "", q


# =========================================================
# NORMALIZATION
# =========================================================

def normalize_query(query):
    original = clean_text(query)
    empty = {
        "original": "", "normalized": "", "pokemon": "", "set_name": "",
        "product_name": "", "product_type": "", "card_number": "", "suffix": "",
    }
    if not original:
        return empty

    q = original.lower()

    card_number = ""
    m = re.search(r"\b(\d{1,4})\s*/\s*(\d{1,4})\b", q)
    if m:
        card_number = f"{m.group(1)}/{m.group(2)}"
        q = re.sub(r"\b\d{1,4}\s*/\s*\d{1,4}\b", " ", q, count=1)

    product_type = ""
    for canonical, pattern in PRODUCT_PATTERNS:
        if re.search(pattern, q):
            product_type = canonical
            q = re.sub(pattern, " ", q)
            break

    set_name, q = _extract(q, _SETS_SORTED)
    if not set_name:
        set_name, q = _extract(q, [(c, c) for c in KNOWN_SETS])

    pokemon, q = _extract(q, _POKEMON_SORTED)

    suffix = ""
    m = re.search(r"\b(vmax|vstar|ex|gx|v)\b", q, re.I)
    if m:
        suffix = m.group(1).lower()
        q = re.sub(r"\b(vmax|vstar|ex|gx|v)\b", " ", q, flags=re.I)

    q = clean_text(re.sub(r"\bpokemon\b", " ", q, flags=re.I))

    if product_type == "elite trainer box":
        parts = [set_name, pokemon, suffix, card_number, "elite trainer box"]
    else:
        parts = [pokemon, suffix, q, card_number, set_name, product_type]
    normalized = clean_text(" ".join(p for p in parts if p))

    return {
        "original": original,
        "normalized": normalized or original,
        "pokemon": pokemon,
        "set_name": set_name,
        "product_name": product_type,
        "product_type": product_type,
        "card_number": card_number,
        "suffix": suffix,
    }


def classify_query(parsed):
    if parsed.get("product_type"):
        return "sealed"
    if (parsed.get("set_name") and not parsed.get("pokemon")
            and not parsed.get("card_number")):
        return "sealed"
    return "card"


# =========================================================
# MATCHING
# =========================================================

def set_matches_text(searchable, set_name):
    searchable = clean_text(searchable).lower()
    set_name = clean_text(set_name).lower()
    if not searchable or not set_name:
        return False
    if words(set_name).issubset(words(searchable)):
        return True
    return any(
        alias.lower() in searchable
        for alias, canonical in SET_ALIASES.items()
        if canonical.lower() == set_name
    )


def card_matches_query(title, extra_text, parsed):
    searchable = clean_text(title + " " + extra_text)
    pokemon, set_name = parsed.get("pokemon"), parsed.get("set_name")
    card_number, suffix = parsed.get("card_number"), parsed.get("suffix")

    if pokemon and not text_contains_word(searchable, pokemon):
        return False, "pokemon_not_found"
    if card_number:
        if card_number.replace(" ", "").lower() not in re.sub(r"\s+", "", searchable.lower()):
            return False, "card_number_not_found"
    if set_name and not set_matches_text(searchable, set_name):
        return False, "set_not_found"
    if suffix and not text_contains_word(searchable, suffix):
        return False, "suffix_not_found"
    return True, "matched"


def sealed_matches_query(title, extra_text, parsed):
    searchable = clean_text(title + " " + extra_text).lower()
    set_name, product_type = parsed.get("set_name"), parsed.get("product_type")

    if set_name and not set_matches_text(searchable, set_name):
        return False, "set_not_found"

    if product_type == "elite trainer box":
        if not ("elite trainer box" in searchable or re.search(r"\betb\b", searchable)):
            return False, "etb_not_found"
        if re.search(r"\b(case|10x|12x|6x)\b", searchable):
            return False, "bulk_product"
    elif product_type and product_type not in searchable:
        return False, "product_type_not_found"
    return True, "matched"


# =========================================================
# PRICE
# =========================================================

def parse_price(text):
    text = clean_text(text)
    if not text:
        return None

    for pattern in (r"(\d{1,6}(?:[.,]\d{1,2})?)\s*€", r"€\s*(\d{1,6}(?:[.,]\d{1,2})?)"):
        m = re.search(pattern, text)
        if m:
            value = m.group(1).replace(" ", "")
            if "," in value and "." in value:
                if value.rfind(",") > value.rfind("."):
                    value = value.replace(".", "").replace(",", ".")
                else:
                    value = value.replace(",", "")
            else:
                value = value.replace(",", ".")
            try:
                return float(value)
            except ValueError:
                pass

    for pattern in (r"(\d{1,8}(?:[.,]\d{1,2})?)\s*(?:Kč|CZK)",
                    r"(?:Kč|CZK)\s*(\d{1,8}(?:[.,]\d{1,2})?)"):
        m = re.search(pattern, text, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ".")) / CZK_PER_EUR
            except ValueError:
                pass
    return None


# =========================================================
# MERCH FILTER (celé slová)
# =========================================================

MERCH_WORDS = [
    "plush", "plyš", "peluche", "figúrka", "figurka", "figure", "figurine",
    "vinyl figure", "statue", "funko", "funko pop", "pop!", "pop vinyl",
    "hrnček", "hrnek", "mug", "tričko", "tricko", "shirt", "mikina", "hoodie",
    "ponožky", "ponozky", "socks", "puzzle", "podložka", "podlozka", "playmat",
    "album", "binder", "obal", "sleeves", "sleeve", "keychain", "kľúčenka",
    "klucenka", "batoh", "backpack", "taška", "taska", "poster", "plagát",
    "plagat", "sticker", "nálepka", "nalepka", "slúchadlá", "sluchatka",
    "headphones", "earphones", "hračka", "hracka", "toy", "toys", "lampa",
    "lamp", "fľaša", "flasa", "bottle", "peňaženka", "penezenka", "wallet",
    "puzdro", "pouzdro", "phone case", "mobile case", "čepice", "cepice",
    "cap", "deka", "blanket", "polštář", "polstar", "vankúš", "vankus",
]
MERCH_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(w) for w in MERCH_WORDS) + r")(?!\w)",
    re.I,
)


def is_merch(title, extra_text=""):
    return MERCH_RE.search(clean_text(title + " " + extra_text)) is not None


# =========================================================
# HTTP
# =========================================================

def fetch(url, timeout=SEARCH_TIMEOUT):
    start = time.monotonic()
    debug = {"url": url, "http_status": None, "elapsed_ms": 0,
             "status": "unknown", "error": ""}
    try:
        resp = get_http_session().get(url, timeout=timeout, allow_redirects=True)
        debug["http_status"] = resp.status_code
        if resp.status_code != 200:
            debug.update(status="http_error", error=f"HTTP {resp.status_code}")
            resp = None
        else:
            debug["status"] = "http_ok"
    except requests.Timeout:
        resp = None
        debug.update(status="timeout", error="Request timeout")
    except Exception as e:
        resp = None
        debug.update(status="request_error", error=str(e))
    debug["elapsed_ms"] = round((time.monotonic() - start) * 1000)
    return resp, debug


def absolute_url(base_url, href):
    href = (href or "").strip()
    if not href or href.startswith(("javascript:", "#")):
        return ""
    return urllib.parse.urljoin(base_url, href)


# =========================================================
# CARDYX PARSING
# =========================================================

IMG_ATTRS = ["src", "data-src", "data-lazy-src", "data-original",
             "data-image", "data-image-src", "data-original-src"]


def img_url(image, base_url):
    """Najlepšia URL obrázka z <img> (src atribúty, potom srcset)."""
    for attr in IMG_ATTRS:
        value = clean_text(image.get(attr, ""))
        if value and not value.startswith("data:image/"):
            absolute = absolute_url(base_url, value)
            if absolute:
                return absolute

    for attr in ("srcset", "data-srcset"):
        candidates = []
        for part in clean_text(image.get(attr, "")).split(","):
            pieces = clean_text(part).split()
            if not pieces:
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


def cardyx_extract_title(anchor):
    title = clean_text(anchor.get_text(" ", strip=True))
    if title:
        return title
    for attr in ("title", "aria-label"):
        value = clean_text(anchor.get(attr, ""))
        if value:
            return value
    image = anchor.find("img")
    if image:
        for attr in ("alt", "title"):
            value = clean_text(image.get(attr, ""))
            if value:
                return value
    return ""


def cardyx_extract_image(anchor, base_url="https://www.cardyx.sk/"):
    image = anchor.find("img")
    current = anchor
    for _ in range(3):
        if image is not None:
            break
        current = current.parent
        if not current:
            break
        image = current.find("img")
    return img_url(image, base_url) if image is not None else ""


def cardyx_extract_product_page_image(soup, product_url):
    for selector in ('meta[property="og:image"]', 'meta[property="og:image:url"]',
                     'meta[name="og:image"]', 'meta[name="twitter:image"]',
                     'meta[property="twitter:image"]'):
        meta = soup.select_one(selector)
        if meta:
            value = clean_text(meta.get("content", ""))
            if value and not value.startswith("data:image/"):
                absolute = absolute_url(product_url, value)
                if absolute:
                    return absolute

    markers = ("product", "produkt", "gallery", "main-image", "main image", "woocommerce")
    images = soup.find_all("img")

    def priority(img):
        marker = " ".join([
            clean_text(img.get("alt", "")),
            clean_text(" ".join(img.get("class", []))),
            clean_text(img.get("id", "")),
        ]).lower()
        return 0 if any(t in marker for t in markers) else 1

    for img in sorted(images, key=priority):
        url = img_url(img, product_url)
        if url:
            return url
    return ""


def cardyx_fetch_product_image(product_url):
    product_url = clean_text(product_url)
    if not product_url:
        return ""
    cached = image_cache.get(product_url)
    if cached is not None:
        return cached
    image_url = ""
    try:
        resp, _ = fetch(product_url, timeout=IMAGE_FETCH_TIMEOUT)
        if resp:
            soup = BeautifulSoup(resp.text, "html.parser")
            image_url = cardyx_extract_product_page_image(soup, product_url)
    except Exception:
        pass
    image_cache.set(product_url, image_url)
    return image_url


def cardyx_enrich_missing_images(results, debug=None):
    missing = [r for r in results
               if not clean_text(r.get("image", "")) and clean_text(r.get("link", ""))]
    if not missing:
        return results

    found = 0
    try:
        with ThreadPoolExecutor(max_workers=IMAGE_FETCH_WORKERS) as ex:
            futures = {ex.submit(cardyx_fetch_product_image, r["link"]): r for r in missing}
            for fut in as_completed(futures):
                try:
                    url = fut.result()
                except Exception:
                    url = ""
                futures[fut]["image"] = url
                found += bool(url)
    except Exception:
        pass

    if debug is not None:
        debug["image_fallback_candidates"] = len(missing)
        debug["image_fallback_found"] = found
    return results


def cardyx_find_product_block(anchor):
    current, best = anchor, ""
    for level in range(1, 7):
        current = current.parent
        if not current:
            break
        text = clean_text(current.get_text(" ", strip=True))
        if text and any(c in text for c in ("€", "Kč", "CZK")) and len(text) < 1800:
            best = text
            if level >= 2:
                break
    if best:
        return best
    return clean_text(anchor.parent.get_text(" ", strip=True)) if anchor.parent else ""


# =========================================================
# CARDYX SEARCH
# =========================================================

def _log(debug, **entry):
    if len(debug["sample_decisions"]) < 20:
        debug["sample_decisions"].append(entry)


def cardyx_search(query, return_debug=False, enrich_images=True,
                  cache_result=True, timeout=None):
    start = time.monotonic()
    timeout = timeout or SEARCH_TIMEOUT

    def finish(results, debug):
        debug["elapsed_ms"] = round((time.monotonic() - start) * 1000)
        return (results, debug) if return_debug else results

    # Cache hit (autocomplete môže použiť aj plné výsledky)
    cached = search_cache.get(clean_text(query).lower())
    if cached is not None:
        cached["debug"]["cache"] = "hit"
        add_suggestions_from_results(cached["results"])
        return finish(cached["results"], cached["debug"])

    results = []
    debug = {
        "shop": "CardyX", "query": query, "url": "", "status": "starting",
        "http_status": None, "results": 0, "links_scanned": 0, "unique_links": 0,
        "price_found": 0, "merch_filtered": 0, "match_filtered": 0, "accepted": 0,
        "images_found": 0, "images_missing": 0, "image_fallback_candidates": 0,
        "image_fallback_found": 0, "image_fallback_skipped": not enrich_images,
        "elapsed_ms": 0, "cache": "miss", "error": "", "sample_decisions": [],
    }

    try:
        url = "https://www.cardyx.sk/search?q=" + urllib.parse.quote(query)
        debug["url"] = url
        response, http_debug = fetch(url, timeout=timeout)
        debug["http_status"] = http_debug.get("http_status")

        if not response:
            debug["status"] = http_debug.get("status", "http_error")
            debug["error"] = http_debug.get("error", "")
            return finish(results, debug)

        soup = BeautifulSoup(response.text, "html.parser")
        links = soup.select('a[href*="/products/"]')
        debug["links_scanned"] = len(links)

        parsed = normalize_query(query)
        kind = classify_query(parsed)
        seen = set()

        for anchor in links:
            href = absolute_url("https://www.cardyx.sk/", anchor.get("href"))
            if not href:
                continue
            key = href.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)

            title = cardyx_extract_title(anchor)
            if not title:
                _log(debug, title="", decision="filtered", reason="no_title")
                continue

            block_text = cardyx_find_product_block(anchor)
            price = parse_price(block_text)
            if price is None and anchor.parent:
                price = parse_price(anchor.parent.get_text(" ", strip=True))
            if price is None:
                _log(debug, title=title, decision="filtered", reason="no_price")
                continue
            debug["price_found"] += 1

            if is_merch(title):
                debug["merch_filtered"] += 1
                _log(debug, title=title, decision="filtered", reason="merch")
                continue

            matcher = card_matches_query if kind == "card" else sealed_matches_query
            matched, reason = matcher(title, block_text, parsed)
            if not matched:
                debug["match_filtered"] += 1
                _log(debug, title=title, decision="filtered", reason=reason)
                continue

            image_url = cardyx_extract_image(anchor)
            results.append({
                "title": title, "shop": "CardyX", "country": "SK",
                "condition": "Nové", "price_eur": round(price, 2),
                "link": href, "image": image_url,
            })
            debug["accepted"] += 1
            _log(debug, title=title, price_eur=round(price, 2),
                 image=bool(image_url), decision="accepted", reason=reason)

        debug["unique_links"] = len(seen)

        if enrich_images:
            cardyx_enrich_missing_images(results, debug)

        debug["images_found"] = sum(1 for r in results if clean_text(r.get("image", "")))
        debug["images_missing"] = len(results) - debug["images_found"]

        results.sort(key=lambda r: float(r.get("price_eur", 999999)))
        debug["results"] = len(results)
        debug["status"] = "ok" if results else "no_results"

    except Exception as e:
        debug["status"] = "parser_error"
        debug["error"] = str(e)

    debug["elapsed_ms"] = round((time.monotonic() - start) * 1000)

    # Do hlavnej cache len plné výsledky (s obrázkami)
    if cache_result and enrich_images:
        search_cache.set(clean_text(query).lower(), {"results": results, "debug": debug})

    add_suggestions_from_results(results)
    return (results, debug) if return_debug else results


# =========================================================
# SEARCH ALL
# =========================================================

def search_all(query, return_debug=False):
    start = time.monotonic()
    try:
        results, debug = cardyx_search(query, return_debug=True,
                                       enrich_images=True, cache_result=True)
    except Exception as e:
        results, debug = [], {"shop": "CardyX", "query": query,
                              "status": "runner_error", "results": 0, "error": str(e)}
    debug["elapsed_ms"] = round((time.monotonic() - start) * 1000)

    results.sort(key=lambda r: float(r.get("price_eur", 999999)))

    if return_debug:
        return results, {"query": query, "shops": [debug], "total_results": len(results)}
    return results


def build_summary(results):
    prices = [float(r["price_eur"]) for r in results if r.get("price_eur") is not None]
    if not prices:
        return {"count": len(results), "lowest_eur": None, "average_eur": None}
    return {
        "count": len(results),
        "lowest_eur": round(min(prices), 2),
        "average_eur": round(sum(prices) / len(prices), 2),
    }


# =========================================================
# AUTOCOMPLETE
# =========================================================

def make_suggestion_from_title(title):
    title = clean_text(title)
    if not title or is_merch(title):
        return None

    p = normalize_query(title)
    parts = [p.get("pokemon"), p.get("suffix"), p.get("card_number"), p.get("set_name")]
    query = clean_text(" ".join(x for x in parts if x)) or title
    if len(query) > 120:
        query = title[:120].strip()

    is_card = not p.get("product_type")
    return {
        "title": title, "query": query,
        "subtitle": "Reálna karta z CardyX",
        "type": "card" if is_card else "product",
        "type_label": "Karta" if is_card else "Produkt",
        "image": "", "price_eur": None, "price": None, "link": "",
    }


def suggestions_from_results(results):
    out = {}
    for r in results or []:
        s = make_suggestion_from_title(r.get("title", ""))
        if not s:
            continue
        s["image"] = clean_text(r.get("image", ""))
        s["link"] = clean_text(r.get("link", ""))
        try:
            price = round(float(r["price_eur"]), 2) if r.get("price_eur") is not None else None
        except (TypeError, ValueError):
            price = None
        s["price_eur"] = s["price"] = price

        key = clean_text(s["query"]).lower()
        if not key:
            continue
        old = out.get(key)
        score = lambda x: bool(x["image"]) + (x["price_eur"] is not None)
        if old is None or score(s) > score(old):
            out[key] = s
    return list(out.values())


def add_suggestions_from_results(results):
    items = suggestions_from_results(results)
    if not items:
        return
    with _catalog_lock:
        for s in items:
            key = clean_text(s["query"]).lower()
            SUGGESTION_CATALOG.pop(key, None)  # presun na koniec
            SUGGESTION_CATALOG[key] = s
        while len(SUGGESTION_CATALOG) > 500:
            SUGGESTION_CATALOG.pop(next(iter(SUGGESTION_CATALOG)))


def suggestion_score(item, query):
    q = clean_text(query).lower()
    title = clean_text(item.get("title", "")).lower()
    score = 0
    if title.startswith(q):
        score += 100
    if any(w.startswith(q) for w in title.split()):
        score += 60
    if q in title:
        score += 40
    if q in clean_text(item.get("subtitle", "")).lower():
        score += 10
    if item.get("price_eur") is not None:
        score += 15
    if clean_text(item.get("image", "")):
        score += 10
    return score + max(0, 20 - len(title) // 10)


@app.get("/api/suggestions")
def api_suggestions():
    q = clean_text(request.args.get("q", ""))
    if len(q) < 2:
        return jsonify({"query": q, "normalized_query": "", "suggestions": []})

    normalized = normalize_query(q).get("normalized", "")

    cached = suggestion_cache.get(q.lower())
    if cached is not None:
        return jsonify({"query": q, "normalized_query": normalized,
                        "suggestions": cached, "source": "suggestion_cache"})

    q_lower = q.lower()
    with _catalog_lock:
        snapshot = [dict(i) for i in SUGGESTION_CATALOG.values()]

    candidates = [
        i for i in snapshot
        if q_lower in clean_text(i.get("title", "")).lower()
        or q_lower in clean_text(i.get("query", "")).lower()
    ]

    remote_used = False
    if len(candidates) < MIN_LOCAL_SUGGESTIONS:
        remote_used = True
        remote = cardyx_search(q, return_debug=False, enrich_images=False,
                               cache_result=False, timeout=SUGGESTION_TIMEOUT)
        known = {clean_text(c.get("query", "")).lower() for c in candidates}
        for s in suggestions_from_results(remote):
            if clean_text(s["query"]).lower() not in known:
                candidates.append(s)

        if not candidates:
            parsed = normalize_query(q)
            norm = parsed.get("normalized", "")
            if norm and norm.lower() != q_lower and (parsed.get("product_type") or parsed.get("pokemon")):
                is_product = bool(parsed.get("product_type"))
                candidates.append({
                    "title": norm, "query": norm,
                    "subtitle": "Produkt" if is_product else "Pokémon",
                    "type": "product" if is_product else "card",
                    "type_label": "Produkt" if is_product else "Karta",
                    "image": "", "price_eur": None, "price": None, "link": "",
                })

    candidates.sort(key=lambda i: suggestion_score(i, q), reverse=True)

    output, seen = [], set()
    for item in candidates:
        key = clean_text(item.get("query", item.get("title", ""))).lower()
        if key and key not in seen:
            seen.add(key)
            output.append(item)
            if len(output) >= 8:
                break

    suggestion_cache.set(q.lower(), output)
    return jsonify({"query": q, "normalized_query": normalized,
                    "suggestions": output,
                    "source": "cardyx" if remote_used else "catalog"})


# =========================================================
# API
# =========================================================

@app.get("/api/parse")
def api_parse():
    parsed = normalize_query(request.args.get("q", ""))
    parsed["type"] = classify_query(parsed)
    return jsonify(parsed)


@app.get("/api/search")
def api_search():
    original = clean_text(request.args.get("q", ""))
    if not original:
        return jsonify({"error": "Chýba vyhľadávanie."}), 400

    parsed = normalize_query(original)
    normalized = parsed.get("normalized", original)
    results, debug = search_all(normalized, return_debug=True)
    save_history(original, results)

    info = {"title": normalized, "subtitle": "", "image": ""}
    if parsed.get("set_name"):
        info["subtitle"] = "Set: " + parsed["set_name"]
    elif parsed.get("pokemon"):
        info["subtitle"] = "Pokémon: " + parsed["pokemon"]

    return jsonify({
        "query": original, "normalized_query": normalized, "parsed": parsed,
        "results": results, "summary": build_summary(results),
        "czk_per_eur": CZK_PER_EUR, "info": info, "debug": debug,
    })


@app.get("/api/debug/search")
def api_debug_search():
    original = clean_text(request.args.get("q", ""))
    if not original:
        return jsonify({"error": "Chýba vyhľadávanie."}), 400
    parsed = normalize_query(original)
    normalized = parsed.get("normalized", original)
    results, debug = search_all(normalized, return_debug=True)
    return jsonify({"query": original, "normalized_query": normalized,
                    "parsed": parsed, "debug": debug, "results": results})


@app.get("/api/debug/cache")
def api_debug_cache():
    s_items, s_active = search_cache.stats()
    sg_items, _ = suggestion_cache.stats()
    i_items, i_active = image_cache.stats()
    with _catalog_lock:
        catalog_items = len(SUGGESTION_CATALOG)
    return jsonify({
        "status": "ok", "version": VERSION,
        "cache_items": s_items, "active": s_active, "expired": s_items - s_active,
        "suggestion_cache_items": sg_items,
        "suggestion_catalog_items": catalog_items,
        "image_cache_items": i_items, "image_cache_active": i_active,
    })


@app.get("/api/debug/cache/clear")
def api_debug_cache_clear():
    if not ADMIN_KEY or request.args.get("key", "") != ADMIN_KEY:
        return jsonify({"error": "Nepovolené."}), 403
    search_cache.clear()
    suggestion_cache.clear()
    image_cache.clear()
    with _catalog_lock:
        SUGGESTION_CATALOG.clear()
    return jsonify({"status": "ok", "message": "Cache a katalóg vymazané."})


@app.get("/health")
def health():
    index_path = find_index()
    with _catalog_lock:
        catalog_items = len(SUGGESTION_CATALOG)
    return jsonify({
        "service": "CardRadar", "status": "ok", "version": VERSION,
        "index_exists": bool(index_path), "active_shops": ["CardyX"],
        "suggestion_catalog_items": catalog_items,
        "image_cache_items": image_cache.stats()[0],
    })


@app.get("/")
def home():
    index_path = find_index()
    if not index_path:
        return Response("<h1>CardRadar</h1><p>index.html nebol nájdený.</p>",
                        status=500, mimetype="text/html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    except Exception as e:
        return Response(f"<h1>CardRadar</h1><p>Chyba pri načítaní stránky.</p><pre>{e}</pre>",
                        status=500, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
