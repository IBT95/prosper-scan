#!/usr/bin/env python3
"""Prosper – Zimmo-scan voor de zone uit config.json.

Leest per gemeente de lijstpagina's van Zimmo (te koop), haalt per zoekertje
code, url, type, adres, prijs, labels, kantoor en eerste foto op, berekent
onlineSinds uit de UUIDv7 van de foto en schrijft het resultaat naar
data/latest.json (en een gedateerde kopie).

Gebruik:  python scan.py            (normale scan)
          python scan.py --debug    (bewaart ook de ruwe HTML in debug/)
"""
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DEBUG = "--debug" in sys.argv

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Accept-Language": "nl-BE,nl;q=0.9",
}
LISTING_RE = re.compile(r"/nl/[a-z0-9-]+/te-koop/([a-z-]+)/([A-Z0-9]{4,8})/?$")
IMAGE_RE = re.compile(r"https://files\.zimmo\.be/[^\s\"',]+/images/([0-9a-f-]{36})")
PRICE_RE = re.compile(r"€\s*([\d.]{4,})")
LABELS = ["Nieuw", "In optie", "Verkocht", "Prijs gedaald", "Project"]
TODAY = dt.date.today()


def slug(gemeente: str) -> str:
    return gemeente.lower().replace(" ", "-")


def online_sinds(img_uuid: str) -> str:
    try:
        ms = int(img_uuid.replace("-", "")[:12], 16)
        d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date()
        if dt.date(2015, 1, 1) <= d <= TODAY:
            return d.isoformat()
    except ValueError:
        pass
    return ""


def card_for(a):
    """Klim op tot het kleinste element dat precies één zoekertjescode bevat."""
    code = LISTING_RE.search(a["href"]).group(2)
    node, best = a, a
    for _ in range(12):
        node = node.parent
        if node is None or node.name in ("body", "html"):
            break
        codes = {LISTING_RE.search(x["href"]).group(2)
                 for x in node.find_all("a", href=True) if LISTING_RE.search(x["href"])}
        if codes != {code}:
            break
        best = node
    return best


def parse(html: str, postcode: str):
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    for a in soup.find_all("a", href=True):
        m = LISTING_RE.search(a["href"])
        if not m:
            continue
        typ, code = m.group(1), m.group(2)
        if code in out:
            continue
        card = card_for(a)
        text = " ".join(card.get_text(" ", strip=True).split())
        html_card = str(card)

        img = IMAGE_RE.search(html_card)
        foto = img.group(0) if img else ""
        prijs = PRICE_RE.search(text)
        prijs = int(prijs.group(1).replace(".", "")) if prijs else None

        kantoor = ""
        for im in card.find_all("img"):
            alt = (im.get("alt") or "").strip()
            if alt and "logo" in alt.lower():
                kantoor = re.sub(r"\s*logo\s*$", "", alt, flags=re.I).strip()
                break
        if kantoor.lower() == "particulier":
            kantoor = ""

        adres = ""
        am = re.search(r"([^€|]{3,80}?\b\d{4}\s+[A-ZÀ-Ü][\w\- ]+)", text)
        if am:
            adres = am.group(1).strip(" ,")
            for w in LABELS + [typ.replace("-", " ")]:
                adres = re.sub(rf"^{re.escape(w)}\s+", "", adres, flags=re.I)

        labels = [l for l in LABELS if re.search(rf"\b{re.escape(l)}\b", text)]
        url = a["href"] if a["href"].startswith("http") else "https://www.zimmo.be" + a["href"]
        out[code] = {
            "code": code,
            "url": url,
            "type": typ,
            "adres": adres,
            "postcode": (re.search(r"\b(\d{4})\b", adres) or [None, ""])[1],
            "prijs": prijs,
            "labels": labels,
            "aanbieder": kantoor,
            "particulier": kantoor == "" or "particulier" in text.lower()
                           or "/customers/99999999-" in foto,
            "foto": foto,
            "onlineSinds": online_sinds(img.group(1)) if img else "",
        }
    return out


def scan_gemeente(gemeente: str, postcode: str, base: str = "", max_pages: int = 15):
    base = (base or f"https://www.zimmo.be/nl/{slug(gemeente)}-{postcode}/te-koop").rstrip("/")
    results, errors = {}, []
    for page in range(1, max_pages + 1):
        url = base if page == 1 else f"{base}?page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            errors.append(f"{url}: {e}")
            break
        if DEBUG and page == 1:
            (ROOT / "debug").mkdir(exist_ok=True)
            (ROOT / "debug" / f"{slug(gemeente)}-p1.html").write_text(r.text, encoding="utf-8")
        if r.status_code != 200:
            errors.append(f"{url}: HTTP {r.status_code}")
            break
        found = parse(r.text, postcode)
        new = {k: v for k, v in found.items() if k not in results}
        if not new:
            break
        results.update(new)
        if "page=" + str(page + 1) not in r.text:
            break
        time.sleep(2)  # beleefd blijven
    return results, errors


def main():
    alles, fouten, per_gemeente = [], [], {}
    for z in CONFIG["zone"]:
        res, err = scan_gemeente(z["gemeente"], z["postcode"], z.get("url", ""))
        fouten += err
        rows = []
        for item in res.values():
            if item["postcode"] and item["postcode"] != z["postcode"]:
                continue  # buurgemeente
            item["gemeente"] = z["gemeente"]
            item["overslaan"] = (
                item["type"] in ("nieuwbouwproject", "project")
                or "Project" in item["labels"]
                or "In optie" in item["labels"]
                or "Verkocht" in item["labels"]
                or any(e.lower() in item["aanbieder"].lower() for e in CONFIG.get("eigenKantoor", []))
            )
            rows.append(item)
        per_gemeente[z["gemeente"]] = len(rows)
        alles += rows
        print(f"{z['gemeente']}: {len(rows)} zoekertjes", *(err or []), sep="\n  ")

    data = {
        "gescand": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "perGemeente": per_gemeente,
        "fouten": fouten,
        "zoekertjes": alles,
    }
    (ROOT / "data").mkdir(exist_ok=True)
    txt = json.dumps(data, ensure_ascii=False, indent=1)
    (ROOT / "data" / "latest.json").write_text(txt, encoding="utf-8")
    (ROOT / "data" / f"zimmo-{TODAY.isoformat()}.json").write_text(txt, encoding="utf-8")
    print(f"Totaal: {len(alles)} zoekertjes, {len(fouten)} fouten")


if __name__ == "__main__":
    main()
