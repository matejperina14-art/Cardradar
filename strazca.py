"""
CARD RADAR – strazca.py
Strážca ceny a naskladnenia:
  - vytvorenie strážcu + potvrdzovací e-mail
  - kontrola cien na pozadí (každých ALERT_CHECK_HOURS hodín)
  - e-mail, keď cena klesne / produkt je skladom
  - mazanie starých strážcov (nepotvrdení po 7 dňoch, splnení po 30 dňoch)

Bez SMTP_HOST v premenných prostredia je strážca vypnutý.
"""

import html
import json
import os
import re
import secrets
import smtplib
import threading
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage

from bs4 import BeautifulSoup

import logika as L
import obchody as O

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
ALERT_CHECK_HOURS = float(os.environ.get("ALERT_CHECK_HOURS", "6"))
ALERTS_ENABLED = bool(SMTP_HOST and SMTP_FROM)

ALERTS_PER_EMAIL = 20
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-z]{2,24}$", re.I)


def eur(v):
    try:
        return f"{float(v):,.2f}".replace(",", " ").replace(".", ",") + " €"
    except (TypeError, ValueError):
        return "–"


# =========================================================
# VYTVORENIE, POTVRDENIE, ZRUŠENIE
# =========================================================

def create_alert(email, link, title, shop, target, site):
    """Vráti (http_kód, {"status"/"error", "message"})."""
    if not ALERTS_ENABLED:
        return 503, {"error": "Strážca ceny zatiaľ nie je na serveri zapnutý."}
    email = L.clean_text(email).lower()
    link, title, shop = L.clean_text(link), L.clean_text(title)[:200], L.clean_text(shop)[:60]
    try:
        target = round(float(target), 2)
    except (TypeError, ValueError):
        target = 0
    if not EMAIL_RE.match(email):
        return 400, {"error": "Zadaj platný e-mail."}
    if not O.is_allowed_link(link):
        return 400, {"error": "Neplatný produkt."}
    if not 0 < target < 100000:
        return 400, {"error": "Zadaj cieľovú cenu."}

    site = PUBLIC_URL or site
    token = secrets.token_urlsafe(24)
    conn = O.db()
    try:
        if conn.execute("SELECT COUNT(*) FROM alerts WHERE email = ?", (email,)).fetchone()[0] >= ALERTS_PER_EMAIL:
            return 400, {"error": f"Na jeden e-mail môžeš mať najviac {ALERTS_PER_EMAIL} strážcov."}
        existing = conn.execute("SELECT id, confirmed FROM alerts WHERE email = ? AND link = ?",
                                (email, link)).fetchone()
        if existing:
            conn.execute("UPDATE alerts SET target = ?, notified = NULL, token = ? WHERE id = ?",
                         (target, token, existing[0]))
            confirmed = bool(existing[1])
        else:
            conn.execute("INSERT INTO alerts (email, link, title, shop, target, token, confirmed, created, site) "
                         "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
                         (email, link, title, shop, target, token,
                          datetime.now(timezone.utc).isoformat(), site))
            confirmed = False
        conn.commit()
    finally:
        conn.close()

    if confirmed:
        return 200, {"status": "ok", "message": f"Strážca upravený na {eur(target)}."}
    try:
        _confirm_mail(email, title, shop, link, target, token, site)
    except Exception as e:
        print(f"[CardRadar] E-mail sa neodoslal: {e}", flush=True)
        return 502, {"error": "Potvrdzovací e-mail sa nepodarilo odoslať. Skús to neskôr."}
    return 200, {"status": "ok", "message": "Poslali sme ti e-mail. Strážca začne fungovať po potvrdení."}


def confirm_alert(token):
    """Vráti (cieľová cena, názov) alebo None, ak odkaz neplatí."""
    conn = O.db()
    try:
        row = conn.execute("SELECT id, target, title FROM alerts WHERE token = ?", (token,)).fetchone()
        if row:
            conn.execute("UPDATE alerts SET confirmed = 1 WHERE id = ?", (row[0],))
            conn.commit()
    finally:
        conn.close()
    return (row[1], row[2]) if row else None


