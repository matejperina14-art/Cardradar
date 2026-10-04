import os
import re
import gzip
import random
import sqlite3
import threading
import time
import copy
import json
import secrets
import smtplib
import urllib.parse
from email.message import EmailMessage
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response

# =========================================================
# CARD RADAR 5.30
# Novinky 5.30:
#  - sklad: rozpoznanie Skladom / Vypredané / Predobjednávka / Na objednávku
#  - cena za booster (ETB, booster box, bundle, "36 balíčkov" v názve...)
#  - denná história cien + trend za 30 dní + /api/history pre graf
#  - strážca ceny: e-mail s potvrdením (SMTP v premenných prostredia)
#  - /api/latest pre obľúbené karty
#  - /api/debug/detect: rozpozná platformu obchodu a navrhne konfiguráciu
# Novinky 5.27:
#  - prísnejší filter merchu (plyšáky, tričká, šálky, hrnčeky, príslušenstvo...)
#  - našepkávač s obrázkami: Shopify predictive search (CardyX) + doťahovanie
#    chýbajúcich obrázkov, katalóg návrhov si obrázky pamätá
# Novinky 5.26:
#  - vyhľadávanie nečaká na obrázky (dotiahnu sa cez /api/images)
#  - chyby obchodov (timeout, HTTP chyba) sa neukladajú do cache
#  - rovnaké súbežné hľadania sa nescrapujú dvakrát (single-flight)
#  - zdieľané thread pooly, lxml parser (ak je nainštalovaný), gzip
#  - rate limit na IP, debug len s ADMIN_KEY
#  - stav obchodov v odpovedi (frontend ukáže, ktorý neodpovedal)
#  - história cien v pozadí + automatické mazanie starých záznamov
# =========================================================

VERSION = "5.30"
app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cardradar.db")
CZK_PER_EUR = 24.4618
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

CACHE_TTL = 600
CACHE_MAX_ITEMS = 300
SUGGESTION_CACHE_TTL = 300
SUGGESTION_CACHE_MAX_ITEMS = 300
IMAGE_CACHE_TTL = 6 * 3600
IMAGE_CACHE_MAX_ITEMS = 2000
SEARCH_TIMEOUT = 8
SUGGESTION_TIMEOUT = 4
IMAGE_FETCH_WORKERS = 10
IMAGE_FETCH_TIMEOUT = 4
IMAGE_BATCH_MAX = 12        # max. odkazov v jednej požiadavke /api/images
IMAGE_BATCH_BUDGET = 6      # max. sekúnd čakania v /api/images
MIN_LOCAL_SUGGESTIONS = 4
HISTORY_KEEP_DAYS = 90

# Rate limit (požiadaviek za minútu na jednu IP)
RATE_SEARCH = 30
RATE_SUGGEST = 120
RATE_IMAGES = 60
RATE_HISTORY = 60
RATE_ALERTS = 5          # nových strážcov za 10 minút na IP
ALERTS_PER_EMAIL = 20

# Strážca ceny (e-mail). Bez SMTP_HOST je funkcia vypnutá.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")   # napr. https://cardradar.sk
ALERT_CHECK_HOURS = float(os.environ.get("ALERT_CHECK_HOURS", "6"))
ALERTS_ENABLED = bool(SMTP_HOST and SMTP_FROM)

try:
    import fcntl  # Linux: zámok, aby strážcu cien spúšťal len jeden proces
except ImportError:
    fcntl = None

try:
    import lxml  # noqa: F401  (pip install lxml = rýchlejšie parsovanie)
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

# =========================================================
# OBCHODY (pridanie obchodu = jeden záznam v tomto zozname)
#   name           názov v zobrazení
#   country        "SK" alebo "CZ"
#   base_url       základná URL (na skladanie odkazov a obrázkov)
#   search_url     URL vyhľadávania, {q} sa nahradí hľadaným textom
#   link_selector  CSS selektor odkazov na produkty vo výsledkoch
#   enabled        False = obchod sa nepoužije (dá sa ho stále testovať)
#   loose_set      True = v názvoch nie je set, stačí číslo karty
# =========================================================

SHOPS = [
    {
        "name": "CardyX", "country": "SK", "enabled": True,
        "base_url": "https://www.cardyx.sk/",
        "search_url": "https://www.cardyx.sk/search?q={q}",
        "link_selector": 'a[href*="/products/"]',
        "shopify": True,  # rýchly našepkávač s obrázkami cez /search/suggest.json
    },
    # --- Shoptet obchody (SK). Vyhľadávanie: /vyhladavanie/?string=... ---
    {
        "name": "TCG Zone Nitra", "country": "SK", "enabled": True,
        "base_url": "https://www.tcgzonenitra.sk/",
        "search_url": "https://www.tcgzonenitra.sk/vyhladavanie/?string={q}",
        "link_selector": "div.product a.name",
        "loose_set": True,
    },
    {
        "name": "Beardex", "country": "SK", "enabled": True,
        "base_url": "https://www.beardex.eu/",
        "search_url": "https://www.beardex.eu/vyhladavanie/?string={q}",
        "link_selector": "div.product a.name",
    },
    {
        "name": "CardEmpire", "country": "SK", "enabled": True,
        "base_url": "https://www.cardempire.sk/",
        "search_url": "https://www.cardempire.sk/vyhladavanie/?string={q}",
        "link_selector": "div.product a.name",
    },
    # --- CZ (Upgates): VYPNUTÉ, kým sa neoverí vyhľadávacia URL
    {
        "name": "Gengar.cz", "country": "CZ", "enabled": False,
        "base_url": "https://www.gengar.cz/",
        "search_url": "https://www.gengar.cz/search?q={q}",
        "link_selector": 'a[href*="/p/"]',
    },
    # ŠABLÓNA - odkomentujte a vyplňte po overení obchodu cez /api/debug/shop:
    # {
    #     "name": "NazovObchodu", "country": "CZ", "enabled": True,
    #     "base_url": "https://www.priklad.cz/",
    #     "search_url": "https://www.priklad.cz/vyhledavani?q={q}",
    #     "link_selector": 'a[href*="/produkt/"]',
    # },
]

ACTIVE_SHOPS = [s for s in SHOPS if s.get("enabled", True)]
ALLOWED_HOSTS = {urllib.parse.urlparse(s["base_url"]).netloc.lower() for s in SHOPS}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "cs-CZ,sk-SK;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# Zdieľané pooly: vlákna (a ich HTTP spojenia) sa používajú opakovane
SHOP_EXECUTOR = ThreadPoolExecutor(max_workers=max(8, len(ACTIVE_SHOPS) * 4),
                                   thread_name_prefix="shop")
IMAGE_EXECUTOR = ThreadPoolExecutor(max_workers=IMAGE_FETCH_WORKERS,
                                    thread_name_prefix="img")
BG_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bg")

_local = threading.local()


def get_http_session():
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
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
# TTL CACHE
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

SUGGESTION_CATALOG = {}
_catalog_lock = threading.Lock()

_inflight = {}
_inflight_lock = threading.Lock()


# =========================================================
# RATE LIMIT
# =========================================================

class RateLimiter:
    def __init__(self, limit, window=60):
        self.limit = limit
        self.window = window
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            if len(self._hits) > 5000:
                for k in [k for k, v in self._hits.items()
                          if not v or now - v[-1] > self.window]:
                    self._hits.pop(k, None)
            return True


