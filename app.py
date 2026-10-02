import os
import re
import sqlite3
import json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request


app = Flask(__name__)

DB_PATH = os.environ.get("CARD_RADAR_DB", "cardradar.db")
TIMEOUT = 15

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1 "
    "CardRadar/1.0"
)

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

        rate = float(r.json()["rates"]["EUR"])

        if 0 < rate < 1:
            return rate

    except Exception:
        pass

    return 0.04


def normalize_number(raw):
    if not raw:
        return None

    raw = raw.replace("\xa0", " ")
    raw = raw.replace(" ", "")
    raw = raw.strip()

    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "")
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")

    elif "," in raw:
        raw = raw.replace(".", "")
        raw = raw.replace(",", ".")

    elif "." in raw:
        parts = raw.split(".")

        if len(parts) == 2 and len(parts[1]) == 3:
            raw = "".join(parts)

    try:
        return float(raw)
    except Exception:
        return None


def parse_price(text):
    if not text:
        return None

    text = text.replace("\xa0", " ")

    patterns = [
        (r"(\d[\d\s.,]*)\s*(?:€|EUR)", "EUR"),
        (r"(\d[\d\s.,]*)\s*(?:Kč|CZK)", "CZK"),
    ]

    for pattern, currency in patterns:
        match = re.search(pattern, text, re.I)

        if not match:
            continue

        value = normalize_number(match.group(1))

        if value is None:
            continue

        if value <= 0 or value > 100000:
            continue

        if currency == "CZK":
            return value * get_czk_rate()

        return value

    return None


def pokemon_info(query):
    try:
        url = "https://api.pokemontcg.io/v2/cards"

        r = session.get(
            url,
            params={
                "q": f'name:"{query}"',
                "orderBy": "-set.releaseDate",
                "pageSize": 1
            },
            timeout=10
        )

        r.raise_for_status()

        data = r.json().get("data", [])

        if not data:
            return {}

        card = data[0]
        images = card.get("images") or {}
        card_set = card.get("set") or {}

        return {
            "title": card.get("name") or query,
            "image": images.get("large") or images.get("small"),
            "subtitle": (
                f'{card_set.get("name", "")} · '
                f'{card.get("rarity", "")}'
            ).strip(" ·"),
        }

    except Exception:
        return {}


EXCLUDE_WORDS = [
    "plyšák",
    "plyšová",
    "plyš",
    "plush",
    "tričko",
    "tricko",
    "mikina",
    "figúrka",
    "figurka",
    "hrnček",
    "hrnek",
    "obal",
    "sleeves",
    "deck box",
    "deckbox",
    "odznak",
    "album",
    "zápisník",
    "playmat",
    "podložka",
    "minca",
    "coin",
    "keychain",
    "kľúčenka",
    "klucenka",
    "puzzle",
    "batoh",
    "taška",
    "taska",
]


def is_excluded_product(title):
    low = title.lower()
    return any(word in low for word in EXCLUDE_WORDS)


def query_words(query):
    return [
        word.lower()
        for word in re.findall(r"[a-zA-Z0-9]+", query)
        if len(word) > 1
    ]


def title_score(title, query):
    low = title.lower()
    words = query_words(query)

    if not words:
        return 0

    score = 0

    for word in words:
        if word in low:
            score += 1

    normalized_title = re.sub(r"\s+", " ", low).strip()
    normalized_query = re.sub(
        r"\s+",
        " ",
        query.lower()
    ).strip()

    if normalized_query in normalized_title:
        score += 5

    return score


def find_product_links(html, base_url, query, domain):
    soup = BeautifulSoup(html, "html.parser")

    candidates = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href")

        if not href:
            continue

        full_url = urljoin(base_url, href)

        if domain not in full_url:
            continue

        if full_url in seen:
            continue

        text = " ".join(a.stripped_strings).strip()

        if not text and a.parent:
            text = " ".join(a.parent.stripped_strings)

        if len(text) < 3 or len(text) > 400:
            continue

        if is_excluded_product(text):
            continue

        score = title_score(text, query)

        if score <= 0:
            continue

        seen.add(full_url)

        candidates.append({
            "title": text[:250],
            "url": full_url,
            "score": score,
        })

    candidates.sort(
        key=lambda x: (
            -x["score"],
            len(x["title"])
        )
    )

    return candidates[:10]


def extract_json_ld_prices(soup):
    prices = []

    for script in soup.find_all(
        "script",
        type="application/ld+json"
    ):
        try:
            data = script.string

            if not data:
                continue

            parsed = json.loads(data)

            objects = (
                parsed
                if isinstance(parsed, list)
                else [parsed]
            )

            for obj in objects:
                if not isinstance(obj, dict):
                    continue

                offers = obj.get("offers")

                if not offers:
                    continue

                if isinstance(offers, dict):
                    offers = [offers]

                for offer in offers:
                    if not isinstance(offer, dict):
                        continue

                    price = offer.get("price")
                    currency = offer.get(
                        "priceCurrency",
                        ""
                    )

                    if price is None:
                        continue

                    try:
                        value = float(
                            str(price).replace(",", ".")
                        )

                        if currency.upper() == "CZK":
                            value *= get_czk_rate()

                        if 0 < value <= 100000:
                            prices.append(value)

                    except Exception:
                        pass

        except Exception:
            continue

    return prices


