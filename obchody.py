"""
CARD RADAR – obchody.py
Všetko, čo súvisí s obchodmi a dátami:
  - zoznam obchodov (SHOPS) + obchody zapnuté cez /admin/obchody
  - sťahovanie stránok, čítanie dlaždíc produktov
  - obchody s vyhľadávaním, katalógové obchody (prechádzajú sa kategórie), XML feedy
  - hľadanie vo všetkých obchodoch s jednou cache
  - obrázky, našepkávač, databáza (história cien, katalógy)

PRIDANIE OBCHODU:
  - Shoptet / Shopify / Upgates / WooCommerce s vyhľadávaním: najľahšie cez
    /admin/obchody?key=ADMIN_KEY (otestuješ a zapneš, bez úpravy kódu)
  - obchod bez použiteľného vyhľadávania: nový záznam s "catalog" do SHOPS nižšie
"""

import copy
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

import logika as L

try:
    import fcntl   # Linux: zámok, aby úlohy na pozadí bežali len v jednom procese
except ImportError:
    fcntl = None

try:
    import lxml  # noqa: F401  rýchlejšie parsovanie
    HTML_PARSER = "lxml"
except ImportError:
    HTML_PARSER = "html.parser"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SEARCH_TIMEOUT = 6          # max. sekúnd na jeden obchod
SEARCH_BUDGET = float(os.environ.get("SEARCH_BUDGET", "2.8"))   # potom sa vráti, čo je hotové
CACHE_FRESH = 600           # 10 min: výsledok je čerstvý
CACHE_STALE = 6 * 3600      # do 6 h: ukáže sa hneď a na pozadí sa obnoví
CATALOG_REFRESH_MIN = float(os.environ.get("CATALOG_REFRESH_MIN", "60"))
PAGE_DELAY = 1.0            # pauza medzi stranami katalógu (šetrne k obchodu)
HISTORY_KEEP_DAYS = 90
OK_STATUSES = ("ok", "no_results")


# =========================================================
# OBCHODY
#   name, country, base_url, enabled
#   search_url + link_selector  = obchod s vyhľadávaním ({q} = hľadaný text)
#   shopify      = sklad a obrázky z JSON obchodu
#   loose_set    = obchod nepíše set do názvu, stačí číslo karty
#   catalog      = zoznam kategórií, ktoré sa prechádzajú (max_pages strán)
#   feed         = adresa XML feedu (Heureka / Google)
#   image_replace = (z, na) úprava adresy obrázka
#   shipping     = poštovné (najlacnejší spôsob), napr.
#                  {"price": 3.9, "free_from": 60, "currency": "EUR"}
#                  {"price": 89, "free_from": 1500, "currency": "CZK"}
#                  free_from = od akej sumy je doprava zdarma (None = nikdy)
#                  Bez "shipping" sa poštovné nezobrazuje (radšej nič ako zlé číslo).
#   affiliate    = partnerský odkaz, {url} = adresa produktu (zakódovaná), napr.
#                  "https://partner.example/click?id=123&url={url}"
# =========================================================

SHOPS = [
    # shopify_catalog = celý Pokémon katalóg cez Shopify JSON (všetky produkty, nie len 1. strana hľadania);
    # kým sa načíta (alebo ak by JSON nefungoval), hľadá sa cez search_url
    {"name": "CardyX", "country": "SK", "enabled": True, "shopify": True,
     "shopify_catalog": ["https://www.cardyx.sk/collections/pokemon"],
     "base_url": "https://www.cardyx.sk/",
     "search_url": "https://www.cardyx.sk/search?q={q}",
     "link_selector": 'a[href*="/products/"]'},
    # Shoptet obchody: celý Pokémon sortiment z kategórií (raz za hodinu, všetky strany);
    # kým sa katalóg načíta (alebo keby kategórie nefungovali), hľadá sa cez search_url.
    # Kategórie sú z menu obchodu (október 2026) – keď obchod pridá novú, doplň ju sem.
    {"name": "TCG Zone Nitra", "country": "SK", "enabled": True, "loose_set": True,
     "base_url": "https://www.tcgzonenitra.sk/",
     "search_url": "https://www.tcgzonenitra.sk/vyhladavanie/?string={q}",
     "link_selector": "div.product a.name",
     "catalog": [
         "https://www.tcgzonenitra.sk/pokemon/",
         "https://www.tcgzonenitra.sk/booster-packy/",
         "https://www.tcgzonenitra.sk/mystery-pokemon-balicky/",
         "https://www.tcgzonenitra.sk/single-karty/",
         "https://www.tcgzonenitra.sk/single-karty-svet-2/",
     ],
     "max_pages": 150},
    {"name": "Beardex", "country": "SK", "enabled": True,
     "base_url": "https://www.beardex.eu/",
     "search_url": "https://www.beardex.eu/vyhladavanie/?string={q}",
     "link_selector": "div.product a.name",
     "catalog": [
         "https://www.beardex.eu/pokemon-tcg/",
         "https://www.beardex.eu/elite-trainer-box/",
         "https://www.beardex.eu/booster/",
         "https://www.beardex.eu/booster-bundle/",
         "https://www.beardex.eu/booster-box/",
         "https://www.beardex.eu/tinky/",
         "https://www.beardex.eu/sealed-case/",
         "https://www.beardex.eu/single-karty/",
     ],
     "max_pages": 100},
    {"name": "CardEmpire", "country": "SK", "enabled": True,
     "base_url": "https://www.cardempire.sk/",
     "search_url": "https://www.cardempire.sk/vyhladavanie/?string={q}",
     "link_selector": "div.product a.name",
     "catalog": [
         "https://www.cardempire.sk/pokemon/",
         "https://www.cardempire.sk/elite-trainer-boxy/",
         "https://www.cardempire.sk/booster-boxy/",
         "https://www.cardempire.sk/booster-bundle/",
         "https://www.cardempire.sk/booster-packy/",
         "https://www.cardempire.sk/sleeved-booster-pack/",
         "https://www.cardempire.sk/premiove-boxy/",
         "https://www.cardempire.sk/tinky/",
         "https://www.cardempire.sk/blistre/",
         "https://www.cardempire.sk/build-battle-kity/",
         "https://www.cardempire.sk/build-battle-stadiumy/",
         "https://www.cardempire.sk/sealed-casy/",
         "https://www.cardempire.sk/pokemon-karty/",
     ],
     "max_pages": 100},
    {"name": "iHRYsko", "country": "SK", "enabled": True,
     "base_url": "https://www.ihrysko.sk/",
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
     "max_pages": 8, "image_replace": ("/xs/products/", "/md/products/")},
    {"name": "imago", "country": "SK", "enabled": True,
     "base_url": "https://www.imago.sk/",
     "catalog": ["https://www.imago.sk/pokemon-kartove-hra"], "max_pages": 15},
    {"name": "Posbírej to", "country": "CZ", "enabled": True,
     "base_url": "https://www.posbirejto.cz/",
     "catalog": [
         "https://www.posbirejto.cz/boosterboxy/",
         "https://www.posbirejto.cz/balicky/",
         "https://www.posbirejto.cz/specialniboxy/",
         "https://www.posbirejto.cz/cinske-produkty/",
         "https://www.posbirejto.cz/ohodnocenekarty-2/",
         "https://www.posbirejto.cz/anglickekarty/",
         "https://www.posbirejto.cz/japonskekarty/",
         "https://www.posbirejto.cz/cinske-karty/",
     ],
     "max_pages": 25},
    # Shoptet, celá Pokémon ponuka v jednej kategórii (~72 produktov, 3 strany) – október 2026
    {"name": "Tlama Games", "country": "CZ", "enabled": True,
     "base_url": "https://www.tlamagames.com/",
     "catalog": ["https://www.tlamagames.com/pokemon/"], "max_pages": 10},
    # Xzone (robots.txt zakazuje čítať kategórie) a Veselý drak (blokuje roboty): len cez XML feed
    # web blokuje roboty (HTTP 403) – zapni, keď dostaneš adresu XML feedu
    {"name": "Herný svet", "country": "SK", "enabled": False,
     "base_url": "https://www.hernysvet.sk/", "feed": "",
     "catalog": ["https://www.hernysvet.sk/tema/pokemon"], "max_pages": 15},
    # Upgates: vyhľadávanie cez adresu nefunguje (HTTP chyba), preto katalóg z kategórií (október 2026)
    {"name": "Gengar.cz", "country": "CZ", "enabled": True,
     "base_url": "https://www.gengar.cz/",
     "catalog": [
         "https://www.gengar.cz/elite-trainer-box",
         "https://www.gengar.cz/booster-box",
         "https://www.gengar.cz/pokemon-booster",
         "https://www.gengar.cz/collection",
         "https://www.gengar.cz/pokemon-tin-plechovky",
         "https://www.gengar.cz/30th-celebration-1",
         "https://www.gengar.cz/japonske-boostery",
         "https://www.gengar.cz/japonske-korejske-boostery-boxy",
         "https://www.gengar.cz/pokemon",                 # 15 strán – všetko sealed aj nové sety
         "https://www.gengar.cz/pokemon-ohodnocene-karty",
         "https://www.gengar.cz/vintage-produkty",
         "https://www.gengar.cz/alba-a-obaly",            # sleeves, albumy (len pri hľadaní príslušenstva)
         "https://www.gengar.cz/kusove-karty",            # ~231 strán single kariet – ako posledné
     ],
     # karty majú 231 strán, preto sa celý katalóg obnovuje raz za 3 hodiny (šetrne k obchodu)
     "max_pages": 240, "refresh_factor": 3},
]

# Návrhy na otestovanie v /admin/obchody (nič sa nezapne samo)
KANDIDATI = [
    ("Veselý drak", "https://www.vesely-drak.cz/"),
    ("Najáda", "https://www.najada.games/"),
    ("Blackfire", "https://www.blackfire.cz/"),
    ("Xzone CZ", "https://www.xzone.cz/"),
    ("Xzone SK", "https://www.xzone.sk/"),
]