search_limiter = RateLimiter(RATE_SEARCH)
suggest_limiter = RateLimiter(RATE_SUGGEST)
images_limiter = RateLimiter(RATE_IMAGES)
history_limiter = RateLimiter(RATE_HISTORY)
alerts_limiter = RateLimiter(RATE_ALERTS, window=600)


def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[0].strip() if xff else "") or request.remote_addr or "?"


def too_many():
    return jsonify({"error": "Príliš veľa požiadaviek. Skús to o chvíľu."}), 429


def is_admin():
    return bool(ADMIN_KEY) and request.args.get("key", "") == ADMIN_KEY


def debug_allowed():
    # bez ADMIN_KEY (lokálny vývoj) sú debug endpointy voľné
    return not ADMIN_KEY or is_admin()


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def prune_history(conn):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM price_daily WHERE day < ?", (cutoff,))


def init_db():
    conn = db_connect()
    # pôvodná tabuľka (už sa do nej nezapisuje, slúži len na prenos dát)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT, shop TEXT, title TEXT,
            price_eur REAL, link TEXT, checked_at TEXT
        )
    """)
    # jedna cena na produkt a deň = malá databáza, rýchle grafy
    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_daily (
            link TEXT NOT NULL, day TEXT NOT NULL,
            shop TEXT, title TEXT, price_eur REAL, stock TEXT,
            PRIMARY KEY (link, day)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL, link TEXT NOT NULL, title TEXT, shop TEXT,
            target REAL NOT NULL, token TEXT UNIQUE NOT NULL,
            confirmed INTEGER DEFAULT 0, created TEXT, site TEXT,
            last_price REAL, last_checked TEXT, notified TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_email ON alerts(email)")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")

    migrated = conn.execute("SELECT v FROM meta WHERE k='migrated_daily'").fetchone()
    if not migrated:
        conn.execute("""
            INSERT OR IGNORE INTO price_daily (link, day, shop, title, price_eur, stock)
            SELECT link, substr(checked_at, 1, 10), shop, title, MIN(price_eur), ''
            FROM price_history WHERE link != '' AND price_eur > 0
            GROUP BY link, substr(checked_at, 1, 10)
        """)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('migrated_daily', '1')")
    prune_history(conn)
    conn.commit()
    conn.close()


init_db()


def _save_history(results):
    conn = None
    try:
        conn = db_connect()
        day = today_str()
        rows = [
            (r["link"], day, r.get("shop", ""), r.get("title", ""),
             r.get("price_eur"), r.get("stock", ""))
            for r in results if r.get("link") and r.get("price_eur")
        ]
        conn.executemany("""
            INSERT INTO price_daily (link, day, shop, title, price_eur, stock)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(link, day) DO UPDATE SET
                price_eur = excluded.price_eur, stock = excluded.stock,
                title = excluded.title
        """, rows)
        if random.random() < 0.02:
            prune_history(conn)
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


def save_history(query, results):
    """Zápis do DB beží na pozadí, odpoveď naň nečaká."""
    if results:
        BG_EXECUTOR.submit(_save_history, copy.deepcopy(results))


def add_trends(results, days=30):
    """Ku každému výsledku pridá najstaršiu cenu za posledných `days` dní."""
    links = [r["link"] for r in results if r.get("link")]
    if not links:
        return
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    today = today_str()
    oldest = {}
    conn = None
    try:
        conn = db_connect()
        for i in range(0, len(links), 400):
            chunk = links[i:i + 400]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT link, day, price_eur FROM price_daily "
                f"WHERE link IN ({marks}) AND day >= ? AND day < ? ORDER BY day ASC",
                (*chunk, since, today)).fetchall()
            for link, day, price in rows:
                oldest.setdefault(link, (day, price))
    except Exception:
        return
    finally:
        if conn:
            conn.close()
    for r in results:
        old = oldest.get(r.get("link"))
        if old and old[1]:
            r["trend"] = {"since": old[0], "price_eur": round(old[1], 2)}


# =========================================================
# INDEX.HTML (načítaný raz, znovu len pri zmene súboru)
# =========================================================

_index_cache = {"path": None, "mtime": None, "html": None}


def find_index():
    for sub in ("templates", "Templates", ""):
        path = os.path.join(BASE_DIR, sub, "index.html")
        if os.path.isfile(path):
            return path
    return None


def load_index():
    path = find_index()
    if not path:
        return None
    mtime = os.path.getmtime(path)
    if _index_cache["path"] != path or _index_cache["mtime"] != mtime:
        with open(path, "r", encoding="utf-8") as f:
            _index_cache.update(path=path, mtime=mtime, html=f.read())
    return _index_cache["html"]


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


def _compile_aliases(pairs):
    out = []
    for alias, canonical in sorted(pairs, key=lambda x: len(x[0]), reverse=True):
        out.append((re.compile(r"\b" + re.escape(alias.lower()) + r"\b"), canonical))
    return out


# predkompilované regexy = rýchlejšia normalizácia
_SETS_RE = _compile_aliases(SET_ALIASES.items())
_KNOWN_SETS_RE = _compile_aliases((c, c) for c in KNOWN_SETS)
_POKEMON_RE = _compile_aliases(POKEMON_ALIASES.items())
_PRODUCT_RE = [(c, re.compile(p)) for c, p in PRODUCT_PATTERNS]
_SUFFIX_RE = re.compile(r"\b(vmax|vstar|ex|gx|v)\b", re.I)
_CARDNUM_RE = re.compile(r"\b(\d{1,4})\s*/\s*(\d{1,4})\b")


def _extract(q, candidates):
    for pattern, canonical in candidates:
        if pattern.search(q):
            return canonical, pattern.sub(" ", q)
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
    m = _CARDNUM_RE.search(q)
    if m:
        card_number = f"{m.group(1)}/{m.group(2)}"
        q = _CARDNUM_RE.sub(" ", q, count=1)

    product_type = ""
    for canonical, pattern in _PRODUCT_RE:
        if pattern.search(q):
            product_type = canonical
            q = pattern.sub(" ", q)
            break

    # najprv celé názvy setov ("surging sparks"), až potom skratky ("sv8", "surging")
    set_name, q = _extract(q, _KNOWN_SETS_RE)
    if not set_name:
        set_name, q = _extract(q, _SETS_RE)
    if set_name:  # zvyšné slová z názvu setu preč ("surging" + "sparks")
        for w in set_name.split():
            q = re.sub(r"\b" + re.escape(w) + r"\b", " ", q)

    pokemon, q = _extract(q, _POKEMON_RE)

    suffix = ""
    m = _SUFFIX_RE.search(q)
    if m:
        suffix = m.group(1).lower()
        q = _SUFFIX_RE.sub(" ", q)

    q = clean_text(re.sub(r"\bpok[eé]mon\b", " ", q, flags=re.I))

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


def card_matches_query(title, extra_text, parsed, loose_set=False):
    searchable = clean_text(title + " " + extra_text)
    pokemon, set_name = parsed.get("pokemon"), parsed.get("set_name")
    card_number, suffix = parsed.get("card_number"), parsed.get("suffix")

    if pokemon and not text_contains_word(searchable, pokemon):
        return False, "pokemon_not_found"
    if card_number:
        if card_number.replace(" ", "").lower() not in re.sub(r"\s+", "", searchable.lower()):
            return False, "card_number_not_found"
    if set_name and not (loose_set and card_number) and not set_matches_text(searchable, set_name):
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

_NUM = r"(\d{1,3}(?:[ ]\d{3})+(?:[.,]\d{1,2})?|\d{1,8}(?:[.,]\d{1,2})?)"
_EX_VAT = re.compile(
    r"(?:€\s*" + _NUM + r"|" + _NUM + r"\s*(?:€|Kč|CZK))\s*(?:bez\s+DPH|excl\.?\s*VAT)",
    re.I,
)
_EUR_RES = [re.compile(r"€\s*" + _NUM), re.compile(_NUM + r"\s*€")]
_CZK_RES = [re.compile(_NUM + r"\s*(?:Kč|CZK)", re.I), re.compile(r"(?:Kč|CZK)\s*" + _NUM, re.I)]


def _to_float(value):
    value = value.replace(" ", "")
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
        return None


def parse_price(text):
    """Vráti cenu v EUR (CZK sa prepočíta). Ceny 'bez DPH' sa ignorujú."""
    text = clean_text(text)
    if not text:
        return None
    text = _EX_VAT.sub(" ", text)

    for pattern in _EUR_RES:
        m = pattern.search(text)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                return v

    for pattern in _CZK_RES:
        m = pattern.search(text)
        if m:
            v = _to_float(m.group(1))
            if v is not None:
                return v / CZK_PER_EUR
    return None


# =========================================================
# JAZYK PRODUKTU
# =========================================================

LANG_PATTERNS = [
    ("JP", re.compile(r"japon\w*|japan\w*|\bjpn\b", re.I)),
    ("KR", re.compile(r"k[óo]rej\w*|korean\w*", re.I)),
    ("CN", re.compile(r"[čc][ií]nsk\w*|[čc][ií]nšt\w*|chinese", re.I)),
    ("ID", re.compile(r"indon[ée]z\w*|indonesian", re.I)),
]
FOREIGN_QUERY_RE = re.compile(
    r"japon|japan|jpn|k[óo]rej|korean|[čc][ií]nsk|chinese|indon", re.I)


def detect_language(title):
    for code, pattern in LANG_PATTERNS:
        if pattern.search(title or ""):
            return code
    return ""


# =========================================================
# MERCH FILTER
# =========================================================

# Slová sa hľadajú ako ZAČIATOK slova, takže "plyš" chytí aj plyšák, plyšová,
# plyšáky a "tričk" chytí tričko, trička, tričká. (Stará verzia hľadala len
# celé slová, preto jej "plyšák" alebo "šálka" prešli.)

# Príslušenstvo: vždy preč (chceme len karty a sealed produkty)
ACCESSORY_PATTERNS = [
    r"sleeves?", r"obal\w*", r"album\w*", r"binder\w*", r"toploader\w*",
    r"playmat\w*", r"podlo[žz]k\w*", r"deck\s*box\w*", r"deckbox\w*",
    r"puzdr\w*", r"pouzdr\w*", r"stojan\w*", r"portfoli\w*", r"one\s*touch",
    r"card\s+holder\w*", r"magnetic\s+holder\w*", r"penny\s+sleeves?",
]

# Merch: preč, pokiaľ názov zároveň neobsahuje znak TCG produktu
# (napr. "Charizard ex Premium Collection with figure" ostane)
MERCH_PATTERNS = [
    # oblečenie
    r"tri[čc]k\w*", r"t-?shirt\w*", r"shirt\w*", r"mikin\w*", r"hoodie\w*",
    r"pono[žz]k\w*", r"socks?", r"[čc]iap\w*", r"[čc]epic\w*", r"[šs]iltovk\w*",
    r"k[šs]iltovk\w*", r"caps?", r"py[žz]am\w*", r"kost[ýy]m\w*", r"costume\w*",
    r"rukavic\w*", r"[šs]atk\w*", r"[šs][áa]l", r"[šs][áa]ly", r"scarf\w*",
    r"tepl[áa]k\w*", r"leg[íi]n\w*", r"[šs]ortk\w*", r"[šs]ortky",
    # plyšáky, figúrky, hračky
    r"ply[šs]\w*", r"plush\w*", r"peluche\w*", r"fig[úu]r\w*", r"figur\w*",
    r"figure\w*", r"statue\w*", r"so[šs]k\w*", r"funko\w*", r"pop!", r"vinyl\w*",
    r"hra[čc]k\w*", r"toys?", r"lego", r"mega\s+construx", r"stavebnic\w*",
    r"puzzle\w*", r"pokladni[čc]k\w*", r"mystery", r"blind\s*box\w*",
    r"gashapon\w*", r"tamagotchi",
    # domácnosť, kuchyňa
    r"hrn[čc]\w*", r"hrnk\w*", r"hrnek", r"mugs?", r"[šs][áa]lk\w*", r"poh[áa]r\w*",
    r"cups?", r"tumbler\w*", r"f[ľl]a[šs]\w*", r"bottle\w*", r"termosk\w*",
    r"lamp", r"lamp[ay]", r"lampi[čc]k\w*", r"svietidl\w*", r"deka", r"deky",
    r"blanket\w*", r"vank[úu][šs]\w*", r"pol[šs]t[áa][řr]\w*", r"uter[áa]k\w*",
    r"osu[šs]k\w*", r"towel\w*", r"oblie[čc]k\w*", r"tanier\w*", r"misk[ay]",
    r"lunch\s*box\w*", r"desiatov\w*",
    # škola, doplnky, elektronika
    r"batoh\w*", r"backpack\w*", r"ruksak\w*", r"ta[šs]k\w*", r"bags?",
    r"pera[čc]n[íi]k\w*", r"z[áa]pisn[íi]k\w*", r"zo[šs]it\w*", r"fixk\w*",
    r"pastel\w*", r"k[ľl][úu][čc]enk\w*", r"keychain\w*", r"keyring\w*",
    r"pr[íi]ves\w*", r"n[áa]ram\w*", r"n[áa]hrdeln[íi]k\w*", r"[šs]perk\w*",
    r"odznak\w*", r"pins?", r"bro[žz]\w*", r"pe[ňn]a[žz]enk\w*", r"wallet\w*",
    r"phone\s+case", r"mobile\s+case", r"hodink\w*", r"sl[úu]chadl\w*",
    r"sluch[áa]tk\w*", r"headphones?", r"earphones?", r"reproduktor\w*",
    r"plag[áa]t\w*", r"poster\w*", r"sticker\w*", r"n[áa]lepk\w*", r"tetov\w*",
    r"knih\w*", r"kniha", r"books?", r"komiks\w*", r"manga", r"omal\w*",
    r"nintendo", r"videohr\w*",
    # jedlo
    r"[čc]okol[áa]d\w*", r"cukrovink\w*", r"candy", r"l[íi]zank\w*", r"[žz]uva[čc]k\w*",
]

# Znaky skutočného TCG produktu (karta / sealed)
TCG_MARKER_RE = re.compile(
    r"booster|elite\s+trainer|\betb\b|collection|kolekci|blister|\btins?\b|\btcg\b"
    r"|battle\s+deck|theme\s+deck|build\s*(?:&|and)?\s*battle|display"
    r"|\b\d{1,3}\s*/\s*\d{1,3}\b",
    re.I,
)


def _words_re(patterns):
    return re.compile(r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)", re.I)


ACCESSORY_RE = _words_re(ACCESSORY_PATTERNS)
MERCH_RE = _words_re(MERCH_PATTERNS)


def merch_reason(title, extra_text=""):
    """'' = je to karta/produkt; inak dôvod vyradenia."""
    text = clean_text(title + " " + extra_text)
    m = ACCESSORY_RE.search(text)
    if m:
        return "accessory:" + m.group(0).lower()
    m = MERCH_RE.search(text)
    if m and not TCG_MARKER_RE.search(text):
        return "merch:" + m.group(0).lower()
    return ""


def is_merch(title, extra_text=""):
    return bool(merch_reason(title, extra_text))


# =========================================================
# SKLAD
# =========================================================

STOCK_OUT_RE = re.compile(
    r"vypredan\w*|nie\s+je\s+skladom|nie\s+je\s+na\s+sklade|nedostupn\w*|vyprodan\w*"
    r"|nen[íi]\s+skladem|nen[íi]\s+dostupn\w*|sold\s*out|out\s+of\s+stock|ausverkauft", re.I)
STOCK_PRE_RE = re.compile(r"predobjedn\w*|p[řr]edobjedn\w*|pre-?order\w*|vorbestell\w*", re.I)
STOCK_ORDER_RE = re.compile(r"na\s+objedn[áa]vku|do\s+\d+\s+dn[íi]|na\s+dotaz", re.I)
STOCK_IN_RE = re.compile(r"skladom|skladem|na\s+sklade|in\s+stock|dostupn[ée]|k\s+odberu|ihne[dď]", re.I)


def detect_stock(text):
    """'in' | 'out' | 'preorder' | 'order' | '' (nevieme)"""
    text = clean_text(text)
    if not text:
        return ""
    if STOCK_OUT_RE.search(text):
        return "out"
    if STOCK_PRE_RE.search(text):
        return "preorder"
    if STOCK_ORDER_RE.search(text):
        return "order"
    if STOCK_IN_RE.search(text):
        return "in"
    return ""


# =========================================================
# POČET BOOSTEROV (cena za booster)
# =========================================================

PACKS_EXPLICIT_RE = re.compile(
    r"(?<![\d/.,])(\d{1,2})\s*(?:-|x)?\s*(?:booster\w*|bal[íi][čc]\w*|packs?\b|packungen|boost\w*)",
    re.I)
PACKS_PAREN_RE = re.compile(r"booster\s*(?:box|display)\D{0,10}\((\d{1,2})\)", re.I)


def estimate_packs(title):
    """Odhad počtu boosterov v produkte; None = nevieme / nemá zmysel."""
    t = clean_text(title).lower()
    if not t:
        return None
    m = PACKS_EXPLICIT_RE.search(t) or PACKS_PAREN_RE.search(t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 36:
            return n
    if re.search(r"booster\s*(?:box|display)", t):
        return 18 if re.search(r"\bhalf\b|poloviční|polovičn", t) else 36
    if re.search(r"elite\s+trainer\s+box|\betb\b", t):
        return 11 if "pokemon center" in t or "pokémon center" in t else 9
    if re.search(r"booster\s*bundle", t):
        return 6
    if re.search(r"sleeved\s+booster|booster\s+pack|\bbooster\b$", t) and not re.search(
            r"collection|box|tin|blister|bundle|display", t):
        return 1
    return None


# =========================================================
# HTTP
# =========================================================

def fetch(url, timeout=SEARCH_TIMEOUT):
    start = time.monotonic()
    debug = {"url": url, "http_status": None, "elapsed_ms": 0,
             "status": "unknown", "error": ""}
    try:
        resp = get_http_session().get(url, timeout=(3, timeout), allow_redirects=True)
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


def is_allowed_link(url):
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    return p.scheme in ("http", "https") and p.netloc.lower() in ALLOWED_HOSTS


# =========================================================
# PARSING
# =========================================================

IMG_ATTRS = ["src", "data-src", "data-lazy-src", "data-original",
             "data-image", "data-image-src", "data-original-src"]


def img_url(image, base_url):
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


def extract_title(anchor):
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


def extract_image(anchor, base_url):
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


def extract_product_page_image(soup, product_url):
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

    def priority(img):
        marker = " ".join([
            clean_text(img.get("alt", "")),
            clean_text(" ".join(img.get("class", []))),
            clean_text(img.get("id", "")),
        ]).lower()
        return 0 if any(t in marker for t in markers) else 1

    for img in sorted(soup.find_all("img"), key=priority):
        url = img_url(img, product_url)
        if url:
            return url
    return ""


_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image)["\'][^>]*>', re.I)
_CONTENT_RE = re.compile(r'content=["\']([^"\']+)["\']', re.I)


def fetch_product_image(product_url):
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
            html = resp.text
            # rýchla cesta: og:image regexom, bez parsovania celej stránky
            m = _OG_IMAGE_RE.search(html)
            if m:
                c = _CONTENT_RE.search(m.group(0))
                if c and not c.group(1).startswith("data:image/"):
                    image_url = absolute_url(product_url, c.group(1))
            if not image_url:
                soup = BeautifulSoup(html, HTML_PARSER)
                image_url = extract_product_page_image(soup, product_url)
    except Exception:
        pass
    image_cache.set(product_url, image_url)
    if image_url:
        catalog_set_image(product_url, image_url)
    return image_url


def catalog_set_image(link, image_url):
    with _catalog_lock:
        for s in SUGGESTION_CATALOG.values():
            if s.get("link") == link and not s.get("image"):
                s["image"] = image_url


def fill_images_from_cache(results):
    for r in results:
        if not clean_text(r.get("image", "")) and r.get("link"):
            cached = image_cache.get(r["link"])
            if cached:
                r["image"] = cached
    return results


def find_product_block(anchor):
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
# SHOP SEARCH
# =========================================================

def _log(debug, **entry):
    if len(debug["sample_decisions"]) < 20:
        debug["sample_decisions"].append(entry)


CACHEABLE_STATUSES = ("ok", "no_results")


def _scrape(shop, query, timeout):
    start = time.monotonic()
    results = []
    debug = {
        "shop": shop["name"], "query": query, "url": "", "status": "starting",
        "http_status": None, "results": 0, "links_scanned": 0, "unique_links": 0,
        "price_found": 0, "merch_filtered": 0, "match_filtered": 0,
        "language_filtered": 0, "accepted": 0, "images_found": 0,
        "images_missing": 0, "elapsed_ms": 0, "cache": "miss", "error": "",
        "sample_decisions": [],
    }

    try:
        url = shop["search_url"].format(q=urllib.parse.quote(query))
        debug["url"] = url
        response, http_debug = fetch(url, timeout=timeout)
        debug["http_status"] = http_debug.get("http_status")

        if not response:
            debug["status"] = http_debug.get("status", "http_error")
            debug["error"] = http_debug.get("error", "")
            debug["elapsed_ms"] = round((time.monotonic() - start) * 1000)
            return results, debug

        soup = BeautifulSoup(response.text, HTML_PARSER)
        links = soup.select(shop["link_selector"])
        debug["links_scanned"] = len(links)

        parsed = normalize_query(query)
        kind = classify_query(parsed)
        foreign_ok = FOREIGN_QUERY_RE.search(query) is not None
        seen = set()

        for anchor in links:
            href = absolute_url(shop["base_url"], anchor.get("href"))
            if not href:
                continue
            key = href.lower().rstrip("/")
            if key in seen:
                continue
            seen.add(key)

            title = extract_title(anchor)
            if not title:
                _log(debug, title="", decision="filtered", reason="no_title")
                continue

            # lacné filtre najprv (pred hľadaním ceny v DOM)
            why = merch_reason(title)
            if why:
                debug["merch_filtered"] += 1
                _log(debug, title=title, decision="filtered", reason=why)
                continue

            lang = detect_language(title)
            if lang and not foreign_ok:
                debug["language_filtered"] += 1
                _log(debug, title=title, decision="filtered", reason="language_" + lang)
                continue

            block_text = find_product_block(anchor)

            if kind == "card":
                matched, reason = card_matches_query(
                    title, block_text, parsed, loose_set=shop.get("loose_set", False))
            else:
                matched, reason = sealed_matches_query(title, block_text, parsed)
            if not matched:
                debug["match_filtered"] += 1
                _log(debug, title=title, decision="filtered", reason=reason)
                continue

            price = parse_price(block_text)
            if price is None and anchor.parent:
                price = parse_price(anchor.parent.get_text(" ", strip=True))
            if price is None:
                _log(debug, title=title, decision="filtered", reason="no_price")
                continue
            if price <= 0:
                _log(debug, title=title, decision="filtered", reason="zero_price")
                continue
            debug["price_found"] += 1

            image_url = extract_image(anchor, shop["base_url"])
            packs = estimate_packs(title)
            results.append({
                "title": title, "shop": shop["name"], "country": shop["country"],
                "condition": "Nové", "language": lang, "price_eur": round(price, 2),
                "link": href, "image": image_url,
                "stock": detect_stock(block_text),
                "packs": packs,
                "price_per_pack": round(price / packs, 2) if packs and packs > 1 else None,
            })
            debug["accepted"] += 1
            _log(debug, title=title, price_eur=round(price, 2),
                 image=bool(image_url), decision="accepted", reason=reason)

        debug["unique_links"] = len(seen)
        fill_images_from_cache(results)
        debug["images_found"] = sum(1 for r in results if clean_text(r.get("image", "")))
        debug["images_missing"] = len(results) - debug["images_found"]

        results.sort(key=lambda r: float(r.get("price_eur", 999999)))
        debug["results"] = len(results)
        debug["status"] = "ok" if results else "no_results"

    except Exception as e:
        debug["status"] = "parser_error"
        debug["error"] = str(e)

    debug["elapsed_ms"] = round((time.monotonic() - start) * 1000)
    return results, debug


def _from_cache(cache_key):
    cached = search_cache.get(cache_key)
    if cached is None:
        return None
    cached["debug"]["cache"] = "hit"
    fill_images_from_cache(cached["results"])
    return cached["results"], cached["debug"]


def shop_search(shop, query, return_debug=False, cache_result=True, timeout=None):
    start = time.monotonic()
    cache_key = shop["name"].lower() + "|" + clean_text(query).lower()
    timeout = timeout or SEARCH_TIMEOUT

    def finish(results, debug):
        debug["elapsed_ms"] = round((time.monotonic() - start) * 1000)
        add_suggestions_from_results(results)
        return (results, debug) if return_debug else results

    hit = _from_cache(cache_key)
    if hit:
        return finish(*hit)

    # single-flight: ak to isté práve hľadá iné vlákno, počkáme na jeho výsledok
    owner = False
    if cache_result:
        with _inflight_lock:
            event = _inflight.get(cache_key)
            if event is None:
                event = _inflight[cache_key] = threading.Event()
                owner = True
        if not owner:
            event.wait(timeout + 3)
            hit = _from_cache(cache_key)
            if hit:
                return finish(*hit)

    try:
        results, debug = _scrape(shop, query, timeout)
        if cache_result and debug["status"] in CACHEABLE_STATUSES:
            search_cache.set(cache_key, {"results": results, "debug": debug})
    finally:
        if owner:
            with _inflight_lock:
                _inflight.pop(cache_key, None)
            event.set()

    return finish(results, debug)


# =========================================================
# SEARCH ALL
# =========================================================

def search_all(query, return_debug=False):
    diagnostics, results = [], []

    def run(shop):
        start = time.monotonic()
        try:
            res, dbg = shop_search(shop, query, return_debug=True, cache_result=True)
        except Exception as e:
            res, dbg = [], {"shop": shop["name"], "query": query,
                            "status": "runner_error", "results": 0, "error": str(e)}
        dbg["elapsed_ms"] = round((time.monotonic() - start) * 1000)
        return res, dbg

    futures = [SHOP_EXECUTOR.submit(run, s) for s in ACTIVE_SHOPS]
    for shop, fut in zip(ACTIVE_SHOPS, futures):
        try:
            res, dbg = fut.result(timeout=SEARCH_TIMEOUT + 6)
        except Exception as e:
            res, dbg = [], {"shop": shop["name"], "query": query,
                            "status": "timeout", "results": 0, "error": str(e)}
        results.extend(res)
        diagnostics.append(dbg)

    unique = {}
    for r in results:
        key = (clean_text(r.get("shop", "")).lower(),
               clean_text(r.get("link", "")).lower().rstrip("/"))
        unique[key] = r
    results = sorted(unique.values(), key=lambda r: float(r.get("price_eur", 999999)))

    if return_debug:
        return results, {"query": query, "shops": diagnostics, "total_results": len(results)}
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


def shops_status(debug):
    return [{
        "name": d.get("shop", ""),
        "status": d.get("status", ""),
        "ok": d.get("status") in CACHEABLE_STATUSES,
        "results": d.get("results", 0),
        "elapsed_ms": d.get("elapsed_ms", 0),
        "cache": d.get("cache", ""),
    } for d in debug.get("shops", [])]


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
        "subtitle": "Reálna karta z obchodu",
        "type": "card" if is_card else "product",
        "type_label": "Karta" if is_card else "Produkt",
        "image": "", "price_eur": None, "price": None, "link": "",
    }


def suggestions_from_results(results):
    out = {}
    score = lambda x: bool(x["image"]) + (x["price_eur"] is not None)
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
            SUGGESTION_CATALOG.pop(key, None)
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


def _shopify_img(url):
    """Menší náhľad zo Shopify CDN (rýchlejšie načítanie)."""
    if url and ("/cdn/shop/" in url or "cdn.shopify.com" in url) and "width=" not in url:
        url += ("&" if "?" in url else "?") + "width=160"
    return url


def shopify_suggest(shop, q, timeout=SUGGESTION_TIMEOUT):
    """Shopify predictive search. Vráti zoznam výsledkov alebo None pri chybe."""
    cache_key = "shopify|" + shop["name"].lower() + "|" + q.lower()
    cached = suggestion_cache.get(cache_key)
    if cached is not None:
        return cached
    url = (shop["base_url"].rstrip("/") + "/search/suggest.json?q=" + urllib.parse.quote(q)
           + "&resources[type]=product&resources[limit]=10"
           + "&resources[options][unavailable_products]=last")
    resp, _ = fetch(url, timeout=timeout)
    if not resp:
        return None
    try:
        products = resp.json()["resources"]["results"]["products"]
    except Exception:
        return None

    foreign_ok = FOREIGN_QUERY_RE.search(q) is not None
    out = []
    for p in products or []:
        title = clean_text(p.get("title", ""))
        if not title or is_merch(title):
            continue
        if detect_language(title) and not foreign_ok:
            continue
        link = absolute_url(shop["base_url"], p.get("url", ""))
        if link:
            link = link.split("?")[0]  # bez ?_pos=...&_sid=...
        image = p.get("image") or ""
        if not image and isinstance(p.get("featured_image"), dict):
            image = p["featured_image"].get("url", "")
        image = _shopify_img(absolute_url(shop["base_url"], image)) if image else ""
        raw = p.get("price", p.get("price_min"))
        price = _to_float(str(raw)) if raw not in (None, "") else None
        out.append({
            "title": title, "shop": shop["name"], "country": shop["country"],
            "price_eur": round(price, 2) if price else None,
            "link": link, "image": image,
        })
    suggestion_cache.set(cache_key, out)
    if out:
        add_suggestions_from_results(out)
    return out


def remote_suggestion_results(q):
    shop = ACTIVE_SHOPS[0]
    if shop.get("shopify"):
        res = shopify_suggest(shop, q)
        if res is not None:
            return res
    return shop_search(shop, q, return_debug=False,
                       cache_result=True, timeout=SUGGESTION_TIMEOUT)


@app.get("/api/suggestions")
def api_suggestions():
    q = clean_text(request.args.get("q", ""))
    if len(q) < 2:
        return jsonify({"query": q, "normalized_query": "", "suggestions": []})
    if not suggest_limiter.allow(client_ip()):
        return too_many()

    normalized = normalize_query(q).get("normalized", "")

    cached = suggestion_cache.get(q.lower())
    if cached is not None:
        fill_images_from_cache(cached)
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
    if len(candidates) < MIN_LOCAL_SUGGESTIONS and ACTIVE_SHOPS:
        remote_used = True
        remote = remote_suggestion_results(q)
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

    for item in output:
        if not item.get("image") and item.get("link"):
            cached_img = image_cache.get(item["link"])
            if cached_img:
                item["image"] = cached_img

    suggestion_cache.set(q.lower(), output)
    return jsonify({"query": q, "normalized_query": normalized,
                    "suggestions": output,
                    "source": "remote" if remote_used else "catalog"})


# =========================================================
# API
# =========================================================

@app.get("/api/config")
def api_config():
    resp = jsonify({
        "version": VERSION,
        "czk_per_eur": CZK_PER_EUR,
        "alerts_enabled": ALERTS_ENABLED,
        "shops": [{"name": s["name"], "country": s["country"], "url": s["base_url"]}
                  for s in ACTIVE_SHOPS],
    })
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@app.get("/api/parse")
def api_parse():
    parsed = normalize_query(request.args.get("q", ""))
    parsed["type"] = classify_query(parsed)
    return jsonify(parsed)


@app.get("/api/search")
def api_search():
    original = clean_text(request.args.get("q", ""))[:150]
    if not original:
        return jsonify({"error": "Zadaj, čo chceš hľadať."}), 400
    if not search_limiter.allow(client_ip()):
        return too_many()

    parsed = normalize_query(original)
    normalized = parsed.get("normalized", original)
    results, debug = search_all(normalized, return_debug=True)
    add_trends(results)
    save_history(original, results)

    info = {"title": normalized, "subtitle": "", "image": ""}
    if parsed.get("set_name"):
        info["subtitle"] = "Set: " + parsed["set_name"]
    elif parsed.get("pokemon"):
        info["subtitle"] = "Pokémon: " + parsed["pokemon"]

    payload = {
        "query": original, "normalized_query": normalized, "parsed": parsed,
        "results": results, "summary": build_summary(results),
        "czk_per_eur": CZK_PER_EUR, "info": info, "shops": shops_status(debug),
    }
    if is_admin():
        payload["debug"] = debug
    return jsonify(payload)


@app.post("/api/images")
def api_images():
    """Dotiahne obrázky z produktových stránok: {"links": [...]} -> {"images": {link: url}}"""
    if not images_limiter.allow(client_ip()):
        return too_many()
    data = request.get_json(silent=True) or {}
    raw = data.get("links") or []
    if not isinstance(raw, list):
        return jsonify({"error": "links musí byť zoznam"}), 400

    links = list(dict.fromkeys(
        clean_text(l) for l in raw[:IMAGE_BATCH_MAX]
        if isinstance(l, str) and is_allowed_link(clean_text(l))
    ))

    out, pending = {}, {}
    for link in links:
        cached = image_cache.get(link)
        if cached is not None:
            out[link] = cached
        else:
            pending[IMAGE_EXECUTOR.submit(fetch_product_image, link)] = link

    if pending:
        try:
            for fut in as_completed(pending, timeout=IMAGE_BATCH_BUDGET):
                try:
                    out[pending[fut]] = fut.result()
                except Exception:
                    out[pending[fut]] = ""
        except Exception:
            pass  # časový limit; zvyšok dobehne na pozadí do image_cache

    return jsonify({"images": out})


@app.get("/api/debug/search")
def api_debug_search():
    if not debug_allowed():
        return jsonify({"error": "Nepovolené."}), 403
    original = clean_text(request.args.get("q", ""))
    if not original:
        return jsonify({"error": "Chýba vyhľadávanie."}), 400
    parsed = normalize_query(original)
    normalized = parsed.get("normalized", original)
    results, debug = search_all(normalized, return_debug=True)
    return jsonify({"query": original, "normalized_query": normalized,
                    "parsed": parsed, "debug": debug, "results": results})


@app.get("/api/debug/shop")
def api_debug_shop():
    """Test jedného obchodu (aj vypnutého): /api/debug/shop?name=CardyX&q=pikachu"""
    if not debug_allowed():
        return jsonify({"error": "Nepovolené."}), 403
    name = clean_text(request.args.get("name", "")).lower()
    q = clean_text(request.args.get("q", ""))
    shop = next((x for x in SHOPS if x["name"].lower() == name), None)
    if not shop or not q:
        return jsonify({"error": "Zadaj ?name=<obchod>&q=<hľadaný text>",
                        "shops": [x["name"] for x in SHOPS]}), 400
    normalized = normalize_query(q).get("normalized", q)
    results, debug = shop_search(shop, normalized, return_debug=True, cache_result=False)
    return jsonify({"shop": shop["name"], "normalized_query": normalized,
                    "debug": debug, "results": results})


@app.get("/api/debug/cache")
def api_debug_cache():
    if not debug_allowed():
        return jsonify({"error": "Nepovolené."}), 403
    s_items, s_active = search_cache.stats()
    sg_items, _ = suggestion_cache.stats()
    i_items, i_active = image_cache.stats()
    with _catalog_lock:
        catalog_items = len(SUGGESTION_CATALOG)
    return jsonify({
        "status": "ok", "version": VERSION, "html_parser": HTML_PARSER,
        "cache_items": s_items, "active": s_active, "expired": s_items - s_active,
        "suggestion_cache_items": sg_items,
        "suggestion_catalog_items": catalog_items,
        "image_cache_items": i_items, "image_cache_active": i_active,
    })


@app.get("/api/debug/cache/clear")
def api_debug_cache_clear():
    if not is_admin():
        return jsonify({"error": "Nepovolené."}), 403
    search_cache.clear()
    suggestion_cache.clear()
    image_cache.clear()
    with _catalog_lock:
        SUGGESTION_CATALOG.clear()
    return jsonify({"status": "ok", "message": "Cache a katalóg vymazané."})


@app.get("/health")
def health():
    with _catalog_lock:
        catalog_items = len(SUGGESTION_CATALOG)
    return jsonify({
        "service": "CardRadar", "status": "ok", "version": VERSION,
        "index_exists": bool(find_index()), "html_parser": HTML_PARSER,
        "active_shops": [f"{x['name']} ({x['country']})" for x in ACTIVE_SHOPS],
        "suggestion_catalog_items": catalog_items,
        "image_cache_items": image_cache.stats()[0],
        "alerts_enabled": ALERTS_ENABLED,
    })


@app.get("/")
def home():
    try:
        html = load_index()
    except Exception as e:
        return Response(f"<h1>CardRadar</h1><p>Chyba pri načítaní stránky.</p><pre>{e}</pre>",
                        status=500, mimetype="text/html")
    if html is None:
        return Response("<h1>CardRadar</h1><p>index.html nebol nájdený.</p>",
                        status=500, mimetype="text/html")
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# =========================================================
# HISTÓRIA CIEN + OBĽÚBENÉ
# =========================================================

@app.get("/api/history")
def api_history():
    """Denné ceny jedného produktu: /api/history?link=..."""
    if not history_limiter.allow(client_ip()):
        return too_many()
    link = clean_text(request.args.get("link", ""))
    if not is_allowed_link(link):
        return jsonify({"error": "Neplatný odkaz."}), 400
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT day, price_eur, stock, title, shop FROM price_daily "
            "WHERE link = ? ORDER BY day ASC", (link,)).fetchall()
    finally:
        conn.close()
    points = [{"day": d, "price_eur": round(p, 2), "stock": s or ""} for d, p, s, _, _ in rows if p]
    title = rows[-1][3] if rows else ""
    shop = rows[-1][4] if rows else ""
    return jsonify({"link": link, "title": title, "shop": shop, "points": points})


@app.post("/api/latest")
def api_latest():
    """Posledná známa cena pre zoznam odkazov (obľúbené): {"links": [...]}"""
    if not history_limiter.allow(client_ip()):
        return too_many()
    data = request.get_json(silent=True) or {}
    links = [clean_text(l) for l in (data.get("links") or [])[:100]
             if isinstance(l, str) and is_allowed_link(clean_text(l))]
    out = {}
    if links:
        conn = db_connect()
        try:
            marks = ",".join("?" * len(links))
            rows = conn.execute(f"""
                SELECT p.link, p.day, p.price_eur, p.stock FROM price_daily p
                JOIN (SELECT link, MAX(day) AS d FROM price_daily
                      WHERE link IN ({marks}) GROUP BY link) m
                  ON p.link = m.link AND p.day = m.d
            """, links).fetchall()
        finally:
            conn.close()
        for link, day, price, stock in rows:
            out[link] = {"day": day, "price_eur": round(price, 2) if price else None,
                         "stock": stock or ""}
    return jsonify({"latest": out})


# =========================================================
# STRÁŽCA CENY
# =========================================================

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,24}$", re.I)


def send_mail(to, subject, text):
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        server.starttls()
    try:
        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    finally:
        server.quit()


def site_url():
    return PUBLIC_URL or request.url_root.rstrip("/")


def _walk_json(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_json(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_json(v)


def fetch_product_offer(link):
    """Aktuálna cena a sklad z produktovej stránky (JSON-LD / meta značky)."""
    resp, _ = fetch(link, timeout=10)
    if not resp:
        return None, ""
    html = resp.text
    soup = BeautifulSoup(html, HTML_PARSER)
    price, currency, stock = None, "EUR", ""

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for node in _walk_json(data):
            if "price" in node or "lowPrice" in node:
                raw = node.get("price", node.get("lowPrice"))
                v = _to_float(str(raw)) if raw not in (None, "") else None
                if v:
                    price = v
                    currency = str(node.get("priceCurrency", "EUR")).upper()
                    avail = str(node.get("availability", "")).lower()
                    if "outofstock" in avail or "soldout" in avail:
                        stock = "out"
                    elif "preorder" in avail:
                        stock = "preorder"
                    elif "instock" in avail:
                        stock = "in"
                    break
        if price:
            break

    if not price:
        for sel in ('meta[property="product:price:amount"]', 'meta[property="og:price:amount"]',
                    '[itemprop="price"]'):
            el = soup.select_one(sel)
            if el:
                v = _to_float(clean_text(el.get("content") or el.get_text()))
                if v:
                    price = v
                    cur = soup.select_one('meta[property="product:price:currency"], '
                                          'meta[property="og:price:currency"], [itemprop="priceCurrency"]')
                    if cur:
                        currency = clean_text(cur.get("content") or cur.get_text()).upper() or "EUR"
                    break

    if price and currency in ("CZK", "KČ"):
        price = price / CZK_PER_EUR
    if not stock:
        stock = detect_stock(soup.get_text(" ", strip=True)[:20000])
    return (round(price, 2) if price else None), stock


@app.post("/api/alerts")
def api_alerts_create():
    if not ALERTS_ENABLED:
        return jsonify({"error": "Strážca ceny zatiaľ nie je na serveri zapnutý."}), 503
    if not alerts_limiter.allow(client_ip()):
        return too_many()
    data = request.get_json(silent=True) or {}
    email = clean_text(data.get("email", "")).lower()
    link = clean_text(data.get("link", ""))
    title = clean_text(data.get("title", ""))[:200]
    shop = clean_text(data.get("shop", ""))[:60]
    try:
        target = round(float(data.get("target")), 2)
    except (TypeError, ValueError):
        target = 0
    if not EMAIL_RE.match(email):
        return jsonify({"error": "Zadaj platný e-mail."}), 400
    if not is_allowed_link(link):
        return jsonify({"error": "Neplatný produkt."}), 400
    if not 0 < target < 100000:
        return jsonify({"error": "Zadaj cieľovú cenu."}), 400

    conn = db_connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM alerts WHERE email = ?", (email,)).fetchone()[0]
        if count >= ALERTS_PER_EMAIL:
            return jsonify({"error": f"Na jeden e-mail môžeš mať najviac {ALERTS_PER_EMAIL} strážcov."}), 400
        existing = conn.execute(
            "SELECT id, confirmed FROM alerts WHERE email = ? AND link = ?", (email, link)).fetchone()
        token = secrets.token_urlsafe(24)
        site = site_url()
        if existing:
            conn.execute("UPDATE alerts SET target = ?, notified = NULL, token = ? WHERE id = ?",
                         (target, token, existing[0]))
            confirmed = bool(existing[1])
        else:
            conn.execute(
                "INSERT INTO alerts (email, link, title, shop, target, token, confirmed, created, site) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (email, link, title, shop, target, token,
                 datetime.now(timezone.utc).isoformat(), site))
            confirmed = False
        conn.commit()
    finally:
        conn.close()

    if confirmed:
        return jsonify({"status": "ok", "message": f"Strážca upravený na {target:.2f} €."})

    try:
        send_mail(email, "Potvrď strážcu ceny – CardRadar",
                  f"Ahoj,\n\nchceš dostať e-mail, keď cena klesne na {target:.2f} € alebo menej?\n\n"
                  f"{title} ({shop})\n{link}\n\n"
                  f"Potvrď kliknutím: {site}/alerts/confirm?token={token}\n\n"
                  f"Ak si o to nežiadal, tento e-mail ignoruj.\n\nCardRadar")
    except Exception:
        return jsonify({"error": "Potvrdzovací e-mail sa nepodarilo odoslať. Skús to neskôr."}), 502
    return jsonify({"status": "ok",
                    "message": "Poslali sme ti e-mail. Strážca začne fungovať po potvrdení."})


def _simple_page(title, text):
    home = PUBLIC_URL or "/"
    return Response(
        f"""<!doctype html><html lang="sk"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<body style="margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f172a;color:#f8fafc;font-family:-apple-system,Segoe UI,sans-serif;padding:20px">