def stop_alert(token):
    conn = O.db()
    try:
        deleted = conn.execute("DELETE FROM alerts WHERE token = ?", (token,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return bool(deleted)


# =========================================================
# KONTROLA CIEN
# =========================================================

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def product_offer(link):
    """Aktuálna (cena, sklad) z produktovej stránky – JSON-LD, inak meta značky."""
    resp, _ = O.fetch(link, timeout=10)
    if not resp:
        return None, ""
    soup = BeautifulSoup(resp.text, O.HTML_PARSER)
    price, currency, stock = None, "EUR", ""

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        for node in _walk(data):
            raw = node.get("price", node.get("lowPrice"))
            v = L.to_float(raw) if raw not in (None, "") else None
            if v:
                price, currency = v, str(node.get("priceCurrency", "EUR")).upper()
                avail = str(node.get("availability", "")).lower()
                stock = ("out" if "outofstock" in avail or "soldout" in avail else
                         "preorder" if "preorder" in avail else "in" if "instock" in avail else "")
                break
        if price:
            break

    if not price:
        for sel in ('meta[property="product:price:amount"]', 'meta[property="og:price:amount"]', '[itemprop="price"]'):
            el = soup.select_one(sel)
            v = L.to_float(L.clean_text(el.get("content") or el.get_text())) if el else None
            if v:
                price = v
                cur = soup.select_one('meta[property="product:price:currency"], '
                                      'meta[property="og:price:currency"], [itemprop="priceCurrency"]')
                if cur:
                    currency = L.clean_text(cur.get("content") or cur.get_text()).upper() or "EUR"
                break

    if price and currency in ("CZK", "KČ"):
        price /= L.KURZ["CZK"]
    if not stock:
        stock = L.detect_stock(soup.get_text(" ", strip=True)[:20000])
    return (round(price, 2) if price else None), stock


def _should_notify(price, stock, target):
    if not price or price > target or stock == "out":
        return False
    if stock in ("in", "preorder", "order"):
        return True
    # sklad sa nedal zistiť: len pri skutočnom poklese pod cieľ
    # (inak by strážca naskladnenia s cieľom = aktuálna cena poslal e-mail hneď)
    return price < target - 0.005


def check_alerts_once():
    conn = O.db()
    try:
        alerts = conn.execute("SELECT id, email, link, title, shop, target, token, site FROM alerts "
                              "WHERE confirmed = 1 AND notified IS NULL").fetchall()
    finally:
        conn.close()

    offers = {}
    for link in {a[2] for a in alerts}:
        try:
            offers[link] = product_offer(link)
        except Exception:
            offers[link] = (None, "")
        time.sleep(1)   # šetrne k obchodom

    now = datetime.now(timezone.utc).isoformat()
    conn = O.db()
    try:
        for aid, email, link, title, shop, target, token, site in alerts:
            price, stock = offers.get(link, (None, ""))
            conn.execute("UPDATE alerts SET last_price = ?, last_checked = ? WHERE id = ?", (price, now, aid))
            if price:
                conn.execute("""INSERT INTO price_daily (link, day, shop, title, price_eur, stock)
                                VALUES (?, ?, ?, ?, ?, ?)
                                ON CONFLICT(link, day) DO UPDATE SET price_eur = excluded.price_eur,
                                    stock = excluded.stock""",
                             (link, O.today_str(), shop, title, price, stock))
            if _should_notify(price, stock, target):
                try:
                    _drop_mail(email, title, shop, link, target, price, token, PUBLIC_URL or site or "")
                    conn.execute("UPDATE alerts SET notified = ? WHERE id = ?", (now, aid))
                except Exception as e:
                    print(f"[CardRadar] E-mail strážcu sa neodoslal: {e}", flush=True)
        conn.commit()
    finally:
        conn.close()


def cleanup_alerts():
    now = datetime.now(timezone.utc)
    conn = O.db()
    try:
        conn.execute("DELETE FROM alerts WHERE notified IS NOT NULL AND notified < ?",
                     ((now - timedelta(days=30)).isoformat(),))
        conn.execute("DELETE FROM alerts WHERE confirmed = 0 AND created < ?",
                     ((now - timedelta(days=7)).isoformat(),))
        conn.commit()
    finally:
        conn.close()


def _loop():
    time.sleep(60)
    while True:
        for job in (cleanup_alerts, check_alerts_once):
            try:
                job()
            except Exception as e:
                print(f"[CardRadar] Strážca: {e}", flush=True)
        time.sleep(max(0.25, ALERT_CHECK_HOURS) * 3600)


def start_background():
    if ALERTS_ENABLED and O.only_one_process("alerts"):
        threading.Thread(target=_loop, daemon=True, name="alerts").start()


# =========================================================
# E-MAILY
# =========================================================

def _utm(url):
    try:
        p = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qsl(p.query) + [("utm_source", "cardradar"), ("utm_medium", "email")]
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))
    except Exception:
        return url


