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
MODUS = (os.environ.get("MODUS") or "nieuw").strip().lower()  # "nieuw" of "volledig"
MAX_RESULTS = int(os.environ.get("MAX_RESULTS") or (900 if MODUS == "volledig" else 40))
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
        "startUrls": [{"url": u} for u in CONFIG.get("zoekUrls", [])]
                     or [{"url": zone_url(z)} for z in CONFIG["zone"]],
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


def g(obj, *path, default=None):
    for k in path:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(k)
    return default if obj is None else obj


def first_image(it):
    imgs = g(it, "files", "images", default=[]) or []
    if not imgs:
        return ""
    th = imgs[0].get("thumbnails") or {}
    return th.get("z-result") or th.get("z-detail") or next(iter(th.values()), "")


def normalise(items):
    """Zet de Apify-output (Zimmo-API-structuur) om naar het Prosper-formaat."""
    zone = {z["postcode"]: z["gemeente"] for z in CONFIG["zone"]}
    eigen = [e.lower() for e in CONFIG.get("eigenKantoor", [])]
    rows, seen = [], set()
    for it in items:
        if it.get("supported") is False:
            continue
        url = g(it, "enriched", "detailPageUrl", "nl", default="")
        code = g(it, "extra", "zimmoCode") or g(it, "extra", "code")
        m = CODE_RE.search(url)
        if not code and m:
            code = m.group(2)
        if not code or code in seen:
            continue
        seen.add(code)
        typ = m.group(1) if m else (g(it, "estate", "type", default="") or "").lower()
        loc = g(it, "estate", "location", default={}) or {}
        pc = str(loc.get("postalCode") or g(it, "sorting", "postalCode") or "")
        dealer = it.get("dealer") or {}
        cat = dealer.get("category", "")
        sold_by = g(it, "estate", "soldBy", default="")
        particulier = cat == "private" or sold_by == "PRIVATE_SALE"
        aanbieder = "" if particulier else (dealer.get("name") or ("notaris" if cat == "notary" else ""))
        stickers = [s.get("type", "") for s in (it.get("stickers") or []) if isinstance(s, dict)]
        status = g(it, "estate", "status", default="")
        foto = first_image(it)
        pub = g(it, "options", "publication", "start", default="") or it.get("createdAt", "")
        online = pub[:10] if re.match(r"\d{4}-\d{2}-\d{2}", pub or "") else online_sinds(foto, {})
        historiek = [
            {"d": (h.get("date") or "")[:10], "voor": g(h, "diff", "before"), "na": g(h, "diff", "after")}
            for h in (it.get("priceHistory") or []) if isinstance(h, dict)
        ]
        rows.append({
            "code": code,
            "url": url,
            "type": typ,
            "adres": " ".join(x for x in [loc.get("street", ""), loc.get("streetNumber", "")] if x),
            "postcode": pc,
            "gemeente": zone.get(pc, g(it, "enriched", "placeName", "nl", default="")),
            "prijs": to_int(g(it, "estate", "price", "value")),
            "aanbieder": aanbieder,
            "aanbiederType": cat,
            "particulier": particulier,
            "telefoon": (dealer.get("mobilePhoneNumber") or dealer.get("phoneNumber") or "") if particulier else "",
            "foto": foto,
            "onlineSinds": online,
            "stickers": stickers,
            "prijsHistoriek": historiek,
            "overslaan": (
                "project" in it
                or "project" in typ
                or bool(g(it, "estate", "newConstruction"))
                or any(s in ("IN_OPTION", "SOLD", "UNDER_OPTION") for s in stickers)
                or (status not in ("", "FOR_SALE"))
                or any(e in (dealer.get("name") or "").lower() for e in eigen)
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
        "modus": MODUS,  # "nieuw": enkel de recentste zoekertjes; "volledig": hele zone
        "maxResults": MAX_RESULTS,
        "apifyStatus": status,
        "ruweItems": len(items),
        "perGemeente": per,
        "fouten": [] if status == "SUCCEEDED" else [f"Apify-run: {status}"],
        "zoekertjes": rows,
    }
    txt = json.dumps(data, ensure_ascii=False, indent=1)
    (ROOT / "data" / "latest.json").write_text(txt, encoding="utf-8")
    (ROOT / "data" / f"zimmo-{TODAY.isoformat()}-{MODUS}.json").write_text(txt, encoding="utf-8")
    if MODUS == "volledig":
        (ROOT / "data" / "volledig.json").write_text(txt, encoding="utf-8")
    print(f"Modus: {MODUS}, max. {MAX_RESULTS}")
    print(f"{len(items)} ruwe items, {len(rows)} in de zone: {per}")


if __name__ == "__main__":
    main()
