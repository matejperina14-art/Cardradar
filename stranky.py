"""
CARD RADAR – stranky.py
  - dáta pre úvodnú stránku (/api/home): zľavy, najlacnejšie ETB/boxy/bundle, top karty, nové sety
  - podmienky, ochrana údajov, pre obchody, robots.txt
  - potvrdzovacie stránky strážcu
  - admin: /admin/test (kontrola hľadania), /admin/obchody (pridanie obchodu), /admin/katalog

Texty stránok upravuješ priamo v TERMS_HTML, PRIVACY_HTML, SHOPS_HTML nižšie.
"""

import hmac
import html
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from flask import Response, jsonify, redirect, request

import logika as L
import obchody as O

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
OPERATOR_NAME = os.environ.get("OPERATOR_NAME", "prevádzkovateľ CardRadar")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "") or os.environ.get("SMTP_FROM", "")
ALLOW_INDEXING = os.environ.get("ALLOW_INDEXING", "0") == "1"


def is_admin():
    key = request.values.get("key", "")
    return bool(ADMIN_KEY) and hmac.compare_digest(key.encode(), ADMIN_KEY.encode())


# =========================================================
# ÚVODNÁ STRÁNKA
# =========================================================

_ETB_RE = re.compile(r"elite\s+trainer\s+box|\betb\b", re.I)
_BOX_RE = re.compile(r"booster\s*(?:box|display)", re.I)
_BUNDLE_RE = re.compile(r"\bbundle\b", re.I)
_SEALED_RE = re.compile(r"booster|bundle|elite\s+trainer|\betb\b|collection|kolekci|\btins?\b|blister|"
                        r"display|deck|premium|build\s*(?:&|and)?\s*battle|\bbox\b|chest|bal[íi][čc]|"
                        r"starter|academy|\bcase\b|mystery|plechovk|\bsada\b", re.I)

_home = {"t": 0.0, "data": None}
_home_lock = threading.Lock()


def _latest_rows():
    now = datetime.now(timezone.utc)
    since3 = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    since30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    conn = O.db()
    try:
        return conn.execute("""
            SELECT p.link, p.title, p.shop, p.price_eur, p.stock, p.image,
                   (SELECT MAX(q.price_eur) FROM price_daily q
                     WHERE q.link = p.link AND q.day >= ? AND q.day < p.day),
                   (SELECT MAX(q.image) FROM price_daily q WHERE q.link = p.link)
            FROM price_daily p
            JOIN (SELECT link, MAX(day) AS d FROM price_daily WHERE day >= ? GROUP BY link) r
              ON p.link = r.link AND p.day = r.d
            WHERE p.price_eur > 0 AND (p.stock IS NULL OR p.stock != 'out')
        """, (since30, since3)).fetchall()
    finally:
        conn.close()


def _uniq(items, limit, key=lambda x: x["price_eur"], reverse=False):
    out, seen = [], set()
    for it in sorted(items, key=key, reverse=reverse):
        k = it["group"] or it["link"]
        if k not in seen:
            seen.add(k)
            out.append(it)
            if len(out) >= limit:
                break
    return out