PLATFORM_PRESETS = {
    "shoptet": {"marker": re.compile(r"shoptet", re.I),
                "search": ["/vyhladavanie/?string={q}", "/vyhledavani/?string={q}"],
                "selector": "div.product a.name"},
    "shopify": {"marker": re.compile(r"cdn\.shopify\.com|Shopify\.theme|/cdn/shop/", re.I),
                "search": ["/search?q={q}&type=product", "/search?q={q}"],
                "selector": 'a[href*="/products/"]'},
    "upgates": {"marker": re.compile(r"upgates", re.I),
                "search": ["/vyhledavani?q={q}", "/vyhladavanie?q={q}", "/search?q={q}", "/hledani?q={q}"],
                "selector": 'a[href*="/p/"]'},
    "woocommerce": {"marker": re.compile(r"woocommerce", re.I),
                    "search": ["/?s={q}&post_type=product"],
                    "selector": "li.product a.woocommerce-LoopProduct-link, a.woocommerce-loop-product__link"},
}


def is_catalog(shop):
    return bool(shop.get("catalog") or shop.get("feed") or shop.get("shopify_catalog"))


def is_local(shop):
    """Hľadá sa len v našej databáze (žiadny internet) – môže bežať hneď v požiadavke."""
    return is_catalog(shop) and not shop.get("search_url")


def shop_query(query):
    """Čo poslať do vyhľadávania obchodu. Kratšie = obchod vráti viac (napr. „pitch black“
    namiesto „pitch black elite trainer box“, lebo obchod môže písať len „ETB“).
    Presný typ produktu, set a číslo karty sa potom vyfiltrujú u nás."""
    p = L.normalize_query(query)
    set_name = p.get("set_name") or ""
    if L.is_accessory_query(query):   # „pikachu sleeves“ – „sleeves“ sa nesmie stratiť
        q = p.get("normalized") or query
    elif set_name and set_name not in L.SET_PARTS and set_name not in L.SERIE:
        q = " ".join(x for x in (set_name, p.get("pokemon")) if x)
    elif p.get("pokemon") and not p.get("card_number"):
        q = " ".join(x for x in (p.get("pokemon"), p.get("suffix")) if x)
    else:
        q = p.get("normalized") or ""
    return L.clean_text(q) or query


def host_of(url):
    return urllib.parse.urlparse(url or "").netloc.lower()


# =========================================================
# HTTP
# =========================================================

BOT_UA = "Mozilla/5.0 (compatible; CardRadarBot/1.0; +https://getcardradar.com/pre-obchody)"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_ua = os.environ.get("CRAWLER_UA", "").strip()
HEADERS = {
    "User-Agent": BROWSER_UA if _ua.lower() == "browser" else (_ua or BOT_UA),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "sk-SK,cs-CZ;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}

# Jedno zdieľané HTTP spojenie pre všetky vlákna – tak to fungovalo od začiatku a obchody to
# akceptujú (menej nových spojení ako pri samostatnom spojení pre každé vlákno).
_session = None
_session_lock = threading.Lock()


def http():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                s.headers.update(HEADERS)
                adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=50)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _session = s
    return _session


def fetch(url, timeout=SEARCH_TIMEOUT):
    """(odpoveď alebo None, info). Pri chybe nikdy nevyhodí výnimku."""
    start = time.monotonic()
    info = {"url": url, "http_status": None, "status": "http_ok", "error": ""}
    resp = None
    try:
        r = http().get(url, timeout=(4, timeout), allow_redirects=True)
        info["http_status"] = r.status_code
        if r.status_code == 200:
            resp = r
        else:
            info.update(status="http_error", error=f"HTTP {r.status_code}")
    except requests.Timeout:
        info.update(status="timeout", error="Obchod neodpovedal včas")
    except Exception as e:
        info.update(status="request_error", error=str(e)[:200])
    info["elapsed_ms"] = round((time.monotonic() - start) * 1000)
    return resp, info


# Zdieľané vlákna
SHOP_POOL = ThreadPoolExecutor(max_workers=40, thread_name_prefix="shop")
IMAGE_POOL = ThreadPoolExecutor(max_workers=10, thread_name_prefix="img")
# REFRESH_POOL: obnova starších výsledkov na pozadí.
# JSON_POOL: drobné požiadavky (Shopify sklad / obrázky). Úlohy v ňom už nič ďalšie
# nespúšťajú – predtým jeden pool čakal sám na seba a zasekol celé hľadanie.
REFRESH_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="refresh")
JSON_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="json")
SMALL_POOL = JSON_POOL   # starý názov (spätná kompatibilita)
BG_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db")   # zápisy do DB po jednom


class TTLCache:
    def __init__(self, ttl, max_items):
        self.ttl, self.max_items = ttl, max_items
        self._d, self._lock = {}, threading.Lock()

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
                self._d.pop(min(self._d, key=lambda k: self._d[k][0]), None)
            self._d[key] = (time.monotonic(), copy.deepcopy(value))

    def clear(self):
        with self._lock:
            self._d.clear()

    def __len__(self):
        return len(self._d)


# =========================================================
# DATABÁZA
# DB_PATH (napr. /var/data/cardradar.db) = trvalý disk na Renderi.
# Pri prvom štarte s diskom sa doň prenesú doterajšie dáta.
# =========================================================

_DEFAULT_DB = os.path.join(BASE_DIR, "cardradar.db")
DB_PATH = _DEFAULT_DB
DB_WARNING = ""


def _choose_db():
    global DB_PATH, DB_WARNING
    want = os.environ.get("DB_PATH", "").strip()
    if not want or os.path.abspath(want) == os.path.abspath(_DEFAULT_DB):
        return
    try:
        folder = os.path.dirname(want)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if not os.path.exists(want) and os.path.exists(_DEFAULT_DB):
            src, dst = sqlite3.connect(_DEFAULT_DB), sqlite3.connect(want)
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        DB_PATH = want
        print(f"[CardRadar] Databáza na disku: {want}", flush=True)
        return
    except Exception as e:
        DB_WARNING = f"DB_PATH={want} sa nedá použiť: {e}"
        print(f"[CardRadar] VAROVANIE: {DB_WARNING}. Používam {_DEFAULT_DB}.", flush=True)


def db_problem():
    """Text problému s databázou alebo '' (história cien by sa pri nasadení stratila)."""
    if DB_WARNING:
        return DB_WARNING
    if not db_persistent():
        return ("DB_PATH nie je nastavená – databáza je v priečinku aplikácie a pri každom nasadení "
                "sa zmaže (história cien, šípky, zľavy, strážcovia). Na Renderi pridaj Disk "
                "(napr. /var/data) a premennú DB_PATH=/var/data/cardradar.db.")
    return ""


