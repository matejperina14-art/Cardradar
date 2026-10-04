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
# CARD RADAR 6.0
# Novinky 6.0:
#  - "group" pri každej ponuke: rovnaký produkt v rôznych obchodoch sa spojí
#  - /api/home: zľavy dňa, najlacnejšie ETB, obľúbené hľadania, nové sety
#  - PWA (manifest, service worker, ikony v priečinku static/)
# Novinky 5.32: jazyk produktu (JP, KR, CN, EN, DE, FR...) – nič sa neskrýva, filtruje frontend
# Novinky 5.31: tvrdý filter merchu (aj s "TCG" v názve) + pozitívna kontrola, že ide o kartu/TCG produkt
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

VERSION = "6.0.3"
app = Flask(__name__, static_folder=None)  # /static obsluhuje funkcia static_files nižšie
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

# Nové sety na úvodnej stránke – NAJNOVŠÍ HORE. Uprav, keď vyjde nový set.
NEW_SETS = [
    {"name": "Delta Reign", "query": "delta reign"},
    {"name": "30th Celebration", "query": "30th celebration"},
    {"name": "Ascended Heroes", "query": "ascended heroes"},
    {"name": "Phantasmal Flames", "query": "phantasmal flames"},
    {"name": "Mega Evolution", "query": "mega evolution"},
    {"name": "Black Bolt", "query": "black bolt"},
    {"name": "White Flare", "query": "white flare"},
    {"name": "Destined Rivals", "query": "destined rivals"},
    {"name": "Journey Together", "query": "journey together"},
    {"name": "Prismatic Evolutions", "query": "prismatic evolutions"},
]
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(price_daily)")}
    if "image" not in cols:
        conn.execute("ALTER TABLE price_daily ADD COLUMN image TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pd_day ON price_daily(day)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_log (
            day TEXT NOT NULL, query TEXT NOT NULL, n INTEGER DEFAULT 1,
            PRIMARY KEY (day, query)
        )
    """)
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
             r.get("price_eur"), r.get("stock", ""), r.get("image", ""))
            for r in results if r.get("link") and r.get("price_eur")
        ]
        conn.executemany("""
            INSERT INTO price_daily (link, day, shop, title, price_eur, stock, image)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(link, day) DO UPDATE SET
                price_eur = excluded.price_eur, stock = excluded.stock,
                title = excluded.title,
                image = COALESCE(NULLIF(excluded.image, ''), price_daily.image)
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
        BG_EXECUTOR.submit(_log_search, clean_text(query).lower()[:80])


def _log_search(query):
    if not query:
        return
    conn = None
    try:
        conn = db_connect()
        conn.execute("""INSERT INTO search_log (day, query, n) VALUES (?, ?, 1)
                        ON CONFLICT(day, query) DO UPDATE SET n = n + 1""", (today_str(), query))
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


def _store_image(link, image_url):
    conn = None
    try:
        conn = db_connect()
        conn.execute("UPDATE price_daily SET image = ? WHERE link = ? AND (image IS NULL OR image = '')",
                     (image_url, link))
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()


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
        "ascended heroes", "perfect order", "black bolt", "white flare",
        "delta reign", "30th celebration", "30th anniversary celebrations",
        "paldean fates", "shrouded fable", "paldea evolved",
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

_NUM = (r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?(?!\d)"        # 1.099,00 / 1.099
        r"|\d{1,3}(?:,\d{3})+\.\d{1,2}(?!\d)"                 # 1,099.00
        r"|\d{1,3}(?:[ ]\d{3})+(?:[.,]\d{1,2})?"              # 1 099,00
        r"|\d{1,8}(?:[.,]\d{1,2})?)")
_EX_VAT = re.compile(
    r"(?:€\s*" + _NUM + r"|" + _NUM + r"\s*(?:€|Kč|CZK))\s*(?:bez\s+DPH|excl\.?\s*VAT)",
    re.I,
)
_EUR_RES = [re.compile(r"€\s*" + _NUM), re.compile(_NUM + r"\s*€")]
_CZK_RES = [re.compile(_NUM + r"\s*(?:Kč|CZK)", re.I), re.compile(r"(?:Kč|CZK)\s*" + _NUM, re.I)]


def _to_float(value):
    value = value.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", value):   # 1.099 = tisíc, nie desatinné
        value = value.replace(".", "")
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

# Jazyk sa zisťuje z názvu: celé slová (bez ohľadu na veľkosť písmen)
# a skratky (len VEĽKÝMI písmenami, aby "de" v texte nebolo nemčina).
_LANG_DEFS = [
    ("JP", r"japon\w*|japan\w*|japonsk\w*", r"JP|JPN|JAP"),
    ("KR", r"k[óo]rej\w*|korean\w*", r"KR|KOR"),
    ("TW", r"traditional\s+chinese|t-?chinese|tradičn\w*\s+[čc][íi]n\w*|taiwan\w*", r"TW|T-?CN"),
    ("CN", r"[čc][ií]nsk\w*|[čc][ií]n[šs]t\w*|chinese|simplified\s+chinese|s-?chinese", r"CN|CHN|S-?CN"),
    ("ID", r"indon[ée]z\w*|indonesian\w*", r"IDN|INDO"),
    ("TH", r"thajsk\w*|thai", r"TH|THA"),
    ("DE", r"nem[ec]ck\w*|n[ěe]meck\w*|german\w*|deutsch\w*", r"DE|GER|DEU"),
    ("FR", r"franc[úu]zsk\w*|francouzsk\w*|french|fran[çc]ais\w*", r"FR|FRA"),
    ("IT", r"talian\w*|italsk\w*|italian\w*|italiano", r"IT|ITA"),
    ("ES", r"[šs]paniel\w*|[šs]pan[ěe]l\w*|spanish|espa[ñn]ol\w*", r"ES|ESP|SPA"),
    ("PT", r"portugal\w*|portugues\w*", r"PT|POR"),
    ("NL", r"holandsk\w*|nizozemsk\w*|dutch|nederlands\w*", r"NL|NLD"),
    ("PL", r"po[ľl]sk\w*|polish|polski", r"PL|POL"),
    ("EN", r"anglick\w*|english|angli[čc]tin\w*", r"EN|ENG|UK"),
]
LANG_PATTERNS = [
    (code, re.compile(r"(?<!\w)(?:" + words + r")(?!\w)", re.I),
     re.compile(r"(?<![A-Za-z0-9])(?:" + codes + r")(?![A-Za-z0-9])"))
    for code, words, codes in _LANG_DEFS
]
ASIAN_LANGS = {"JP", "KR", "CN", "TW", "ID", "TH"}
FOREIGN_QUERY_RE = re.compile(
    r"japon|japan|jpn|k[óo]rej|korean|[čc][ií]nsk|chinese|indon|thai|thajsk", re.I)