def build_home():
    deals, etb, boxes, bundles, cards, all_items = [], [], [], [], [], []
    for link, title, shop, price, stock, image, old_max, any_image in _latest_rows():
        if not L.is_tcg_product(title):
            continue
        lang = L.detect_language(title)
        if lang in L.ASIAN_LANGS:
            continue
        it = {"link": link, "title": title, "shop": shop, "price_eur": round(price, 2),
              "stock": stock or "", "image": image or any_image or "", "language": lang,
              "group": L.group_key(title, lang),
              "query": (O.make_suggestion(title) or {}).get("query") or title}
        all_items.append(it)
        if old_max and old_max - price >= 1 and price <= old_max * 0.97:
            deals.append(dict(it, old_price_eur=round(old_max, 2),
                              drop_pct=round((old_max - price) / old_max * 100)))
        if L.is_combo(title) or L._BULK_RE.search(title):
            continue
        if _ETB_RE.search(title):
            etb.append(it)
        elif _BOX_RE.search(title):
            boxes.append(it)
        elif _BUNDLE_RE.search(title) and not re.search(r"display", title, re.I):
            bundles.append(it)
        elif not _SEALED_RE.search(title) and price >= 10 and L.CARD_MARKER_RE.search(title):
            cards.append(it)   # len skutočné karty (číslo, rarita, PSA...), nie iné produkty

    sets = []
    for s in L.NOVE_SETY:
        s = dict(s)
        mine = [i for i in all_items if L.set_matches_text(i["title"], s["query"])]
        s_etb = [i for i in mine if _ETB_RE.search(i["title"])]
        s_box = [i for i in mine if _BOX_RE.search(i["title"])]
        pic = next((i["image"] for i in s_etb + s_box + mine if i["image"].startswith("https://")), "")
        if pic:
            s["image"] = pic
        if s_etb:
            s["etb_from"] = min(i["price_eur"] for i in s_etb)
        if s_box:
            s["box_from"] = min(i["price_eur"] for i in s_box)
        s["offers"] = len(mine)
        sets.append(s)

    return {
        "deals": _uniq(deals, 12, key=lambda d: d["drop_pct"], reverse=True),
        "cheap_etb": _uniq(etb, 8),
        "cheap_box": _uniq(boxes, 12),
        "cheap_bundle": _uniq(bundles, 12),
        "top_cards": _uniq(cards, 12, reverse=True),
        "popular": O.popular_queries(),
        "new_sets": sets,
        "kurz_czk": {"rate": L.KURZ["CZK"], "date": L.KURZ_INFO.get("date", "")},
    }


def home_data():
    """Úvodná stránka z pamäte (najviac 10 min stará)."""
    if _home["data"] is None or time.monotonic() - _home["t"] > 600:
        with _home_lock:
            if _home["data"] is None or time.monotonic() - _home["t"] > 600:
                try:
                    _home["data"] = build_home()
                except Exception as e:
                    print(f"[CardRadar] Úvodná stránka: {e}", flush=True)
                    _home["data"] = _home["data"] or {"new_sets": L.NOVE_SETY}
                _home["t"] = time.monotonic()
    return _home["data"]


def _home_warmer():
    """Pripraví úvodnú stránku vopred, aby ju nikto nemusel čakať."""
    time.sleep(20)
    while True:
        try:
            data = build_home()
            _home["data"], _home["t"] = data, time.monotonic()
        except Exception:
            pass
        time.sleep(480)


def start_background():
    threading.Thread(target=_home_warmer, daemon=True, name="home").start()


# =========================================================
# TEXTOVÉ STRÁNKY
# =========================================================

PAGE_CSS = """
body{margin:0;background:#f3f5fa;color:#0d1633;font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
header{background:#101b44;padding:16px 20px}
header a{display:inline-flex;align-items:center;gap:10px;font-weight:800;font-size:21px;text-decoration:none;color:#38d8ff;
  text-shadow:0 0 4px rgba(56,216,255,.55),0 0 14px rgba(56,216,255,.45)}
main{max-width:760px;margin:24px auto 60px;padding:28px 24px;background:#fff;border:1px solid #e2e6ef;border-radius:14px}
h1{font-size:28px;line-height:1.2;margin:6px 0 14px}h2{font-size:18px;margin:1.6em 0 .4em}
p,li{color:#3c4766}a{color:#2453d6}.back{font-size:14px;text-decoration:none}
header img{width:34px;height:34px;border-radius:9px;display:block}
code{background:#f3f5fa;border-radius:5px;padding:1px 5px;font-size:.92em}
.upd{margin-top:2em;font-size:13px;color:#8b94ad}
.box{text-align:center}.btn{display:inline-block;margin-top:12px;background:#ffcf3a;color:#101b44;
  padding:11px 18px;border-radius:10px;font-weight:800;text-decoration:none}
input,button{font:inherit;padding:10px;border-radius:9px;border:1px solid #d3d9e6}
button{background:#ffcf3a;border:0;font-weight:800;cursor:pointer}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}.ok{color:#0a8a4a}.err{color:#d4334b}
.w{color:#b7791f}small{color:#8b94ad}li{margin:4px 0}
details{border:1px solid #e2e6ef;border-radius:12px;padding:10px 12px;margin:8px 0}summary{cursor:pointer}
table{width:100%;border-collapse:collapse}td,th{padding:7px 6px;border-bottom:1px solid #e2e6ef;text-align:left}
.wrap{overflow-x:auto}.bar{height:12px;background:#eef1f7;border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;background:#2453d6}
"""


