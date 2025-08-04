# -*- coding: utf-8 -*-
# detector.py — Détecteur d'erreurs de prix (6 sites + alerte Telegram)
# - lit urls.csv (généré par collect_urls.py)
# - extrait le prix (JSON-LD, meta, ou sélecteurs de secours)
# - enregistre l'historique (SQLite prix.db)
# - alerte si prix < 1 € ou très bas vs historique (Médiane+MAD)

import csv, os, re, json, time, sqlite3, requests
from bs4 import BeautifulSoup

CSV_URLS = "urls.csv"
DB_FILE = "prix.db"

# Règles d'anomalie
MIN_POINTS = 8        # historique mini avant de juger
REL_FACTOR = 0.40     # alerte si prix < 40% de la médiane historique
ABS_FLOOR  = 1.00     # alerte immédiate si prix < 1.00 €

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DetecteurPrixBot/1.0)"}

# Sélecteurs de secours par domaine
SELECTEURS_PAR_SITE = {
    "alltricks.fr":   ["[itemprop=price]", "[class*=price]"],
    "cdiscount.com":  ["meta[itemprop=price]", "[class*=price]", "[id*=price]"],
    "leroymerlin.fr": ["[itemprop=price]", "[data-qa=product-price]", "[class*=price]"],
    "ikea.com":       ["[itemprop=price]", "[class*=price]"],
    "fnac.com":       ['meta[property="product:price:amount"]', "[class*=price]"],
    "boulanger.com":  ["meta[itemprop=price]", "[class*=price]"],
}

# ---------- Envoi Telegram ----------
def send_telegram(text: str):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Telegram non configuré."); return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15
        )
        if resp.status_code != 200:
            print("Erreur envoi Telegram:", resp.text)
    except Exception as e:
        print("Erreur envoi Telegram:", e)

# ---------- Extraction du prix ----------
def _to_number(txt: str) -> float:
    m = re.search(r'(\d+(?:[.,]\d+)?)', txt)
    if not m: raise ValueError(f"Nombre introuvable dans {txt!r}")
    return float(m.group(1).replace(",", "."))

def _jsonld_prices(soup):
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        blocks = data if isinstance(data, list) else [data]
        for b in blocks:
            if not isinstance(b, dict): continue
            # Product + offers.price
            if b.get("@type") == "Product":
                offers = b.get("offers")
                if isinstance(offers, dict) and offers.get("price") is not None:
                    yield str(offers["price"])
                ps = b.get("offers", {}).get("priceSpecification", {})
                if isinstance(ps, dict) and ps.get("price") is not None:
                    yield str(ps["price"])
            # Offer direct
            if b.get("@type") == "Offer" and b.get("price") is not None:
                yield str(b["price"])

def _meta_prices(soup):
    metas = [
        ('meta[itemprop="price"]', "content"),
        ('meta[property="product:price:amount"]', "content"),
        ('meta[name="price"]', "content"),
    ]
    for sel, attr in metas:
        for el in soup.select(sel):
            v = el.get(attr)
            if v: yield v

def _text_prices(soup, host):
    for sel in SELECTEURS_PAR_SITE.get(host, []):
        for el in soup.select(sel): yield el.get_text(" ", strip=True)
    for sel in ("[class*=price]", "[id*=price]"):
        for el in soup.select(sel): yield el.get_text(" ", strip=True)

def extract_price(url: str) -> float:
    if "amazon." in url:
        raise RuntimeError("Amazon: utiliser l'API Keepa (non activée ici).")
    r = requests.get(url, timeout=40, headers=HEADERS)
    r.encoding = r.encoding or "utf-8"
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()

    candidates = []
    candidates += list(_jsonld_prices(soup))
    candidates += list(_meta_prices(soup))
    candidates += list(_text_prices(soup, host))
    for c in candidates:
        try:
            return _to_number(c)
        except Exception:
            continue
    raise RuntimeError("Prix introuvable")