def _product_image(link):
    conn = O.db()
    try:
        row = conn.execute("SELECT image FROM price_daily WHERE link = ? AND image IS NOT NULL AND image != '' "
                           "ORDER BY day DESC LIMIT 1", (link,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    img = row[0] if row else O.fetch_product_image(link)
    return img if img and img.startswith("https://") else ""


def _send(to, subject, text, html_body, headers=None):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = SMTP_FROM, to, subject
    for k, v in (headers or {}).items():
        msg[k] = v
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
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


def _email_html(*, preheader, heading, intro, title, shop, image, price_html,
                button_text, button_url, extra_html="", footer_html="", site=""):
    e = html.escape
    img_cell = (f'<td width="104" valign="top" style="padding:0 16px 0 0"><img src="{e(image)}" width="104" alt="" '
                f'style="display:block;width:104px;height:auto;border-radius:10px;border:1px solid #e3e7f1;background:#fff"></td>'
                if image else "")
    logo = (f'<img src="{e(site)}/static/icon-192.png" width="40" height="40" alt="" '
            f'style="display:block;border-radius:10px">' if site else "")
    font = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    return f"""<!doctype html>
<html lang="sk"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{e(heading)}</title></head>
<body style="margin:0;padding:0;background:#eef1f8">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{e(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#eef1f8">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border-radius:18px;overflow:hidden;font-family:{font};color:#1b2a5c">
  <tr><td style="background:#101b44;padding:18px 24px">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="padding-right:12px">{logo}</td>
      <td style="font-size:22px;font-weight:800;color:#38d8ff">CardRadar</td>
    </tr></table>
  </td></tr>
  <tr><td style="padding:28px 24px 8px">
    <h1 style="margin:0 0 10px;font-size:22px;line-height:1.3">{e(heading)}</h1>
    <p style="margin:0;font-size:15px;line-height:1.6;color:#46557d">{intro}</p>
  </td></tr>
  <tr><td style="padding:18px 24px">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f8fc;border:1px solid #e3e7f1;border-radius:14px">
      <tr><td style="padding:16px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
        {img_cell}
        <td valign="top">
          <div style="font-size:15px;font-weight:700;line-height:1.4">{e(title)}</div>
          <div style="margin-top:4px;font-size:13px;color:#6b7aa0">{e(shop)}</div>
          <div style="margin-top:10px;font-size:15px;line-height:1.5">{price_html}</div>
        </td>
      </tr></table></td></tr>
    </table>
  </td></tr>
  <tr><td align="center" style="padding:6px 24px 8px">
    <table role="presentation" cellpadding="0" cellspacing="0"><tr>
      <td style="background:#ffcf3a;border-radius:12px;border-bottom:3px solid #e6b416">
        <a href="{e(button_url)}" style="display:inline-block;padding:14px 28px;font-size:16px;font-weight:800;color:#101b44;text-decoration:none">{e(button_text)}</a>
      </td>
    </tr></table>
  </td></tr>
  {extra_html}
  <tr><td style="padding:22px 24px 26px;border-top:1px solid #eef1f8;font-size:12px;line-height:1.6;color:#8a96b5">{footer_html}</td></tr>
</table>
<div style="padding:14px 12px 0;font-family:{font};font-size:11px;color:#9aa5c2">
CardRadar je nezávislý porovnávač cien. Pokémon je ochranná známka svojich vlastníkov.</div>
</td></tr></table></body></html>"""


def _confirm_mail(email, title, shop, link, target, token, site):
    e = html.escape
    confirm = f"{site}/alerts/confirm?token={urllib.parse.quote(token)}"
    text = (f"Ahoj,\n\nchceš dostať e-mail, keď cena klesne na {eur(target)} alebo menej?\n\n"
            f"{title} ({shop})\n{link}\n\nPotvrď kliknutím: {confirm}\n\n"
            f"Ak si o to nežiadal, tento e-mail ignoruj.\n\nCardRadar")
    body = _email_html(
        preheader=f"Potvrď strážcu ceny pre {title}", heading="Potvrď strážcu ceny 🔔",
        intro=f"Napíšeme ti, keď cena klesne na <b>{e(eur(target))}</b> alebo menej. "
              f"Stačí jedno kliknutie na potvrdenie.",
        title=title, shop=shop, image=_product_image(link),
        price_html=f'Tvoj cieľ: <b style="color:#0a8a4a">{e(eur(target))}</b>',
        button_text="Potvrdiť strážcu", button_url=confirm, site=site,
        footer_html=f'Ak si o strážcu nežiadal, tento e-mail pokojne ignoruj, nič sa nezapne.<br>'
                    f'<a href="{e(site)}" style="color:#6b7aa0">{e(site.replace("https://", ""))}</a>')
    _send(email, "Potvrď strážcu ceny – CardRadar", text, body)


def _drop_mail(email, title, shop, link, target, price, token, site):
    e = html.escape
    stop = f"{site}/alerts/stop?token={urllib.parse.quote(token)}"
    query = (O.make_suggestion(title) or {}).get("query") or title
    compare = f"{site}/?q={urllib.parse.quote(query)}"
    text = (f"Ahoj,\n\n{title} ({shop}) je teraz za {eur(price)} (tvoj cieľ bol {eur(target)}).\n\n"
            f"{link}\n\nPorovnať ceny: {compare}\n\nStrážca sa tým vypína. Nový si nastavíš na {site}\n"
            f"Zrušiť: {stop}\n\nCardRadar")
    body = _email_html(
        preheader=f"{title} je teraz za {eur(price)}", heading="Cena klesla! 🎉",
        intro=f"Produkt, ktorý strážiš, je teraz za <b>{e(eur(price))}</b>. "
              f"Ceny sa menia rýchlo, tak neváhaj príliš dlho.",
        title=title, shop=shop, image=_product_image(link),
        price_html=(f'<span style="font-size:22px;font-weight:800;color:#0a8a4a">{e(eur(price))}</span>'
                    f'<br><span style="font-size:13px;color:#6b7aa0">tvoj cieľ bol {e(eur(target))}</span>'),
        button_text=f"Otvoriť v obchode {shop}", button_url=_utm(link), site=site,
        extra_html=(f'<tr><td align="center" style="padding:4px 24px 6px"><a href="{e(compare)}" '
                    f'style="font-size:14px;color:#2453d6;font-weight:700;text-decoration:none">'
                    f'Porovnať ceny vo všetkých obchodoch →</a></td></tr>'),
        footer_html=(f'Strážca sa po tomto upozornení vypína. Nový si nastavíš na '
                     f'<a href="{e(site)}" style="color:#6b7aa0">{e(site.replace("https://", ""))}</a>.<br>'
                     f'<a href="{e(stop)}" style="color:#6b7aa0">Zrušiť strážcu</a>'))
    _send(email, f"Cena klesla: {title} za {eur(price)}", text, body,
          headers={"List-Unsubscribe": f"<{stop}>"})
