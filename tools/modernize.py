"""Create the "Modern" AI design concept for a property's photos with Gemini (Nano Banana),
using the house style in tools/modern_prompts.json. Runs in .github/workflows/import-photos.yml.

Inputs (environment):
  SLUG            property folder name (required)
  ONLY            optional comma-separated photo ids to (re)do; default = every photo without a Modern version
  REDO            "true" to replace existing Modern versions of the ONLY photos
  GEMINI_API_KEY  required (image output needs a paid Gemini API plan)
  GEMINI_IMAGE_MODEL  optional model id override
Writes properties/<slug>/img/<photo>-modern-<hash>.jpg, sets each photo's "modern" src, and
appends a note to properties/<slug>/import.json.
"""
import base64, hashlib, io, json, os, pathlib, re, sys, time

import requests
from PIL import Image, ImageOps

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import photos as P  # reuse the room classifier

ROOT = pathlib.Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "tools" / "modern_prompts.json").read_text())
API = "https://generativelanguage.googleapis.com/v1beta"


def prompt_for(room):
    if not room:
        return CFG["prefix"] + " " + CFG["prompts"]["auto"]
    for key, rooms in CFG["rooms"].items():
        if (room or "") in rooms:
            return CFG["prefix"] + " " + CFG["prompts"][key]
    return CFG["prefix"] + " " + CFG["prompts"]["interior"]


def image_model(key):
    want = os.environ.get("GEMINI_IMAGE_MODEL", "").strip()
    if want:
        return want
    try:
        r = requests.get(f"{API}/models", params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=30)
        names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        imgs = [n for n in names if "image" in n and "flash" in n and "preview" not in n and "lite" not in n]
        imgs = imgs or [n for n in names if "image" in n and "flash" in n]
        if imgs:
            def ver(n):
                m = re.search(r"gemini-([\d.]+)", n)
                return tuple(int(x) for x in m.group(1).split(".") if x) if m else (0,)
            return max(imgs, key=ver)
    except Exception as e:
        print("model list failed:", e)
    return "gemini-2.5-flash-image"


def to_jpeg(raw, target_size):
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    # keep the original photo's shape so Original/Modern line up on the page
    tw, th = target_size
    r = tw / th
    if abs(im.width / im.height - r) > 0.02:
        size = (round(im.height * r), im.height) if im.width / im.height > r else (im.width, round(im.width / r))
        im = ImageOps.fit(im, size, Image.LANCZOS)
    if max(im.size) > 2000:
        im.thumbnail((2000, 2000), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=86, optimize=True, progressive=True)
    return buf.getvalue()


