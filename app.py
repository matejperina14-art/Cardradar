import os
import re
import json
import sqlite3
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin, unquote

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request


app = Flask(__name__)

DB_PATH = os.environ.get("CARD_RADAR_DB", "cardradar.db")
TIMEOUT = 15

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/26.6.1 Mobile/15E148 Safari/604.1 "
    "CardRadar/2.0"
)

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
})


# =========================================================
# OBCHODY
# =========================================================

SHOPS = [
    {
        "name": "CardyX",
        "country": "SK",
        "search_url": "https://www.cardyx.sk/vyhladavanie?s={q}",
        "domain": "cardyx.sk",
    },
    {
        "name": "iHRYsko",
        "country": "SK",
        "search_url": "https://www.ihrysko.sk/vyhladavanie?q={q}",
        "domain": "ihrysko.sk",
    },
    {
        "name": "Černý Rytíř",
        "country": "CZ",
        "search_url": "https://www.cernyrytir.cz/index.php3?akce=3&jmeno={q}",
        "domain": "cernyrytir.cz",
    },
    {
        "name": "Veselý Drak",
        "country": "CZ",
        "search_url": "https://www.vesely-drak.cz/vyhladavanie/?search={q}",
        "domain": "vesely-drak.cz",
    },
]


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            shop TEXT NOT NULL,
            country TEXT NOT NULL,
            title TEXT NOT NULL,
            price_eur REAL NOT NULL,
            url TEXT NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)

    conn.commit()
    return conn


# =========================================================
# NORMALIZÁCIA TEXTU
# =========================================================

def normalize_text(text):
    if not text:
        return ""

    text = unquote(str(text))
    text = text.replace("\xa0", " ")

    # malé písmená
    text = text.lower()

    # rôzne pomlčky -> normálna pomlčka
    text = re.sub(r"[‐-‒–—−]", "-", text)

    # desatinné/oddeľovacie znaky necháme,
    # ale zjednotíme medzery
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def clean_query(query):
    query = normalize_text(query)

    # odstránenie zbytočných znakov na začiatku/konci
    query = query.strip(" -_/")

    return query


def query_tokens(query):
    """
    Rozdelí dotaz na významové tokeny.

    Príklad:
    Mega Charizard X ex 013/094

    ->
    mega
    charizard
    x
    ex
    013
    094
    """

    query = normalize_text(query)

    tokens = re.findall(
        r"[a-z0-9]+",
        query
    )

    return tokens


# =========================================================
# ROZPOZNANIE TYPU VYHĽADÁVANIA
# =========================================================

def detect_search_type(query):
    q = normalize_text(query)

    # Číslo karty napr. 013/094
    card_number = re.search(
        r"\b(\d{1,4})\s*/\s*(\d{1,4})\b",
        q
    )

    # Sealed produkty
    sealed_words = [
        "booster box",
        "boosterbox",
        "booster bundle",
        "booster pack",
        "elite trainer box",
        "etb",
        "collection box",
        "premium collection",
        "collection",
        "tin",
        "bundle",
        "box",
        "pack",
    ]

    is_sealed = any(
        word in q
        for word in sealed_words
    )

    return {
        "is_card_number": bool(card_number),
        "card_number": (
            card_number.group(0)
            if card_number
            else None
        ),
        "is_sealed": is_sealed,
    }


# =========================================================
# MERCH FILTER
# =========================================================

EXCLUDE_WORDS = [
    "plyšák",
    "plyšová",
    "plyš",
    "plush",
    "tričko",
    "tricko",
    "shirt",
    "t-shirt",
    "mikina",
    "hoodie",
    "figúrka",
    "figurka",
    "figure",
    "figurine",
    "hrnček",
    "hrnek",
    "mug",
    "obal",
    "sleeves",
    "sleeve",
    "deck box",
    "deckbox",
    "odznak",
    "badge",
    "album",
    "binder",
    "zápisník",
    "notebook",
    "playmat",
    "podložka",
    "mat",
    "minca",
    "coin",
    "keychain",
    "kľúčenka",
    "klucenka",
    "puzzle",
    "batoh",
    "backpack",
    "taška",
    "taska",
    "bag",
    "poster",
    "plagát",
    "figurky",
]


