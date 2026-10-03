"""RentCast data for every property.

  python3 tools/rentcast.py enrich           fill in facts for brand-new properties (needsEnrich: true)
  python3 tools/rentcast.py market _site      write _site/<slug>/rentcast.json for every property

The API key comes from the repo secret RENTCAST_API_KEY. The free plan has 50 requests a month, so:
- a new property costs 3 requests once (property facts + value/comps + rent),
- the weekly refresh (schedule) or a manual "Run workflow" costs 2 per property,
- every other site update reuses the numbers already live on off-marketfinds.com (0 requests).
"""
import datetime, json, os, pathlib, sys, urllib.parse, urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = os.environ.get("RENTCAST_API_KEY", "").strip()
SITE = os.environ.get("SITE_URL", "https://off-marketfinds.com").rstrip("/")
STATUS = {"Active": "Active listing", "Inactive": "Off market"}
TYPES = {"Single Family", "Condo", "Townhouse", "Multi-Family", "Manufactured", "Land"}


def get(path, params):
    url = "https://api.rentcast.io/v1/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Api-Key": KEY, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def address_of(P):
    return ", ".join(x for x in [P.get("street"), P.get("cityLine")] if x)


def market(P):
    q = {"address": address_of(P), "propertyType": P.get("propertyType") or "Single Family", "compCount": 20}
    for k, f in (("bedrooms", "beds"), ("bathrooms", "baths"), ("squareFootage", "sqft")):
        if P.get(f): q[k] = P[f]
    v = get("avm/value", q)
    try:
        r = get("avm/rent/long-term", q)
    except Exception as e:
        print("rent lookup failed:", e); r = {}
    same = [c for c in v.get("comparables") or [] if (c.get("propertyType") or "") == q["propertyType"] and c.get("price")]
    same.sort(key=lambda c: (-(c.get("correlation") or 0), c.get("distance") or 99))
    comps = [{"address": c.get("formattedAddress"), "price": c.get("price"), "beds": c.get("bedrooms"),
              "baths": c.get("bathrooms"), "sqft": c.get("squareFootage"), "yearBuilt": c.get("yearBuilt"),
              "type": c.get("propertyType"), "distance": c.get("distance"),
              "status": STATUS.get(c.get("status"), c.get("status")), "date": c.get("removedDate") or c.get("listedDate")}
             for c in same[:4]]
    return {"source": "RentCast", "asOf": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "value": v.get("price"), "low": v.get("priceRangeLow"), "high": v.get("priceRangeHigh"),
            "rent": r.get("rent"), "rentLow": r.get("rentRangeLow"), "rentHigh": r.get("rentRangeHigh"), "comps": comps}


def enrich():
    """Fill beds/baths/sqft/lot/year/type, a default ARV and overview for properties created by text or admin."""
    changed = []
    for f in sorted((ROOT / "properties").glob("*/property.json")):
        d = json.loads(f.read_text())
        if not d.get("needsEnrich"):
            continue
        P = d.setdefault("property", {})
        if KEY:
            try:
                res = get("properties", {"address": address_of(P), "limit": 1})
                rec = res[0] if isinstance(res, list) and res else (res if isinstance(res, dict) else {})
                fill = {"beds": rec.get("bedrooms"), "baths": rec.get("bathrooms"), "sqft": rec.get("squareFootage"),
                        "lot": rec.get("lotSize"), "yearBuilt": str(rec.get("yearBuilt") or "")}
                for k, v in fill.items():
                    if v not in (None, "", 0) and P.get(k) in (None, "", 0):
                        P[k] = v
                if rec.get("propertyType") in TYPES:
                    P["propertyType"] = rec["propertyType"]
                if rec.get("city") and not P.get("cityLine"):
                    P["cityLine"] = f"{rec['city']}, {rec.get('state', '')} {rec.get('zipCode', '')}".strip()
            except Exception as e:
                print(f.parent.name, "property lookup failed:", e)
            try:
                m = market(P)
                d["market"] = m
                if not P.get("arv") and m.get("value"):
                    P["arv"] = m["value"]
            except Exception as e:
                print(f.parent.name, "value lookup failed:", e)
        if not P.get("overview"):
            bits = []
            if P.get("beds"): bits.append(f"{P['beds']} bedroom{'s' if P['beds'] != 1 else ''}")
            if P.get("baths"): bits.append(f"{P['baths']:g} bath{'s' if P['baths'] != 1 else ''}" if isinstance(P["baths"], (int, float)) else f"{P['baths']} baths")
            if P.get("sqft"): bits.append(f"{int(P['sqft']):,} sq ft")
            kind = (P.get("propertyType") or "home").lower().replace("single family", "single-family home")
            first = f"A {kind}" + (", " + ", ".join(bits) if bits else "") + (f", built in {P['yearBuilt']}." if P.get("yearBuilt") else ".")
            P["overview"] = [first, "Contact Robert for access, disclosures and the full investor package."]
        d.pop("needsEnrich", None)
        f.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
        changed.append(f.parent.name)
        print(f.parent.name, "enriched:", {k: P.get(k) for k in ("beds", "baths", "sqft", "yearBuilt", "arv")})
    return changed


def write_market(site):
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    refresh = event in ("schedule", "workflow_dispatch") or os.environ.get("FORCE_REFRESH") == "1"
    for f in sorted((ROOT / "properties").glob("*/property.json")):
        d = json.loads(f.read_text()); P = d.get("property", {}); slug = f.parent.name
        if not P.get("street"):
            continue
        if P.get("hideStreet"):
            import hashlib
            slug = "listing-" + hashlib.sha1(slug.encode()).hexdigest()[:10]
        out = site / slug / "rentcast.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        emb = d.get("market") or {}
        fresh_embedded = emb.get("asOf") and (datetime.datetime.now(datetime.timezone.utc)
                                              - datetime.datetime.fromisoformat(emb["asOf"])).days < 7
        if not refresh:
            try:
                with urllib.request.urlopen(f"{SITE}/{slug}/rentcast.json", timeout=20) as r:
                    data = r.read()
                json.loads(data); out.write_bytes(data)
                print(slug, "reused live RentCast data (no API request)"); continue
            except Exception:
                if fresh_embedded:
                    out.write_text(json.dumps(emb, indent=2)); print(slug, "used this week's saved RentCast data"); continue
        if not KEY:
            if emb: out.write_text(json.dumps(emb, indent=2))
            continue
        try:
            m = market(P)
            out.write_text(json.dumps(m, indent=2))
            print(slug, "value", m["value"], "comps", len(m["comps"]))
        except Exception as e:
            print(slug, "RentCast lookup failed:", e)
            if emb: out.write_text(json.dumps(emb, indent=2))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "market"
    if cmd == "enrich":
        print("enriched:", enrich())
    else:
        write_market(pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "_site"))
