import os
import re
import sqlite3
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
DB_PATH = os.environ.get("CARD_RADAR_DB", "cardradar.db")
TIMEOUT = 12
USER_AGENT = "CardRadar/1.0 (+https://cardradar.com)"

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
    "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7",
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

def parse_price(text):
    if not text:
        return None
    text = text.replace("\xa0", " ").strip()
    patterns = [
        r"(\d[\d\s.,]*)\s*(?:€|EUR)",
        r"(\d[\d\s.,]*)\s*(?:Kč|CZK)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            raw = m.group(1).replace(" ", "").replace(".", "").replace(",", ".")
            try:
                # Ošetrenie pre prípad viacerých desatinných bodiek/čiarkok
                if raw.count('.') > 1:
                    parts = raw.split('.')
                    raw = "".join(parts[:-1]) + "." + parts[-1]
                
                value = float(raw)
                if "Kč" in m.group(0) or "CZK" in m.group(0):
                    return value / get_czk_rate()
                return value
            except ValueError:
                pass
    return None

def get_czk_rate():
    try:
        r = session.get("https://api.frankfurter.app/latest?from=CZK&to=EUR", timeout=8)
        r.raise_for_status()
        return float(r.json()["rates"]["EUR"])
    except Exception:
        return 1 / 25.0

def pokemon_info(query):
    try:
        url = "https://api.pokemontcg.io/v2/cards"
        r = session.get(url, params={
            "q": f'name:"{query}"',
            "orderBy": "-set.releaseDate",
            "pageSize": 1
        }, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return {}
        card = data[0]
        return {
            "title": card.get("name") or query,
            "image": (card.get("images") or {}).get("large") or (card.get("images") or {}).get("small"),
            "subtitle": f'{card.get("set", {}).get("name", "")} · {card.get("rarity", "")}'.strip(" ·"),
        }
    except Exception:
        return {}

def extract_candidates(html, base_url, query):
    soup = BeautifulSoup(html, "html.parser")
    qwords = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", query) if len(w) > 2]
    candidates = []

    # Zoznam slov, ktoré chceme ignorovať (plyšáky, oblečenie, doplnky atď.)
    exclude_words = ["plyšák", "plyšová", "plush", "tričko", "tricko", "figúrka", "figurka", "hrnček", "obal", "sleeves", "deck box", "odznak"]

    nodes = soup.find_all(["a", "article", "li", "div"])
    seen = set()
    for node in nodes:
        text = " ".join(node.stripped_strings)
        if len(text) < 10 or len(text) > 600:
            continue
        low = text.lower()
        
        # Preskočiť, ak produkt obsahuje nežiaduci výraz (plyšák a pod.)
        if any(bad in low for bad in exclude_words):
            continue

        score = sum(1 for w in qwords if w in low)
        if score == 0:
            continue
            
        price = parse_price(text)
        # Nastavený reálny cenový strop (napr. karta málokedy stojí viac ako 2500 € v bežnom obchode)
        if price is None or price <= 0 or price > 2500:
            continue
            
        link = node if node.name == "a" and node.get("href") else node.find("a", href=True)
        if not link:
            continue
        href = urljoin(base_url, link.get("href"))
        if href in seen:
            continue
        seen.add(href)
        
        title = " ".join(link.stripped_strings) or text[:160]
        for tag in node.find_all(["h1","h2","h3","h4","strong"], limit=2):
            t = " ".join(tag.stripped_strings)
            if len(t) >= 3:
                title = t
                break
                
        candidates.append({
            "title": title[:220],
            "price_eur": round(price, 2),
            "link": href,
            "score": score,
        })
        if len(candidates) >= 12:
            break

    candidates.sort(key=lambda x: (-x["score"], x["price_eur"]))
    return candidates[:5]

def scrape_shop(shop, query):
    url = shop["search_url"].format(q=quote(query))
    try:
        r = session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        candidates = extract_candidates(r.text, r.url, query)
        if not candidates:
            return []
        result = []
        for c in candidates:
            result.append({
                "title": c["title"],
                "price_eur": c["price_eur"],
                "shop": shop["name"],
                "country": shop["country"],
                "condition": "Sealed" if re.search(r"ETB|box|booster|collection", query, re.I) else "NM",
                "language": "EN",
                "link": c["link"],
                "in_stock": True,
            })
        return result
    except Exception as e:
        return []

def save_history(query, results):
    if not results:
        return
    conn = db()
    now = datetime.utcnow().isoformat()
    for x in results:
        conn.execute(
            """INSERT INTO price_history
               (query, shop, country, title, price_eur, url, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (query, x["shop"], x["country"], x["title"], x["price_eur"], x["link"], now)
        )
    conn.commit()
    conn.close()

@app.get("/")
def index():
    return render_template("index.html")

@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Zadaj hľadaný výraz."}), 400

    info = pokemon_info(query)
    results = []
    with ThreadPoolExecutor(max_workers=len(SHOPS)) as pool:
        futures = [pool.submit(scrape_shop, shop, query) for shop in SHOPS]
        for future in as_completed(futures):
            try:
                results.extend(future.result())
            except Exception:
                pass

    results.sort(key=lambda x: x["price_eur"])
    save_history(query, results)

    history = []
    conn = db()
    rows = conn.execute("""
        SELECT price_eur, checked_at FROM price_history
        WHERE query = ? ORDER BY id DESC LIMIT 30
    """, (query,)).fetchall()
    conn.close()
    for price, checked in reversed(rows):
        history.append({"price_eur": price, "checked_at": checked})

    return jsonify({
        "query": query,
        "info": info,
        "results": results,
        "history": history,
        "czk_per_eur": round(1 / get_czk_rate(), 4)
    })

@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "CardRadar"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
