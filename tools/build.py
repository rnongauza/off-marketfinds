"""Build the whole off-marketfinds.com site into _site/.

  python3 tools/build.py [_site]

Sources (all in this repo):
  properties/<slug>/property.json   one folder per property (+ img/ with its photos)
  links.json                         short links: off-marketfinds.com/go/<code>
  contact.json                       Robert's contact block, shared by every page
  site/property.html                 public property page template
  site/home.html                     home page template (list of properties)
  site/admin/index.html              owner admin app
  site/config.json                   {"api": "<worker url>"} written by the worker deploy
"""
import html, json, os, pathlib, shutil, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "_site").resolve()
SITE_URL = os.environ.get("SITE_URL", "https://off-marketfinds.com").rstrip("/")

DEFAULT_CALC = {"level": "moderate", "light": 40, "moderate": 65, "closingPct": 2, "months": 3,
                "holdingMonthly": 2200, "sellerClosingPct": 1, "targetPct": 10, "loanPct": 80,
                "ratePct": 10, "pointsPct": 2}
DEFAULT_PROPERTY = {"eyebrow": "Investor opportunity", "headline": "OFF-MARKET OPPORTUNITY",
                    "propertyType": "Single Family", "overview": [],
                    "offer": {"deadline": "Contact Robert for details.", "access": "Call Robert at 510-459-3029",
                              "closing": "Please do your due diligence"},
                    "deal": {"emd": "Contact Robert for details.", "closing": "Confirm with dispositions",
                             "openHouse": "Often times 1 hour on a specific day - Contact Robert"}}


def read_json(p, default):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except FileNotFoundError:
        return default


def script_json(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def load_properties():
    props = []
    for f in sorted((ROOT / "properties").glob("*/property.json")):
        d = json.loads(f.read_text())
        d["slug"] = f.parent.name
        props.append(d)
    return props


def short_url(code):
    return f"{SITE_URL}/go/{code}" if code else ""


def photo_src(slug, v):
    if not v or not v.get("src"):
        return None
    s = v["src"]
    if s.startswith(("http://", "https://", "/")):
        return {"src": s}
    return {"src": f"/{slug}/{s}"}


def build_property(d, contact, tpl):
    slug = d["slug"]
    P = dict(DEFAULT_PROPERTY)
    P.update({k: v for k, v in (d.get("property") or {}).items() if not (k in ("eyebrow", "headline", "propertyType") and not v)})
    for k in ("offer", "deal"):
        merged = dict(DEFAULT_PROPERTY[k]); merged.update({a: b for a, b in (P.get(k) or {}).items() if b})
        P[k] = merged
    for k in ("price", "arv", "sqft", "beds", "baths", "lot"):
        if P.get(k) in (None, ""):
            P[k] = 0 if k in ("price", "arv", "sqft") else ""
    photos = []
    for i, ph in enumerate(d.get("photos") or []):
        o = {"id": ph.get("id") or f"p{i+1}", "caption": ph.get("caption") or ""}
        if isinstance(ph.get("focus"), (int, float)):
            o["focus"] = max(0, min(100, ph["focus"]))
        for k in ("original", "modern"):
            v = photo_src(slug, ph.get(k))
            if v: o[k] = v
        if o.get("original") or o.get("modern"):
            photos.append(o)
    calc = dict(DEFAULT_CALC); calc.update({k: v for k, v in (d.get("calc") or {}).items() if k not in ("sellingPct", "listingPct", "buyerBrokerPct")})
    L = d.get("links") or {}
    links = {"more": L.get("more") or "", "showMore": bool(L.get("showMore") and L.get("more")),
             "moreShortUrl": short_url(L.get("moreShort"))}
    data = {"property": P, "photos": photos, "calc": calc, "contact": contact,
            "market": d.get("market") or None, "links": links}

    street = P.get("street") or "Off-market property"
    city = P.get("cityLine") or ""
    title = street
    desc = f"Off-market investor opportunity at {street}{', ' + city if city else ''}. See every room as a Modern design concept, RentCast comps, and run your own flip numbers."
    cover = photos[0] if photos else None
    og = SITE_URL + (cover.get("modern") or cover.get("original"))["src"] if cover else ""
    page = (tpl.replace("{{TITLE}}", html.escape(title)).replace("{{DESCRIPTION}}", html.escape(desc))
               .replace("{{OG_IMAGE}}", html.escape(og)).replace("/*DATA*/", script_json(data)))

    dest = OUT / slug
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "index.html").write_text(page)
    img = ROOT / "properties" / slug / "img"
    if img.is_dir():
        shutil.copytree(img, dest / "img", dirs_exist_ok=True)
    (dest / "version.json").write_text(json.dumps({"updatedAt": d.get("updatedAt") or 0,
                                                    "commit": os.environ.get("GITHUB_SHA", "")}))
    card_img = None
    if cover:
        card_img = (cover.get("modern") or cover.get("original"))["src"]
    return {"street": street, "city": city, "url": f"/{slug}/", "image": card_img,
            "aiImage": bool(cover and cover.get("modern")), "label": "Off-market",
            "price": P.get("price") or 0, "arv": P.get("arv") or 0,
            "beds": P.get("beds"), "baths": P.get("baths"), "createdAt": d.get("createdAt") or 0}