def is_merch(title):
    low = normalize_text(title)

    return any(
        word in low
        for word in EXCLUDE_WORDS
    )


# =========================================================
# RELEVANTNOSŤ PRODUKTU
# =========================================================

def token_matches(token, title):
    """
    Kontroluje skutočnú zhodu tokenu.

    Nepovoľuje napríklad:
    'x' -> náhodné písmeno vo futbalovom produkte.

    Jednopísmenové tokeny sa kontrolujú ako celé slová.
    """

    title = normalize_text(title)

    if len(token) == 1:
        return bool(
            re.search(
                rf"\b{re.escape(token)}\b",
                title
            )
        )

    return token in title


def product_relevance(query, title):
    """
    Vráti skóre zhody produktu s dotazom.

    Vyššie skóre = lepšia zhoda.
    """

    query = clean_query(query)
    title = normalize_text(title)

    if not query or not title:
        return 0

    search = detect_search_type(query)

    tokens = query_tokens(query)

    if not tokens:
        return 0

    matched = 0

    for token in tokens:
        if token_matches(token, title):
            matched += 1

    # -----------------------------------------------------
    # Povinná zhoda pri čísle karty
    # -----------------------------------------------------

    if search["card_number"]:

        number = normalize_text(
            search["card_number"]
        )

        normalized_number = re.sub(
            r"\s+",
            "",
            number
        )

        normalized_title = re.sub(
            r"\s+",
            "",
            title
        )

        if normalized_number not in normalized_title:
            return 0

    # -----------------------------------------------------
    # Presná fráza
    # -----------------------------------------------------

    normalized_query = re.sub(
        r"\s+",
        " ",
        query
    )

    if normalized_query in title:
        return 100 + matched

    # -----------------------------------------------------
    # Pri karte s číslom musí sedieť väčšina tokenov
    # -----------------------------------------------------

    if search["is_card_number"]:

        required = len(tokens)

        if matched < max(
            2,
            required - 1
        ):
            return 0

    else:

        # Pri bežnom hľadaní požadujeme aspoň
        # polovicu významových slov.
        if matched < max(
            1,
            int(len(tokens) * 0.5)
        ):
            return 0

    score = matched * 10

    # bonus za Charizard + X + ex atď.
    if search["is_card_number"]:
        score += 50

    # bonus, ak je názov veľmi podobný dotazu
    for token in tokens:
        if token in title:
            score += 2

    return score


# =========================================================
# MENA
# =========================================================

def get_czk_rate():
    try:
        r = session.get(
            "https://api.frankfurter.app/latest",
            params={
                "from": "CZK",
                "to": "EUR"
            },
            timeout=8
        )

        r.raise_for_status()

        rate = float(
            r.json()["rates"]["EUR"]
        )

        if 0 < rate < 1:
            return rate

    except Exception:
        pass

    return 0.04


def parse_number(value):
    if value is None:
        return None

    value = str(value)
    value = value.replace("\xa0", " ")
    value = value.strip()

    if not value:
        return None

    # 1 299,99
    value = value.replace(" ", "")

    if "," in value and "." in value:

        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "")
            value = value.replace(",", ".")
        else:
            value = value.replace(",", "")

    elif "," in value:
        value = value.replace(",", ".")

    try:
        return float(value)

    except Exception:
        return None


def price_to_eur(value, currency):
    number = parse_number(value)

    if number is None:
        return None

    currency = (
        str(currency)
        .strip()
        .upper()
    )

    if currency in [
        "EUR",
        "€",
    ]:
        return number

    if currency in [
        "CZK",
        "KČ",
        "Kc".upper(),
    ]:
        return number * get_czk_rate()

    return None