# ---------- Stockage ----------
def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute("""CREATE TABLE IF NOT EXISTS prices(
        url TEXT, name TEXT, ts INTEGER, price REAL
    )""")
    con.execute("""CREATE INDEX IF NOT EXISTS idx_prices ON prices(url, ts)""")
    con.execute("""CREATE TABLE IF NOT EXISTS alerts(
        url TEXT, ts INTEGER, price REAL, msg TEXT
    )""")
    con.commit(); con.close()

def save_price(url, name, price):
    con = sqlite3.connect(DB_FILE)
    con.execute("INSERT INTO prices(url,name,ts,price) VALUES(?,?,?,?)",
                (url, name, int(time.time()), price))
    con.commit(); con.close()

def recently_alerted(url, cooldown_h=12):
    since = int(time.time()) - cooldown_h*3600
    con = sqlite3.connect(DB_FILE)
    row = con.execute("SELECT 1 FROM alerts WHERE url=? AND ts>=? LIMIT 1",
                      (url, since)).fetchone()
    con.close()
    return bool(row)

def save_alert(url, price, msg):
    con = sqlite3.connect(DB_FILE)
    con.execute("INSERT INTO alerts(url,ts,price,msg) VALUES(?,?,?,?)",
                (url, int(time.time()), price, msg))
    con.commit(); con.close()

def load_history(url, days=90):
    since = int(time.time()) - days*86400
    con = sqlite3.connect(DB_FILE)
    rows = con.execute(
        "SELECT price FROM prices WHERE url=? AND ts>=? ORDER BY ts",
        (url, since)
    ).fetchall()
    con.close()
    return [r[0] for r in rows]

# ---------- Stats & règle d'anomalie ----------
def median(vals):
    s = sorted(vals); n = len(s)
    if n == 0: return None
    m = n // 2
    return s[m] if n % 2 else (s[m-1] + s[m]) / 2

def mad(vals, med):
    if med is None: return 1.0
    devs = [abs(v - med) for v in vals]
    m = median(devs)
    return m if m not in (None, 0) else 1.0

def is_anomaly(current, hist):
    if current < ABS_FLOOR:
        return True, f"ANOMALIE: {current:.2f} < {ABS_FLOOR:.2f} (seuil absolu)"
    if len(hist) < MIN_POINTS:
        return False, f"Pas assez d'historique ({len(hist)}/{MIN_POINTS})."
    med = median(hist)
    sigma = 1.4826 * mad(hist, med)    # écart-type robuste
    seuil_rel = REL_FACTOR * med
    seuil_rob = med - 3 * sigma
    seuil = max(seuil_rel, seuil_rob)
    if current < seuil:
        return True, f"ANOMALIE: {current:.2f} < max({seuil_rel:.2f}, {seuil_rob:.2f}) (med={med:.2f})"
    return False, f"OK: {current:.2f} ≥ max({seuil_rel:.2f}, {seuil_rob:.2f}) (med={med:.2f})"

# ---------- Main ----------
def main():
    if not os.path.exists(CSV_URLS):
        print("urls.csv manquant. Exécute d'abord collect_urls.py"); return
    init_db()
    with open(CSV_URLS, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = row["url"].strip()
            name = (row.get("nom") or url).strip()
            try:
                price = extract_price(url)
                save_price(url, name, price)
                hist = load_history(url)
                anomaly, msg = is_anomaly(price, hist[:-1])  # compare au passé
                line = (("⚠️ " if anomaly else "✅ ") +
                        f"{name} | {price:.2f} | {msg} | {url}")
                print(line)
                if anomaly and not recently_alerted(url):
                    send_telegram("Anomalie de prix détectée ⚠️\n" + line)
                    save_alert(url, price, msg)
            except Exception as e:
                print("❌", name, "|", url, "|", e)

if __name__ == "__main__":
    main()