def db_persistent():
    want = os.environ.get("DB_PATH", "").strip()
    return bool(want) and os.path.abspath(want) == os.path.abspath(DB_PATH)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def today_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def init_db():
    _choose_db()
    conn = db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS price_daily (
                link TEXT NOT NULL, day TEXT NOT NULL,
                shop TEXT, title TEXT, price_eur REAL, stock TEXT, image TEXT,
                PRIMARY KEY (link, day));
            CREATE INDEX IF NOT EXISTS idx_pd_day ON price_daily(day);
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL, link TEXT NOT NULL, title TEXT, shop TEXT,
                target REAL NOT NULL, token TEXT UNIQUE NOT NULL,
                confirmed INTEGER DEFAULT 0, created TEXT, site TEXT,
                last_price REAL, last_checked TEXT, notified TEXT);
            CREATE INDEX IF NOT EXISTS idx_alerts_email ON alerts(email);
            CREATE TABLE IF NOT EXISTS search_log (
                day TEXT NOT NULL, query TEXT NOT NULL, n INTEGER DEFAULT 1,
                PRIMARY KEY (day, query));
            CREATE TABLE IF NOT EXISTS catalog_items (
                shop TEXT NOT NULL, link TEXT NOT NULL, title TEXT,
                price_eur REAL, image TEXT, stock TEXT,
                PRIMARY KEY (shop, link));
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS clicks (
                day TEXT NOT NULL, shop TEXT NOT NULL, n INTEGER DEFAULT 1,
                PRIMARY KEY (day, shop));
        """)
        # staršie databázy nemali stĺpec image / price_czk
        cols = {r[1] for r in conn.execute("PRAGMA table_info(price_daily)")}
        if "image" not in cols:
            conn.execute("ALTER TABLE price_daily ADD COLUMN image TEXT")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(catalog_items)")}
        if "price_czk" not in cols:
            conn.execute("ALTER TABLE catalog_items ADD COLUMN price_czk REAL")
        _fix_czk_once(conn)
        _fix_prices_once(conn)
        _drop_implausible(conn)
        _prune(conn)
        conn.commit()
    finally:
        conn.close()
    _load_czk()


def _drop_implausible(conn):
    """Zmaže z histórie a katalógu nezmyselné ceny (napr. 21 929 € za kartu)."""
    for table in ("price_daily", "catalog_items"):
        rows = conn.execute(f"SELECT rowid, title, price_eur FROM {table} WHERE price_eur > 2000").fetchall()
        bad = [(rid,) for rid, title, price in rows if not L.price_plausible(title or "", price)]
        if bad:
            conn.executemany(f"DELETE FROM {table} WHERE rowid = ?", bad)
            print(f"[CardRadar] {table}: zmazaných {len(bad)} nezmyselných cien", flush=True)


def _fix_czk_once(conn):
    """Jednorazovo: ceny z CZ obchodov boli zle prečítané (napr. „1 299,- Kč“ -> 0,08 €).
    Zmaže ich históriu a katalóg, aby sa načítali nanovo so správnym prepočtom."""
    if conn.execute("SELECT v FROM meta WHERE k = 'fix_czk_v1'").fetchone():
        return
    cz = [s["name"] for s in SHOPS if s.get("country") == "CZ"]
    if cz:
        marks = ",".join("?" * len(cz))
        conn.execute(f"DELETE FROM price_daily WHERE shop IN ({marks})", cz)
        conn.execute(f"DELETE FROM catalog_items WHERE shop IN ({marks})", cz)
        conn.execute(f"DELETE FROM meta WHERE k IN ({','.join('?' * len(cz))})", ["catalog:" + n for n in cz])
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('fix_czk_v1', ?)",
                 (datetime.now(timezone.utc).isoformat(),))


def _fix_prices_once(conn):
    """Jednorazovo (8. 10. 2026): verzia s chybou „135 €210“ ukladala zlé ceny (Beardex a ďalšie
    obchody s „€“ pred číslom). Zmaže dnešnú históriu a vynúti nové prejdenie katalógov."""
    if conn.execute("SELECT v FROM meta WHERE k = 'fix_prices_v2'").fetchone():
        return
    conn.execute("DELETE FROM price_daily WHERE day >= '2026-10-08'")
    conn.execute("DELETE FROM meta WHERE k LIKE 'catalog:%'")   # katalógy sa prejdú nanovo do pár minút
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('fix_prices_v2', ?)",
                 (datetime.now(timezone.utc).isoformat(),))


def _prune(conn):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    conn.execute("DELETE FROM price_daily WHERE day < ?", (cutoff,))


def meta_get(k):
    conn = db()
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def meta_set(k, v):
    conn = db()
    try:
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))
        conn.commit()
    finally:
        conn.close()


# =========================================================
# OBCHODY ZAPNUTÉ CEZ /admin/obchody (uložené v databáze)
# Všetky procesy servera si zmenu prevezmú do 30 sekúnd.
# =========================================================

_extra = {"v": None, "t": 0.0, "shops": [], "hosts": set()}
_extra_lock = threading.Lock()


def extra_shops():
    try:
        return json.loads(meta_get("extra_shops") or "[]")
    except Exception:
        return []


def save_extra_shops(shops):
    meta_set("extra_shops", json.dumps(shops, ensure_ascii=False))
    meta_set("extra_shops_v", str(time.time()))
    _extra["t"] = 0   # tento proces hneď


def _sync_extra():
    if time.monotonic() - _extra["t"] < 30:
        return
    with _extra_lock:
        _extra["t"] = time.monotonic()
        try:
            v = meta_get("extra_shops_v")
        except Exception:
            return
        if v == _extra["v"]:
            return
        _extra["v"] = v
        builtin = {host_of(s["base_url"]).replace("www.", "") for s in SHOPS}
        _extra["shops"] = [dict(s, enabled=True, _extra=True) for s in extra_shops()
                           if host_of(s.get("base_url")).replace("www.", "") not in builtin]
        _extra["hosts"] = {host_of(s["base_url"]) for s in _extra["shops"]}


def all_shops():
    _sync_extra()
    return SHOPS + _extra["shops"]


def active_shops():
    return [s for s in all_shops() if s.get("enabled", True)]


def is_allowed_link(url):
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return False
    hosts = {host_of(s["base_url"]) for s in all_shops()}
    return p.scheme in ("http", "https") and p.netloc.lower() in hosts


def shop_by_link(url):
    host = host_of(url).replace("www.", "")
    return next((s for s in all_shops() if host_of(s["base_url"]).replace("www.", "") == host), None)


def add_shipping(shop, r):
    ship = L.shipping_eur(shop, r.get("price_eur"))
    r["shipping_eur"] = ship
    r["total_eur"] = round(r["price_eur"] + ship, 2) if ship is not None else None


# =========================================================
# ODCHOD DO OBCHODU (/go): utm parametre, partnerský odkaz, počítanie klikov
# =========================================================

def go_link(link):
    """Adresa tlačidla „Do obchodu“ na webe (prejde cez /go a započíta klik)."""
    return "/go?u=" + urllib.parse.quote(link or "", safe="")


def out_url(link, medium="referral"):
    """Skutočná adresa v obchode: partnerský odkaz, inak odkaz s utm_source=cardradar."""
    shop = shop_by_link(link) or {}
    tpl = shop.get("affiliate")
    if tpl and "{url}" in tpl:
        return tpl.replace("{url}", urllib.parse.quote(link, safe=""))
    try:
        p = urllib.parse.urlsplit(link)
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if not k.startswith("utm_")]
        q += [("utm_source", "cardradar"), ("utm_medium", medium), ("utm_campaign", "porovnanie")]
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))
    except Exception:
        return link


def _count_click_now(shop_name):
    conn = db()
    try:
        conn.execute("""INSERT INTO clicks (day, shop, n) VALUES (?, ?, 1)
                        ON CONFLICT(day, shop) DO UPDATE SET n = n + 1""", (today_str(), shop_name))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def count_click(link):
    shop = shop_by_link(link)
    if shop:
        BG_POOL.submit(_count_click_now, shop["name"])


def click_stats(days=30):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = db()
    try:
        rows = conn.execute("SELECT shop, SUM(n) FROM clicks WHERE day >= ? GROUP BY shop ORDER BY 2 DESC",
                            (since,)).fetchall()
    finally:
        conn.close()
    return [{"shop": s, "clicks": n} for s, n in rows]


# =========================================================
# ČÍTANIE STRÁNOK OBCHODU
# =========================================================

IMG_ATTRS = ["src", "data-src", "data-lazy-src", "data-original",
             "data-image", "data-image-src", "data-original-src"]
_PLACEHOLDER_RE = re.compile(r"loading|placeholder|blank|spacer|lazy[-_]?load|1x1|pixel\.", re.I)
_TRACKING_RE = re.compile(
    r"^(?:_pos|_sid|_ss|_psq|_fid|_v|utm_\w+|fbclid|gclid|srsltid|ref|variant_id|hgtid|hgid)$", re.I)
_STRIKE_SELECTOR = ("del, s, strike, [class*='old'], [class*='before'], [class*='original'], "
                    "[class*='crossed'], [class*='strike'], [class*='standard'], "
                    "[class*='compare'], [class*='regular']")
_HIDDEN_CLASSES = {"hidden", "hide", "d-none", "is-hidden", "u-hidden"}


def abs_url(base, href):
    href = (href or "").strip()
    if not href or href.startswith(("javascript:", "#")):
        return ""
    return urllib.parse.urljoin(base, href)


def clean_link(url):
    """Odkaz bez sledovacích parametrov – ten istý produkt má vždy rovnakú adresu."""
    if not url:
        return url
    try:
        p = urllib.parse.urlsplit(url)
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
             if not _TRACKING_RE.match(k)]
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))
    except Exception:
        return url


def img_url(img, base):
    """Adresa obrázka z <img> (aj lazy-load), bez zástupných obrázkov."""
    for attr in IMG_ATTRS:
        v = L.clean_text(img.get(attr, ""))
        if v and not v.startswith("data:image/") and not _PLACEHOLDER_RE.search(v):
            u = abs_url(base, v)
            if u:
                return u
    for attr in ("srcset", "data-srcset"):
        cands = []
        for part in L.clean_text(img.get(attr, "")).split(","):
            pieces = L.clean_text(part).split()
            if not pieces or _PLACEHOLDER_RE.search(pieces[0]):
                continue
            m = re.search(r"(\d+)w", pieces[1]) if len(pieces) > 1 else None
            u = abs_url(base, pieces[0])
            if u:
                cands.append((int(m.group(1)) if m else 0, u))
        if cands:
            return max(cands)[1]
    return ""


def extract_title(a):
    t = L.clean_text(a.get_text(" ", strip=True))
    if t:
        return t
    for attr in ("title", "aria-label"):
        if L.clean_text(a.get(attr, "")):
            return L.clean_text(a.get(attr))
    img = a.find("img")
    if img:
        return L.clean_text(img.get("alt") or img.get("title") or "")
    return ""


def extract_image(a, base):
    el, img = a, a.find("img")
    for _ in range(3):
        if img is not None or el is None:
            break
        el = el.parent
        img = el.find("img") if el is not None else None
    return img_url(img, base) if img is not None else ""


def _has_other_product(el, own_href):
    """Obsahuje prvok odkaz na INÝ produkt? (potom to už nie je dlaždica jedného produktu)"""
    own = (own_href or "").lower().rstrip("/").split("?")[0]
    for x in el.find_all("a", href=True):
        h = x["href"].lower().rstrip("/").split("?")[0]
        if own and (h == own or own.endswith(h) or h.endswith(own)):
            continue
        if L.is_tcg_product(extract_title(x)):
            return True
    return False


def find_block(a):
    """Dlaždica produktu okolo odkazu (najbližší rodič s cenou), bez prečiarknutej ceny.
    Nikdy nevystúpi tak vysoko, aby obsahovala iný produkt – inak by sa zobrala jeho cena."""
    cur, best = a, None
    own = urllib.parse.urlsplit(a.get("href") or "").path
    for level in range(1, 7):
        cur = cur.parent
        if not cur:
            break
        if _has_other_product(cur, own):
            break
        text = L.clean_text(cur.get_text(" ", strip=True))
        if text and any(c in text for c in ("€", "Kč", "CZK")) and len(text) < 1800:
            best = cur
            if level >= 2:
                break
    el = best or a.parent
    if el is None:
        return None
    try:
        if el.select(_STRIKE_SELECTOR):
            c = copy.copy(el)
            for old in c.select(_STRIKE_SELECTOR):
                old.decompose()
            if L.parse_price(c.get_text(" ", strip=True)) is not None:
                return c
    except Exception:
        pass
    return el


def _hidden(tag):
    if tag.has_attr("hidden") or str(tag.get("aria-hidden", "")).lower() == "true":
        return True
    if _HIDDEN_CLASSES & set(tag.get("class") or []):
        return True
    style = str(tag.get("style", "")).replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def visible_text(el):
    if el is None:
        return ""
    parts = []
    for s in el.find_all(string=True):
        p, hidden = s.parent, False
        while p is not None and p is not el:
            if getattr(p, "name", None) in ("script", "style", "template", "noscript") or _hidden(p):
                hidden = True
                break
            p = p.parent
        if not hidden:
            parts.append(str(s))
    return L.clean_text(" ".join(parts))


_CART_BTN = 'button[name="add"], button[type="submit"], .add-to-cart, .btn-cart, .btn-add-to-cart'


def detect_stock_el(el):
    """Sklad z dlaždice: tlačidlo košíka, potom viditeľný text."""
    if el is None:
        return ""
    for btn in el.select(_CART_BTN):
        if btn.has_attr("disabled") or "disabled" in (btn.get("class") or []):
            return "preorder" if L.STOCK_PRE_RE.search(btn.get_text(" ", strip=True)) else "out"
    stock = L.detect_stock(visible_text(el))
    if stock:
        return stock
    for btn in el.select(_CART_BTN):
        label = visible_text(btn).lower()
        if L.STOCK_PRE_RE.search(label):
            return "preorder"
        if re.search(r"do\s+ko[šs][íi]ka|add\s+to\s+cart|koupit|k[úu]pi[ťt]", label):
            return "in"
    if L.COMING_RE.search(el.get_text(" ", strip=True)):
        return "preorder"
    return ""


def _new_debug(shop, query):
    return {"shop": shop["name"], "query": query, "url": "", "status": "starting",
            "http_status": None, "results": 0, "links_scanned": 0, "merch_filtered": 0,
            "match_filtered": 0, "accepted": 0, "elapsed_ms": 0, "cache": "", "error": "",
            "sample_decisions": []}


def _log(debug, **entry):
    if len(debug["sample_decisions"]) < 20:
        debug["sample_decisions"].append(entry)


def _matches(shop, title, extra, parsed, kind):
    # príslušenstvo (sleeves, album...): pri hľadaní príslušenstva vždy, inak len ako záložka „Príslušenstvo“
    if L.is_accessory_query(parsed.get("original")) or (not L.is_tcg_product(title) and L.is_accessory(title)):
        return L.accessory_matches_query(title, parsed)
    if kind == "card":
        return L.card_matches_query(title, extra, parsed, loose_set=shop.get("loose_set", False))
    return L.sealed_matches_query(title, extra, parsed)


# =========================================================
# OBCHOD S VYHĽADÁVANÍM
# =========================================================

MAX_SEARCH_PAGES = 5        # koľko strán výsledkov vyhľadávania obchodu prejdeme
SEARCH_PAGES_BUDGET = 12    # sekúnd na všetky strany spolu
_page_cache = TTLCache(600, 120)   # stiahnuté stránky vyhľadávania (10 min)


def fetch_page(url, timeout=SEARCH_TIMEOUT):
    """(html alebo None, info) – rovnaká stránka sa 10 min nesťahuje znova."""
    hit = _page_cache.get(url)
    if hit is not None:
        return hit, {"url": url, "http_status": 200, "status": "http_ok", "error": "", "elapsed_ms": 0, "cache": True}
    resp, info = fetch(url, timeout)
    if not resp:
        return None, info
    html = resp.text
    _page_cache.set(url, html)
    return html, info


def scrape_search(shop, query, timeout=SEARCH_TIMEOUT, fetch_q=None):
    """fetch_q = text pre vyhľadávanie obchodu (kratší); query = čo naozaj hľadáme (filter)."""
    start = time.monotonic()
    debug = _new_debug(shop, query)
    results = []
    fetch_q = fetch_q or query
    # Shopify: JSON so skladom sa sťahuje súčasne s vyhľadávaním
    shopify_fut = JSON_POOL.submit(_shopify_suggest_raw, shop, fetch_q) if shop.get("shopify") else None
    try:
        url = shop["search_url"].format(q=urllib.parse.quote(fetch_q))
        debug["url"] = url
        html, info = fetch_page(url, timeout)
        debug["http_status"] = info["http_status"]
        debug["fetch_ms"] = info.get("elapsed_ms")
        if not html:
            debug.update(status=info["status"], error=info["error"])
            return results, debug

        soup = BeautifulSoup(html, HTML_PARSER)
        links = soup.select(shop["link_selector"])
        # ďalšie strany výsledkov (odkaz „ďalšia strana“ priamo z obchodu – funguje pre každú platformu)
        page_url, n, visited, host = url, 1, {url}, host_of(shop["base_url"])
        while (n < MAX_SEARCH_PAGES and len(links) >= 8 * n
               and time.monotonic() - start < SEARCH_PAGES_BUDGET):
            nxt = _next_page(soup, page_url, n, host)
            if not nxt or nxt in visited:
                break
            visited.add(nxt)
            html2, _ = fetch_page(nxt, timeout)
            if not html2:
                break
            soup = BeautifulSoup(html2, HTML_PARSER)
            more = soup.select(shop["link_selector"])
            if not more:
                break
            links += more
            page_url, n = nxt, n + 1
        debug["pages"] = n
        debug["links_scanned"] = len(links)
        parsed = L.normalize_query(query)
        kind = L.classify_query(parsed)
        acc_query = L.is_accessory_query(query)
        seen = set()

        for a in links:
            href = clean_link(abs_url(shop["base_url"], a.get("href")))
            if not href or href.lower().rstrip("/") in seen:
                continue
            seen.add(href.lower().rstrip("/"))
            title = extract_title(a)
            if not title:
                continue
            why = "" if L.is_accessory(title) else \
                (L.merch_reason(title) or ("" if L.looks_like_tcg(title) else "not_tcg"))
            if acc_query and not L.is_accessory(title):
                why = why or "not_accessory"
            if why:
                debug["merch_filtered"] += 1
                _log(debug, title=title, decision="filtered", reason=why)
                continue
            block = find_block(a)
            block_text = L.clean_text(block.get_text(" ", strip=True)) if block is not None else ""
            ok, reason = _matches(shop, title, block_text, parsed, kind)
            if not ok:
                debug["match_filtered"] += 1
                _log(debug, title=title, decision="filtered", reason=reason)
                continue
            amount, cur = L.parse_price_raw(block_text, title)
            if amount is None and a.parent:
                amount, cur = L.parse_price_raw(a.parent.get_text(" ", strip=True), title)
            if not amount or amount <= 0:
                _log(debug, title=title, decision="filtered", reason="no_price")
                continue
            price_czk = amount if cur == "CZK" else None
            price = L.czk_to_eur(amount) if price_czk else amount
            if not L.price_plausible(title, price):
                _log(debug, title=title, decision="filtered", reason=f"price_implausible:{amount} {cur}")
                continue
            results.append(L.make_result(shop, title, price, href,
                                         extract_image(a, shop["base_url"]), detect_stock_el(block),
                                         price_czk=price_czk))
            _log(debug, title=title, price_eur=round(price, 2), decision="accepted")

        if shopify_fut and results:
            try:
                _shopify_enrich(shop, results, shopify_fut.result(timeout=4))
            except Exception:
                pass
        results.sort(key=lambda r: r["price_eur"])
        debug.update(results=len(results), accepted=len(results),
                     status="ok" if results else "no_results", fetched_at=time.time())
    except Exception as e:
        debug.update(status="parser_error", error=str(e)[:200])
    finally:
        debug["elapsed_ms"] = round((time.monotonic() - start) * 1000)
    return results, debug


# ---------- Shopify (CardyX): sklad a obrázky z JSON ----------

def _shopify_img(url, width):
    if url and ("/cdn/shop/" in url or "cdn.shopify.com" in url) and "width=" not in url:
        url += ("&" if "?" in url else "?") + f"width={width}"
    return url


def _shopify_suggest_raw(shop, query, timeout=4):
    url = (shop["base_url"].rstrip("/") + "/search/suggest.json?q=" + urllib.parse.quote(query)
           + "&resources[type]=product&resources[limit]=10"
           + "&resources[options][unavailable_products]=last")
    resp, _ = fetch(url, timeout)
    if not resp:
        return None
    try:
        return resp.json()["resources"]["results"]["products"] or []
    except Exception:
        return None


def _shopify_product_image(p, base, width):
    img = p.get("image") or ""
    if not img and isinstance(p.get("featured_image"), dict):
        img = p["featured_image"].get("url", "")
    return _shopify_img(abs_url(base, img), width) if img else ""


def _shopify_enrich(shop, results, products):
    info = {}
    for p in products or []:
        path = urllib.parse.urlsplit(p.get("url") or "").path.rstrip("/").lower()
        if path:
            info[path] = (p.get("available"), _shopify_product_image(p, shop["base_url"], 400))
    for r in results:
        hit = info.get(urllib.parse.urlsplit(r["link"]).path.rstrip("/").lower())
        if not hit:
            continue
        available, img = hit
        if not r["stock"] and available is not None:
            r["stock"] = "in" if available else "out"
        if not r["image"] and img:
            r["image"] = img
    # mimo prvých 10: /products/<handle>.js, najviac 8 naraz
    rest = [r for r in results if not r["stock"] and "/products/" in r["link"]][:8]
    if rest:
        futs = [JSON_POOL.submit(_shopify_product_js, r) for r in rest]
        wait(futs, timeout=5)   # nikdy nečaká donekonečna; čo nestihne, ostane bez skladu


def _shopify_product_js(r):
    try:
        u = urllib.parse.urlsplit(r["link"])
        resp, _ = fetch(f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}.js", timeout=3)
        if not resp:
            return
        d = resp.json()
        if d.get("available") is not None:
            r["stock"] = "in" if d["available"] else "out"
        img = d.get("featured_image") or ""
        if img and not r["image"]:
            r["image"] = "https:" + img if img.startswith("//") else img
    except Exception:
        pass


# =========================================================
# KATALÓGOVÉ OBCHODY
# Raz za hodinu sa prejdú kategórie (aj ďalšie strany), produkty sa uložia
# do databázy a pri hľadaní sa filtruje lokálne = okamžité hľadanie.
# =========================================================

_cat_mem = {}            # obchod -> (monotonic, položky, čas aktualizácie)
_crawl_sem = threading.Semaphore(1)
_cat_lock = threading.Lock()
_crawling = set()


def _save_catalog(shop_name, items):
    now = datetime.now(timezone.utc).isoformat()
    conn = db()
    try:
        conn.execute("DELETE FROM catalog_items WHERE shop = ?", (shop_name,))
        conn.executemany(
            "INSERT OR REPLACE INTO catalog_items (shop, link, title, price_eur, image, stock, price_czk) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(shop_name, i["link"], i["title"], i["price_eur"], i["image"], i["stock"], i.get("price_czk"))
             for i in items])
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", ("catalog:" + shop_name, now))
        conn.commit()
    finally:
        conn.close()
    with _cat_lock:
        _cat_mem.pop(shop_name, None)
    return now


def load_catalog(shop_name):
    with _cat_lock:
        hit = _cat_mem.get(shop_name)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1], hit[2]
    conn = db()
    try:
        rows = conn.execute("SELECT link, title, price_eur, image, stock, price_czk FROM catalog_items "
                            "WHERE shop = ?", (shop_name,)).fetchall()
        meta = conn.execute("SELECT v FROM meta WHERE k = ?", ("catalog:" + shop_name,)).fetchone()
    except Exception:
        rows, meta = [], None
    finally:
        conn.close()
    items = [{"link": r[0], "title": r[1], "price_eur": r[2], "image": r[3] or "", "stock": r[4] or "",
              "price_czk": r[5], "_f": L.fold(r[1] or "")} for r in rows]   # _f = názov bez diakritiky (predfilter)
    updated = meta[0] if meta else ""
    with _cat_lock:
        _cat_mem[shop_name] = (time.monotonic(), items, updated)
    return items, updated


def _is_stale(updated, factor=1.0):
    try:
        t = datetime.fromisoformat(updated)
    except (TypeError, ValueError):
        return True
    return datetime.now(timezone.utc) - t > timedelta(minutes=CATALOG_REFRESH_MIN * factor)


def _other_links(block, href, page_url):
    own, other = href.lower().rstrip("/"), set()
    for a in block.find_all("a", href=True):
        h = clean_link(abs_url(page_url, a["href"]))
        if h and h.lower().rstrip("/") != own:
            other.add(h.lower().rstrip("/"))
    return len(other)


def _parse_listing(shop, html, page_url, debug=None):
    soup = BeautifulSoup(html, HTML_PARSER)
    host = host_of(shop["base_url"])
    by_href = {}
    for a in soup.find_all("a", href=True):
        href = clean_link(abs_url(page_url, a["href"]))
        p = urllib.parse.urlparse(href) if href else None
        if p and p.netloc.lower() == host and p.path not in ("", "/"):
            by_href.setdefault(href.lower().rstrip("/"), (href, []))[1].append(a)

    items = []
    for href, anchors in by_href.values():
        named = [(len(t), t, a) for a in anchors for t in [extract_title(a)]
                 if 6 <= len(t) <= 200 and L.is_listed_product(t)]
        if not named:
            continue
        _, title, a = min(named, key=lambda x: x[0])
        block = find_block(a)
        if block is None or _other_links(block, href, page_url) > 3:
            continue   # menu, päta, zoznam – nie dlaždica produktu
        amount, cur = L.parse_price_raw(block.get_text(" ", strip=True), title)
        if not amount or amount <= 0:
            continue
        price_czk = amount if cur == "CZK" else None
        price = L.czk_to_eur(amount) if price_czk else amount
        if not L.price_plausible(title, price):
            if debug is not None:
                debug.append({"title": title, "rejected": f"nezmyselná cena {amount} {cur}",
                              "text": L.clean_text(block.get_text(" ", strip=True))[:300]})
            continue
        if debug is not None:
            debug.append({"title": title, "price": amount, "currency": cur, "price_eur": round(price, 2),
                          "text": L.clean_text(block.get_text(" ", strip=True))[:300]})
        image = extract_image(a, page_url)
        if image and shop.get("image_replace"):
            image = image.replace(*shop["image_replace"])
        items.append({"link": href, "title": title, "price_eur": round(price, 2), "price_czk": price_czk,
                      "image": image, "stock": detect_stock_el(block)})
    return items, soup


def _next_page(soup, page_url, n, host):
    el = soup.select_one('link[rel~="next"], a[rel~="next"]')
    if el is not None and el.get("href"):
        u = abs_url(page_url, el["href"])
        if host_of(u) == host:
            return u
    pat = re.compile(r"(?:[?&](?:page|strana|stranka|p|pg)=|/strana-|/page[/-]?|/pg-)" + str(n + 1) + r"(?!\d)", re.I)
    for a in soup.find_all("a", href=True):
        if pat.search(a["href"]):
            u = abs_url(page_url, a["href"])
            if host_of(u) == host:
                return u
    return None


def debug_listing(shop, url=None):
    """Pre admina: ako sa prečítala jedna strana katalógu (názov, cena, text dlaždice)."""
    url = url if url and host_of(url) == host_of(shop["base_url"]) else shop["catalog"][0]
    resp, info = fetch(url, timeout=15)
    if not resp:
        return {"url": url, "error": info["error"] or info["status"]}
    rows = []
    _parse_listing(shop, resp.text, url, debug=rows)
    return {"url": url, "kurz_czk": L.KURZ["CZK"], "items": rows}


def crawl_shop(shop):
    """Prejde katalóg obchodu a uloží ho. Vráti prehľad (pre admin)."""
    if shop.get("feed"):
        return crawl_feed(shop)
    if shop.get("shopify_catalog"):
        return crawl_shopify(shop)
    start = time.monotonic()
    host = host_of(shop["base_url"])
    items, pages, errors = {}, 0, []
    first_load = not load_catalog(shop["name"])[0]

    for url in shop["catalog"]:
        n, visited, prev_links = 1, set(), None
        while url and n <= shop.get("max_pages", 10) and url not in visited:
            visited.add(url)
            resp, info = fetch(url, timeout=15)
            if not resp:
                errors.append({"url": url, "error": info["error"] or info["status"]})
                break
            found, soup = _parse_listing(shop, resp.text, url)
            pages += 1
            page_links = frozenset(it["link"] for it in found)
            for it in found:
                items.setdefault(it["link"], it)
            # koniec kategórie: prázdna strana alebo tá istá strana znova (obchod vrátil 1. stranu).
            # Predtým: koniec, keď strana nemala NOVÉ produkty – súhrnná kategória (napr. /pokemon)
            # s produktmi z predošlých kategórií sa tak prestala čítať po 2. strane.
            if n > 1 and (not page_links or page_links == prev_links):
                break
            prev_links = page_links
            url = _next_page(soup, url, n, host)
            n += 1
            if url:
                time.sleep(PAGE_DELAY)
        if first_load and items:
            _save_catalog(shop["name"], list(items.values()))   # hľadateľné hneď po 1. kategórii

    result = list(items.values())
    updated = ""
    if result:   # pri chybe ostane starý katalóg
        updated = _save_catalog(shop["name"], result)
        save_history([dict(r, shop=shop["name"]) for r in result], log_query=None)
    return {"shop": shop["name"], "items": len(result), "pages": pages, "errors": errors,
            "updated": updated, "elapsed_ms": round((time.monotonic() - start) * 1000),
            "sample": result[:10]}


def _shopify_items(shop, products, pokemon_only=False):
    base = shop["base_url"].rstrip("/")
    items = []
    for p in products or []:
        title = L.clean_text(p.get("title"))
        if not title or not p.get("handle"):
            continue
        if pokemon_only:
            meta = L.fold(" ".join([title, str(p.get("product_type") or ""), " ".join(p.get("tags") or [])
                                    if isinstance(p.get("tags"), list) else str(p.get("tags") or ""),
                                    str(p.get("vendor") or "")]))
            if "pokemon" not in meta:
                continue
        if not L.is_listed_product(title):
            continue
        variants = p.get("variants") or []
        avail = [v for v in variants if v.get("available")]
        prices = [L.to_float(v.get("price")) for v in (avail or variants)]
        prices = [x for x in prices if x]
        if not prices:
            continue
        price = min(prices)
        if not L.price_plausible(title, price):
            continue
        imgs = p.get("images") or []
        img = imgs[0].get("src", "") if imgs and isinstance(imgs[0], dict) else ""
        items.append({"link": f"{base}/products/{p['handle']}", "title": title, "price_eur": round(price, 2),
                      "price_czk": None, "image": _shopify_img(img, 400) if img else "",
                      "stock": "in" if avail else "out"})
    return items


def crawl_shopify(shop):
    """Celý katalóg Shopify obchodu cez verejný JSON (/collections/<x>/products.json).
    Ak kolekcia neexistuje, skúsi /products.json a nechá len Pokémon produkty."""
    start = time.monotonic()
    items, errors, pages = {}, [], 0
    sources = [(c.rstrip("/") + "/products.json", False) for c in shop["shopify_catalog"]]
    for src, pokemon_only in sources + [(shop["base_url"].rstrip("/") + "/products.json", True)]:
        if items and pokemon_only:
            break   # kolekcie fungovali, celý obchod netreba
        for page in range(1, 41):
            resp, info = fetch(f"{src}?limit=250&page={page}", timeout=20)
            if not resp:
                errors.append({"url": src, "page": page, "error": info["error"] or info["status"]})
                break
            try:
                products = resp.json().get("products") or []
            except Exception as e:
                errors.append({"url": src, "page": page, "error": "nie je JSON: " + str(e)[:80]})
                break
            pages += 1
            if not products:
                break
            for it in _shopify_items(shop, products, pokemon_only):
                items[it["link"]] = it
            if len(products) < 250:
                break
            time.sleep(0.5)
    result = list(items.values())
    updated = ""
    if result:
        updated = _save_catalog(shop["name"], result)
        save_history([dict(r, shop=shop["name"]) for r in result], log_query=None)
    return {"shop": shop["name"], "source": "shopify_json", "items": len(result), "pages": pages,
            "errors": errors, "updated": updated,
            "elapsed_ms": round((time.monotonic() - start) * 1000), "sample": result[:10]}


def _tag(tag):
    """Názov XML značky bez menného priestoru, veľkými písmenami ({ns}item -> ITEM)."""
    return tag.rsplit("}", 1)[-1].upper()


def _feed_stock(d):
    av = (d.get("AVAILABILITY") or "").lower().replace("_", " ")
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
    return "order" if dd.isdigit() else ("preorder" if dd else "")


def crawl_feed(shop):
    """XML feed (Heureka / Google Merchant) po kúskoch, aj veľký."""
    start = time.monotonic()
    host = host_of(shop["base_url"])
    items, errors, scanned = {}, [], 0
    try:
        resp = http().get(shop["feed"], timeout=(5, 90), stream=True)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}")
        resp.raw.decode_content = True
        for _, el in ET.iterparse(resp.raw, events=("end",)):
            if _tag(el.tag) not in ("SHOPITEM", "ITEM", "ENTRY"):
                continue
            scanned += 1
            d = {}
            for ch in el:
                d.setdefault(_tag(ch.tag), (ch.text or "").strip())
            el.clear()
            title = L.clean_text(d.get("PRODUCTNAME") or d.get("PRODUCT") or d.get("TITLE"))
            link = clean_link(L.clean_text(d.get("URL") or d.get("LINK")))
            if not title or not link or host_of(link) != host or not L.is_listed_product(title):
                continue
            raw = d.get("PRICE_VAT") or d.get("SALE_PRICE") or d.get("PRICE") or ""
            m = re.search(r"\d[\d\s.,]*", raw)
            price = L.to_float(m.group(0).strip()) if m else None
            if not price or price <= 0:
                continue
            price_czk = None
            if "CZK" in raw.upper() or "KČ" in raw.upper() or shop.get("currency") == "CZK":
                price_czk, price = price, L.czk_to_eur(price)
            if not L.price_plausible(title, price):
                continue
            items[link] = {"link": link, "title": title, "price_eur": round(price, 2), "price_czk": price_czk,
                           "image": d.get("IMGURL") or d.get("IMAGE_LINK") or "",
                           "stock": _feed_stock(d)}
            if resp.raw.tell() > 150 * 1024 * 1024:
                errors.append({"url": shop["feed"], "error": "feed je príliš veľký, načítaná len časť"})
                break
    except Exception as e:
        errors.append({"url": shop.get("feed"), "error": str(e)[:200]})
    result = list(items.values())
    updated = ""
    if result:
        updated = _save_catalog(shop["name"], result)
        save_history([dict(r, shop=shop["name"]) for r in result], log_query=None)
    return {"shop": shop["name"], "source": "feed", "items": len(result), "scanned": scanned,
            "errors": errors, "updated": updated,
            "elapsed_ms": round((time.monotonic() - start) * 1000), "sample": result[:10]}


def crawl_in_background(shop):
    with _cat_lock:
        if shop["name"] in _crawling:
            return
        _crawling.add(shop["name"])

    def run():
        try:
            with _crawl_sem:   # katalógy sa sťahujú po jednom – slabý server inak nestíha hľadanie
                crawl_shop(shop)
        except Exception as e:
            print(f"[CardRadar] Katalóg {shop['name']}: {e}", flush=True)
        finally:
            with _cat_lock:
                _crawling.discard(shop["name"])

    threading.Thread(target=run, daemon=True, name="catalog-" + shop["name"]).start()


def is_crawling(shop_name):
    return shop_name in _crawling


_cat_query_cache = None   # vytvorí sa nižšie (TTLCache je definovaná vyššie)
MAX_SHOP_RESULTS = 400    # najviac ponúk z jedného katalógového obchodu na jedno hľadanie


def catalog_scrape(shop, query):
    start = time.monotonic()
    debug = _new_debug(shop, query)
    debug["url"] = "catalog"
    items, updated = load_catalog(shop["name"])
    ckey = f"{shop['name']}|{L.clean_text(query).lower()}|{updated}"
    hit = _cat_query_cache.get(ckey) if items else None
    if hit is not None:   # rovnaké hľadanie v posledných 2 min – bez prechádzania tisícok položiek
        res, dbg = hit
        dbg["cache"] = "hit"
        return res, dbg
    if not items or _is_stale(updated, factor=3 * shop.get("refresh_factor", 1)):
        crawl_in_background(shop)
    if not items:
        debug.update(status="catalog_loading", error="Katalóg sa práve načítava, skús o minútu.")
        return [], debug
    try:
        debug["fetched_at"] = datetime.fromisoformat(updated).timestamp()
    except Exception:
        pass
    parsed = L.normalize_query(query)
    kind = L.classify_query(parsed)
    results = []
    matched = []
    acc_query = L.is_accessory_query(query)
    anchors = [] if acc_query else L.quick_anchors(parsed, shop.get("loose_set", False))
    for it in items:
        if anchors and not L.anchors_hit(it.get("_f") or L.fold(it["title"]), anchors):
            debug["match_filtered"] += 1
            continue
        if not (L.is_accessory(it["title"]) if acc_query else L.is_listed_product(it["title"])):
            debug["merch_filtered"] += 1
            continue
        if not _matches(shop, it["title"], "", parsed, kind)[0]:
            debug["match_filtered"] += 1
            continue
        if not L.price_plausible(it["title"], it["price_eur"]):   # staršie zle prečítané položky
            continue
        matched.append(it)
    # pri veľmi všeobecnom hľadaní („scarlet violet“) sú to tisíce položiek – ďalej ide len MAX_SHOP_RESULTS
    matched.sort(key=lambda it: it["price_eur"] or 0)
    debug["total_matches"] = len(matched)
    for it in matched[:MAX_SHOP_RESULTS]:
        results.append(L.make_result(shop, it["title"], it["price_eur"], it["link"], it["image"], it["stock"],
                                     price_czk=it.get("price_czk")))
    results.sort(key=lambda r: r["price_eur"])
    debug.update(links_scanned=len(items), accepted=len(results), results=len(results),
                 status="ok" if results else "no_results",
                 elapsed_ms=round((time.monotonic() - start) * 1000))
    _cat_query_cache.set(ckey, (results, debug))
    return results, debug


_cat_query_cache = TTLCache(120, 400)


# =========================================================
# HĽADANIE VO VŠETKÝCH OBCHODOCH
# Jedna cache: do 10 min čerstvé, do 6 h sa ukáže hneď a obnoví na pozadí.
# Rovnaké súbežné hľadanie sa sťahuje len raz.
# =========================================================

_cache, _cache_lock = {}, threading.Lock()
_inflight, _busy = {}, set()


def _cache_put(key, results, debug):
    with _cache_lock:
        if key not in _cache and len(_cache) >= 800:
            _cache.pop(min(_cache, key=lambda k: _cache[k][0]), None)
        _cache[key] = (time.monotonic(), copy.deepcopy(results), copy.deepcopy(debug))


def clear_caches():
    with _cache_lock:
        _cache.clear()
    image_cache.clear()
    suggestion_cache.clear()
    with _sug_lock:
        SUGGESTIONS.clear()


def _scrape_and_store(shop, query, key, timeout):
    res, dbg = scrape_search(shop, query, timeout, fetch_q=shop_query(query))
    if dbg["status"] in OK_STATUSES:
        _cache_put(key, res, dbg)
    return res, dbg


def _refresh(shop, query, key):
    try:
        _scrape_and_store(shop, query, key, SEARCH_TIMEOUT)
    except Exception:
        pass
    finally:
        with _cache_lock:
            _busy.discard(key)


def shop_search(shop, query, use_cache=True, timeout=SEARCH_TIMEOUT, wait_inflight=False):
    """Hľadanie v jednom obchode -> (výsledky, debug).
    wait_inflight=False: ak to isté práve hľadá iné vlákno, nečaká (vráti 'pending') –
    stránka si výsledok o chvíľu dotiahne z cache. Čakajúce vlákna predtým zapĺňali pool."""
    res = dbg = None
    if is_catalog(shop):
        res, dbg = catalog_scrape(shop, query)
        if dbg["status"] == "catalog_loading" and shop.get("search_url"):
            res = dbg = None   # katalóg ešte nie je (alebo nefunguje) – hľadáme cez vyhľadávanie obchodu
    if res is not None:
        pass
    elif not use_cache:
        res, dbg = scrape_search(shop, query, timeout, fetch_q=shop_query(query))
    else:
        key = shop["name"].lower() + "|" + L.clean_text(query).lower()
        res = dbg = None
        with _cache_lock:
            ent = _cache.get(key)
        if ent and time.monotonic() - ent[0] < CACHE_STALE:
            age = time.monotonic() - ent[0]
            if age >= CACHE_FRESH:
                with _cache_lock:
                    start = key not in _busy
                    _busy.add(key)
                if start:
                    REFRESH_POOL.submit(_refresh, shop, query, key)
            res, dbg = copy.deepcopy(ent[1]), copy.deepcopy(ent[2])
            dbg["cache"] = "stale" if age >= CACHE_FRESH else "hit"
        else:
            with _cache_lock:
                event = _inflight.get(key)
                owner = event is None
                if owner:
                    event = _inflight[key] = threading.Event()
            if not owner and not wait_inflight:   # to isté práve hľadá iné vlákno
                res, dbg = [], dict(_new_debug(shop, query), status="pending")
            elif not owner:
                event.wait(timeout + 3)
                with _cache_lock:
                    ent = _cache.get(key)
                if ent:
                    res, dbg = copy.deepcopy(ent[1]), copy.deepcopy(ent[2])
                    dbg["cache"] = "hit"
            if res is None:
                try:
                    res, dbg = _scrape_and_store(shop, query, key, timeout)
                finally:
                    if owner:
                        with _cache_lock:
                            _inflight.pop(key, None)
                        event.set()
    for r in res or []:
        L.reprice(r)   # Kč -> € vždy aktuálnym kurzom, aj pri starších výsledkoch z cache
        add_shipping(shop, r)
        r["out"] = go_link(r["link"])
    fill_images_from_cache(res)
    add_suggestions(res)
    return res, dbg


def search_all(query, wait_all=False):
    """Hľadá vo všetkých zapnutých obchodoch naraz.
    wait_all=False (web): po SEARCH_BUDGET sekundách vráti, čo je hotové, pomalé obchody
    sú 'pending' a dobehnú do cache – stránka si ich o chvíľu potichu dotiahne.
    wait_all=True (admin test): čaká na všetky."""
    ensure_czk()
    shops = active_shops()

    def run(shop):
        start = time.monotonic()
        try:
            res, dbg = shop_search(shop, query, wait_inflight=wait_all)
        except Exception as e:
            res, dbg = [], dict(_new_debug(shop, query), status="runner_error", error=str(e)[:200])
        dbg["elapsed_ms"] = round((time.monotonic() - start) * 1000)
        return res, dbg

    # obchody s vyhľadávaním idú cez internet súbežne vo vláknach;
    # katalógy sú v našej databáze, tie sa prejdú hneď tu (nečakajú na voľné vlákno)
    started = time.monotonic()
    futs = {s["name"]: SHOP_POOL.submit(run, s) for s in shops if not is_local(s)}
    local = {s["name"]: run(s) for s in shops if is_local(s)}
    budget = SEARCH_TIMEOUT + 6 if wait_all else SEARCH_BUDGET
    wait(list(futs.values()), timeout=max(0.3, budget - (time.monotonic() - started)))
    results, diagnostics = [], []
    for shop in shops:
        fut = futs.get(shop["name"])
        if fut is None:
            res, dbg = local[shop["name"]]
        elif fut.done():
            res, dbg = fut.result()
        else:
            res, dbg = [], dict(_new_debug(shop, query), status="pending",
                                elapsed_ms=round(SEARCH_BUDGET * 1000))
        results.extend(res)
        diagnostics.append(dbg)

    unique = {}
    for r in results:
        unique[(r["shop"].lower(), r["link"].lower().rstrip("/"))] = r
    results = sorted(unique.values(), key=lambda r: r.get("price_eur") or 999999)
    return results, diagnostics


def pool_stats():
    """Pre /health: koľko úloh čaká vo vláknach (veľké číslo = niečo sa zasekáva)."""
    def q(pool):
        try:
            return pool._work_queue.qsize()
        except Exception:
            return None
    return {"shop_queue": q(SHOP_POOL), "refresh_queue": q(REFRESH_POOL), "json_queue": q(JSON_POOL),
            "image_queue": q(IMAGE_POOL), "db_queue": q(BG_POOL)}


def shops_status(diagnostics):
    return [{
        "name": d.get("shop", ""), "status": d.get("status", ""),
        "ok": d.get("status") in OK_STATUSES, "results": d.get("results", 0),
        "elapsed_ms": d.get("elapsed_ms", 0), "cache": d.get("cache", ""),
        "fetched_at": round(d["fetched_at"]) if d.get("fetched_at") else None,
    } for d in diagnostics]


# =========================================================
# OBRÁZKY
# =========================================================

image_cache = TTLCache(6 * 3600, 2000)
_OG_IMAGE_RE = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image)["\'][^>]*>', re.I)
_CONTENT_RE = re.compile(r'content=["\']([^"\']+)["\']', re.I)


def fill_images_from_cache(results):
    for r in results or []:
        if not r.get("image") and r.get("link"):
            r["image"] = image_cache.get(r["link"]) or ""


def fetch_product_image(link):
    """Obrázok z produktovej stránky (og:image, inak prvý obrázok produktu)."""
    cached = image_cache.get(link)
    if cached is not None:
        return cached
    url = ""
    resp, _ = fetch(link, timeout=4)
    if resp:
        m = _OG_IMAGE_RE.search(resp.text)
        c = _CONTENT_RE.search(m.group(0)) if m else None
        if c and not c.group(1).startswith("data:image/"):
            url = abs_url(link, c.group(1))
        if not url:
            try:
                soup = BeautifulSoup(resp.text, HTML_PARSER)
                marks = ("product", "produkt", "gallery", "main-image", "woocommerce")
                imgs = sorted(soup.find_all("img"), key=lambda i: 0 if any(
                    k in " ".join([i.get("alt", ""), " ".join(i.get("class", [])), i.get("id", "")]).lower()
                    for k in marks) else 1)
                url = next((u for u in (img_url(i, link) for i in imgs) if u), "")
            except Exception:
                pass
    image_cache.set(link, url)
    if url:
        BG_POOL.submit(_store_image, link, url)
        with _sug_lock:
            for s in SUGGESTIONS.values():
                if s.get("link") == link and not s.get("image"):
                    s["image"] = url
    return url


def images_for(links, budget=6):
    """{odkaz: obrázok} pre najviac 12 odkazov, čaká najviac `budget` sekúnd."""
    links = list(dict.fromkeys(L.clean_text(l) for l in links[:12]
                               if isinstance(l, str) and is_allowed_link(L.clean_text(l))))
    out, pending = {}, {}
    for link in links:
        c = image_cache.get(link)
        if c is not None:
            out[link] = c
        else:
            pending[IMAGE_POOL.submit(fetch_product_image, link)] = link
    try:
        for fut in as_completed(pending, timeout=budget):
            out[pending[fut]] = fut.result() or ""
    except Exception:
        pass   # zvyšok dobehne na pozadí do cache
    return out


def _store_image(link, url):
    conn = db()
    try:
        conn.execute("UPDATE price_daily SET image = ? WHERE link = ? AND (image IS NULL OR image = '')",
                     (url, link))
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


# =========================================================
# NAŠEPKÁVAČ
# Pamätá si produkty z posledných hľadaní; keď je málo návrhov,
# opýta sa rýchleho vyhľadávania Shopify obchodu (CardyX).
# =========================================================

SUGGESTIONS = {}
_sug_lock = threading.Lock()
suggestion_cache = TTLCache(300, 300)


def make_suggestion(title):
    title = L.clean_text(title)
    if not title or not L.is_tcg_product(title):
        return None
    p = L.normalize_query(title)
    # aj typ produktu („pitch black elite trainer box“), inak by ťuknutie na ETB hľadalo celý set
    query = L.clean_text(" ".join(x for x in (p["pokemon"], p["suffix"], p["card_number"], p["set_name"],
                                              p["product_type"]) if x))
    query = (query or title)[:120]
    is_card = not p["product_type"]
    return {"title": title, "query": query, "type": "card" if is_card else "product",
            "image": "", "price_eur": None, "link": ""}


def _suggestions_from(results):
    out = {}
    for r in results or []:
        s = make_suggestion(r.get("title", ""))
        if not s:
            continue
        s.update(image=r.get("image") or "", link=r.get("link") or "", price_eur=r.get("price_eur"))
        key = s["query"].lower()
        old = out.get(key)
        score = bool(s["image"]) + (s["price_eur"] is not None)
        if old is None or score > bool(old["image"]) + (old["price_eur"] is not None):
            out[key] = s
    return list(out.values())


def add_suggestions(results):
    items = _suggestions_from(results)
    if not items:
        return
    with _sug_lock:
        for s in items:
            SUGGESTIONS.pop(s["query"].lower(), None)
            SUGGESTIONS[s["query"].lower()] = s
        while len(SUGGESTIONS) > 500:
            SUGGESTIONS.pop(next(iter(SUGGESTIONS)))


def _sug_score(item, q):
    title = item["title"].lower()
    score = 100 if title.startswith(q) else 0
    score += 60 if any(w.startswith(q) for w in title.split()) else 0
    score += 40 if q in title else 0
    score += 15 if item.get("price_eur") is not None else 0
    score += 10 if item.get("image") else 0
    score += 80 if item.get("_set") else 0   # celý set / séria má byť v návrhoch navrchu
    return score + max(0, 20 - len(title) // 10)


def _remote_suggestions(q):
    shop = next((s for s in active_shops() if s.get("shopify")), None)
    if not shop:
        return []
    products = _shopify_suggest_raw(shop, q, timeout=2.5)
    if products is None:
        return []   # obchod neodpovedá – našepkávač nesmie čakať na celé hľadanie
    foreign_ok = L.FOREIGN_QUERY_RE.search(q) is not None
    out = []
    for p in products:
        title = L.clean_text(p.get("title", ""))
        if not title or not L.is_tcg_product(title):
            continue
        if L.detect_language(title) in L.ASIAN_LANGS and not foreign_ok:
            continue
        raw = p.get("price", p.get("price_min"))
        price = L.to_float(raw) if raw not in (None, "") else None
        link = abs_url(shop["base_url"], p.get("url", "")).split("?")[0]
        out.append({"title": title, "shop": shop["name"], "link": link,
                    "price_eur": round(price, 2) if price else None,
                    "image": _shopify_product_image(p, shop["base_url"], 160)})
    add_suggestions(out)
    return out


# názvy setov a sérií pre našepkávač („sca“ -> Scarlet & Violet, „sur“ -> Surging Sparks)
_SET_TITLES = {L.fold(n): n for n in
               [" ".join(w[:1].upper() + w[1:] for w in n.split()) for n in sorted(L.TCG_SET_NAMES) if not n.isdigit()] +
               [s["name"] for s in L.NOVE_SETY]}   # NOVE_SETY posledné = ich presný zápis má prednosť
_SET_TITLES["scarlet & violet"] = _SET_TITLES["scarlet violet"] = "Scarlet & Violet"
_SET_TITLES["sword & shield"] = _SET_TITLES["sword shield"] = "Sword & Shield"


def _set_suggestions(q):
    fq = L.fold(q)
    out, seen = [], set()
    for folded, title in _SET_TITLES.items():
        words = folded.replace("&", " ").split()
        if folded.startswith(fq) or any(w.startswith(fq) for w in words) or (len(fq) >= 4 and fq in folded):
            query = L.clean_text(folded.replace("&", " ").replace("pokemon ", ""))
            if query not in seen:
                seen.add(query)
                out.append({"title": title, "query": query, "type": "product", "_set": True,
                            "image": "", "price_eur": None, "link": ""})
    return out[:4]


def _catalog_suggestions(q, limit=30):
    """Produkty z katalógov (naša databáza, okamžite) – obsahujú hľadaný text."""
    fq, found = L.fold(q), []
    for shop in active_shops():
        if not is_catalog(shop):
            continue
        for it in load_catalog(shop["name"])[0]:
            if fq in L.fold(it["title"]) and L.is_tcg_product(it["title"]):
                found.append({"title": it["title"], "link": it["link"], "image": it["image"],
                              "price_eur": it["price_eur"]})
                if len(found) >= limit:
                    return found
    return found


def suggestions(q):
    q = L.clean_text(q)
    key = q.lower()
    cached = suggestion_cache.get(key)
    if cached is not None:
        return cached
    with _sug_lock:
        cands = [dict(s) for s in SUGGESTIONS.values()
                 if key in s["title"].lower() or key in s["query"].lower()]
    cands = _set_suggestions(q) + cands
    if len(cands) < 6:
        known = {c["query"].lower() for c in cands}
        cands += [s for s in _suggestions_from(_catalog_suggestions(q)) if s["query"].lower() not in known]
    if len(cands) < 4:
        known = {c["query"].lower() for c in cands}
        cands += [s for s in _suggestions_from(_remote_suggestions(q)) if s["query"].lower() not in known]
    if not cands:
        p = L.normalize_query(q)
        if p["normalized"].lower() != key and (p["product_type"] or p["pokemon"]):
            cands.append({"title": p["normalized"], "query": p["normalized"],
                          "type": "product" if p["product_type"] else "card",
                          "image": "", "price_eur": None, "link": ""})
    cands.sort(key=lambda s: _sug_score(s, key), reverse=True)
    out, seen = [], set()
    for s in cands:
        if s["query"].lower() not in seen:
            seen.add(s["query"].lower())
            if not s.get("image") and s.get("link"):
                s["image"] = image_cache.get(s["link"]) or ""
            out.append(s)
            if len(out) >= 8:
                break
    suggestion_cache.set(key, out)
    return out


# =========================================================
# HISTÓRIA CIEN A ŠTATISTIKA HĽADANÍ
# =========================================================

def _save_history_now(results, log_query):
    conn = db()
    try:
        day = today_str()
        conn.executemany("""
            INSERT INTO price_daily (link, day, shop, title, price_eur, stock, image)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(link, day) DO UPDATE SET
                price_eur = excluded.price_eur, stock = excluded.stock, title = excluded.title,
                image = COALESCE(NULLIF(excluded.image, ''), price_daily.image)
        """, [(r["link"], day, r.get("shop", ""), r.get("title", ""), r["price_eur"],
               r.get("stock", ""), r.get("image", "")) for r in results
              if r.get("link") and r.get("price_eur")])
        if log_query:
            conn.execute("""INSERT INTO search_log (day, query, n) VALUES (?, ?, 1)
                            ON CONFLICT(day, query) DO UPDATE SET n = n + 1""", (day, log_query))
        if time.time() % 50 < 1:
            _prune(conn)
        conn.commit()
    except Exception as e:
        print(f"[CardRadar] Zápis histórie zlyhal: {e}", flush=True)
    finally:
        conn.close()


def save_history(results, log_query=None):
    """Zápis na pozadí (odpoveď naň nečaká). log_query = započítať do „Najhľadanejšie“."""
    if results:
        BG_POOL.submit(_save_history_now, copy.deepcopy(results),
                       L.clean_text(log_query).lower()[:80] if log_query else None)


def add_trends(results, days=30):
    """Ku každému výsledku najstaršia cena za posledných `days` dní (šípka ↓↑ na webe)."""
    links = [r["link"] for r in results if r.get("link")]
    if not links:
        return
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    oldest = {}
    conn = db()
    try:
        for i in range(0, len(links), 400):
            chunk = links[i:i + 400]
            rows = conn.execute(
                f"SELECT link, day, price_eur FROM price_daily WHERE link IN ({','.join('?' * len(chunk))}) "
                "AND day >= ? AND day < ? ORDER BY day ASC", (*chunk, since, today_str())).fetchall()
            for link, day, price in rows:
                oldest.setdefault(link, (day, price))
    except Exception:
        return
    finally:
        conn.close()
    for r in results:
        old = oldest.get(r.get("link"))
        if not old or not old[1]:
            continue
        # CZ obchody: pohyb kurzu nie je zmena ceny (cena v Kč je rovnaká)
        if r.get("price_czk") and abs(r["price_eur"] - old[1]) < max(0.5, old[1] * 0.02):
            continue
        r["trend"] = {"since": old[0], "price_eur": round(old[1], 2)}


def price_history(link):
    conn = db()
    try:
        rows = conn.execute("SELECT day, price_eur, stock, title, shop FROM price_daily "
                            "WHERE link = ? ORDER BY day ASC", (link,)).fetchall()
    finally:
        conn.close()
    return {"link": link, "title": rows[-1][3] if rows else "", "shop": rows[-1][4] if rows else "",
            "points": [{"day": d, "price_eur": round(p, 2), "stock": s or ""} for d, p, s, _, _ in rows if p]}


def latest_prices(links):
    """Posledná známa cena a sklad (pre obľúbené)."""
    links = [L.clean_text(l) for l in links[:100] if isinstance(l, str) and is_allowed_link(L.clean_text(l))]
    if not links:
        return {}
    conn = db()
    try:
        rows = conn.execute(f"""
            SELECT p.link, p.day, p.price_eur, p.stock FROM price_daily p
            JOIN (SELECT link, MAX(day) AS d FROM price_daily
                  WHERE link IN ({','.join('?' * len(links))}) GROUP BY link) m
              ON p.link = m.link AND p.day = m.d""", links).fetchall()
    finally:
        conn.close()
    return {link: {"day": day, "price_eur": round(price, 2) if price else None, "stock": stock or ""}
            for link, day, price, stock in rows}


def popular_queries(days=14, limit=8):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = db()
    try:
        rows = conn.execute("SELECT query, SUM(n) AS c FROM search_log WHERE day >= ? "
                            "GROUP BY query ORDER BY c DESC LIMIT 20", (since,)).fetchall()
    finally:
        conn.close()
    return [q for q, _ in rows if len(q) >= 3][:limit]


# =========================================================
# ROZPOZNANIE NOVÉHO OBCHODU (/admin/obchody)
# =========================================================

def detect_shop(url, q="pikachu"):
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return {"error": "Neplatná adresa."}
    base = f"{p.scheme}://{p.netloc}/"
    resp, info = fetch(base, timeout=10)
    if not resp:
        return {"error": f"Stránka neodpovedá ({info['error'] or info['status']})."}
    platform = next((n for n, pr in PLATFORM_PRESETS.items() if pr["marker"].search(resp.text)), None)
    if not platform:
        return {"error": "Platformu sa nepodarilo rozpoznať. Takýto obchod pôjde len cez XML feed "
                         "alebo ako katalóg v SHOPS."}
    host = p.netloc.lower()
    preset = PLATFORM_PRESETS[platform]
    norm = L.normalize_query(q)["normalized"] or q
    tried = []
    for path in preset["search"]:
        shop = {"name": host.replace("www.", ""), "country": "CZ" if host.endswith(".cz") else "SK",
                "base_url": base, "search_url": base.rstrip("/") + path, "link_selector": preset["selector"]}
        if platform == "shopify":
            shop["shopify"] = True
        res, d = scrape_search(shop, norm, 10)
        tried.append({"url": shop["search_url"], "status": d["status"],
                      "links": d["links_scanned"], "results": len(res)})
        if res:
            return {"platform": platform, "config": shop, "sample": res[:8], "tried": tried}
    return {"platform": platform, "tried": tried,
            "error": "Platforma rozpoznaná, ale vyhľadávanie nevrátilo produkty."}


# =========================================================
# ÚLOHY NA POZADÍ (spúšťa app.py raz pri štarte)
# =========================================================

_czk_state = {"checked": 0.0, "running": False}
_czk_lock = threading.Lock()
CZK_MAX_AGE = 6 * 3600   # kurz sa skúša obnoviť najneskôr po 6 hodinách


def _load_czk():
    """Posledný známy kurz z databázy (zdieľaný všetkými procesmi, prežije reštart)."""
    try:
        data = json.loads(meta_get("czk_rate") or "null")
    except Exception:
        data = None
    if data and 15 < float(data.get("rate", 0)) < 40:
        L.KURZ["CZK"] = float(data["rate"])
        L.KURZ_INFO.update(date=data.get("date", ""), source="ECB")


def update_czk():
    """Kurz CZK z Európskej centrálnej banky. Vráti True, ak sa podaril."""
    resp, info = fetch("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml", timeout=10)
    text = resp.text if resp else ""
    m = re.search(r"currency=['\"]CZK['\"]\s+rate=['\"]([\d.]+)", text)
    day = re.search(r"time=['\"](\d{4}-\d{2}-\d{2})['\"]", text)
    if not m or not 15 < float(m.group(1)) < 40:
        print(f"[CardRadar] Kurz CZK sa nepodarilo načítať: {info.get('error') or 'neznámy formát'}", flush=True)
        _load_czk()   # aspoň kurz, ktorý medzitým uložil iný proces
        return False
    rate = float(m.group(1))
    L.KURZ["CZK"] = rate
    L.KURZ_INFO.update(date=day.group(1) if day else today_str(), source="ECB")
    try:
        meta_set("czk_rate", json.dumps({"rate": rate, "date": L.KURZ_INFO["date"]}))
    except Exception:
        pass
    return True


def ensure_czk():
    """Ak je kurz starší ako CZK_MAX_AGE, obnoví ho na pozadí (hľadanie nečaká)."""
    if time.monotonic() - _czk_state["checked"] < CZK_MAX_AGE and _czk_state["checked"]:
        return
    with _czk_lock:
        if _czk_state["running"]:
            return
        _czk_state["running"] = True

    def run():
        try:
            if update_czk():
                _czk_state["checked"] = time.monotonic()
            else:   # pri chybe skús znova o 15 min
                _czk_state["checked"] = time.monotonic() - CZK_MAX_AGE + 900
        except Exception:
            pass
        finally:
            _czk_state["running"] = False

    threading.Thread(target=run, daemon=True, name="czk-refresh").start()


def _catalog_loop():
    time.sleep(3)
    while True:
        for shop in SHOPS:
            if shop.get("enabled", True) and is_catalog(shop):
                try:
                    if _is_stale(load_catalog(shop["name"])[1], factor=shop.get("refresh_factor", 1)):
                        crawl_in_background(shop)
                except Exception:
                    pass
        time.sleep(300)


def _czk_loop():
    while True:
        try:
            ensure_czk()
        except Exception:
            pass
        time.sleep(1800)


_lock_files = {}


def only_one_process(name):
    """True len v jednom procese servera (zámok súboru), aby sa úlohy nespúšťali viackrát."""
    if fcntl is None:
        return True
    try:
        f = open(os.path.join(BASE_DIR, f".{name}.lock"), "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_files[name] = f
        return True
    except OSError:
        return False


def start_background():
    threading.Thread(target=_czk_loop, daemon=True, name="czk").start()   # každý proces potrebuje kurz
    if only_one_process("catalog"):
        threading.Thread(target=_catalog_loop, daemon=True, name="catalog").start()