# =========================================================
# DÔVERYHODNÁ CENA
# =========================================================

def valid_price(price):
    if price is None:
        return False

    # CardRadar zatiaľ pracuje s kartami/sealed
    # produktmi v rozumnom retailovom rozsahu.
    #
    # Ak sa raz budeme chcieť venovať drahým graded kartám,
    # tento limit upravíme podľa typu produktu.

    if price <= 0:
        return False

    if price > 5000:
        return False

    return True


# =========================================================
# JSON-LD
# =========================================================

def extract_json_ld_product(soup):
    products = []

    for script in soup.find_all(
        "script",
        type="application/ld+json"
    ):

        raw = script.string

        if not raw:
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        objects = []

        if isinstance(data, list):
            objects.extend(data)

        elif isinstance(data, dict):

            if "@graph" in data and isinstance(
                data["@graph"],
                list
            ):
                objects.extend(
                    data["@graph"]
                )

            else:
                objects.append(data)

        for obj in objects:

            if not isinstance(obj, dict):
                continue

            obj_type = obj.get(
                "@type",
                ""
            )

            if isinstance(
                obj_type,
                list
            ):
                is_product = (
                    "Product" in obj_type
                )
            else:
                is_product = (
                    obj_type == "Product"
                )

            if not is_product:
                continue

            products.append(obj)

    return products


def extract_json_ld_data(soup):
    products = extract_json_ld_product(
        soup
    )

    for product in products:

        name = product.get(
            "name"
        )

        offers = product.get(
            "offers"
        )

        if isinstance(
            offers,
            list
        ):
            offers = (
                offers[0]
                if offers
                else None
            )

        if not isinstance(
            offers,
            dict
        ):
            offers = {}

        price = offers.get(
            "price"
        )

        currency = offers.get(
            "priceCurrency"
        )

        availability = offers.get(
            "availability",
            ""
        )

        if name or price:

            return {
                "name": name,
                "price": price,
                "currency": currency,
                "availability": availability,
                "source": "json-ld",
            }

    return None


# =========================================================
# CENA Z PRODUKTOVEJ STRÁNKY
# =========================================================

def extract_price_from_page(soup):

    # -----------------------------------------------------
    # 1. JSON-LD
    # -----------------------------------------------------

    data = extract_json_ld_data(
        soup
    )

    if data:

        price = price_to_eur(
            data.get("price"),
            data.get("currency")
        )

        if valid_price(price):

            return {
                "price_eur": round(
                    price,
                    2
                ),
                "source": "json-ld",
            }

    # -----------------------------------------------------
    # 2. itemprop price
    # -----------------------------------------------------

    elements = soup.select(
        "[itemprop='price']"
    )

    for element in elements:

        value = (
            element.get("content")
            or element.get_text(
                " ",
                strip=True
            )
        )

        currency = (
            element.get(
                "data-currency"
            )
            or element.get(
                "itemprop",
                ""
            )
        )

        # Pokusíme sa nájsť menu v okolí
        parent_text = ""

        if element.parent:
            parent_text = element.parent.get_text(
                " ",
                strip=True
            )

        if "Kč" in parent_text or "CZK" in parent_text:
            currency = "CZK"
        elif "€" in parent_text or "EUR" in parent_text:
            currency = "EUR"

        price = price_to_eur(
            value,
            currency
        )

        if valid_price(price):
            return {
                "price_eur": round(
                    price,
                    2
                ),
                "source": "itemprop",
            }

    # -----------------------------------------------------
    # 3. Špecifické cenové elementy
    # -----------------------------------------------------

    selectors = [
        ".product-price",
        ".product__price",
        ".current-price",
        ".price-final",
        ".price-new",
        ".selling-price",
        ".price",
    ]

    for selector in selectors:

        try:
            elements = soup.select(
                selector
            )
        except Exception:
            elements = []

        for element in elements:

            text = element.get_text(
                " ",
                strip=True
            )

            if not text:
                continue

            # EUR
            eur = re.search(
                r"(\d[\d\s.,]*)\s*(?:€|EUR)",
                text,
                re.I
            )

            if eur:

                price = price_to_eur(
                    eur.group(1),
                    "EUR"
                )

                if valid_price(price):
                    return {
                        "price_eur": round(
                            price,
                            2
                        ),
                        "source": "price-element",
                    }

            # CZK
            czk = re.search(
                r"(\d[\d\s.,]*)\s*(?:Kč|CZK)",
                text,
                re.I
            )

            if czk:

                price = price_to_eur(
                    czk.group(1),
                    "CZK"
                )

                if valid_price(price):
                    return {
                        "price_eur": round(
                            price,
                            2
                        ),
                        "source": "price-element",
                    }

    # -----------------------------------------------------
    # ŽIADNY NÁHODNÝ TEXT CELEJ STRÁNKY
    # -----------------------------------------------------
    #
    # Toto je zámerné.
    #
    # Ak nevieme nájsť dôveryhodný cenový element,
    # produkt radšej nevrátime.
    #

    return None


