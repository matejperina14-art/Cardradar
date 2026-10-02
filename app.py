import os
import re
import sqlite3
import unicodedata
from datetime import datetime
from urllib.parse import quote, urljoin

from flask import Flask, jsonify, request, send_from_directory
from bs4 import BeautifulSoup
import requests

app = Flask(__name__)

VERSION = "5.1"
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
CZK_PER_EUR = 24.4618


# =========================================================
# DATABASE
# =========================================================

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


# =========================================================
# NORMALIZATION
# =========================================================

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
    return re.findall(
        r"[a-z0-9]+",
        normalize(text)
    )


# =========================================================
# QUERY TYPE
# =========================================================

MERCH_WORDS = {
    "plysak",
    "plysovy",
    "hracka",
    "hracky",
    "figurka",
    "funko",
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


# =========================================================
# CARD NUMBER
# =========================================================

def card_number_from_query(query):

    m = re.search(
        r"\b(\d{1,3})\s*/\s*(\d{1,3})\b",
        query
    )

    if not m:
        return None

    return (
        f"{int(m.group(1))}/"
        f"{int(m.group(2))}"
    )


# =========================================================
# PRODUCT FILTERS
# =========================================================

def candidate_is_merch(title):

    t = normalize(title)

    for bad in MERCH_WORDS:

        if re.search(
            r"\b" + re.escape(bad) + r"\b",
            t
        ):
            return True

    return False


def card_matches_query(title, query):

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

        if not re.search(
            r"(?<![a-z0-9])ex(?![a-z0-9])",
            title_n
        ):
            return False

    # -----------------------------------------------------
    # VMAX
    # -----------------------------------------------------

    if "vmax" in qwords:

        if "vmax" not in title_n:
            return False

    # -----------------------------------------------------
    # VSTAR
    # -----------------------------------------------------

    if "vstar" in qwords:

        if "vstar" not in title_n:
            return False

    # -----------------------------------------------------
    # GX
    # -----------------------------------------------------

    if "gx" in qwords:

        if not re.search(
            r"(?<![a-z0-9])gx(?![a-z0-9])",
            title_n
        ):
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
    # IMPORTANT NAME WORDS
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
        w
        for w in qwords
        if w not in ignore
        and not w.isdigit()
    ]

    for word in important:

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

    # BOOSTER BOX
    if (
        "booster" in qwords
        and "box" in qwords
    ):

        if "booster box" not in title_n:
            return False

    # BOOSTER BUNDLE
    if (
        "booster" in qwords
        and "bundle" in qwords
    ):

        if "booster bundle" not in title_n:
            return False

    # TIN
    if "tin" in qwords:

        if not re.search(
            r"\btin\b",
            title_n
        ):
            return False

    # COLLECTION
    if "collection" in qwords:

        if "collection" not in title_n:
            return False

    # SET / PRODUCT NAME
    important = []

    for w in qwords:

        if w in SEALED_WORDS:
            continue

        if w in {
            "pokemon",
            "tcg"
        }:
            continue

        important.append(w)

    for word in important:

        if len(word) <= 1:
            continue

        if word not in title_n:
            return False

    return True


# =========================================================
# PRICE
# =========================================================

def parse_number(value):

    if value is None:
        return None

    value = str(value).strip()

    value = value.replace(
        "\xa0",
        " "
    )

    value = value.replace(
        "€",
        ""
    )

    value = value.replace(
        "EUR",
        ""
    )

    value = value.replace(
        "Kč",
        ""
    )

    value = value.replace(
        "CZK",
        ""
    )

    value = value.strip()

    if "," in value and "." in value:

        if value.rfind(",") > value.rfind("."):

            value = value.replace(
                ".",
                ""
            )

            value = value.replace(
                ",",
                "."
            )

        else:

            value = value.replace(
                ",",
                ""
            )

    elif "," in value:

        value = value.replace(
            ".",
            ""
        )

        value = value.replace(
            ",",
            "."
        )

    else:

        if value.count(".") > 1:

            parts = value.split(".")

            value = (
                "".join(parts[:-1])
                + "."
                + parts[-1]
            )

    m = re.search(
        r"\d+(?:\.\d+)?",
        value
    )

    if not m:
        return None

    try:
        return float(m.group(0))

    except Exception:
        return None


def czk_to_eur(value):

    if value is None:
        return None

    return float(value) / CZK_PER_EUR


# =========================================================
# HTTP
# =========================================================

def get(url):

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT
        )

        if response.status_code != 200:
            return None

        return response

    except Exception:

        return None


# =========================================================
# CARDYX
# =========================================================

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

        m = re.search(
            r'"price"\s*:\s*"?(\\?[\d.,]+)',
            raw
        )

        if m:

            value = parse_number(
                m.group(1)
            )

            if (
                value is not None
                and 0.5 <= value <= 100000
            ):
                return value

    # -----------------------------------------------------
    # META
    # -----------------------------------------------------

    meta = soup.select_one(
        'meta[property="product:price:amount"]'
    )

    if meta:

        value = parse_number(
            meta.get("content")
        )

        if (
            value is not None
            and 0.5 <= value <= 100000
        ):
            return value

    # -----------------------------------------------------
    # SHOPIFY PRICE
    # -----------------------------------------------------

    selectors = [
        ".price-item--sale",
        ".price-item--regular",
        ".price__regular",
        ".price",
    ]

    for selector in selectors:

        for node in soup.select(
            selector
        ):

            value = parse_number(
                node.get_text(
                    " ",
                    strip=True
                )
            )

            if (
                value is not None
                and 0.5 <= value <= 100000
            ):
                return value

    return None


