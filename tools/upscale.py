"""Make the cover photo (photo 1) sharp enough to fill the whole width of the page.

Upscales photo 1's Original and Modern versions 2x with Real-ESRGAN on Replicate when they are
narrower than 1800px, and points the property at the sharper files. Runs in import-photos.yml.

Inputs (environment): SLUG (required), REPLICATE_API_TOKEN (required, otherwise it does nothing),
UPSCALE_MODEL (optional, default nightmareai/real-esrgan), ALL=true to do every photo, not just the cover.
"""
import base64, hashlib, io, json, os, pathlib, re, sys, time

import requests
from PIL import Image, ImageOps

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIN_WIDTH = 1800      # wide enough for a full-width cover on most screens
MAX_SIDE = 3200       # keep files a sensible size


def run(token, model, jpg):
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    m = requests.get(f"https://api.replicate.com/v1/models/{model}", headers=H, timeout=30).json()
    version = (m.get("latest_version") or {}).get("id")
    if not version:
        raise RuntimeError(f"couldn't find model {model}: {m.get('detail', m)}")
    uri = "data:image/jpeg;base64," + base64.b64encode(jpg).decode()
    body = {"version": version, "input": {"image": uri, "scale": 2, "face_enhance": False}}
    for attempt in range(4):
        r = requests.post("https://api.replicate.com/v1/predictions", headers={**H, "Prefer": "wait=60"}, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(15 * (attempt + 1)); continue
        if r.status_code == 402:
            raise RuntimeError("Replicate says the account needs credit.")
        if r.status_code >= 400:
            raise RuntimeError(f"Replicate error {r.status_code}: {r.text[:200]}")
        pred = r.json()
        for _ in range(80):
            if pred.get("status") in ("succeeded", "failed", "canceled"):
                break
            time.sleep(3)
            pred = requests.get(pred["urls"]["get"], headers=H, timeout=30).json()
        if pred.get("status") != "succeeded":
            raise RuntimeError(f"upscale didn't finish: {pred.get('error') or pred.get('status')}")
        out = pred.get("output")
        url = out[0] if isinstance(out, list) else out
        return requests.get(url, timeout=120).content
    raise RuntimeError("Replicate kept refusing requests (rate limit).")


def main():
    slug = os.environ["SLUG"].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,90}", slug):
        sys.exit("bad slug")
    token = os.environ.get("REPLICATE_API_TOKEN", "").strip()
    if not token:
        print("No REPLICATE_API_TOKEN; cover not sharpened."); return
    model = os.environ.get("UPSCALE_MODEL", "").strip() or "nightmareai/real-esrgan"
    pdir = ROOT / "properties" / slug
    pfile = pdir / "property.json"
    d = json.loads(pfile.read_text())
    photos = d.get("photos") or []
    todo = photos if os.environ.get("ALL") == "true" else photos[:1]
    made = 0
    for p in todo:
        for k in ("original", "modern"):
            src = (p.get(k) or {}).get("src") or ""
            if not src.startswith("img/") or not (pdir / src).exists():
                continue
            im = ImageOps.exif_transpose(Image.open(pdir / src)).convert("RGB")
            if im.width >= MIN_WIDTH:
                continue
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=95)
            try:
                big = ImageOps.exif_transpose(Image.open(io.BytesIO(run(token, model, buf.getvalue())))).convert("RGB")
            except Exception as e:
                print("photo", p.get("id"), k, "not sharpened:", e); continue
            if max(big.size) > MAX_SIDE:
                big.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            out = io.BytesIO(); big.save(out, "JPEG", quality=88, optimize=True, progressive=True)
            fn = f"img/{p['id']}-{k}-hd-{hashlib.sha1(out.getvalue()).hexdigest()[:8]}.jpg"
            (pdir / fn).write_bytes(out.getvalue())
            if (pdir / src).exists() and src != fn:
                (pdir / src).unlink()
            p[k] = {"src": fn}
            made += 1
            print(f"sharpened {src} {im.size} -> {fn} {big.size}")
    if made:
        d["updatedAt"] = int(time.time() * 1000)
        pfile.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    print(f"Sharpened {made} cover image(s)")


if __name__ == "__main__":
    main()