def page(title, body, back=True, status=200):
    home = PUBLIC_URL or "/"
    link = f'<a class="back" href="{home}">← Späť na CardRadar</a>' if back else ""
    doc = f"""<!doctype html><html lang="sk"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} – CardRadar</title>
<link rel="icon" href="/static/icon-192.png">
<style>{PAGE_CSS}</style><header><a href="{home}"><img src="/static/icon-192.png" alt="" onerror="this.remove()">CardRadar</a></header>
<main>{link}{body}</main>"""
    return Response(doc, status=status, mimetype="text/html", headers={"Cache-Control": "no-store"})


def simple_page(title, text):
    """Krátke oznámenie (potvrdenie strážcu a pod.)."""
    return page(title, f'<div class="box"><h1>{title}</h1><p>{text}</p>'
                       f'<a class="btn" href="{PUBLIC_URL or "/"}">Späť na CardRadar</a></div>', back=False)


def _contact():
    if CONTACT_EMAIL:
        e = html.escape(CONTACT_EMAIL)
        return f'<a href="mailto:{e}">{e}</a>'
    return "cez kontakt uvedený na webe"


def _text_page(title, body):
    body = body.replace("{KONTAKT}", _contact()).replace("{PREVADZKOVATEL}", html.escape(OPERATOR_NAME))
    return page(title, f"<h1>{title}</h1>{body}<p class='upd'>Posledná aktualizácia: október 2026</p>")


TERMS_HTML = """
<p>CardRadar je bezplatný porovnávač cien Pokémon TCG produktov, ktorý prevádzkuje {PREVADZKOVATEL}.</p>
<h2>Čo CardRadar robí</h2>
<p>Zobrazuje ceny a dostupnosť produktov z verejne dostupných stránok a feedov internetových obchodov.
CardRadar nič nepredáva. Nákup prebieha vždy priamo v obchode a riadi sa jeho obchodnými podmienkami.</p>
<h2>Presnosť údajov</h2>
<p>Ceny a sklad sa aktualizujú automaticky, no môžu byť oneskorené alebo nepresné. Pred nákupom
si vždy over cenu a dostupnosť v obchode. Ceny z českých obchodov prepočítavame z Kč na € podľa
aktuálneho denného kurzu Európskej centrálnej banky, preto sú orientačné.</p>
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
<li><b>IP adresa:</b> drží sa v pamäti servera asi minútu, na ochranu pred zneužitím
(obmedzenie počtu požiadaviek). Do databázy sa neukladá, krátkodobo sa môže objaviť
v technických záznamoch servera.</li>
<li><b>Obľúbené produkty</b> sa ukladajú iba v tvojom prehliadači, nie na serveri.</li>
</ul>
<h2>Ako dlho</h2>
<p>Nepotvrdený strážca sa zmaže po 7 dňoch, splnený 30 dní po odoslaní upozornenia.
Aktívny strážca trvá, kým ho nezrušíš odkazom v e-maile.</p>
<h2>Kto k údajom má prístup</h2>
<p>Web beží na serveroch spoločnosti Render Services, Inc. v dátovom centre vo Frankfurte (EÚ).
E-maily strážcu ceny odosiela služba Resend zo serverov v Írsku (EÚ). Doménu a DNS spravuje
Cloudflare. Stránka nenačítava písma ani skripty od tretích strán. Údaje nepredávame
ani nezdieľame na reklamné účely.</p>
<h2>Cookies</h2>
<p>CardRadar nepoužíva reklamné ani sledovacie cookies. Prehliadač si ukladá len súbory
potrebné na rýchlejšie načítanie a obľúbené produkty.</p>
<h2>Tvoje práva</h2>
<p>Máš právo na prístup k údajom, ich opravu, vymazanie a odvolanie súhlasu. Stačí napísať na
kontakt vyššie. Sťažnosť môžeš podať na Úrad na ochranu osobných údajov SR (dataprotection.gov.sk).</p>
"""

