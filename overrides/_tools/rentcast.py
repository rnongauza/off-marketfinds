"""Pull the RentCast value/rent estimate and the best 3-4 same-type comps for each property.
Runs inside the GitHub publish workflow; the API key comes from the repo secret RENTCAST_API_KEY.
Writes <site>/<slug>/rentcast.json, which each property page loads automatically."""
import json, os, sys, pathlib, urllib.parse, urllib.request, datetime

site = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "_site")
key = os.environ.get("RENTCAST_API_KEY", "").strip()
if not key:
    print("RENTCAST_API_KEY not set - skipping RentCast refresh"); sys.exit(0)

def get(path, params):
    url = "https://api.rentcast.io/v1/" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Api-Key": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

STATUS = {"Active": "Active listing", "Inactive": "Off market"}
props = json.loads((pathlib.Path(__file__).parent / "properties.json").read_text())
for p in props:
    q = {"address": p["address"], "propertyType": p["propertyType"], "compCount": 20}
    for k in ("bedrooms", "bathrooms", "squareFootage"):
        if p.get(k): q[k] = p[k]
    try:
        v = get("avm/value", q)
    except Exception as e:
        print("value lookup failed for", p["slug"], e); continue
    try:
        r = get("avm/rent/long-term", q)
    except Exception as e:
        print("rent lookup failed for", p["slug"], e); r = {}
    same = [c for c in v.get("comparables") or [] if (c.get("propertyType") or "") == p["propertyType"] and c.get("price")]
    same.sort(key=lambda c: (-(c.get("correlation") or 0), c.get("distance") or 99))
    comps = [{
        "address": c.get("formattedAddress"),
        "price": c.get("price"),
        "beds": c.get("bedrooms"), "baths": c.get("bathrooms"), "sqft": c.get("squareFootage"),
        "yearBuilt": c.get("yearBuilt"), "type": c.get("propertyType"),
        "distance": c.get("distance"),
        "status": STATUS.get(c.get("status"), c.get("status")),
        "date": c.get("removedDate") or c.get("listedDate"),
    } for c in same[:4]]
    out = {
        "source": "RentCast",
        "asOf": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "value": v.get("price"), "low": v.get("priceRangeLow"), "high": v.get("priceRangeHigh"),
        "rent": r.get("rent"), "rentLow": r.get("rentRangeLow"), "rentHigh": r.get("rentRangeHigh"),
        "comps": comps,
    }
    d = site / p["slug"]; d.mkdir(parents=True, exist_ok=True)
    (d / "rentcast.json").write_text(json.dumps(out, indent=2))
    print(p["slug"], "value", out["value"], "comps", len(comps))
