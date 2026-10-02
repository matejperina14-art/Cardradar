import os
import re
import sqlite3
import unicodedata
from datetime import datetime
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

app = Flask(__name__)

VERSION = "5.0"

DB_FILE = "cardradar.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8,cs;q=0.7",
}

TIMEOUT = 15

# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            shop TEXT NOT NULL,
            title TEXT NOT NULL,
            price_eur REAL NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------
# NORMALIZATION
# ---------------------------------------------------------

def normalize(text):
    if not text:
        return ""

    text = str(text)

    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("’", "'")

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def words(text):
    return re.findall(r"[a-z0-9]+", normalize(text))


# ---------------------------------------------------------
# QUERY CLASSIFICATION
# ---------------------------------------------------------

MERCH_WORDS = {
    "plysak",
    "plysovy",
    "plysovy",
    "hracka",
    "hracky",
    "figurka",
    "figurka",
    "funko",
    "figurine",
    "toy",
    "plush",
    "plushie",
    "album",
    "obal",
    "sleeves",
    "sleeve",
    "binder",
    "dekoracia",
    "dekoracie",
    "tricko",
    "mikina",
    "taska",
    "batoh",
    "hrncek",
    "pohar",
    "poster",
    "poduska",
    "vankus",
    "keychain",
    "privesok",
}


SEALED_WORDS = {
    "etb",
    "elite",
    "trainer",
    "booster",
    "box",
    "bundle",
    "tin",
    "collection",
    "premium",
    "upc",
    "display",
    "pack",
}


def classify_query(query):
    q = normalize(query)

    if (
        "etb" in q
        or "elite trainer box" in q
        or "booster box" in q
        or "booster bundle" in q
        or re.search(r"\btin\b", q)
        or "collection box" in q
        or "premium collection" in q
    ):
        return "sealed"

    return "card"


# ---------------------------------------------------------
# QUERY REQUIREMENTS
# ---------------------------------------------------------

def card_number_from_query(query):
    m = re.search(
        r"\b(\d{1,3})\s*/\s*(\d{1,3})\b",
        query
    )

    if not m:
        return None

    return f"{int(m.group(1))}/{int(m.group(2))}"


def extract_set_terms(query):
    """
    Z query odstráni typické card suffixy a číslo.
    Zvyšok použijeme ako pomocné hľadanie.
    """

    q = normalize(query)

    q = re.sub(
        r"\b\d{1,3}\s*/\s*\d{1,3}\b",
        " ",
        q
    )

    q = re.sub(
        r"\b(pokemon|tcg|card|karte|karta)\b",
        " ",
        q
    )

    q = re.sub(
        r"\b(nm|near mint|lp|light played|mp|played)\b",
        " ",
        q
    )

    q = re.sub(r"\s+", " ", q)

    return q.strip()


# ---------------------------------------------------------
# RELEVANCE
# ---------------------------------------------------------

def candidate_is_merch(title):
    t = normalize(title)

    for bad in MERCH_WORDS:
        if re.search(r"\b" + re.escape(bad) + r"\b", t):
            return True

    return False