SHOPS_HTML = """
<p>CardRadar je bezplatný porovnávač cien Pokémon TCG kariet, ETB a booster boxov zo slovenských
obchodov. Zberateľ zadá kartu alebo set a na jednom mieste vidí, kde je produkt skladom
a za koľko. Kliknutím ide <b>priamo na stránku produktu vo vašom e-shope</b>. CardRadar nič nepredáva.</p>
<h2>Čo o vašich produktoch zobrazujeme</h2>
<ul>
<li>názov produktu, aktuálnu cenu a dostupnosť (skladom, predobjednávka, vypredané),</li>
<li>obrázok produktu, vždy načítaný z vášho webu,</li>
<li>názov vášho obchodu a odkaz na konkrétny produkt.</li>
</ul>
<h2>Návštevnosť, ktorú uvidíte</h2>
<p>Všetky odkazy na váš e-shop majú parameter <code>utm_source=cardradar</code>, takže
návštevy aj objednávky z CardRadaru uvidíte v Google Analytics alebo v štatistikách vášho
e-shopu ako samostatný zdroj.</p>
<h2>Ako údaje získavame</h2>
<p>Ideálne z vášho XML feedu produktov (formát Heureka alebo Google Merchant). Je to najpresnejšie
a váš web to nijako nezaťaží. Bez feedu čítame verejne dostupné stránky vyhľadávania alebo
kategórií, šetrne: výsledky si pamätáme a kategórie prechádzame najviac raz za hodinu s pauzou
medzi stránkami. Náš robot sa predstavuje ako <code>CardRadarBot</code>.</p>
<h2>Opravy a odstránenie</h2>
<p>Ak je niektorý údaj nesprávny, opravíme ho. Ak si neželáte, aby bol váš obchod na CardRadare,
stačí napísať a odstránime ho, zvyčajne do 24 hodín.</p>
<h2>Spolupráca</h2>
<p>Radi sa dohodneme na XML feede, partnerskom programe alebo zvýraznení nových produktov
a predobjednávok. Napíšte nám: {KONTAKT}</p>
"""


def terms_page():
    return _text_page("Podmienky používania", TERMS_HTML)


def privacy_page():
    return _text_page("Ochrana osobných údajov", PRIVACY_HTML)


def shops_page():
    return _text_page("Pre obchody", SHOPS_HTML)


def robots_txt():
    if ALLOW_INDEXING:
        body = "User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin/\nDisallow: /alerts/\n"
    else:
        body = "User-agent: *\nDisallow: /\n"
    return Response(body, mimetype="text/plain")


# =========================================================
# ADMIN: KONTROLA HĽADANIA (/admin/test?key=...)
# Beží na pozadí, priebeh sa ukladá do databázy (vidia ho všetky procesy).
# =========================================================

SELFTEST_QUERIES = [
    "pitch black booster box", "pitch black etb", "chaos rising etb",
    "destined rivals etb", "destined rivals booster box", "prismatic evolutions etb",
    "surging sparks booster bundle", "151 etb", "ascended heroes etb",
    "charizard ex", "pikachu ex", "umbreon vmax", "rare candy", "trick or trade",
]


def _kind(t):
    if _ETB_RE.search(t):
        return "etb"
    if _BOX_RE.search(t):
        return "box"
    if re.search(r"booster|bundle|collection|kolekci|\btins?\b|blister|battle\s+deck|premium|display", t, re.I):
        return "other"
    return "card"


