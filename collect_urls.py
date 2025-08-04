# collect_urls.py — remplit urls.csv depuis les sitemaps (6 sites)
import re, requests, xml.etree.ElementTree as ET

SITES = [
    ("alltricks.fr",   "https://www.alltricks.fr/sitemap.xml",         r"/(p|produit|product|fiche)/"),
    ("cdiscount.com",  "https://www.cdiscount.com/sitemap.xml",        r"/(dp\.asp|prd/|\.html$)"),
    ("leroymerlin.fr", "https://www.leroymerlin.fr/sitemap.xml",       r"/p-"),
    ("ikea.com",       "https://www.ikea.com/sitemap.xml",             r"/p/"),
    ("fnac.com",       "https://www.fnac.com/sitemap.xml",             r"/(a/|p/|ProductDetail)"),
    ("boulanger.com",  "https://www.boulanger.com/sitemap.xml",        r"/(ref|product|fiche-produit)"),
]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DetecteurPrixBot/1.0)"}

def fetch(url):
    r = requests.get(url, timeout=40, headers=HEADERS); r.raise_for_status()
    return r.text

def parse_sitemap(url):
    xml = fetch(url)
    root = ET.fromstring(xml)
    return [e.text.strip() for e in root.iter("{*}loc")]

def main(max_per_site=150):  # augmente ensuite si tu veux
    all_urls = []
    for domain, rootmap, pattern in SITES:
        rx = re.compile(pattern, re.I)
        to_visit, seen, found = [rootmap], set(), []
        while to_visit and len(found) < max_per_site:
            sm = to_visit.pop()
            if sm in seen: continue
            seen.add(sm)
            try:
                locs = parse_sitemap(sm)
            except Exception:
                continue
            for loc in locs:
                if loc.endswith(".xml"):
                    to_visit.append(loc)     # sitemap enfant
                elif rx.search(loc):
                    found.append(loc)
                    if len(found) >= max_per_site: break
        print(f"{domain}: {len(found)} URLs")
        all_urls.extend(found)

    # dédup + écriture
    seen, out = set(), []
    for u in all_urls:
        if u not in seen:
            seen.add(u); out.append(u)

    with open("urls.csv", "w", encoding="utf-8") as f:
        f.write("url,nom\n")
        for u in out:
            f.write(f"{u},{u}\n")
    print(f"Écrit urls.csv ({len(out)} URLs).")

if __name__ == "__main__":
    main()