def card_matches_query(title, query):
    """
    Dôležité:
    'Pikachu ex' musí mať Pikachu aj ex ako samostatné slová.
    Plyšák a merch sa vyradia.
    """

    if not title:
        return False

    title_n = normalize(title)
    query_n = normalize(query)

    if candidate_is_merch(title):
        return False

    qwords = words(query_n)

    # -----------------------------------------------------
    # EX
    # -----------------------------------------------------

    if "ex" in qwords:

        if not re.search(r"(?<![a-z0-9])ex(?![a-z0-9])", title_n):
            return False

    # -----------------------------------------------------
    # VMAX
    # -----------------------------------------------------

    if "vmax" in qwords:

        if "vmax" not in title_n:
            return False

    # -----------------------------------------------------
    # V
    # -----------------------------------------------------

    if "v" in qwords:

        if not re.search(r"(?<![a-z0-9])v(?![a-z0-9])", title_n):
            return False

    # -----------------------------------------------------
    # GX
    # -----------------------------------------------------

    if "gx" in qwords:

        if not re.search(r"(?<![a-z0-9])gx(?![a-z0-9])", title_n):
            return False

    # -----------------------------------------------------
    # EXACT CARD NUMBER
    # -----------------------------------------------------

    number = card_number_from_query(query)

    if number:

        a, b = number.split("/")

        possible = {
            f"{int(a)}/{int(b)}",
            f"{a.zfill(2)}/{b.zfill(2)}",
            f"{a.zfill(3)}/{b.zfill(3)}",
        }

        found = False

        for p in possible:
            if p in title_n:
                found = True
                break

        if not found:
            return False

    # -----------------------------------------------------
    # MAIN NAME
    # -----------------------------------------------------

    ignore = {
        "pokemon",
        "tcg",
        "card",
        "karte",
        "karta",
        "ex",
        "v",
        "vmax",
        "vstar",
        "gx",
        "nm",
        "near",
        "mint",
        "lp",
        "mp",
        "played",
    }

    important = [
        w for w in qwords
        if w not in ignore
        and not w.isdigit()
    ]

    for word in important:

        # čísla neriešime ako samostatné slová
        if len(word) <= 1:
            continue

        if word not in title_n:
            return False

    return True


def sealed_matches_query(title, query):
    if not title:
        return False

    title_n = normalize(title)
    query_n = normalize(query)

    if candidate_is_merch(title):
        return False

    qwords = words(query_n)

    # ETB
    if "etb" in qwords:

        if not (
            "etb" in title_n
            or "elite trainer box" in title_n
        ):
            return False

    # Booster Box
    if "booster" in qwords and "box" in qwords:

        if "booster box" not in title_n:
            return False

    # Booster Bundle
    if "booster" in qwords and "bundle" in qwords:

        if "booster bundle" not in title_n:
            return False

    # Tin
    if "tin" in qwords:

        if not re.search(r"\btin\b", title_n):
            return False

    # Collection
    if "collection" in qwords:

        if "collection" not in title_n:
            return False

    # Set/name
    important = []

    for w in qwords:

        if w in SEALED_WORDS:
            continue

        if w in {"pokemon", "tcg"}:
            continue

        important.append(w)

    for word in important:

        if len(word) <= 1:
            continue

        if word not in title_n:
            return False

    return True


# ---------------------------------------------------------
# PRICE PARSING
# ---------------------------------------------------------

def parse_number(value):
    if value is None:
        return None

    value = str(value).strip()

    value = value.replace("\xa0", " ")
    value = value.replace("€", "")
    value = value.replace("EUR", "")
    value = value.replace("Kč", "")
    value = value.replace("CZK", "")
    value = value.strip()

    # 1 299,99
    if "," in value and "." in value:

        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "")
            value = value.replace(",", ".")
        else:
            value = value.replace(",", "")

    elif "," in value:

        value = value.replace(".", "")
        value = value.replace(",", ".")

    else:

        # 1.299.99 -> 1299.99
        if value.count(".") > 1:
            parts = value.split(".")
            value = "".join(parts[:-1]) + "." + parts[-1]

    m = re.search(r"\d+(?:\.\d+)?", value)

    if not m:
        return None

    try:
        return float(m.group(0))
    except Exception:
        return None


# ---------------------------------------------------------
# CZK / EUR
# ---------------------------------------------------------

CZK_PER_EUR = 24.4618


def czk_to_eur(value):
    if value is None:
        return None

    return float(value) / CZK_PER_EUR


# ---------------------------------------------------------
# HTTP
# ---------------------------------------------------------

def get(url):
    try:

        r = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT
        )

        if r.status_code != 200:
            return None

        return r

    except Exception:
        return None


# ---------------------------------------------------------
# CARDYX
# ---------------------------------------------------------