# =========================================================
# SKLAD
# =========================================================

def extract_stock(soup):

    data = extract_json_ld_data(
        soup
    )

    if data:

        availability = normalize_text(
            data.get(
                "availability",
                ""
            )
        )

        if "outofstock" in availability:
            return False

        if "instock" in availability:
            return True

    text = normalize_text(
        soup.get_text(
            " ",
            strip=True
        )
    )

    unavailable = [
        "vypredané",
        "vyprodáno",
        "nie je skladom",
        "není skladem",
        "out of stock",
        "sold out",
        "unavailable",
    ]

    if any(
        word in text
        for word in unavailable
    ):
        return False

    return True


# =========================================================
# NÁZOV PRODUKTU
# =========================================================

def extract_product_title(
    soup,
    fallback=""
):

    data = extract_json_ld_data(
        soup
    )

    if data and data.get("name"):
        return str(
            data["name"]
        ).strip()

    og = soup.find(
        "meta",
        property="og:title"
    )

    if og and og.get("content"):
        return og["content"].strip()

    h1 = soup.find("h1")

    if h1:
        text = h1.get_text(
            " ",
            strip=True
        )

        if text:
            return text

    if soup.title:
        return soup.title.get_text(
            " ",
            strip=True
        )

    return fallback


# =========================================================
# PRODUKTOVÉ LINKY
# =========================================================

def find_product_links(
    html,
    base_url,
    query,
    domain
):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates = []
    seen = set()

    for a in soup.find_all(
        "a",
        href=True
    ):

        href = a.get(
            "href"
        )

        if not href:
            continue

        url = urljoin(
            base_url,
            href
        )

        if domain not in url:
            continue

        if url in seen:
            continue

        text = a.get_text(
            " ",
            strip=True
        )

        # Ak link nemá text, pozri rodiča
        if not text and a.parent:
            text = a.parent.get_text(
                " ",
                strip=True
            )

        if not text:
            continue

        if len(text) > 500:
            continue

        # Merch vyradíme už tu
        if is_merch(text):
            continue

        score = product_relevance(
            query,
            text
        )

        if score <= 0:
            continue

        seen.add(url)

        candidates.append({
            "title": text,
            "url": url,
            "score": score,
        })

    candidates.sort(
        key=lambda x: (
            -x["score"],
            len(x["title"])
        )
    )

    return candidates[:12]


# =========================================================
# SCRAPE JEDNÉHO PRODUKTU
# =========================================================