<div style="max-width:420px;text-align:center"><h1 style="color:#facc15">{title}</h1>
<p style="color:#94a3b8;line-height:1.5">{text}</p>
<a href="{home}" style="display:inline-block;margin-top:10px;background:#facc15;color:#111827;
padding:10px 16px;border-radius:8px;font-weight:800;text-decoration:none">Späť na CardRadar</a></div>""",
        mimetype="text/html")


@app.get("/alerts/confirm")
def alerts_confirm():
    token = clean_text(request.args.get("token", ""))
    conn = db_connect()
    try:
        row = conn.execute("SELECT id, target, title FROM alerts WHERE token = ?", (token,)).fetchone()
        if row:
            conn.execute("UPDATE alerts SET confirmed = 1 WHERE id = ?", (row[0],))
            conn.commit()
    finally:
        conn.close()
    if not row:
        return _simple_page("Odkaz neplatí", "Tento strážca už neexistuje alebo bol odkaz zmenený.")
    stop = f"{PUBLIC_URL or request.url_root.rstrip('/')}/alerts/stop?token={token}"
    return _simple_page("Strážca je zapnutý 🔔",
                        f"Napíšeme ti, keď {row[2] or 'produkt'} klesne na {row[1]:.2f} € alebo menej."
                        f"<br><br><a href='{stop}' style='color:#94a3b8'>Zrušiť strážcu</a>")