def search_cardyx_cards(query):
    """
    CardyX má samostatnú kolekciu Kusové karty.
    Preto pri kartách nehľadáme cez všeobecný Pokémon shop,
    ale priamo cez kolekciu kusových kariet.

    CardyX stránky používajú Shopify štruktúru.
    """

    results = []

    query_n = normalize(query)

    # -----------------------------------------------------
    # Najprv Shopify search
    # -----------------------------------------------------

    search_url = (
        "https://www.cardyx.sk/search"
        "?type=product&q="
        + quote(query)
    )

    r = get(search_url)

    if r:

        soup = BeautifulSoup(
            r.text,
            "html.parser"
        )

        # Shopify produktové karty
        cards = soup.select(
            "a[href*='/products/']"
        )

        seen = set()

        for a in cards:

            href = a.get("href")

            if not href:
                continue

            url = urljoin(
                "https://www.cardyx.sk",
                href
            )

            if url in seen:
                continue

            seen.add(url)

            title = a.get_text(
                " ",
                strip=True
            )

            if not title:
                continue

            # Ak samotný anchor obsahuje veľa textu,
            # skúsime nájsť názov v rodičovi.
            parent = a.parent

            if parent:

                text = parent.get_text(
                    " ",
                    strip=True
                )

                if len(text) > len(title):
                    title = text

            # odfiltruj merch
            if candidate_is_merch(title):
                continue

            if not card_matches_query(
                title,
                query_n
            ):
                continue

            price = extract_cardyx_price_from_product(
                url
            )

            if price is None:
                continue

            results.append({
                "shop": "CardyX",
                "country": "SK",
                "language": "EN",
                "condition": "NM",
                "in_stock": True,
                "title": title,
                "price_eur": round(price, 2),
                "link": url,
            })

    # odstránenie duplicít
    unique = {}

    for item in results:

        key = (
            item["title"],
            item["link"],
        )

        unique[key] = item

    return list(unique.values())


def extract_cardyx_price_from_product(url):

    r = get(url)

    if not r:
        return None

    soup = BeautifulSoup(
        r.text,
        "html.parser"
    )

    # -----------------------------------------------------
    # JSON-LD
    # -----------------------------------------------------

    for script in soup.select(
        'script[type="application/ld+json"]'
    ):

        raw = script.get_text(
            strip=True
        )

        if not raw:
            continue

        # price
        m = re.search(
            r'"price"\s*:\s*"?(\\?[\d.,]+)',
            raw
        )

        if m:

            value = parse_number(
                m.group(1)
            )

            if value is not None:

                # CardyX je EUR
                if 0 < value < 100000:
                    return value

    # -----------------------------------------------------
    # Shopify meta
    # -----------------------------------------------------

    meta = soup.select_one(
        'meta[property="product:price:amount"]'
    )

    if meta:

        value = parse_number(
            meta.get("content")
        )

        if value is not None:
            return value

    # -----------------------------------------------------
    # Shopify price elements
    # -----------------------------------------------------

    selectors = [
        ".price-item--sale",
        ".price-item--regular",
        ".price__regular",
        ".price",
    ]

    for selector in selectors:

        for node in soup.select(selector):

            text = node.get_text(
                " ",
                strip=True
            )

            value = parse_number(text)

            if value is not None:

                # ochrana proti chybnému číslu
                if 0.5 <= value <= 100000:
                    return value

    return None


# ---------------------------------------------------------
# CARDYX SEALED
# ---------------------------------------------------------

def search_cardyx_sealed(query):
    results = []

    url = (
        "https://www.cardyx.sk/search"
        "?type=product&q="
        + quote(query)
    )

    r = get(url)

    if not r:
        return results

    soup = BeautifulSoup(
        r.text,
        "html.parser"
    )

    cards = soup.select(
        "a[href*='/products/']"
    )

    seen = set()

    for a in cards:

        href = a.get("href")

        if not href:
            continue

        link = urljoin(
            "https://www.cardyx.sk",
            href
        )

        if link in seen:
            continue

        seen.add(link)

        title = a.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        parent = a.parent

        if parent:

            txt = parent.get_text(
                " ",
                strip=True
            )

            if len(txt) > len(title):
                title = txt

        if not sealed_matches_query(
            title,
            query
        ):
            continue

        price = extract_cardyx_price_from_product(
            link
        )

        if price is None:
            continue

        results.append({
            "shop": "CardyX",
            "country": "SK",
            "language": "EN",
            "condition": "Sealed",
            "in_stock": True,
            "title": title,
            "price_eur": round(price, 2),
            "link": link,
        })

    return results