def detect_language(title):
    """Kód jazyka produktu ('JP', 'EN', 'DE'...) alebo '' ak nie je uvedený."""
    title = title or ""
    for code, words_re, codes_re in LANG_PATTERNS:
        if words_re.search(title) or codes_re.search(title):
            return code
    return ""


def query_language(q):
    lang = detect_language(q)
    return lang if lang and lang != "EN" else ""


# =========================================================
# MERCH FILTER
# =========================================================

# Slová sa hľadajú ako ZAČIATOK slova ("plyš" chytí plyšák, plyšová, plyšáky).
# Tri úrovne:
#  1. ACCESSORY  – príslušenstvo, vždy preč
#  2. MERCH_HARD – oblečenie, hrnčeky, plyšáky..., VŽDY preč (aj keď je v názve "TCG")
#  3. MERCH_SOFT – figúrky, odznaky...: preč, iba ak to nie je TCG kolekcia
#                  ("Charizard ex Premium Collection with figure" ostane)

ACCESSORY_PATTERNS = [
    r"sleeves?", r"obal\w*", r"album\w*", r"binder\w*", r"toploader\w*",
    r"playmat\w*", r"podlo[žz]k\w*", r"deck\s*box\w*", r"deckbox\w*",
    r"puzdr\w*", r"pouzdr\w*", r"stojan\w*", r"portfoli\w*", r"one\s*touch",
    r"card\s+holder\w*", r"magnetic\s+holder\w*", r"penny\s+sleeves?", r"r[áa]m[čc]ek\w*",
]

MERCH_HARD_PATTERNS = [
    # oblečenie
    r"tri[čc]k\w*", r"trik[oa]", r"trik[aů]", r"t-?shirt\w*", r"\w*shirt\w*", r"tee",
    r"mikin\w*", r"hoodie\w*", r"hoody", r"sweat\w*", r"pono[žz]k\w*", r"socks?",
    r"[čc]iap\w*", r"[čc]epi[cč]\w*", r"k?[šs]iltovk\w*", r"caps?", r"beanie\w*", r"hats?",
    r"py[žz]am\w*", r"pyjam\w*", r"pajam\w*", r"kost[ýy]m\w*", r"costume\w*",
    r"rukavic\w*", r"[šs]atk\w*", r"[šs][áa]l", r"[šs][áa]ly", r"scarf\w*",
    r"tepl[áa]k\w*", r"leg[íi]n\w*", r"[šs]ortk\w*", r"bund[ay]", r"jacket\w*",
    r"papu[čc]\w*", r"slippers?", r"oble[čc]en\w*", r"textil\w*", r"bunda",
    # plyšáky, hračky
    r"ply[šs]\w*", r"plush\w*", r"peluche\w*", r"hra[čc]k\w*", r"toys?", r"lego",
    r"mega\s+construx", r"stavebnic\w*", r"puzzle\w*", r"pokladni[čc]k\w*",
    r"gashapon\w*", r"tamagotchi", r"funko\w*", r"pop!", r"vinyl\w*",
    # kuchyňa, domácnosť
    r"hrn[čc]\w*", r"hrn[íi][čc]\w*", r"hrnk\w*", r"hrnek", r"termo\w*", r"mugs?",
    r"[šs][áa]lk\w*", r"[šs][áa]lek", r"poh[áa]r\w*", r"cups?", r"tumbler\w*",
    r"f[ľl]a[šs]\w*", r"lahv\w*", r"lahev", r"bottle\w*", r"lamp", r"lamp[ayu]", r"lampi[čc]k\w*",
    r"svietidl\w*", r"sv[ií]tidl\w*", r"deka", r"deky", r"blanket\w*", r"vank[úu][šs]\w*",
    r"pol[šs]t[áa][řr]\w*", r"uter[áa]k\w*", r"ru[čc]n[íi]k\w*", r"osu[šs]k\w*", r"towel\w*",
    r"oblie[čc]k\w*", r"povle[čc]\w*", r"tanier\w*", r"tal[íi][řr]\w*", r"misk[ayu]",
    r"lunch\s*box\w*", r"desiatov\w*", r"svačin\w*", r"box\s+na\s+jedlo",
    # škola, doplnky, elektronika
    r"batoh\w*", r"backpack\w*", r"ruksak\w*", r"ta[šs]k\w*", r"bags?", r"kabelk\w*",
    r"pera[čc]n[íi]k\w*", r"penál\w*", r"z[áa]pisn[íi]k\w*", r"zo[šs]it\w*", r"se[šs]it\w*",
    r"fixk\w*", r"pastel\w*", r"pero", r"pera",
    r"k[ľl][úu][čc]enk\w*", r"kl[íi][čc]enk\w*", r"keychain\w*", r"keyring\w*",
    r"pr[íi]ves\w*", r"n[áa]ram\w*", r"n[áa]hrdeln[íi]k\w*", r"[šs]perk\w*",
    r"pe[ňn]a[žz]enk\w*", r"wallet\w*", r"phone\s+case", r"mobile\s+case", r"kryt\s+na",
    r"hodink\w*", r"hodiny", r"watch", r"sl[úu]chadl\w*", r"sluch[áa]tk\w*",
    r"headphones?", r"earphones?", r"reproduktor\w*", r"powerbank\w*",
    r"plag[áa]t\w*", r"poster\w*", r"sticker\w*", r"n[áa]lepk\w*", r"samolep\w*", r"tetov\w*",
    r"knih\w*", r"kniha", r"books?", r"komiks\w*", r"manga", r"omal\w*", r"encyklop\w*",
    r"nintendo", r"videohr\w*", r"switch",
    # jedlo
    r"[čc]okol[áa]d\w*", r"cukrovink\w*", r"bonbon\w*", r"candy", r"l[íi]zank\w*",
    r"[žz]uva[čc]k\w*", r"ramune", r"limon[áa]d\w*",
]