def _check_query(q):
    parsed = L.normalize_query(q)
    norm = parsed["normalized"] or q
    t0 = time.monotonic()
    results, diagnostics = O.search_all(norm, wait_all=True)
    want = {"elite trainer box": "etb", "booster box": "box"}.get(parsed["product_type"])
    issues, merge = [], {}
    for r in results:
        t, p, c = r["title"], r["price_eur"] or 0, _kind(r["title"])
        asia = r.get("language") in L.ASIAN_LANGS
        why = []
        if p <= 0 or p > 3000:
            why.append("nezmyselná cena")
        if c == "etb" and p < 30 and not asia:
            why.append("ETB pod 30 €")
        if c == "box" and p < 70 and not asia:
            why.append("booster box pod 70 €")
        if c == "box" and p > 700:
            why.append("booster box nad 700 €")
        if parsed["set_name"] and not L.set_matches_text(t, parsed["set_name"]):
            why.append("iný set")
        if want and c != want:
            why.append("iný typ produktu")
        if not r["image"]:
            why.append("bez obrázka")
        if not r["stock"]:
            why.append("sklad neuvedený")
        if why:
            issues.append({"shop": r["shop"], "title": t, "price": p, "why": why})
        if c != "card" and not L.is_combo(t):
            gt = next((n for n, rx in L.GROUP_TYPES if rx.search(t)), "")
            pp = L.normalize_query(t)
            if gt and gt not in L._DETAIL_TYPES and gt != "collection" and pp["set_name"]:
                k = (pp["set_name"], gt, pp["pokemon"].lower(), r.get("language") or "EN")
                merge.setdefault(k, []).append(r)
    missed = [[f"{r['shop']}: {r['title']}" for r in rs] for rs in merge.values()
              if len({r["shop"] for r in rs}) > 1 and len({r["group"] for r in rs}) > 1]
    return {"query": q, "ms": round((time.monotonic() - t0) * 1000), "results": len(results),
            "shops": O.shops_status(diagnostics), "issues": issues, "missed": missed}


def _run_selftest(queries):
    state = {"status": "running", "started": time.time(), "done": 0, "total": len(queries), "report": []}
    O.meta_set("selftest", json.dumps(state))
    out = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_check_query, q): q for q in queries}
        for f in as_completed(futs):
            q = futs[f]
            try:
                out[q] = f.result()
            except Exception as e:
                out[q] = {"query": q, "ms": 0, "results": 0, "shops": [], "missed": [],
                          "issues": [{"shop": "-", "title": "Chyba testu: " + str(e)[:150],
                                      "price": 0, "why": ["chyba testu"]}]}
            state["done"] = len(out)
            O.meta_set("selftest", json.dumps(state))
    state.update(status="done", finished=time.time(), report=[out[q] for q in queries if q in out])
    O.meta_set("selftest", json.dumps(state, ensure_ascii=False))


