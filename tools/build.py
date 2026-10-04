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
import hashlib, html, json, os, pathlib, shutil, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "_site").resolve()
SITE_URL = os.environ.get("SITE_URL", "https://off-marketfinds.com").rstrip("/")

DEFAULT_CALC = {"level": "light", "light": 40, "moderate": 65, "closingPct": 2, "months": 3,
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


HIDDEN_STREET = "Address provided on request"


def public_dir(slug, d):
    """Folder the public page lives in. A property whose street is hidden gets a neutral folder name
    so the URL doesn't give the address away (the old address URL redirects there)."""
    if (d.get("property") or {}).get("hideStreet"):
        return "listing-" + hashlib.sha1(slug.encode()).hexdigest()[:10]
    return slug


def neutral_code(slug, photos=False):
    """Short code without the house number, used when the street is hidden."""
    return "h" + hashlib.sha1(slug.encode()).hexdigest()[:6] + ("p" if photos else "")


def short_url(code):
    return f"{SITE_URL}/go/{code}" if code else ""


def photo_src(pub, v):
    if not v or not v.get("src"):
        return None
    s = v["src"]
    if s.startswith(("http://", "https://", "/")):
        return {"src": s}
    return {"src": f"/{pub}/{s}"}


def build_property(d, contact, tpl):
    slug = d["slug"]
    pub = public_dir(slug, d)
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
        # how the photo sits in its frame (set in the admin photo editor)
        for k in ("focus", "x", "y", "cx", "cy"):
            if isinstance(ph.get(k), (int, float)):
                o[k] = max(0, min(100, ph[k]))
        for k in ("z", "cz"):
            if isinstance(ph.get(k), (int, float)) and ph[k] > 0:
                o[k] = min(4, ph[k])
        fr = ph.get("frame")
        if fr == "photo" or (isinstance(fr, (int, float)) and 0.3 <= fr <= 3.5):
            o["frame"] = fr
        for k in ("original", "modern"):
            v = photo_src(pub, ph.get(k))
            if v: o[k] = v
        if o.get("original") or o.get("modern"):
            photos.append(o)
    calc = dict(DEFAULT_CALC); calc.update({k: v for k, v in (d.get("calc") or {}).items() if k not in ("sellingPct", "listingPct", "buyerBrokerPct")})
    L = d.get("links") or {}
    links = {"more": L.get("more") or "", "showMore": bool(L.get("showMore") and L.get("more")),
             "moreShortUrl": short_url(neutral_code(slug, True) if (d.get("property") or {}).get("hideStreet") and L.get("moreShort") else L.get("moreShort"))}
    t = P.get("heroText")
    if not (isinstance(t, dict) and all(isinstance(t.get(k), (int, float)) for k in ("x", "y"))):
        P.pop("heroText", None)
    hide = bool(P.get("hideStreet"))
    if hide:  # never put the real street in the public page
        P["street"] = ""
    P.pop("hideStreet", None)
    data = {"property": P, "photos": photos, "calc": calc, "contact": contact,
            "market": d.get("market") or None, "links": links}

    city = P.get("cityLine") or ""
    street = HIDDEN_STREET if hide else (P.get("street") or "Off-market property")
    title = f"Off-market property in {city}" if hide and city else street
    desc = f"Off-market investor opportunity {'in ' + city if hide else 'at ' + street + (', ' + city if city else '')}. See every room as a Modern design concept, RentCast comps, and run your own flip numbers."
    cover = photos[0] if photos else None
    og = SITE_URL + (cover.get("modern") or cover.get("original"))["src"] if cover else ""
    page = (tpl.replace("{{TITLE}}", html.escape(title)).replace("{{DESCRIPTION}}", html.escape(desc))
               .replace("{{OG_IMAGE}}", html.escape(og)).replace("/*DATA*/", script_json(data)))

    dest = OUT / pub
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "index.html").write_text(page)
    img = ROOT / "properties" / slug / "img"
    if img.is_dir():
        shutil.copytree(img, dest / "img", dirs_exist_ok=True)
    ver = OUT / slug
    if pub != slug:  # old address URL forwards to the neutral one; version.json stays here for the admin
        ver.mkdir(parents=True, exist_ok=True)
        (ver / "index.html").write_text(redirect_page(f"/{pub}/", "", ""))
    (ver / "version.json").write_text(json.dumps({"updatedAt": d.get("updatedAt") or 0,
                                                    "commit": os.environ.get("GITHUB_SHA", "")}))
    card_img = None
    if cover:
        card_img = (cover.get("modern") or cover.get("original"))["src"]
    return {"street": street, "city": city, "url": f"/{pub}/", "image": card_img,
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
            prop_url = f"/{public_dir(d['slug'], d)}/"
            e = links.setdefault(d["short"], {"url": prop_url, "label": (d.get("property") or {}).get("street", d["slug"]),
                                              "slug": d["slug"], "kind": "property"})
            if isinstance(e, dict) and e.get("slug") == d["slug"] and e.get("kind", "property") == "property":
                e["url"] = prop_url
        L = d.get("links") or {}
        if (d.get("property") or {}).get("hideStreet"):  # address-free short links too
            links[neutral_code(d["slug"])] = {"url": f"/{public_dir(d['slug'], d)}/", "label": "Property", "slug": d["slug"], "kind": "alias"}
            if L.get("more"):
                links[neutral_code(d["slug"], True)] = {"url": L["more"], "label": "More photos", "slug": d["slug"], "kind": "alias"}
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