def generate(key, model, src_jpg, prompt):
    body = {"contents": [{"role": "user", "parts": [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(src_jpg).decode()}},
        {"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
    for attempt in range(5):
        r = requests.post(f"{API}/models/{model}:generateContent", headers={"x-goog-api-key": key},
                          json=body, timeout=180)
        if r.status_code in (429, 500, 503):
            time.sleep(10 * (attempt + 1)); continue
        if r.status_code in (400, 403):
            msg = r.json().get("error", {}).get("message", r.text[:200])
            raise RuntimeError(msg)
        r.raise_for_status()
        for cand in r.json().get("candidates", []):
            for part in (cand.get("content") or {}).get("parts", []):
                data = part.get("inline_data") or part.get("inlineData")
                if data and data.get("data"):
                    return base64.b64decode(data["data"])
        raise RuntimeError("Gemini didn't return an image for this photo (it may have been blocked).")
    raise RuntimeError("Gemini kept refusing requests (rate limit). Try again in a few minutes.")


def replicate_generate(token, model, src_jpg, prompt):
    """Run the edit on Replicate (default google/nano-banana). Returns image bytes."""
    H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    uri = "data:image/jpeg;base64," + base64.b64encode(src_jpg).decode()
    props = {}
    try:
        m = requests.get(f"https://api.replicate.com/v1/models/{model}", headers=H, timeout=30).json()
        props = (((m.get("latest_version") or {}).get("openapi_schema") or {}).get("components", {})
                 .get("schemas", {}).get("Input", {}).get("properties", {}))
    except Exception as e:
        print("schema lookup failed:", e)
    inp = {"prompt": prompt}
    if "image_input" in props or not props:
        inp["image_input"] = [uri]
    for k in ("input_image", "image"):
        if k in props:
            inp[k] = uri; break
    if "aspect_ratio" in props:
        inp["aspect_ratio"] = "match_input_image"
    if "output_format" in props or not props:
        inp["output_format"] = "jpg"
    for attempt in range(4):
        r = requests.post(f"https://api.replicate.com/v1/models/{model}/predictions",
                          headers={**H, "Prefer": "wait=60"}, json={"input": inp}, timeout=120)
        if r.status_code == 429:
            time.sleep(15 * (attempt + 1)); continue
        if r.status_code == 402:
            raise RuntimeError("Replicate says the account needs credit. Add credit at replicate.com/account/billing.")
        if r.status_code >= 400:
            raise RuntimeError(f"Replicate error {r.status_code}: {r.json().get('detail', r.text[:200])}")
        pred = r.json()
        for _ in range(60):
            if pred.get("status") in ("succeeded", "failed", "canceled"):
                break
            time.sleep(3)
            pred = requests.get(pred["urls"]["get"], headers=H, timeout=30).json()
        if pred.get("status") != "succeeded":
            raise RuntimeError(f"Replicate didn't finish: {pred.get('error') or pred.get('status')}")
        out = pred.get("output")
        url = out[0] if isinstance(out, list) else out
        return requests.get(url, timeout=120).content
    raise RuntimeError("Replicate kept refusing requests (rate limit). Try again in a few minutes.")


def main():
    slug = os.environ["SLUG"].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,90}", slug):
        sys.exit("bad slug")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    rep = os.environ.get("REPLICATE_API_TOKEN", "").strip()
    rep_model = os.environ.get("REPLICATE_MODEL", "").strip() or "google/nano-banana"
    pdir = ROOT / "properties" / slug
    pfile = pdir / "property.json"
    d = json.loads(pfile.read_text())
    only = [x for x in os.environ.get("ONLY", "").split(",") if x.strip()]
    redo = os.environ.get("REDO") == "true"
    todo = [p for p in d.get("photos") or []
            if (p.get("original") or {}).get("src") and ((p["id"] in only and (redo or not p.get("modern"))) if only else not p.get("modern"))]
    notes, made, failed = [], 0, 0
    if not todo:
        notes.append("Every photo already has a Modern version.")
    elif not key and not rep:
        notes.append("Modern versions weren't made: add the REPLICATE_API_TOKEN secret (or a paid GEMINI_API_KEY).")
    else:
        # rooms decide which house-style prompt to use; ask Gemini for any that are unknown
        need = [p for p in todo if not p.get("room")]
        imgs = {p["id"]: ImageOps.exif_transpose(Image.open(pdir / p["original"]["src"])).convert("RGB") for p in todo}
        if need and key:
            try:
                res = P.classify([imgs[p["id"]] for p in need], key)
                for i, p in enumerate(need):
                    if i in res: p["room"] = res[i]["room"]
            except Exception as e:
                print("room check failed:", e)
        model = rep_model if rep else image_model(key)
        print("Image model:", ("replicate " if rep else "gemini ") + model)
        for p in todo:
            im = imgs[p["id"]]
            src = im.copy(); src.thumbnail((1280, 1280) if rep else (1600, 1600))
            buf = io.BytesIO(); src.save(buf, "JPEG", quality=90)
            try:
                gen = replicate_generate(rep, model, buf.getvalue(), prompt_for(p.get("room"))) if rep \
                    else generate(key, model, buf.getvalue(), prompt_for(p.get("room")))
                out = to_jpeg(gen, im.size)
            except Exception as e:
                failed += 1; print("photo", p["id"], "failed:", e)
                if not notes or str(e) not in notes[-1]:
                    notes.append(f"Photo {p.get('caption') or p['id']}: {e}")
                if any(w in str(e).lower() for w in ("billing", "quota", "free tier", "credit")):
                    if not rep: notes.append("Image generation needs billing turned on for the Gemini API key.")
                    break
                continue
            old = (p.get("modern") or {}).get("src")
            fn = f"img/{p['id']}-modern-{hashlib.sha1(out).hexdigest()[:8]}.jpg"
            (pdir / fn).write_bytes(out)
            if old and old.startswith("img/") and old != fn and (pdir / old).exists():
                (pdir / old).unlink()
            p["modern"] = {"src": fn}
            made += 1
            print("made", fn, "for", p.get("caption"), "room", p.get("room"))
    d["updatedAt"] = int(time.time() * 1000)
    pfile.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    imp_f = pdir / "import.json"
    imp = json.loads(imp_f.read_text()) if imp_f.exists() else {"state": "done"}
    summary = f"Made {made} Modern version{'s' if made != 1 else ''}" + (f", {failed} failed" if failed else "")
    imp.update({"state": "done", "at": int(time.time() * 1000),
                "summary": (imp.get("summary", "") + ". " if os.environ.get("APPEND") == "true" and imp.get("summary") else "") + summary,
                "notes": (imp.get("notes") or [] if os.environ.get("APPEND") == "true" else []) + notes})
    imp_f.write_text(json.dumps(imp, indent=2) + "\n")
    print(summary); [print("note:", n) for n in notes]
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write("modern=" + (summary + (". " + " ".join(notes) if notes else "")).replace("\n", " ") + "\n")


if __name__ == "__main__":
    main()