MERCH_SOFT_PATTERNS = [
    r"fig[úu]r\w*", r"figur\w*", r"figure\w*", r"statue\w*", r"so[šs]k\w*",
    r"odznak\w*", r"badge\w*", r"pins?", r"bro[žz]\w*", r"mystery", r"blind\s*box\w*",
]

# Znaky skutočného TCG produktu (karta / sealed)
TCG_MARKER_RE = re.compile(
    r"booster|elite\s+trainer|\betb\b|collection|kolekci|blister|\btins?\b|\btcg\b"
    r"|battle\s+deck|theme\s+deck|build\s*(?:&|and)?\s*battle|display"
    r"|\b\d{1,3}\s*/\s*\d{1,3}\b",
    re.I,
)

# Znaky jednotlivej karty (pri hľadaní karty musí mať aspoň jeden)
CARD_MARKER_RE = re.compile(
    r"\b\d{1,3}\s*/\s*\d{1,3}\b|#\s?\d{1,3}\b|\b(?:sv|swsh|sm|xy|me|bw|svp|sve)\s?-?\d"
    r"|\b(?:ex|gx|v|vmax|vstar|lv\.?\s?x|break|prime|legend|tag\s+team)\b"
    r"|holo|reverse|full\s*art|rare|promo|illustration|secret|trainer\s+gallery|alt\w*\s+art"
    r"|\bsir\b|\bir\b|\bsr\b|\bur\b|\bar\b|\bchr\b|\bshiny\b|\bkart[ay]\b|\bcard\b"
    r"|\bpsa\b|\bcgc\b|\bbgs\b|graded|\bnm\b|near\s+mint|mint",
    re.I,
)


def _words_re(patterns):
    return re.compile(r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)", re.I)


ACCESSORY_RE = _words_re(ACCESSORY_PATTERNS)
MERCH_HARD_RE = _words_re(MERCH_HARD_PATTERNS)
MERCH_SOFT_RE = _words_re(MERCH_SOFT_PATTERNS)
MERCH_RE = MERCH_HARD_RE  # spätná kompatibilita


def merch_reason(title, extra_text=""):
    """'' = je to karta/produkt; inak dôvod vyradenia."""
    text = clean_text(title + " " + extra_text)
    m = ACCESSORY_RE.search(text)
    if m:
        return "accessory:" + m.group(0).lower()
    m = MERCH_HARD_RE.search(text)
    if m:
        return "merch:" + m.group(0).lower()
    m = MERCH_SOFT_RE.search(text)
    if m and not TCG_MARKER_RE.search(text):
        return "merch:" + m.group(0).lower()
    return ""


# Názvy setov (aj starších) pre pozitívnu kontrolu
TCG_SET_NAMES = set(KNOWN_SETS) | {
    "ascended heroes", "perfect order", "white flare", "black bolt", "mega evolution",
    "scarlet violet", "scarlet & violet", "paldean fates", "shrouded fable", "paldea evolved",
    "paradox rift", "temporal forces", "twilight masquerade", "stellar crown",
    "crown zenith", "silver tempest", "lost origin", "pokemon go", "pokémon go", "astral radiance",
    "brilliant stars", "fusion strike", "celebrations", "evolving skies", "chilling reign",
    "battle styles", "shining fates", "vivid voltage", "champion's path", "champions path",
    "darkness ablaze", "rebel clash", "sword shield", "sword & shield", "cosmic eclipse",
    "hidden fates", "unified minds", "unbroken bonds", "team up", "lost thunder",
    "dragon majesty", "celestial storm", "forbidden light", "ultra prism", "crimson invasion",
    "shining legends", "burning shadows", "guardians rising", "sun moon", "sun & moon",
    "evolutions", "steam siege", "fates collide", "generations", "breakpoint", "breakthrough",
    "ancient origins", "roaring skies", "primal clash", "phantom forces", "furious fists",
    "flashfire", "base set", "jungle", "fossil", "team rocket", "neo genesis", "gym heroes",
    "151", "shiny treasure", "vstar universe", "terastal", "night wanderer",
    "stellar miracle", "battle partners", "heat wave arena", "glory of team rocket",
}
_TCG_SET_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in sorted(TCG_SET_NAMES, key=len, reverse=True)) + r")\b",
    re.I)


def looks_like_tcg(title):
    """Pozitívna kontrola: názov vyzerá ako karta alebo TCG produkt."""
    text = clean_text(title)
    if TCG_MARKER_RE.search(text) or CARD_MARKER_RE.search(text):
        return True
    return _TCG_SET_RE.search(text) is not None


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
# SPÁJANIE ROVNAKÝCH PRODUKTOV (group key)
# =========================================================