def extract_product_page(
    html,
    url,
    fallback_title,
    query
):
    soup = BeautifulSoup(html, "html.parser")

    title = None

    og_title = soup.find(
        "meta",
        property="og:title"
    )

    if og_title and og_title.get("content"):
        title = og_title["content"].strip()

    if not title:
        h1 = soup.find("h1")

        if h1:
            title = " ".join(
                h1.stripped_strings
            )

    if not title and soup.title:
        title = soup.title.get_text(
            " ",
            strip=True
        )

    if not title:
        title = fallback_title

    title = title[:300]

    if is_excluded_product(title):
        return None

    prices = extract_json_ld_prices(soup)

    price = min(prices) if prices else None

    if price is None:
        selectors = [
            "[itemprop='price']",
            ".price",
            ".product-price",
            ".product__price",
            ".current-price",
            ".price-final",
            ".price-new",
            ".selling-price",
            "[class*='price']",
        ]

        price_texts = []

        for selector in selectors:
            try:
                elements = soup.select(selector)
            except Exception:
                elements = []

            for element in elements:
                text = " ".join(
                    element.stripped_strings
                )

                if text:
                    price_texts.append(text)

        for text in price_texts:
            parsed = parse_price(text)

            if parsed is not None and parsed <= 400:
                price = parsed
                break

    if price is None:
        body_text = soup.get_text(
            " ",
            strip=True
        )

        patterns = [
            r"(?:Cena|Cena s DPH|Price)\s*:?\s*"
            r"([0-9\s.,]+)\s*(?:€|EUR)",

            r"([0-9\s.,]+)\s*(?:€|EUR)",

            r"([0-9\s.,]+)\s*(?:Kč|CZK)",
        ]

        for pattern in patterns:
            match = re.search(
                pattern,
                body_text,
                re.I
            )

            if not match:
                continue

            parsed = parse_price(
                match.group(0)
            )

            if parsed is not None and parsed <= 400:
                price = parsed
                break

    if price is None:
        return None

    body = soup.get_text(
        " ",
        strip=True
    ).lower()

    unavailable_words = [
        "vypredané",
        "vyprodáno",
        "nie je skladom",
        "není skladem",
        "out of stock",
        "sold out",
        "unavailable",
    ]

    in_stock = not any(
        word in body
        for word in unavailable_words
    )

    condition = "NM"

    if re.search(
        r"\b(etb|booster box|boosterbox|"
        r"booster|box|collection|bundle)\b",
        title,
        re.I
    ):
        condition = "Sealed"

    return {
        "title": title,
        "price_eur": round(price, 2),
        "link": url,
        "in_stock": in_stock,
        "condition": condition,
        "language": "EN",
    }


def scrape_shop(shop, query):
    search_url = shop["search_url"].format(
        q=quote(query)
    )

    try:
        response = session.get(
            search_url,
            timeout=TIMEOUT,
            allow_redirects=True
        )

        response.raise_for_status()

        links = find_product_links(
            response.text,
            response.url,
            query,
            shop["domain"]
        )

        if not links:
            return []

        results = []

        for candidate in links[:6]:
            try:
                product_response = session.get(
                    candidate["url"],
                    timeout=TIMEOUT,
                    allow_redirects=True
                )

                product_response.raise_for_status()

                product = extract_product_page(
                    product_response.text,
                    product_response.url,
                    candidate["title"],
                    query
                )

                if not product:
                    continue

                product["shop"] = shop["name"]
                product["country"] = shop["country"]

                score = title_score(
                    product["title"],
                    query
                )

                if score <= 0:
                    continue

                results.append(product)

            except Exception:
                continue

        unique = {}

        for item in results:
            key = (
                item["shop"],
                item["link"]
            )

            unique[key] = item

        results = list(unique.values())

        results.sort(
            key=lambda x: x["price_eur"]
        )

        return results[:5]

    except Exception:
        return []


def save_history(query, results):
    if not results:
        return

    conn = db()

    now = datetime.now(
        timezone.utc
    ).isoformat()

    for item in results:
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
                now
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
        ORDER BY id DESC
        LIMIT 30
        """,
        (query,)
    ).fetchall()

    conn.close()

    history = []

    for price, checked in reversed(rows):
        history.append({
            "price_eur": price,
            "checked_at": checked
        })

    return history


@app.get("/")
def index():
    return render_template(
        "index.html"
    )


@app.get("/api/search")
def api_search():
    query = request.args.get(
        "q",
        ""
    ).strip()

    if not query:
        return jsonify({
            "error": "Zadaj hľadaný výraz."
        }), 400

    info = pokemon_info(query)

    results = []

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

        for future in as_completed(futures):
            try:
                shop_results = future.result()

                if shop_results:
                    results.extend(
                        shop_results
                    )

            except Exception:
                pass

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

    results.sort(
        key=lambda x: (
            x["price_eur"],
            x["shop"]
        )
    )

    save_history(
        query,
        results
    )

    history = get_history(
        query
    )

    czk_rate = get_czk_rate()

    czk_per_eur = (
        round(
            1 / czk_rate,
            4
        )
        if czk_rate > 0
        else 25.0
    )

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


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "CardRadar",
        "shops": len(SHOPS),
    })


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