def scrape_product(
    shop,
    candidate,
    query
):

    try:

        response = session.get(
            candidate["url"],
            timeout=TIMEOUT,
            allow_redirects=True
        )

        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        title = extract_product_title(
            soup,
            candidate["title"]
        )

        # -------------------------------------------------
        # KRITICKÁ KONTROLA NÁZVU
        # -------------------------------------------------

        score = product_relevance(
            query,
            title
        )

        if score <= 0:
            return None

        # -------------------------------------------------
        # MERCH
        # -------------------------------------------------

        if is_merch(title):
            return None

        # -------------------------------------------------
        # CENA
        # -------------------------------------------------

        price_data = extract_price_from_page(
            soup
        )

        if not price_data:
            return None

        price = price_data[
            "price_eur"
        ]

        if not valid_price(price):
            return None

        # -------------------------------------------------
        # STOCK
        # -------------------------------------------------

        in_stock = extract_stock(
            soup
        )

        # -------------------------------------------------
        # TYPE
        # -------------------------------------------------

        low_title = normalize_text(
            title
        )

        if any(
            word in low_title
            for word in [
                "booster box",
                "boosterbox",
                "booster pack",
                "elite trainer box",
                "etb",
                "collection box",
                "premium collection",
                "bundle",
                "tin",
            ]
        ):
            condition = "Sealed"
        else:
            condition = "NM"

        return {
            "title": title[:300],
            "price_eur": price,
            "shop": shop["name"],
            "country": shop["country"],
            "condition": condition,
            "language": "EN",
            "link": response.url,
            "in_stock": in_stock,
            "match_score": score,
            "price_source": price_data[
                "source"
            ],
        }

    except Exception:
        return None


# =========================================================
# SCRAPE OBCHODU
# =========================================================

def scrape_shop(
    shop,
    query
):

    try:

        search_url = shop[
            "search_url"
        ].format(
            q=quote(query)
        )

        response = session.get(
            search_url,
            timeout=TIMEOUT,
            allow_redirects=True
        )

        response.raise_for_status()

        candidates = find_product_links(
            response.text,
            response.url,
            query,
            shop["domain"]
        )

        if not candidates:
            return []

        results = []

        # Otvoríme iba najlepších kandidátov.
        for candidate in candidates[:8]:

            product = scrape_product(
                shop,
                candidate,
                query
            )

            if product:
                results.append(
                    product
                )

        # odstránenie duplicít
        unique = {}

        for item in results:

            key = (
                item["shop"],
                item["link"]
            )

            unique[key] = item

        results = list(
            unique.values()
        )

        # najprv najlepšia zhoda,
        # potom cena
        results.sort(
            key=lambda x: (
                -x["match_score"],
                x["price_eur"]
            )
        )

        return results[:5]

    except Exception:
        return []


# =========================================================
# POKEMON TCG API
# =========================================================

def pokemon_info(query):

    try:

        # Pri čísle karty odstránime číslo,
        # pretože API hľadá názov karty.
        clean = re.sub(
            r"\b\d{1,4}\s*/\s*\d{1,4}\b",
            "",
            query
        ).strip()

        url = (
            "https://api.pokemontcg.io/v2/cards"
        )

        response = session.get(
            url,
            params={
                "q": f'name:"{clean}"',
                "orderBy": "-set.releaseDate",
                "pageSize": 1,
            },
            timeout=10
        )

        response.raise_for_status()

        data = response.json().get(
            "data",
            []
        )

        if not data:
            return {}

        card = data[0]

        images = card.get(
            "images"
        ) or {}

        card_set = card.get(
            "set"
        ) or {}

        return {
            "title": card.get(
                "name"
            ) or clean,

            "image": (
                images.get("large")
                or images.get("small")
            ),

            "subtitle": (
                f'{card_set.get("name", "")}'
                f' · '
                f'{card.get("rarity", "")}'
            ).strip(" ·"),

            "set": card_set.get(
                "name"
            ),

            "number": card.get(
                "number"
            ),
        }

    except Exception:
        return {}


# =========================================================
# HISTÓRIA
# =========================================================