def search_cardyx_cards(query):

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

    links = soup.select(
        "a[href*='/products/']"
    )

    seen = set()

    for a in links:

        href = a.get("href")

        if not href:
            continue

        product_url = urljoin(
            "https://www.cardyx.sk",
            href
        )

        if product_url in seen:
            continue

        seen.add(product_url)

        title = a.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        parent = a.parent

        if parent:

            parent_text = parent.get_text(
                " ",
                strip=True
            )

            if len(parent_text) > len(title):
                title = parent_text

        if not card_matches_query(
            title,
            query
        ):
            continue

        price = extract_cardyx_price_from_product(
            product_url
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
            "price_eur": round(
                price,
                2
            ),
            "link": product_url,
        })

    return results


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

    links = soup.select(
        "a[href*='/products/']"
    )

    seen = set()

    for a in links:

        href = a.get("href")

        if not href:
            continue

        product_url = urljoin(
            "https://www.cardyx.sk",
            href
        )

        if product_url in seen:
            continue

        seen.add(product_url)

        title = a.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        parent = a.parent

        if parent:

            parent_text = parent.get_text(
                " ",
                strip=True
            )

            if len(parent_text) > len(title):
                title = parent_text

        if not sealed_matches_query(
            title,
            query
        ):
            continue

        price = extract_cardyx_price_from_product(
            product_url
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
            "price_eur": round(
                price,
                2
            ),
            "link": product_url,
        })

    return results


# =========================================================
# OTHER SHOPS
# =========================================================

SHOPS = [

    {
        "name": "Veselý Drak",
        "country": "CZ",
        "base": "https://www.vesely-drak.cz",
        "search": (
            "https://www.vesely-drak.cz/"
            "vyhledavani/?q={q}"
        ),
    },

    {
        "name": "iHRYsko",
        "country": "SK",
        "base": "https://www.ihrysko.sk",
        "search": (
            "https://www.ihrysko.sk/"
            "vysledky-vyhladavania/?q={q}"
        ),
    },

    {
        "name": "Černý Rytíř",
        "country": "CZ",
        "base": "https://www.cernyrytir.cz",
        "search": (
            "https://www.cernyrytir.cz/"
            "index.php3?akce=3&stranka=1&search={q}"
        ),
    },
]


def find_price_near_link(
    anchor,
    country
):

    nodes = []

    parent = anchor

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

            if (
                value is not None
                and 0.5 <= value <= 100000
            ):
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

                if (
                    value is not None
                    and 1 <= value <= 1000000
                ):
                    return czk_to_eur(
                        value
                    )

    return None


def generic_search(
    shop,
    query,
    mode
):

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

    links = soup.find_all(
        "a",
        href=True
    )

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

        if len(title) < 3:
            continue

        if len(title) > 300:
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

        link = urljoin(
            shop["base"],
            href
        )

        if link in seen:
            continue

        seen.add(link)

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
            "condition": (
                "Sealed"
                if mode == "sealed"
                else "NM"
            ),
            "in_stock": True,
            "title": title,
            "price_eur": round(
                price,
                2
            ),
            "link": link,
        })

        if len(results) >= 10:
            break

    return results


# =========================================================
# HISTORY
# =========================================================

def save_history(
    query,
    result
):

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


# =========================================================
# SORT
# =========================================================

def sort_results(results):

    unique = {}

    for result in results:

        key = (
            normalize(
                result["title"]
            ),
            result["shop"],
            result["link"],
        )

        unique[key] = result

    results = list(
        unique.values()
    )

    results.sort(
        key=lambda x:
        x["price_eur"]
    )

    return results


# =========================================================
# API SEARCH
# =========================================================

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

    mode = classify_query(
        query
    )

    results = []

    # -----------------------------------------------------
    # CARDYX
    # -----------------------------------------------------

    if mode == "card":

        results.extend(
            search_cardyx_cards(
                query
            )
        )

    else:

        results.extend(
            search_cardyx_sealed(
                query
            )
        )

    # -----------------------------------------------------
    # OTHER SHOPS
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
    # FINAL FILTER
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

        if price <= 0:
            continue

        if candidate_is_merch(
            title
        ):
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

        clean.append(
            result
        )

    results = sort_results(
        clean
    )

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


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    return jsonify({
        "service": "CardRadar",
        "status": "ok",
        "version": VERSION,
    })


# =========================================================
# FRONTEND
# =========================================================

@app.route("/")
def home():

    return send_from_directory(
        ".",
        "index.html"
    )


# =========================================================
# START
# =========================================================

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