# ---------------------------------------------------------
# GENERIC SHOP SEARCH
# ---------------------------------------------------------

SHOPS = [
    {
        "name": "Veselý Drak",
        "country": "CZ",
        "base": "https://www.vesely-drak.cz",
        "search": "https://www.vesely-drak.cz/vyhledavani/?q={q}",
    },
    {
        "name": "iHRYsko",
        "country": "SK",
        "base": "https://www.ihrysko.sk",
        "search": "https://www.ihrysko.sk/vysledky-vyhladavania/?q={q}",
    },
    {
        "name": "Černý Rytíř",
        "country": "CZ",
        "base": "https://www.cernyrytir.cz",
        "search": "https://www.cernyrytir.cz/index.php3?akce=3&stranka=1&search={q}",
    },
]


def generic_search(shop, query, mode):

    results = []

    url = shop["search"].format(
        q=quote(query)
    )

    r = get(url)

    if not r:
        return results

    soup = BeautifulSoup(
        r.text,
        "html.parser"
    )

    links = soup.find_all("a", href=True)

    seen = set()

    for a in links:

        href = a.get("href")

        if not href:
            continue

        title = a.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        title_n = normalize(title)

        # -------------------------------------------------
        # veľmi dôležité:
        # nechceme navigačné odkazy
        # -------------------------------------------------

        if len(title) < 3:
            continue

        if len(title) > 300:
            continue

        # -------------------------------------------------
        # správna relevancia
        # -------------------------------------------------

        if mode == "card":

            if not card_matches_query(
                title,
                query
            ):
                continue

        else:

            if not sealed_matches_query(
                title,
                query
            ):
                continue

        link = urljoin(
            shop["base"],
            href
        )

        if link in seen:
            continue

        seen.add(link)

        # -------------------------------------------------
        # cena
        # -------------------------------------------------

        price = find_price_near_link(
            a,
            shop["country"]
        )

        if price is None:
            continue

        results.append({
            "shop": shop["name"],
            "country": shop["country"],
            "language": "EN",
            "condition": "Sealed" if mode == "sealed" else "NM",
            "in_stock": True,
            "title": title,
            "price_eur": round(price, 2),
            "link": link,
        })

        if len(results) >= 10:
            break

    return results


def find_price_near_link(a, country):

    # -----------------------------------------------------
    # Nečítame ľubovoľné čísla z celej stránky.
    # Hľadáme cenu iba v blízkom rodičovi.
    # -----------------------------------------------------

    nodes = []

    parent = a

    for _ in range(5):

        if parent is None:
            break

        nodes.append(parent)

        parent = parent.parent

    for node in nodes:

        text = node.get_text(
            " ",
            strip=True
        )

        # EUR
        m = re.search(
            r"(\d[\d\s.,]*)\s*(?:€|EUR)",
            text,
            re.I
        )

        if m:

            value = parse_number(
                m.group(1)
            )

            if value is not None and 0.5 <= value <= 100000:
                return value

        # CZK
        if country == "CZ":

            m = re.search(
                r"(\d[\d\s.,]*)\s*(?:Kč|CZK)",
                text,
                re.I
            )

            if m:

                value = parse_number(
                    m.group(1)
                )

                if value is not None and 1 <= value <= 1000000:
                    return czk_to_eur(value)

    return None


# ---------------------------------------------------------
# HISTORY
# ---------------------------------------------------------