@app.get("/alerts/stop")
def alerts_stop():
    token = clean_text(request.args.get("token", ""))
    conn = db_connect()
    try:
        cur = conn.execute("DELETE FROM alerts WHERE token = ?", (token,))
        conn.commit()
        deleted = cur.rowcount
    finally:
        conn.close()
    if not deleted:
        return _simple_page("Hotovo", "Tento strážca už bol zrušený.")
    return _simple_page("Strážca zrušený", "Viac ti o tomto produkte písať nebudeme.")


def check_alerts_once():
    conn = db_connect()
    try:
        alerts = conn.execute(
            "SELECT id, email, link, title, shop, target, token, site FROM alerts "
            "WHERE confirmed = 1 AND notified IS NULL").fetchall()
    finally:
        conn.close()

    offers = {}
    for link in {a[2] for a in alerts}:
        try:
            offers[link] = fetch_product_offer(link)
        except Exception:
            offers[link] = (None, "")
        time.sleep(1)  # šetrne k obchodom

    now = datetime.now(timezone.utc).isoformat()
    conn = db_connect()
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
                """, (link, today_str(), shop, title, price, stock))
            if price and price <= target and stock != "out":
                try:
                    send_mail(email, f"Cena klesla: {title} za {price:.2f} €",
                              f"Ahoj,\n\n{title} ({shop}) je teraz za {price:.2f} € "
                              f"(tvoj cieľ bol {target:.2f} €).\n\n{link}\n\n"
                              f"Strážca sa tým vypína. Nový si nastavíš na {site}\n"
                              f"Zrušiť: {site}/alerts/stop?token={token}\n\nCardRadar")
                    conn.execute("UPDATE alerts SET notified = ? WHERE id = ?", (now, aid))
                except Exception:
                    pass
        conn.commit()
    finally:
        conn.close()


_alert_lock_file = None


def _alerts_loop():
    time.sleep(60)
    while True:
        try:
            check_alerts_once()
        except Exception:
            pass
        time.sleep(max(0.25, ALERT_CHECK_HOURS) * 3600)


def start_alert_worker():
    """Spustí strážcu na pozadí – len v jednom procese (zámok súboru)."""
    global _alert_lock_file
    if not ALERTS_ENABLED:
        return
    if fcntl is not None:
        try:
            _alert_lock_file = open(os.path.join(BASE_DIR, ".alerts.lock"), "w")
            fcntl.flock(_alert_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # iný proces už strážcu spúšťa
    threading.Thread(target=_alerts_loop, daemon=True, name="alerts").start()


start_alert_worker()


# =========================================================
# PRIDANIE OBCHODU: automatické rozpoznanie platformy
# =========================================================

PLATFORM_PRESETS = {
    "shoptet": {
        "marker": re.compile(r"shoptet", re.I),
        "search": ["/vyhladavanie/?string={q}", "/vyhledavani/?string={q}"],
        "selector": "div.product a.name",
    },
    "shopify": {
        "marker": re.compile(r"cdn\.shopify\.com|Shopify\.theme|/cdn/shop/", re.I),
        "search": ["/search?q={q}&type=product", "/search?q={q}"],
        "selector": 'a[href*="/products/"]',
    },
    "upgates": {
        "marker": re.compile(r"upgates", re.I),
        "search": ["/vyhledavani?q={q}", "/vyhladavanie?q={q}", "/search?q={q}", "/hledani?q={q}"],
        "selector": 'a[href*="/p/"]',
    },
    "woocommerce": {
        "marker": re.compile(r"woocommerce", re.I),
        "search": ["/?s={q}&post_type=product"],
        "selector": "li.product a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link",
    },
}


@app.get("/api/debug/detect")
def api_debug_detect():
    """/api/debug/detect?url=https://www.obchod.cz&q=pikachu -> návrh konfigurácie"""
    if not debug_allowed():
        return jsonify({"error": "Nepovolené."}), 403
    url = clean_text(request.args.get("url", ""))
    q = clean_text(request.args.get("q", "")) or "pikachu"
    if not re.match(r"^https?://[^/\s]+", url):
        return jsonify({"error": "Zadaj ?url=https://www.obchod.sk"}), 400
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}/"
    resp, dbg = fetch(base, timeout=10)
    if not resp:
        return jsonify({"error": "Stránka neodpovedá.", "debug": dbg}), 502

    platform = next((name for name, p in PLATFORM_PRESETS.items()
                     if p["marker"].search(resp.text)), None)
    if not platform:
        return jsonify({"base_url": base, "platform": None,
                        "message": "Platformu sa nepodarilo rozpoznať. Vyhľadávaciu URL a selektor "
                                   "treba zistiť ručne (vyhľadaj na webe a skopíruj adresu)."})

    preset = PLATFORM_PRESETS[platform]
    host = parsed.netloc.lower()
    ALLOWED_HOSTS.add(host)  # aby test prešiel cez kontrolu odkazov
    tried = []
    for path in preset["search"]:
        shop = {"name": host, "country": "CZ" if host.endswith(".cz") else "SK",
                "base_url": base, "search_url": base.rstrip("/") + path,
                "link_selector": preset["selector"]}
        results, debug = shop_search(shop, normalize_query(q)["normalized"] or q,
                                     return_debug=True, cache_result=False)
        tried.append({"search_url": shop["search_url"], "status": debug["status"],
                      "links": debug.get("links_scanned", 0), "results": len(results)})
        if debug.get("links_scanned"):
            config = {"name": parsed.netloc.replace("www.", ""), "country": shop["country"],
                      "enabled": True, "base_url": base, "search_url": shop["search_url"],
                      "link_selector": preset["selector"]}
            if platform == "shopify":
                config["shopify"] = True
            return jsonify({"platform": platform, "tried": tried, "config": config,
                            "sample": results[:5],
                            "message": "Funguje. Skopíruj 'config' do zoznamu SHOPS v app.py."})
    return jsonify({"platform": platform, "tried": tried,
                    "message": "Platforma rozpoznaná, ale vyhľadávanie nevrátilo produkty. "
                               "Over vyhľadávaciu URL ručne."})


# =========================================================
# GZIP + BEZPEČNOSTNÉ HLAVIČKY
# =========================================================

@app.after_request
def finalize(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")

    if (resp.status_code != 200 or resp.direct_passthrough
            or "Content-Encoding" in resp.headers
            or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
            or resp.mimetype not in ("application/json", "text/html")):
        return resp
    data = resp.get_data()
    if len(data) < 1024:
        return resp
    resp.set_data(gzip.compress(data, compresslevel=5))
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers.add("Vary", "Accept-Encoding")
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), threaded=True)