GROUP_TYPES = [
    ("etb", re.compile(r"elite\s+trainer\s+box|\betb\b", re.I)),
    ("booster box", re.compile(r"booster\s*(?:box|display)", re.I)),
    ("booster bundle", re.compile(r"booster\s*bundle", re.I)),
    ("sleeved booster", re.compile(r"sleeved\s+booster", re.I)),
    ("3-pack blister", re.compile(r"3\s*-?\s*pack|three\s+pack|3\s*booster\s+blister", re.I)),
    ("checklane blister", re.compile(r"checklane|1\s*-?\s*pack\s+blister|single\s+blister", re.I)),
    ("blister", re.compile(r"blister", re.I)),
    ("mini tin", re.compile(r"mini\s+tin", re.I)),
    ("tin", re.compile(r"\btins?\b", re.I)),
    ("build battle", re.compile(r"build\s*(?:&|and)?\s*battle", re.I)),
    ("booster pack", re.compile(r"booster\s+pack|\bbooster\b", re.I)),
    ("collection", re.compile(r"collection|kolekci", re.I)),
]
_VARIANT_RES = [
    ("pc", re.compile(r"pok[eé]mon\s+center", re.I)),
    ("half", re.compile(r"\bhalf\b|polovi[čc]n", re.I)),
    ("rev", re.compile(r"reverse", re.I)),
    ("psa", re.compile(r"\b(?:psa|cgc|bgs|graded)\b", re.I)),
]


_COMBO_TYPES = [
    re.compile(r"elite\s+trainer\s+box|\betb\b", re.I),
    re.compile(r"booster\s*(?:box|display)", re.I),
    re.compile(r"booster\s*bundle", re.I),
    re.compile(r"blister", re.I),
    re.compile(r"\btins?\b", re.I),
    re.compile(r"collection|kolekci", re.I),
]


def is_combo(title):
    """Viac produktov v jednom balení ("Booster Bundle + ETB", "2x ETB", "set ...")."""
    t = clean_text(title)
    kinds = sum(1 for rx in _COMBO_TYPES if rx.search(t))
    if kinds >= 2:
        return True
    return bool(re.search(
        r"(?<![\w/.,])(?:[2-9]|1[0-9])\s*(?:x|ks|kusy|pcs)(?![a-z])"   # 2x, 3 ks
        r"|\bx\s*(?:[2-9]|1[0-9])\b"                                   # ETB x2
        r"|\b(?:bundle\s+deal|komplet\w*|set\s+of|sada)\b", t, re.I))


def group_key(title, lang=""):
    """Kľúč, podľa ktorého sa spoja rovnaké produkty z rôznych obchodov.
    None = nevieme s istotou povedať, o aký produkt ide (zobrazí sa samostatne)."""
    t = clean_text(title)
    if not t or is_combo(t):
        return None
    p = normalize_query(t)
    lang = lang or "EN"
    variants = ",".join(v for v, rx in _VARIANT_RES if rx.search(t))
    ptype = next((name for name, rx in GROUP_TYPES if rx.search(t)), "")
    pokemon = (p.get("pokemon") or "").lower()
    set_name = p.get("set_name") or ""
    number = p.get("card_number") or ""

    if ptype and set_name and ptype != "collection":
        return f"s|{set_name}|{ptype}|{pokemon}|{variants}|{lang}"
    if number and pokemon:
        return f"c|{pokemon}|{number}|{variants}|{lang}"
    if pokemon and set_name and p.get("suffix") and not ptype:
        return f"c|{pokemon}|{p['suffix']}|{set_name}|{variants}|{lang}"
    return None


# =========================================================
# POČET BOOSTEROV (cena za booster)
# =========================================================

PACKS_EXPLICIT_RE = re.compile(
    r"(?<![\d/.,])(\d{1,2})\s*(?:-|x)?\s*(?:booster\w*|bal[íi][čc]\w*|packs?\b|packungen|boost\w*)",
    re.I)
PACKS_PAREN_RE = re.compile(r"booster\s*(?:box|display)\D{0,10}\((\d{1,2})\)", re.I)


def estimate_packs(title, lang=""):
    """Odhad počtu boosterov v produkte; None = nevieme / nemá zmysel."""
    t = clean_text(title).lower()
    if not t or is_combo(t):
        return None
    m = PACKS_EXPLICIT_RE.search(t) or PACKS_PAREN_RE.search(t)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 36:
            return n
    if lang in ASIAN_LANGS:
        return None  # ázijské boxy majú rôzny počet balíčkov (10, 20, 30...)
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


_TRACKING_PARAM_RE = re.compile(r"^(?:_pos|_sid|_ss|_psq|_fid|_v|utm_\w+|fbclid|gclid|srsltid|ref|variant_id)$", re.I)


def clean_link(url):
    """Odkaz bez sledovacích parametrov (Shopify _pos/_sid/_ss, utm_...),
    aby mal ten istý produkt vždy rovnakú adresu (história, obľúbené, strážca)."""
    if not url:
        return url
    try:
        p = urllib.parse.urlsplit(url)
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not _TRACKING_PARAM_RE.match(k)]
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))
    except Exception:
        return url


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
        BG_EXECUTOR.submit(_store_image, product_url, image_url)
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


def find_product_block_el(anchor):
    """Najbližší rodič, ktorý obsahuje cenu (dlaždica produktu)."""
    current, best = anchor, None
    for level in range(1, 7):
        current = current.parent
        if not current:
            break
        text = clean_text(current.get_text(" ", strip=True))
        if text and any(c in text for c in ("€", "Kč", "CZK")) and len(text) < 1800:
            best = current
            if level >= 2:
                break
    return best or anchor.parent


def find_product_block(anchor):
    el = find_product_block_el(anchor)
    return clean_text(el.get_text(" ", strip=True)) if el is not None else ""