def save_history(query, result):

    try:

        conn = db()

        conn.execute(
            """
            INSERT INTO history
            (
                query,
                shop,
                title,
                price_eur,
                checked_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                query,
                result["shop"],
                result["title"],
                result["price_eur"],
                datetime.utcnow().isoformat(),
            )
        )

        conn.commit()
        conn.close()

    except Exception:
        pass


def get_history(query):

    try:

        conn = db()

        rows = conn.execute(
            """
            SELECT
                checked_at,
                price_eur
            FROM history
            WHERE query = ?
            ORDER BY checked_at DESC
            LIMIT 10
            """,
            (query,)
        ).fetchall()

        conn.close()

        return [
            {
                "checked_at": row["checked_at"],
                "price_eur": row["price_eur"],
            }
            for row in rows
        ]

    except Exception:
        return []


# ---------------------------------------------------------
# SORTING
# ---------------------------------------------------------

def sort_results(results):

    # odstránenie rovnakých ponúk
    unique = {}

    for r in results:

        key = (
            normalize(r["title"]),
            r["shop"],
            r["link"],
        )

        unique[key] = r

    results = list(unique.values())

    results.sort(
        key=lambda x: x["price_eur"]
    )

    return results


# ---------------------------------------------------------
# API SEARCH
# ---------------------------------------------------------

@app.route("/api/search")
def api_search():

    query = request.args.get(
        "q",
        ""
    ).strip()

    if not query:

        return jsonify({
            "error": "Chýba vyhľadávanie."
        }), 400

    mode = classify_query(query)

    results = []

    # -----------------------------------------------------
    # CARDYX
    # -----------------------------------------------------

    if mode == "card":

        results.extend(
            search_cardyx_cards(query)
        )

    else:

        results.extend(
            search_cardyx_sealed(query)
        )

    # -----------------------------------------------------
    # OSTATNÉ OBCHODY
    # -----------------------------------------------------

    for shop in SHOPS:

        try:

            results.extend(
                generic_search(
                    shop,
                    query,
                    mode
                )
            )

        except Exception as e:

            print(
                f"{shop['name']} error:",
                e
            )

    # -----------------------------------------------------
    # FINÁLNE FILTROVANIE
    # -----------------------------------------------------

    clean = []

    for result in results:

        title = result.get(
            "title",
            ""
        )

        price = result.get(
            "price_eur"
        )

        if not title:
            continue

        if price is None:
            continue

        # žiadne nulové / nezmyselné ceny
        if price <= 0:
            continue

        # odstránenie merchu
        if candidate_is_merch(title):
            continue

        if mode == "card":

            if not card_matches_query(
                title,
                query
            ):
                continue

        else:

            if not sealed_matches_query(
                title,
                query
            ):
                continue

        clean.append(result)

    results = sort_results(clean)

    # -----------------------------------------------------
    # HISTORY
    # -----------------------------------------------------

    for result in results[:10]:
        save_history(
            query,
            result
        )

    # -----------------------------------------------------
    # INFO
    # -----------------------------------------------------

    info = {}

    if results:

        info = {
            "title": results[0]["title"],
            "subtitle": (
                f"{results[0]['shop']} • "
                f"{results[0]['condition']}"
            ),
        }

    return jsonify({
        "query": query,
        "type": mode,
        "version": VERSION,
        "czk_per_eur": CZK_PER_EUR,
        "results": results,
        "history": get_history(query),
        "info": info,
    })


# ---------------------------------------------------------
# HEALTH
# ---------------------------------------------------------

@app.route("/health")
def health():

    return jsonify({
        "service": "CardRadar",
        "status": "ok",
        "version": VERSION,
    })


# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------

@app.route("/")
def home():

    return """
    <!doctype html>
    <html lang="sk">
    <head>
        <meta charset="utf-8">
        <title>CardRadar</title>
    </head>
    <body>
        <h1>CardRadar</h1>
        <p>Backend is running.</p>
        <p>Version: 5.0</p>
    </body>
    </html>
    """


# ---------------------------------------------------------
# START
# ---------------------------------------------------------

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