def _report_html(state):
    e = html.escape
    report = state.get("report", [])
    shops = {}
    for qr in report:
        for d in qr["shops"]:
            s = shops.setdefault(d["name"], {"ok": 0, "n": 0, "res": 0, "ms": 0, "errs": set()})
            s["n"] += 1
            s["ms"] += d["elapsed_ms"] or 0
            s["res"] += d["results"] or 0
            if d["ok"]:
                s["ok"] += 1
            else:
                s["errs"].add(d["status"])
    rows = "".join(
        f"<tr><td>{e(n)}</td><td class='{'ok' if s['ok'] == s['n'] else 'err'}'>{s['ok']}/{s['n']}</td>"
        f"<td>{s['res']}</td><td>{round(s['ms'] / max(1, s['n']))} ms</td><td>{e(', '.join(sorted(s['errs'])))}</td></tr>"
        for n, s in shops.items())
    kinds = {}
    for qr in report:
        for it in qr["issues"]:
            for w in it["why"]:
                kinds[w] = kinds.get(w, 0) + 1
    kinds_html = "".join(f"<li><b>{e(k)}</b>: {v}×</li>" for k, v in sorted(kinds.items(), key=lambda x: -x[1])) \
        or "<li>Nič 🎉</li>"
    blocks = ""
    for qr in report:
        serious = [i for i in qr["issues"] if set(i["why"]) - {"bez obrázka", "sklad neuvedený"}]
        lines = "".join(f"<li><b>{e(i['shop'])}</b> · {e(i['title'])} · {i['price']:.2f} € "
                        f"<span class='w'>{e(', '.join(i['why']))}</span></li>" for i in serious[:15])
        miss = "".join(f"<li>{'<br>'.join(e(x) for x in m)}</li>" for m in qr["missed"][:6])
        blocks += (f"<details{' open' if lines or miss else ''}><summary><b>{e(qr['query'])}</b> · "
                   f"{qr['results']} ponúk · {qr['ms']} ms · {len(serious)} problémov</summary>"
                   + (f"<p>Podozrivé:</p><ul>{lines}</ul>" if lines else "")
                   + (f"<p>Rovnaký produkt v rôznych obchodoch, ale nespojený:</p><ul>{miss}</ul>" if miss else "")
                   + ("" if lines or miss else "<p class='ok'>Bez problémov.</p>") + "</details>")
    when = datetime.fromtimestamp(state.get("finished", time.time()), timezone.utc).strftime("%d.%m. %H:%M UTC")
    again = "/admin/test?key=" + urllib.parse.quote(request.args.get("key", "")) + "&start=1"
    return (f"<h1>Kontrola hľadania</h1><p>Dokončené {when}. <a href='{e(again)}'>Spustiť znova</a></p>"
            f"<h2>Obchody</h2><div class='wrap'><table><tr><th>Obchod</th><th>Odpovedal</th><th>Ponúk</th>"
            f"<th>Čas</th><th>Chyby</th></tr>{rows}</table></div>"
            f"<h2>Typy problémov</h2><ul>{kinds_html}</ul><h2>Hľadania</h2>{blocks}")


def admin_test():
    if not is_admin():
        return page("Nepovolené", "<h1>Nepovolené</h1><p>Pridaj ?key=ADMIN_KEY</p>", status=403)
    try:
        state = json.loads(O.meta_get("selftest") or "null")
    except Exception:
        state = None
    running = bool(state and state.get("status") == "running" and time.time() - state.get("started", 0) < 600)
    if not running and (request.args.get("start") or not state or state.get("status") == "running"):
        queries = [q.strip() for q in request.args.get("q", "").split(",") if q.strip()] or SELFTEST_QUERIES
        threading.Thread(target=_run_selftest, args=(queries[:20],), daemon=True, name="selftest").start()
        state, running = {"status": "running", "done": 0, "total": len(queries[:20])}, True
    if running:
        pct = round(state.get("done", 0) / max(1, state.get("total", 1)) * 100)
        resp = page("Kontrola beží", f"<div class='box'><h1>Kontrola beží…</h1>"
                                     f"<p>Hotových {state.get('done', 0)} z {state.get('total', 0)} hľadaní</p>"
                                     f"<div class='bar'><i style='width:{pct}%'></i></div>"
                                     f"<p><small>Stránka sa obnovuje sama, trvá to 1 – 2 minúty.</small></p></div>",
                    back=False)
        resp.headers["Refresh"] = "4"
        return resp
    if request.args.get("format") == "json":
        return jsonify(state)
    return page("Kontrola hľadania", _report_html(state), back=False)


# =========================================================
# ADMIN: OBCHODY (/admin/obchody?key=...)
# Otestuješ ľubovoľný e-shop a jedným ťuknutím ho zapneš / vypneš.
# =========================================================

