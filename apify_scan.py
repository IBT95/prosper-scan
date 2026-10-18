#!/usr/bin/env python3
"""Prosper – Zimmo-scan via Apify (actor dz_omar/zimmo-scraper).

Start één Apify-run met de zone uit config.json, wacht tot die klaar is,
bewaart de ruwe output in data/apify-raw.json en zet alles om naar
data/latest.json (zelfde formaat als scan.py).

Nodig: omgevingsvariabele APIFY_TOKEN (GitHub-secret).
Optioneel: MAX_RESULTS (standaard 600) om de kosten te begrenzen.
"""
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
ACTOR = "dz_omar~zimmo-scraper"
TOKEN = os.environ.get("APIFY_TOKEN", "").strip()
MAX_RESULTS = int(os.environ.get("MAX_RESULTS") or 600)
API = "https://api.apify.com/v2"
TODAY = dt.date.today()

CODE_RE = re.compile(r"/te-koop/([a-z-]+)/([A-Z0-9]{4,8})/?")
IMG_UUID_RE = re.compile(r"/images/([0-9a-f-]{36})")


def zone_url(z):
    slug = z["gemeente"].lower().replace(" ", "-")
    return z.get("url") or f"https://www.zimmo.be/nl/{slug}-{z['postcode']}/te-koop/"


def run_actor():
    if not TOKEN:
        sys.exit("APIFY_TOKEN ontbreekt (zet het als GitHub-secret).")
    body = {
        "startUrls": [{"url": zone_url(z)} for z in CONFIG["zone"]],
        "maxResults": MAX_RESULTS,
        "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": []},
    }
    r = requests.post(f"{API}/acts/{ACTOR}/runs", params={"token": TOKEN}, json=body, timeout=60)
    r.raise_for_status()
    run = r.json()["data"]
    print("Apify-run gestart:", run["id"])
    for _ in range(120):  # max. ~40 min
        time.sleep(20)
        run = requests.get(f"{API}/actor-runs/{run['id']}", params={"token": TOKEN}, timeout=60).json()["data"]
        if run["status"] not in ("READY", "RUNNING"):
            break
    print("Status:", run["status"])
    items = requests.get(
        f"{API}/datasets/{run['defaultDatasetId']}/items",
        params={"token": TOKEN, "clean": "true", "format": "json"}, timeout=120,
    ).json()
    return run["status"], items


def walk(obj, prefix=""):
    """Alle (pad, waarde)-paren in een genest object."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def pick(item, *names):
    """Eerste niet-lege waarde waarvan de sleutel (laatste deel) in names staat."""
    names = [n.lower() for n in names]
    for path, val in walk(item):
        key = re.split(r"[.\[]", path)[-1].rstrip("]").lower()
        if key in names and val not in (None, "", [], {}):
            return val
    return None


def find_obj(obj, *names):
    """Zoek recursief een sleutel (ook als de waarde een object is)."""
    names = [n.lower() for n in names]
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            r = find_obj(v, *names)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_obj(v, *names)
            if r is not None:
                return r
    return None


def to_int(v):
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        d = re.sub(r"[^\d]", "", v.split(",")[0])
        return int(d) if d else None
    return None


def online_sinds(foto, item):
    m = IMG_UUID_RE.search(foto or "")
    if m:
        try:
            ms = int(m.group(1).replace("-", "")[:12], 16)
            d = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date()
            if dt.date(2015, 1, 1) <= d <= TODAY:
                return d.isoformat()
        except ValueError:
            pass
    v = pick(item, "publishedAt", "publicationDate", "createdAt", "dateCreated", "listedAt", "onlineSince")
    if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    return ""


def normalise(items):
    zone = {z["postcode"]: z["gemeente"] for z in CONFIG["zone"]}
    eigen = [e.lower() for e in CONFIG.get("eigenKantoor", [])]
    rows, seen = [], set()
    for it in items:
        url = next((v for _, v in walk(it) if isinstance(v, str) and CODE_RE.search(v)), "")
        m = CODE_RE.search(url)
        if not m or m.group(2) in seen:
            continue
        typ, code = m.group(1), m.group(2)
        seen.add(code)
        foto = next((v for _, v in walk(it) if isinstance(v, str) and "/images/" in v
                     and v.startswith("http")), "")
        pc = str(pick(it, "postalCode", "postcode", "zip", "zipCode") or "")
        if not pc:
            pm = re.search(r"-(\d{4})/te-koop", url)
            pc = pm.group(1) if pm else ""
        straat = pick(it, "street", "streetName", "straat") or ""
        nr = pick(it, "houseNumber", "number", "huisnummer") or ""
        adres = pick(it, "address", "fullAddress", "adres")
        if not isinstance(adres, str):
            adres = f"{straat} {nr}".strip()
        aanbieder = find_obj(it, "agencyName", "agency", "brokerName", "broker", "agentName",
                         "realEstateAgency", "officeName", "companyName") or ""
        if isinstance(aanbieder, dict):
            aanbieder = aanbieder.get("name") or aanbieder.get("title") or ""
        aanbieder = str(aanbieder)
        tekst = json.dumps(it, ensure_ascii=False).lower()
        status = str(pick(it, "status", "saleStatus", "label", "labels") or "").lower()
        rows.append({
            "code": code,
            "url": url,
            "type": typ,
            "adres": adres,
            "postcode": pc,
            "gemeente": zone.get(pc, pick(it, "city", "municipality", "gemeente") or ""),
            "prijs": to_int(pick(it, "price", "askingPrice", "prijs", "vraagprijs")),
            "aanbieder": aanbieder,
            "particulier": aanbieder == "" or "particulier" in tekst,
            "foto": foto,
            "onlineSinds": online_sinds(foto, it),
            "overslaan": (
                "project" in typ
                or any(w in status for w in ("optie", "option", "verkocht", "sold"))
                or any(e in aanbieder.lower() for e in eigen)
            ),
            "inZone": pc in zone,
        })
    return rows


def main():
    status, items = run_actor()
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "apify-raw.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    rows = [r for r in normalise(items) if r["inZone"]]
    per = {}
    for r in rows:
        per[r["gemeente"]] = per.get(r["gemeente"], 0) + 1
    data = {
        "gescand": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "bron": "apify:" + ACTOR,
        "apifyStatus": status,
        "ruweItems": len(items),
        "perGemeente": per,
        "fouten": [] if status == "SUCCEEDED" else [f"Apify-run: {status}"],
        "zoekertjes": rows,
    }
    txt = json.dumps(data, ensure_ascii=False, indent=1)
    (ROOT / "data" / "latest.json").write_text(txt, encoding="utf-8")
    (ROOT / "data" / f"zimmo-{TODAY.isoformat()}.json").write_text(txt, encoding="utf-8")
    print(f"{len(items)} ruwe items, {len(rows)} in de zone: {per}")


if __name__ == "__main__":
    main()