def save_history(
    query,
    results
):

    if not results:
        return

    conn = db()

    now = datetime.now(
        timezone.utc
    ).isoformat()

    for item in results:

        # Do histórie ukladaj iba
        # dôveryhodné ceny.
        if not valid_price(
            item["price_eur"]
        ):
            continue

        conn.execute(
            """
            INSERT INTO price_history
            (
                query,
                shop,
                country,
                title,
                price_eur,
                url,
                checked_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                query,
                item["shop"],
                item["country"],
                item["title"],
                item["price_eur"],
                item["link"],
                now,
            )
        )

    conn.commit()
    conn.close()


def get_history(query):

    conn = db()

    rows = conn.execute(
        """
        SELECT
            price_eur,
            checked_at
        FROM price_history
        WHERE query = ?
        AND price_eur > 0
        AND price_eur <= 5000
        ORDER BY id DESC
        LIMIT 50
        """,
        (query,)
    ).fetchall()

    conn.close()

    history = []

    for price, checked in reversed(
        rows
    ):

        history.append({
            "price_eur": price,
            "checked_at": checked,
        })

    return history


# =========================================================
# API SEARCH
# =========================================================

@app.get("/")
def index():

    return render_template(
        "index.html"
    )


@app.get("/api/search")
def api_search():

    query = clean_query(
        request.args.get(
            "q",
            ""
        )
    )

    if not query:

        return jsonify({
            "error": "Zadaj hľadaný výraz."
        }), 400

    # Informácie o karte
    info = pokemon_info(
        query
    )

    results = []

    # -----------------------------------------------------
    # VŠETKY OBCHODY PARALELNE
    # -----------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=len(SHOPS)
    ) as pool:

        futures = [
            pool.submit(
                scrape_shop,
                shop,
                query
            )
            for shop in SHOPS
        ]

        for future in as_completed(
            futures
        ):

            try:

                shop_results = (
                    future.result()
                )

                if shop_results:
                    results.extend(
                        shop_results
                    )

            except Exception:
                pass

    # -----------------------------------------------------
    # GLOBÁLNE ODSTRÁNENIE DUPLICÍT
    # -----------------------------------------------------

    unique = {}

    for item in results:

        key = (
            item["shop"],
            item["link"]
        )

        if key not in unique:
            unique[key] = item

    results = list(
        unique.values()
    )

    # -----------------------------------------------------
    # IBA DÔVERYHODNÉ VÝSLEDKY
    # -----------------------------------------------------

    results = [
        item
        for item in results
        if valid_price(
            item["price_eur"]
        )
    ]

    # -----------------------------------------------------
    # NAJLEPŠIA ZHODA + CENA
    # -----------------------------------------------------

    results.sort(
        key=lambda x: (
            -x["match_score"],
            x["price_eur"]
        )
    )

    # match_score nechceme nutne zobrazovať
    # na frontend-e
    for item in results:
        item.pop(
            "match_score",
            None
        )
        item.pop(
            "price_source",
            None
        )

    # -----------------------------------------------------
    # HISTÓRIA
    # -----------------------------------------------------

    save_history(
        query,
        results
    )

    history = get_history(
        query
    )

    # -----------------------------------------------------
    # KURZ
    # -----------------------------------------------------

    czk_rate = get_czk_rate()

    if czk_rate > 0:
        czk_per_eur = round(
            1 / czk_rate,
            4
        )
    else:
        czk_per_eur = 25.0

    # -----------------------------------------------------
    # RESPONSE
    # -----------------------------------------------------

    return jsonify({

        "query": query,

        "info": info,

        "results": results,

        "history": history,

        "czk_per_eur": czk_per_eur,

        "shops_checked": [
            shop["name"]
            for shop in SHOPS
        ],

        "checked_at": datetime.now(
            timezone.utc
        ).isoformat(),
    })


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():

    return jsonify({
        "status": "ok",
        "service": "CardRadar",
        "version": "2.0",
        "shops": len(SHOPS),
    })


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False
    )