def admin_obchody():
    e = html.escape
    if not is_admin():
        return page("Nepovolené", "<h1>Nepovolené</h1><p>Pridaj ?key=ADMIN_KEY</p>", status=403)
    key = request.values.get("key", "")
    base = "/admin/obchody?key=" + urllib.parse.quote(key)
    saved = O.extra_shops()

    if request.method == "POST":
        act = request.form.get("act")
        if act == "add":
            try:
                c = json.loads(request.form.get("config", ""))
                ok = O.host_of(c["search_url"]) == O.host_of(c["base_url"])
            except Exception:
                ok = False
            if ok:
                c["name"] = (request.form.get("name") or c["name"]).strip()[:40]
                c = {k: c[k] for k in ("name", "country", "base_url", "search_url", "link_selector", "shopify")
                     if k in c}
                O.save_extra_shops([s for s in saved if s["name"] != c["name"]] + [c])
        elif act == "remove":
            O.save_extra_shops([s for s in saved if s["name"] != request.form.get("name")])
        return redirect(base, code=303)

    hidden = f"<input type=hidden name=key value='{e(key)}'>"
    h = "<h1>Obchody</h1><h2>Zapnuté obchody</h2><ul>"
    for s in O.active_shops():
        kind = "katalóg" if O.is_catalog(s) else "vyhľadávanie"
        h += f"<li><b>{e(s['name'])}</b> <small>{e(s['country'])} · {kind}</small>"
        if s.get("_extra"):
            h += (f"<form method=post class=row>{hidden}<input type=hidden name=act value=remove>"
                  f"<input type=hidden name=name value='{e(s['name'])}'><button>Vypnúť</button></form>")
        h += "</li>"
    h += "</ul><p><small>Obchody bez tlačidla Vypnúť sú v súbore obchody.py (zoznam SHOPS).</small></p>"

    test = request.args.get("test", "").strip()
    q = request.args.get("q", "").strip() or "pikachu"
    h += (f"<h2>Otestovať obchod</h2><form class=row>{hidden}"
          f"<input name=test placeholder='https://www.obchod.cz' value='{e(test)}' style='flex:1'>"
          f"<input name=q value='{e(q)}' style='width:130px'><button>Test</button></form>")
    if test:
        r = O.detect_shop(test, q)
        if r.get("platform"):
            h += f"<p>Platforma: <b>{e(r['platform'])}</b></p>"
        for t in r.get("tried", []):
            h += (f"<p><small>{e(t['url'])} → {e(str(t['status']))}, odkazov {t['links']}, "
                  f"produktov {t['results']}</small></p>")
        if r.get("error"):
            h += f"<p class=err>{e(r['error'])}</p>"
        if r.get("config"):
            h += "<p class=ok>Funguje. Ukážka:</p><ul>" + "".join(
                f"<li>{e(x['title'])} – <b>{x['price_eur']:.2f} €</b> <small>{e(x['stock'] or '?')}</small></li>"
                for x in r["sample"]) + "</ul>"
            h += (f"<form method=post class=row>{hidden}<input type=hidden name=act value=add>"
                  f"<input type=hidden name=config value='{e(json.dumps(r['config']))}'>"
                  f"<input name=name value='{e(r['config']['name'])}'><button>Zapnúť obchod</button></form>"
                  f"<p><small>Skontroluj ceny v ukážke. Ak sedia, zapni.</small></p>")

    h += "<h2>Návrhy na otestovanie</h2><ul>" + "".join(
        f"<li><a href='{e(base)}&test={urllib.parse.quote(u)}'>{e(n)}</a> <small>{e(u)}</small></li>"
        for n, u in O.KANDIDATI) + "</ul>"
    return page("Obchody", h, back=False)


# =========================================================
# ADMIN: KATALÓGY (/admin/katalog?key=...  a  &name=imago&refresh=1)
# =========================================================

def admin_katalog():
    if not is_admin():
        return jsonify({"error": "Nepovolené. Pridaj ?key=ADMIN_KEY"}), 403
    shops = [s for s in O.SHOPS if O.is_catalog(s)]
    name = L.clean_text(request.args.get("name", "")).lower()
    if name:
        shop = next((s for s in shops if s["name"].lower() == name), None)
        if not shop:
            return jsonify({"error": "Neznámy obchod.", "shops": [s["name"] for s in shops]}), 400
        if request.args.get("refresh"):
            return jsonify(O.crawl_shop(shop))
        items, updated = O.load_catalog(shop["name"])
        return jsonify({"shop": shop["name"], "items": len(items), "updated": updated, "sample": items[:20]})
    return jsonify({"refresh_min": O.CATALOG_REFRESH_MIN, "catalogs": [
        {"shop": s["name"], "enabled": s.get("enabled", True), "items": len(O.load_catalog(s["name"])[0]),
         "updated": O.load_catalog(s["name"])[1], "crawling": O.is_crawling(s["name"])} for s in shops]})