def redirect_page(target, code, api):
    t = html.escape(target, quote=True)
    js_t = json.dumps(target)
    ping = ""
    if api:
        ping = f"try{{navigator.sendBeacon({json.dumps(api.rstrip('/') + '/c/' + code)})}}catch(e){{}}"
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Opening…</title>"
            "<meta name=\"robots\" content=\"noindex\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta http-equiv=\"refresh\" content=\"1;url={t}\"><link rel=\"canonical\" href=\"{t}\"></head>"
            "<body style=\"font:16px Arial,sans-serif;margin:40px;color:#333\">"
            f"<p>Opening <a href=\"{t}\">{t}</a> …</p>"
            f"<script>{ping}location.replace({js_t});</script></body></html>")


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    contact = read_json(ROOT / "contact.json", {})
    config = read_json(ROOT / "site" / "config.json", {})
    api = os.environ.get("API_URL") or config.get("api") or ""
    tpl = (ROOT / "site" / "property.html").read_text()

    cards = []
    props = load_properties()
    for d in props:
        c = build_property(d, contact, tpl)
        if d.get("listed", True):
            cards.append(c)
    cards.sort(key=lambda c: -(c.pop("createdAt") or 0))

    home = (ROOT / "site" / "home.html").read_text().replace("/*PROPERTIES*/[]", script_json(cards))
    (OUT / "index.html").write_text(home)

    # short links: every property gets /go/<short>, plus links.json entries
    links = read_json(ROOT / "links.json", {})
    for d in props:
        if d.get("short"):
            links.setdefault(d["short"], {"url": f"/{d['slug']}/", "label": (d.get("property") or {}).get("street", d["slug"]),
                                          "slug": d["slug"], "kind": "property"})
        L = d.get("links") or {}
        if L.get("moreShort") and L.get("more"):
            links.setdefault(L["moreShort"], {"url": L["more"], "label": "More photos", "slug": d["slug"], "kind": "photos"})
    for code, v in links.items():
        url = v["url"] if isinstance(v, dict) else v
        if not url:
            continue
        g = OUT / "go" / code
        g.mkdir(parents=True, exist_ok=True)
        (g / "index.html").write_text(redirect_page(url, code, api))

    # admin app
    adm = OUT / "admin"
    adm.mkdir()
    a = (ROOT / "site" / "admin" / "index.html").read_text()
    a = a.replace("/*CONFIG*/{}", script_json({"api": api, "site": SITE_URL}))
    (adm / "index.html").write_text(a)
    for extra in (ROOT / "site" / "admin").iterdir():
        if extra.name != "index.html":
            (shutil.copytree if extra.is_dir() else shutil.copy)(extra, adm / extra.name)

    if (ROOT / "CNAME").exists():
        shutil.copy(ROOT / "CNAME", OUT / "CNAME")
    (OUT / ".nojekyll").write_text("")
    print(f"built {len(props)} properties, {len(cards)} on the home page, {len(links)} short links -> {OUT}")


if __name__ == "__main__":
    main()