_HIDDEN_CLASSES = {"hidden", "hide", "d-none", "is-hidden", "u-hidden", "visually-hidden-hidden"}


def _is_hidden(tag):
    if tag.has_attr("hidden") or str(tag.get("aria-hidden", "")).lower() == "true":
        return True
    if _HIDDEN_CLASSES & set(tag.get("class") or []):
        return True
    style = str(tag.get("style", "")).replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def visible_text(el):
    """Text dlaždice bez skrytých prvkov (napr. skryté 'Vypredané' v Shopify)."""
    if el is None:
        return ""
    parts = []
    for s in el.find_all(string=True):
        p, hidden = s.parent, False
        while p is not None and p is not el:
            if getattr(p, "name", None) in ("script", "style", "template", "noscript") or _is_hidden(p):
                hidden = True
                break
            p = p.parent
        if not hidden:
            parts.append(str(s))
    return clean_text(" ".join(parts))


def detect_stock_el(el):
    """Sklad z dlaždice: najprv tlačidlo košíka, potom viditeľný text."""
    if el is None:
        return ""
    for btn in el.select('button[name="add"], button[type="submit"], .add-to-cart, .btn-cart, .btn-add-to-cart'):
        label = clean_text(btn.get_text(" ", strip=True)).lower()
        if btn.has_attr("disabled") or "disabled" in (btn.get("class") or []):
            if STOCK_PRE_RE.search(label):
                return "preorder"
            return "out"
    stock = detect_stock(visible_text(el))
    if not stock:
        for btn in el.select('button[name="add"], button[type="submit"], .add-to-cart, .btn-cart'):
            if btn.has_attr("disabled"):
                continue
            label = visible_text(btn).lower()
            if STOCK_PRE_RE.search(label):
                return "preorder"
            if re.search(r"do\s+ko[šs][íi]ka|add\s+to\s+cart|koupit|k[úu]pi[ťt]", label):
                return "in"
    return stock


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
        seen = set()

        for anchor in links:
            href = clean_link(absolute_url(shop["base_url"], anchor.get("href")))
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
            if not looks_like_tcg(title):
                debug["merch_filtered"] += 1
                _log(debug, title=title, decision="filtered", reason="not_tcg")
                continue

            lang = detect_language(title)

            block_el = find_product_block_el(anchor)
            block_text = clean_text(block_el.get_text(" ", strip=True)) if block_el is not None else ""

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
            packs = estimate_packs(title, lang)
            results.append({
                "title": title, "shop": shop["name"], "country": shop["country"],
                "condition": "Nové", "language": lang, "price_eur": round(price, 2),
                "link": href, "image": image_url,
                "stock": detect_stock_el(block_el),
                "packs": packs,
                "price_per_pack": round(price / packs, 2) if packs and packs > 1 else None,
                "group": group_key(title, lang),
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
    if not title or is_merch(title) or not looks_like_tcg(title):
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
        if not title or is_merch(title) or not looks_like_tcg(title):
            continue
        if detect_language(title) in ASIAN_LANGS and not foreign_ok:
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
        "query_lang": query_language(original),
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
# ÚVODNÁ STRÁNKA
# =========================================================

home_cache = TTLCache(600, 2)


def build_home():
    now = datetime.now(timezone.utc)
    since3 = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    since30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    since14 = (now - timedelta(days=14)).strftime("%Y-%m-%d")
    conn = db_connect()
    try:
        latest = conn.execute("""
            SELECT p.link, p.title, p.shop, p.price_eur, p.stock, p.image, p.day,
                   (SELECT MAX(q.price_eur) FROM price_daily q
                     WHERE q.link = p.link AND q.day >= ? AND q.day < p.day) AS old_max,
                   (SELECT MAX(q.image) FROM price_daily q WHERE q.link = p.link) AS any_image
            FROM price_daily p
            JOIN (SELECT link, MAX(day) AS d FROM price_daily WHERE day >= ? GROUP BY link) r
              ON p.link = r.link AND p.day = r.d
            WHERE p.price_eur > 0 AND (p.stock IS NULL OR p.stock != 'out')
        """, (since30, since3)).fetchall()
        popular = conn.execute("""
            SELECT query, SUM(n) AS c FROM search_log WHERE day >= ?
            GROUP BY query ORDER BY c DESC LIMIT 10
        """, (since14,)).fetchall()
    finally:
        conn.close()

    deals, cheap_etb, seen_deal, seen_etb = [], [], set(), set()
    for link, title, shop, price, stock, image, day, old_max, any_image in latest:
        if is_merch(title) or not looks_like_tcg(title):
            continue
        lang = detect_language(title)
        if lang in ASIAN_LANGS:
            continue
        item = {"link": link, "title": title, "shop": shop, "price_eur": round(price, 2),
                "stock": stock or "", "image": image or any_image or "", "language": lang,
                "group": group_key(title, lang), "day": day,
                "query": (make_suggestion_from_title(title) or {}).get("query") or title}
        if old_max and old_max - price >= 1 and price <= old_max * 0.97:
            item["old_price_eur"] = round(old_max, 2)
            item["drop_pct"] = round((old_max - price) / old_max * 100)
            deals.append(item)
        if re.search(r"elite\s+trainer\s+box|\betb\b", title, re.I):
            cheap_etb.append(item)

    deals.sort(key=lambda d: d["drop_pct"], reverse=True)
    cheap_etb.sort(key=lambda d: d["price_eur"])

    def uniq(items, seen, limit):
        out = []
        for it in items:
            k = it["group"] or it["link"]
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
            if len(out) >= limit:
                break
        return out

    return {
        "deals": uniq(deals, seen_deal, 12),
        "cheap_etb": uniq(cheap_etb, seen_etb, 8),
        "popular": [q for q, _ in popular if len(q) >= 3][:8],
        "new_sets": NEW_SETS,
    }


@app.get("/api/home")
def api_home():
    data = home_cache.get("home")
    if data is None:
        try:
            data = build_home()
        except Exception:
            data = {"deals": [], "cheap_etb": [], "popular": [], "new_sets": NEW_SETS}
        home_cache.set("home", data)
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "public, max-age=120"
    return resp


# =========================================================
# LOGO A IKONY (vložené priamo sem – priečinok static/ netreba)
# =========================================================

import base64 as _b64

LOGO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" xmlns:c2pa="http://c2pa.org/manifest"><metadata><c2pa:manifest>AAAWgmp1bWIAAAAeanVtZGMycGEAEQAQgAAAqgA4m3EDYzJwYQAAABZcanVtYgAAAEdqdW1kYzJtYQARABCAAACqADibcQN1cm46YzJwYTo2NzU3NmI0Yy1kYmY1LTQ1OGUtYWE0ZC03OTU0OGFlM2IwMTQAAAADl2p1bWIAAAApanVtZGMyYXMAEQAQgAAAqgA4m3EDYzJwYS5hc3NlcnRpb25zAAAAALxqdW1iAAAARGp1bWRjYm9yABEAEIAAAKoAOJtxE2MycGEuaW5ncmVkaWVudC52MwAAAAAYYzJzaBNutsyNb8NaKL3jaGZvrwoAAABwY2JvcqNpZGM6Zm9ybWF0bWltYWdlL3N2Zyt4bWxqaW5zdGFuY2VJRHgseG1wOmlpZDowMTBkM2NjMi1lNjY3LTQ3MzktYTA4Ny1iYjljNmRkNDkwMThscmVsYXRpb25zaGlwaHBhcmVudE9mAAAB4mp1bWIAAABBanVtZGNib3IAEQAQgAAAqgA4m3ETYzJwYS5hY3Rpb25zLnYyAAAAABhjMnNo9Kd8mblflcp6NUeNp1HOhwAAAZljYm9yomdhY3Rpb25zgqJmYWN0aW9ua2MycGEub3BlbmVkanBhcmFtZXRlcnOha2luZ3JlZGllbnRzgaJjdXJseC1zZWxmI2p1bWJmPWMycGEuYXNzZXJ0aW9ucy9jMnBhLmluZ3JlZGllbnQudjNkaGFzaFgguf9UG1be4JR9oSfWv9PjG3Vg1QPUTRmgKjWdK448qpekZmFjdGlvbngdY29tLmFudGhyb3BpYy5jbGF1ZGUucHJvdmlkZWRqcGFyYW1ldGVyc6F4H2NvbS5hbnRocm9waWMub3JpZ2luLWNvbmZpZGVuY2VndW5rbm93bmtkZXNjcmlwdGlvbnhmQ2xhdWRlIHByb3ZpZGVkIHRoaXMgZmlsZSBhdCB0aGUgcmVxdWVzdCBvZiBhIHVzZXIgYW5kIG1heSBoYXZlIGNyZWF0ZWQgb3IgbW9kaWZpZWQgdGhlIGZpbGUgY29udGVudHMubXNvZnR3YXJlQWdlbnShZG5hbWVmQ2xhdWRlcmFsbEFjdGlvbnNJbmNsdWRlZPUAAADIanVtYgAAAEBqdW1kY2JvcgARABCAAACqADibcRNjMnBhLmhhc2guZGF0YQAAAAAYYzJzaNMAI5Yd3urZs3ddaPZ0yjAAAACAY2JvcqVjYWxnZnNoYTI1NmNwYWRNAAAAAAAAAAAAAAAAAGRoYXNoWCA3WBulOQMoN3ufjRYbMJNlPnLgrWPo2jN7A6sbUzw6VGRuYW1lbmp1bWJmIG1hbmlmZXN0amV4Y2x1c2lvbnOBomVzdGFydBh7Zmxlbmd0aBkeBAAAAj5qdW1iAAAAJ2p1bWRjMmNsABEAEIAAAKoAOJtxA2MycGEuY2xhaW0udjIAAAACD2Nib3KlY2FsZ2ZzaGEyNTZpc2lnbmF0dXJleE1zZWxmI2p1bWJmPS9jMnBhL3VybjpjMnBhOjY3NTc2YjRjLWRiZjUtNDU4ZS1hYTRkLTc5NTQ4YWUzYjAxNC9jMnBhLnNpZ25hdHVyZWppbnN0YW5jZUlEeCx4bXA6aWlkOjA1MDQyMDk3LWE5MjctNDQ2YS04M2ZjLTExMTRkODY0ODRjNnJjcmVhdGVkX2Fzc2VydGlvbnODomN1cmx4LXNlbGYjanVtYmY9YzJwYS5hc3NlcnRpb25zL2MycGEuaW5ncmVkaWVudC52M2RoYXNoWCC5/1QbVt7glH2hJ9a/0+MbdWDVA9RNGaAqNZ0rjjyql6JjdXJseCpzZWxmI2p1bWJmPWMycGEuYXNzZXJ0aW9ucy9jMnBhLmFjdGlvbnMudjJkaGFzaFggjIL9yfbkaoRLmmuobBUQLZMcwKbZEt58bnpoN7tYHgKiY3VybHgpc2VsZiNqdW1iZj1jMnBhLmFzc2VydGlvbnMvYzJwYS5oYXNoLmRhdGFkaGFzaFggS59ORhFFa4kHcpdQnIv0cs1yCweSCpAtZDfW5OlOGfl0Y2xhaW1fZ2VuZXJhdG9yX2luZm+jZG5hbWVvQW50aHJvcGljIEZpbGVzZ3ZlcnNpb25lMS4wLjBrc3BlY1ZlcnNpb25lMi40LjAAABA4anVtYgAAAChqdW1kYzJjcwARABCAAACqADibcQNjMnBhLnNpZ25hdHVyZQAAABAIY2JvctKEWQISogEmGCFZAgowggIGMIIBjaADAgECAhRA5aAK7sI50L64g/oGQgU9Z1UTADAKBggqhkjOPQQDAzBJMRcwFQYDVQQKEw5BbnRocm9waWMsIFBCQzEuMCwGA1UEAxMlQW50aHJvcGljIENvbnRlbnQgQ3JlZGVudGlhbHMgUm9vdCBDQTAeFw0yNjA4MDcxODQzNTZaFw0yODA4MDYxOTQzNTZaMEQxFzAVBgNVBAoTDkFudGhyb3BpYywgUEJDMSkwJwYDVQQDEyBBbnRocm9waWMgQ2xhdWRlIENvbnRlbnQgU2lnbmluZzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABJh6CmvLUBgFFNU0vUKlOVtE6djd17L5SuwX0LemFisBM3dkd/3cyjxFA3Qo5S46fX0/ihY0VZ7mfb9KF703t5OjWDBWMA4GA1UdDwEB/wQEAwIHgDAVBgNVHSUEDjAMBgorBgEEAYPoXgIBMAwGA1UdEwEB/wQCMAAwHwYDVR0jBBgwFoAUzlHiBIFOZFsj+OPEz5o+nMHXXMIwCgYIKoZIzj0EAwMDZwAwZAIwMXMdFJ4BetLLVY7ORuE9noqbbAZOZn/aArXyTwFAZfKrPzxF2vPoJNf1+UCdg1XGAjBwX1zd9WGqYkqmL5SFqw1QySjr1zJfpJM9+1rdDwSPLMOPOjKuiXjoU/pUUeG9RwmhY3BhZFkNngAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPZYQEYvBWjJfZyChT02EPNGoEs61iuYzFbTubmURp3fMSzy7Id2ZvtroOy9ecu7Bpcn3vP6OZD0CR91pRDXXpVzeqg=</c2pa:manifest></metadata>
  <defs>
    <radialGradient id="bg" cx="50%" cy="40%" r="75%"><stop offset="0" stop-color="#1b2a4a"/><stop offset="1" stop-color="#070b14"/></radialGradient>
    <linearGradient id="sw" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#facc15" stop-opacity=".85"/><stop offset="1" stop-color="#facc15" stop-opacity="0"/></linearGradient>
    <radialGradient id="core" cx="45%" cy="40%"><stop offset="0" stop-color="#fff7c2"/><stop offset=".55" stop-color="#facc15"/><stop offset="1" stop-color="#d97706"/></radialGradient>
  </defs>
  <rect width="64" height="64" rx="14" fill="url(#bg)"/>
  <g fill="none" stroke="#facc15" stroke-opacity=".45" stroke-width="2">
    <circle cx="32" cy="32" r="22"/><circle cx="32" cy="32" r="14"/>
  </g>
  <path d="M32 32 L32 10 A22 22 0 0 1 51 21 Z" fill="url(#sw)"/>
  <rect x="40" y="15" width="9" height="12" rx="1.6" transform="rotate(14 44.5 21)" fill="#93c5fd" stroke="#fff" stroke-width="1"/>
  <circle cx="32" cy="32" r="6.5" fill="url(#core)"/>
  <circle cx="21" cy="42" r="2.2" fill="#4ade80"/>
</svg>"""

_ICONS_B64 = {
    "icon-192.png": (
        "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"),
    "icon-512.png": (
        "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"
        "RK5CYII="),
    "icon-maskable-512.png": (
        "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"),
    "apple-touch-icon.png": (
        "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAMAAAAKE/YAAAABIFBMVEX+9sX/3RH/2hH+0xb/0RL/1hH/5BD/4BD/3Q/7zhf6zBb6"
        "zBX7zRP5yhP2yRT2yBT4xQ/b49n0yBXzxxXjxVHzxRXxxRXXsRvrwBfvvROayf2Uxf2Txf2ezO1Q3YaLvuy/lhyljCOfiSbgewTZ"
        "dwbadQXCehHIbQqQfi2IdieAcSp0bTRwZSxrXiteVy1NTjZJQS88PjMzNjUlLTccJjgYJDsXIjkWIjkXITkWIToWITkVIzwVITwU"
        "ITsVIToUIToVITkVIDoUIDsUIDoTIDoUIDkSHzsTHzkRHDYPHTwOHDwNGzwLGz0LGj0PGS8JGTwNEyMKDxwIEB8KDhoJDhsJDhoJ"
        "DhkIDhoJDRoIDRoIDRgIDRcGDRsJChkAACEAAACqfAclAAAUK0lEQVR42tWd+1/ayNfHQ0wINGAkCMF9tlsQ7IrlHi41ahTRcA2X"
        "FpDS7e7//188Z3LBEJIAAfuV80NfFBXeHD5zLpOZCfafo/0ze5nMfrxMfpu9/JhNXmb/OFNhDj/7PhvPJuPReDz5jTaGN5zAG393"
        "A/3r+2wyGv1WXgP5aDSZff+1JfRs/PK/An4FfxnPtoD+Pp6MJu/ARpPx9w2h/315H8gq9su/m0BP3w+yij1dD/19PJ68KxuvSsQE"
        "/evHaPLubPTjlxP09GU8eYc2fpnaQ09n75IZqGdTO+h3y2ymxg6C2UT9Cv3zPTMj6p+r0N9e3jUzGo3fVqBn75wZ+doMPRtN3r2N"
        "ZsvQe9SG8kpTzRZP7EkhRuh/p3t65el8/nM+m09Hmk3hPz/n8+meoKf/GqCno939O5vP55PRcNjvt/qtpmbwsN8fDkcT+OFsd5+P"
        "pq/Q/+xMDM4cIdzm3S2yu0fNtP82ETq4fb4z9z8L6B0dPZ8DcOsZ0TUajeeGydATCP65BeDAvburEfRwNx+Ph/0mMFngmtDhl5r9"
        "4Xg3fw81aNeOHk/nE4X40Rn4FfxR4Z7MXQ98xdUA/cvltMZ4BrJo3ZmJ6/UHQRBqisGDh3rdzH3XApm4TWYvvxRod3U/IH/rN2/u"
        "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"),
    "favicon-32.png": (
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAMAAABEpIrGAAABgFBMVEUIDRkLGDcWITlIRzACAgVzZyuGdSYoKzULEBouMzZlXS1X"
        "Uy+giCCZhCP5yhEHDhkJDRoJDRqGdimSxvyz1/iahCV9cCh1Rx9xWCP70hVpSCSHvfncdgTL5/3miwfxqA1J7Ihqa2h9cDN6g5WE"
        "ZiLIphzAoB7D3fjwlAb5ugn50S386Hv89L4BDSklL0Y6PjYgSUk1bVA7tHNePiZQTTdHTmJTUjBdY3Nde0JCvHJ2aiZpYkBuokyA"
        "ZiGBcR+KdBKAcSybgxyVi2ODipeTmZ2dq8OmiBa6hxm4mh6+nh6+nymvlCC7o0OtrJe6ydbOrSDbzZDZ39IAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACI70x0AAAAYHRSTlP+/v3bBsW89v3ty9Ky"
        "tf9Eidr+/v77wf////////3////y9/7/vM38///////+/t3+9f///v/+/dj8/v/SvrvTwdn++f//wP+13LLcuvz7stDmAAAAAAAA"
        "AAAAAAAAAAAMVjHhAAABmUlEQVR42oVT13bDIAxFxkDwoIntODvpTtK99957/P/PVJDh0bTRA+fAvWhcScRGK5bIBCsVNUZse4b8"
        "aTOa8A+uGcRObgUYWiF5s0kxBQvqMOZuWilKkYzyA5DM55Q+Ly89MbGOHwaZkjHueBS9W+VyEFx/3e5DNhkAzhC1DOFkvhtF0Rwp"
        "ZHBXwyDpazmo3kSNRrSQIhTAYRp3Pc6Wlz4v36MwbM2ligHpaZwzgSGC+YfvMAyjFwmJg5gC4g4eEATV01bYCO/p1dgFCB8hN8ZD"
        "LD4uHuzdfXRbC+CLkQugHDFfWFBXNdWpSku+HW4Ap2MCQwUkRyX7q2srtTYyLzAYZRkCXqGqkKA6FRi95An1fk2pWm8ywYRoK6U6"
        "TYG6yQxBJ2l5GHq32Ws36zAoK5XkoMwjXWalgmJB7JqyICPUzuy2Fkqbo8umcaK1kfp49uxcaFgwHRG8ROphs7ZMsxj3TGOZA/l2"
        "g2k3lWYuXCxq4sDA4GQ5PBk5Y9RzEjw/tJRyn8kxXvo99g4VmbGfujjTV2/q8k5Z/x8Ocxs3uGnFbAAAAABJRU5ErkJggg==")
}
EMBEDDED_STATIC = {name: (_b64.b64decode(data), "image/png") for name, data in _ICONS_B64.items()}
EMBEDDED_STATIC["logo.svg"] = (LOGO_SVG.encode("utf-8"), "image/svg+xml")


@app.get("/static/<path:name>")
def static_files(name):
    """Ikony z app.py; iné súbory (ak niekedy pribudnú) z priečinka static/."""
    if name in EMBEDDED_STATIC:
        data, mime = EMBEDDED_STATIC[name]
        resp = Response(data, mimetype=mime)
        resp.headers["Cache-Control"] = "public, max-age=604800"
        return resp
    path = os.path.normpath(os.path.join(BASE_DIR, "static", name))
    if path.startswith(os.path.join(BASE_DIR, "static") + os.sep) and os.path.isfile(path):
        with open(path, "rb") as f:
            data = f.read()
        import mimetypes
        return Response(data, mimetype=mimetypes.guess_type(path)[0] or "application/octet-stream")
    return Response("Nenájdené", status=404, mimetype="text/plain")


@app.get("/favicon.ico")
def favicon():
    data, mime = EMBEDDED_STATIC["favicon-32.png"]
    resp = Response(data, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


# =========================================================
# PWA (inštalácia na mobil)
# =========================================================

@app.get("/manifest.webmanifest")
def manifest():
    data = {
        "name": "CardRadar – ceny Pokémon kariet",
        "short_name": "CardRadar",
        "description": "Porovnanie cien Pokémon kariet, ETB a booster boxov.",
        "start_url": "/?source=pwa",
        "scope": "/",
        "display": "standalone",
        "background_color": "#070b14",
        "theme_color": "#070b14",
        "lang": "sk",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-maskable-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    resp = Response(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


SERVICE_WORKER = """
const CACHE = 'cardradar-v6';
const SHELL = ['/', '/static/icon-192.png', '/static/logo.svg'];
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
  if (url.pathname.startsWith('/api/')) return;            // ceny vždy čerstvé zo siete
  if (e.request.mode === 'navigate') {                      // stránka: sieť, offline z cache
    e.respondWith(fetch(e.request).then(r => {
      const copy = r.clone(); caches.open(CACHE).then(c => c.put('/', copy)); return r;
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
            or resp.mimetype not in ("application/json", "text/html", "application/javascript",
                                     "application/manifest+json", "image/svg+xml")):
        return resp
    data = resp.get_data()
    if len(data) < 1024:
        return resp
    resp.set_data(gzip.compress(data, compresslevel=5))
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers.add("Vary", "Accept-Encoding")
    return resp

import catalog_shops
catalog_shops.install(globals())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), threaded=True)
