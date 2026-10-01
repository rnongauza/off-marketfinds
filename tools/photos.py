"""Pull a property's photos from a share link (or text-message photos), let Gemini pick the
cover and put the rest in order, and save the best 10 into properties/<slug>/.

Runs in .github/workflows/import-photos.yml. Inputs come from environment variables:
  SLUG       property folder name                       (required)
  MODE       replace | add | sort                        (default replace)
  URLS       JSON list of share links                    (Google Drive, Dropbox, iCloud shared album,
                                                          Google Photos album, or direct image links)
  MEDIA      JSON list of Twilio MMS media URLs
  MAX        how many photos to keep                     (default 10)
  AI         "false" to skip Gemini and keep the order as found
  SET_MORE   "true" to use the first link as the "See all photos" link
  GEMINI_API_KEY, GEMINI_MODEL (optional), GOOGLE_API_KEY (optional, for Drive folders),
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN (for text-message photos)
Writes properties/<slug>/import.json with the result (also on failure) and prints a one-line
summary to $GITHUB_OUTPUT as `summary=...`.
"""
import base64, hashlib, html, io, json, os, pathlib, re, sys, time, urllib.parse, zipfile

import requests
from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:  # HEIC support is optional
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
UA = {"User-Agent": "Mozilla/5.0 (off-marketfinds photo importer)"}
MAX_DOWNLOADS = 80
MAX_BYTES = 40 * 1024 * 1024
LONG_EDGE = 2000
ORDER = ["living", "kitchen", "bathroom", "bedroom", "garage", "backyard", "pool"]
LABEL = {"exterior_front": "Front of house", "exterior_other": "Exterior", "living": "Living room", "family": "Family room",
         "dining": "Dining area", "kitchen": "Kitchen", "bathroom": "Bathroom", "bedroom": "Bedroom", "office": "Office",
         "laundry": "Laundry", "garage": "Garage", "backyard": "Backyard", "pool": "Pool", "view": "View",
         "hallway": "Hallway", "other": "Photo"}
ROOMS = list(LABEL)


class Skip(Exception):
    pass


# ---------------------------------------------------------------- sources

def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=60, **kw)
    r.raise_for_status()
    return r


def drive_files(url):
    key = os.environ.get("GOOGLE_API_KEY", "").strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]{10,})", url)
    if not m:
        m2 = re.search(r"(?:/file/d/|[?&]id=)([A-Za-z0-9_-]{10,})", url)
        if not m2:
            raise Skip("That Google Drive link isn't a folder or file link.")
        return [(m2.group(1), "drive-file")]
    fid = m.group(1)
    out = []
    if key:
        token = None
        while True:
            q = {"q": f"'{fid}' in parents and trashed=false and mimeType contains 'image/'", "key": key,
                 "fields": "nextPageToken,files(id,name)", "pageSize": 200, "orderBy": "name"}
            if token: q["pageToken"] = token
            r = requests.get("https://www.googleapis.com/drive/v3/files", params=q, timeout=60)
            if r.status_code != 200:
                break
            j = r.json(); out += [(f["id"], f["name"]) for f in j.get("files", [])]
            token = j.get("nextPageToken")
            if not token: break
        if out:
            return out
    # no API key (or it failed): read the public folder view
    page = get(f"https://drive.google.com/embeddedfolderview?id={fid}#grid").text
    ids = re.findall(r'id="entry-([A-Za-z0-9_-]{10,})"', page)
    names = re.findall(r'class="flip-entry-title">(.*?)<', page)
    for i, x in enumerate(ids):
        n = html.unescape(names[i]) if i < len(names) else x
        if re.search(r"\.(jpe?g|png|heic|heif|webp)$", n, re.I) or "." not in n:
            out.append((x, n))
    if not out:
        raise Skip("I couldn't see any photos in that Google Drive folder. In Drive, set Share > General access to 'Anyone with the link'.")
    return out


def drive_download(fid):
    r = requests.get("https://drive.google.com/uc", params={"export": "download", "id": fid}, headers=UA, timeout=120)
    if "text/html" in r.headers.get("content-type", ""):
        # big files show a confirmation page; follow its form
        m = re.search(r'action="([^"]+)"', r.text)
        if m:
            params = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', r.text))
            r = requests.get(html.unescape(m.group(1)), params=params, headers=UA, timeout=120)
    r.raise_for_status()
    return r.content


def from_drive(url):
    files = drive_files(url)[:MAX_DOWNLOADS]
    for fid, name in files:
        try:
            yield name, drive_download(fid)
        except Exception as e:
            print("drive: skipped", name, e)


def from_dropbox(url):
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query)); q["dl"] = "1"
    r = get(urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q))), stream=True)
    data = r.content
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if re.search(r"\.(jpe?g|png|heic|heif|webp)$", n, re.I) and "__MACOSX" not in n]
            for n in sorted(names)[:MAX_DOWNLOADS]:
                yield pathlib.Path(n).name, z.read(n)
    else:
        yield pathlib.Path(u.path).name, data


B62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def from_icloud(url):
    m = re.search(r"sharedalbum/#([A-Za-z0-9]+)", url)
    if not m:
        raise Skip("For iCloud, use a Shared Album link with 'Public Website' turned on "
                   "(Photos > Shared Albums > the album > People icon > Public Website). "
                   "Single 'Copy iCloud Link' photo links can't be read automatically, so upload those in the admin page instead.")
    token = m.group(1)
    part = B62.index(token[1]) if token[0] == "A" else B62.index(token[1]) * 62 + B62.index(token[2])
    host = f"p{part:02d}-sharedstreams.icloud.com"
    stream = None
    for _ in range(3):
        r = requests.post(f"https://{host}/{token}/sharedstreams/webstream", json={"streamCtag": None}, headers=UA, timeout=60)
        if r.status_code == 330:
            host = r.json().get("X-Apple-MMe-Host") or host
            continue
        r.raise_for_status(); stream = r.json(); break
    if not stream:
        raise Skip("I couldn't open that iCloud shared album. Check that Public Website is on.")
    photos = [p for p in stream.get("photos", []) if p.get("mediaAssetType", "") != "video"][:MAX_DOWNLOADS]
    best = {}
    for p in photos:
        ders = p.get("derivatives", {})
        if not ders: continue
        top = max(ders.values(), key=lambda d: int(d.get("width") or 0) * int(d.get("height") or 0))
        best[p["photoGuid"]] = top["checksum"]
    if not best:
        raise Skip("That iCloud album has no photos I can read.")
    r = requests.post(f"https://{host}/{token}/sharedstreams/webasseturls", json={"photoGuids": list(best)}, headers=UA, timeout=60)
    r.raise_for_status()
    items = r.json().get("items", {})
    for guid, ck in best.items():
        it = items.get(ck)
        if not it: continue
        try:
            yield guid + ".jpg", get(f"https://{it['url_location']}{it['url_path']}").content
        except Exception as e:
            print("icloud: skipped", guid, e)


def from_google_photos(url):
    page = get(url, allow_redirects=True).text
    seen = []
    for u in re.findall(r"https://lh3\.googleusercontent\.com/pw/[A-Za-z0-9_-]{40,}", page):
        if u not in seen: seen.append(u)
    if not seen:
        raise Skip("I couldn't read that Google Photos album. Make sure link sharing is on.")
    for u in seen[:MAX_DOWNLOADS]:
        try:
            yield u.rsplit("/", 1)[-1][:16] + ".jpg", get(u + "=w2400-h2400").content
        except Exception as e:
            print("google photos: skipped", e)


def from_mms(urls):
    sid, tok = os.environ.get("TWILIO_ACCOUNT_SID", ""), os.environ.get("TWILIO_AUTH_TOKEN", "")
    for i, u in enumerate(urls):
        try:
            r = requests.get(u, auth=(sid, tok) if sid else None, headers=UA, timeout=60, allow_redirects=True)
            r.raise_for_status()
            yield f"text-photo-{i+1}.jpg", r.content
        except Exception as e:
            print("mms: skipped", e)


def fetch_source(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    if "drive.google.com" in host or "docs.google.com" in host:
        return from_drive(url)
    if "dropbox.com" in host:
        return from_dropbox(url)
    if "icloud.com" in host:
        return from_icloud(url)
    if "photos.app.goo.gl" in host or "photos.google.com" in host or "goo.gl" in host:
        return from_google_photos(url)
    if re.search(r"\.(jpe?g|png|heic|webp)(\?|$)", url, re.I):
        return iter([(pathlib.Path(urllib.parse.urlparse(url).path).name, get(url).content)])
    if "onedrive" in host or "1drv.ms" in host or "sharepoint" in host:
        raise Skip("OneDrive links aren't supported yet. Use Google Drive, Dropbox, iCloud or upload the photos in the admin page.")
    raise Skip("I don't recognize that link. Use a Google Drive folder, Dropbox folder, iCloud shared album or Google Photos album link.")


# ---------------------------------------------------------------- images

def normalize(raw):
    """Return (jpeg_bytes, PIL image) or None for files that aren't usable photos."""
    if len(raw) > MAX_BYTES:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception:
        return None
    if min(im.size) < 400:
        return None
    if max(im.size) > LONG_EDGE:
        im.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=85, optimize=True, progressive=True)
    return buf.getvalue(), im


def ahash(im):
    g = im.convert("L").resize((9, 8), Image.BILINEAR)
    px = list(g.getdata())
    return sum(1 << i for i in range(64) if px[(i // 8) * 9 + i % 8] > px[(i // 8) * 9 + i % 8 + 1])


def hamming(a, b):
    return bin(a ^ b).count("1")


def thumb_b64(im, edge=768):
    t = im.copy(); t.thumbnail((edge, edge))
    buf = io.BytesIO(); t.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------- Gemini

PROMPT = """You are sorting real estate listing photos for an investor property web page.
The images are numbered in the order given, starting at 0. For EVERY image return one item with:
- i: the image number
- room: one of exterior_front (front of the house as seen from the street, showing the facade or front yard),
  exterior_other (side or back of the house, driveway, street, roof, detached garage from outside),
  living, family, dining, kitchen, bathroom, bedroom, office, laundry, garage (garage interior or the garage itself),
  backyard, pool, view, hallway, other (floor plans, documents, close-ups of details, damage close-ups, random objects)
- score: 1-10, how good it is as a listing photo (sharp, bright, level, wide, shows the whole space; low for blurry,
  dark, tilted, cluttered or close-up shots)
- people: true if a person, or a framed photo/portrait of a person, is clearly visible
- caption: 2 to 5 plain words describing the shot (for example "Kitchen with island")"""

SCHEMA = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
    "i": {"type": "INTEGER"}, "room": {"type": "STRING", "enum": ROOMS}, "score": {"type": "INTEGER"},
    "people": {"type": "BOOLEAN"}, "caption": {"type": "STRING"}},
    "required": ["i", "room", "score", "people", "caption"]}}


def pick_model(key):
    want = os.environ.get("GEMINI_MODEL", "").strip()
    if want:
        return want
    try:
        r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 200},
                         headers={"x-goog-api-key": key}, timeout=30)
        names = [m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        flash = [n for n in names if re.fullmatch(r"gemini-[\d.]+-flash", n)]
        if flash:
            return max(flash, key=lambda n: tuple(int(x) for x in re.search(r"gemini-([\d.]+)-flash", n).group(1).split(".") if x))
        for alias in ("gemini-flash-latest", "gemini-2.5-flash"):
            if alias in names: return alias
    except Exception as e:
        print("model list failed:", e)
    return "gemini-flash-latest"


def classify(images, key):
    model = pick_model(key)
    print("Gemini model:", model)
    out = {}
    for start in range(0, len(images), 6):
        batch = images[start:start + 6]
        parts = [{"text": PROMPT}]
        for j, im in enumerate(batch):
            parts += [{"text": f"Image {j}:"}, {"inline_data": {"mime_type": "image/jpeg", "data": thumb_b64(im)}}]
        body = {"contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0.1}}
        for attempt in range(6):
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              headers={"x-goog-api-key": key, "content-type": "application/json"}, json=body, timeout=120)
            if r.status_code in (429, 500, 503):
                time.sleep(min(60, 8 * (attempt + 1))); continue
            r.raise_for_status()
            try:
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                for it in json.loads(txt):
                    if 0 <= int(it["i"]) < len(batch):
                        out[start + int(it["i"])] = it
            except Exception as e:
                print("could not read Gemini answer:", e)
            break
        else:
            raise RuntimeError("Gemini kept refusing requests (rate limit). Try again in a few minutes.")
    return out


def choose(cands, limit):
    """cands: list of dicts with room/score/people. Returns ordered list (cover first)."""
    ok = [c for c in cands if not c.get("people") and c.get("score", 5) >= 3]
    if not ok:
        ok = [c for c in cands if not c.get("people")] or cands
    used, picked = set(), []

    def best(pred):
        pool = [c for c in ok if id(c) not in used and pred(c)]
        return max(pool, key=lambda c: (c.get("score", 0), c.get("existing", False))) if pool else None

    cover = best(lambda c: c.get("room") == "exterior_front") or best(lambda c: c.get("room") == "exterior_other") or best(lambda c: True)
    if cover:
        picked.append(cover); used.add(id(cover))
    for room in ORDER:
        if len(picked) >= limit: break
        c = best(lambda c, r=room: c.get("room") == r)
        if c: picked.append(c); used.add(id(c))
    # fill: prefer rooms we don't have yet, then the best of the rest
    while len(picked) < limit:
        have = {c.get("room") for c in picked}
        c = best(lambda c: c.get("room") not in have and c.get("room") != "other" and c.get("score", 0) >= 5) \
            or best(lambda c: c.get("room") != "other" and c.get("score", 0) >= 6)
        if not c: break
        picked.append(c); used.add(id(c))
    return picked


def captions(picked):
    counts, seen = {}, {}
    for c in picked:
        counts[c["room"]] = counts.get(c["room"], 0) + 1
    for c in picked:
        r = c.get("room") or "other"
        if r in ("other", "view", "hallway", "office", "family", "dining", "exterior_other") and c.get("caption"):
            c["caption_out"] = c["caption"][:60].strip().capitalize()
            continue
        seen[r] = seen.get(r, 0) + 1
        label = LABEL.get(r, "Photo")
        c["caption_out"] = label + (f" {seen[r]}" if counts[r] > 1 and r in ("bedroom", "bathroom") else "")


# ---------------------------------------------------------------- main

def main():
    slug = os.environ["SLUG"].strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,90}", slug):
        sys.exit("bad slug")
    pdir = ROOT / "properties" / slug
    pfile = pdir / "property.json"
    d = json.loads(pfile.read_text())
    mode = os.environ.get("MODE", "replace")
    limit = max(1, min(20, int(os.environ.get("MAX", "10") or 10)))
    urls = [u for u in (json.loads(os.environ.get("URLS") or "null") or []) if u]
    if os.environ.get("URL_ONE", "").strip():
        urls = [os.environ["URL_ONE"].strip()]
    media = [u for u in (json.loads(os.environ.get("MEDIA") or "null") or []) if u]
    use_ai = os.environ.get("AI", "true") != "false"
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    notes = []

    cands = []
    seen_hashes = []

    def add(name, jpg, im, existing=None):
        h = ahash(im)
        if any(hamming(h, x) <= 5 for x in seen_hashes):
            return
        seen_hashes.append(h)
        cands.append({"name": name, "jpg": jpg, "im": im, "existing": existing is not None, "photo": existing,
                      "room": (existing or {}).get("room") or "", "score": 5, "people": False,
                      "caption": (existing or {}).get("caption") or re.sub(r"[-_]+", " ", pathlib.Path(name).stem)})

    if mode in ("add", "sort"):
        for ph in d.get("photos") or []:
            src = (ph.get("original") or {}).get("src") or (ph.get("modern") or {}).get("src")
            if not src or not (pdir / src).exists():
                continue
            raw = (pdir / src).read_bytes()
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
            add(src, None, im, existing=ph)

    found = 0
    for u in urls:
        try:
            for name, raw in fetch_source(u):
                n = normalize(raw)
                if n:
                    found += 1; add(name, n[0], n[1])
                if found >= MAX_DOWNLOADS: break
        except Skip as e:
            notes.append(str(e))
        except Exception as e:
            notes.append(f"I couldn't download from that link ({type(e).__name__}).")
            print("download error:", repr(e))
    if media:
        for name, raw in from_mms(media):
            n = normalize(raw)
            if n:
                found += 1; add(name, n[0], n[1])

    new_count = sum(1 for c in cands if not c["existing"])
    if (urls or media) and not new_count:
        raise RuntimeError(" ".join(notes) or "I didn't find any usable photos at that link.")

    if use_ai and key and cands:
        try:
            res = classify([c["im"] for c in cands], key)
            for i, c in enumerate(cands):
                if i in res:
                    c.update({"room": res[i]["room"], "score": int(res[i]["score"]), "people": bool(res[i]["people"]),
                              "caption": res[i]["caption"]})
        except Exception as e:
            notes.append(f"The AI sorter was unavailable ({e}); photos are in their original order.")
            use_ai = False
    elif use_ai and not key:
        notes.append("GEMINI_API_KEY isn't set, so photos are in their original order.")
        use_ai = False

    if use_ai:
        picked = choose(cands, limit)
        captions(picked)
    else:
        picked = [c for c in cands][:limit]
        for c in picked:
            c["caption_out"] = c["caption"] if c["existing"] else (LABEL.get(c["room"]) if c["room"] else "")

    # write files + new photo list
    imgdir = pdir / "img"; imgdir.mkdir(exist_ok=True)
    photos = []
    for c in picked:
        if c["existing"]:
            ph = dict(c["photo"])
        else:
            h = hashlib.sha1(c["jpg"]).hexdigest()[:10]
            fn = f"img/{h}.jpg"
            (pdir / fn).write_bytes(c["jpg"])
            ph = {"id": "ph" + h, "original": {"src": fn}}
        ph["caption"] = c.get("caption_out") or ph.get("caption") or ""
        if c.get("room"): ph["room"] = c["room"]
        if use_ai: ph["ai"] = {"score": c.get("score"), "room": c.get("room")}
        photos.append(ph)

    # remove files no longer used by any photo
    keep = {v["src"] for p in photos for k, v in p.items() if k in ("original", "modern") and isinstance(v, dict) and v.get("src")}
    for old in d.get("photos") or []:
        for k in ("original", "modern"):
            src = (old.get(k) or {}).get("src", "")
            if src.startswith("img/") and src not in keep and (pdir / src).exists():
                (pdir / src).unlink()

    d["photos"] = photos
    if os.environ.get("SET_MORE") == "true" and urls and not notes:
        L = d.setdefault("links", {})
        if L.get("more") != urls[0]:
            L["more"] = urls[0]; L["showMore"] = True
            links_f = ROOT / "links.json"
            links = json.loads(links_f.read_text()) if links_f.exists() else {}
            code = L.get("moreShort")
            if not code or (links.get(code) or {}).get("slug") != slug:
                base = re.sub(r"[^a-z0-9]", "", (d.get("short") or "p").lower()) + "pics"
                code = base; i = 2
                while code in links: code = f"{base}{i}"; i += 1
            links[code] = {"url": urls[0], "label": "More photos: " + d["property"].get("street", slug), "slug": slug,
                           "kind": "photos", "createdAt": int(time.time() * 1000)}
            L["moreShort"] = code
            links_f.write_text(json.dumps(links, indent=2, ensure_ascii=False) + "\n")
    d["updatedAt"] = int(time.time() * 1000)
    pfile.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")

    order = ", ".join(p["caption"] or "photo" for p in photos)
    dropped = len(cands) - len(picked)
    summary = f"{len(photos)} photos on {d['property'].get('street', slug)}"
    if use_ai and photos:
        summary += f". Cover: {photos[0]['caption'] or 'photo 1'}"
    if dropped > 0:
        summary += f". Left out {dropped} (duplicates, people, blurry or extra)"
    result = {"state": "done", "at": int(time.time() * 1000), "mode": mode, "found": found, "kept": len(photos),
              "order": order, "notes": notes, "summary": summary}
    (pdir / "import.json").write_text(json.dumps(result, indent=2) + "\n")
    print(summary); print("order:", order)
    for n in notes: print("note:", n)
    site = os.environ.get("SITE_URL", "https://off-marketfinds.com").rstrip("/").replace("https://", "")
    text = (f"Photos are in for {d['property'].get('street', slug)}: {len(photos)} picked"
            + (f", cover is {photos[0]['caption'].lower()}" if use_ai and photos and photos[0].get('caption') else "")
            + f". Live in about a minute: {site}/go/{d.get('short') or ''}"
            + (". Note: " + " ".join(notes) if notes else ""))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write("summary=" + (summary + (". " + " ".join(notes) if notes else "")).replace("\n", " ") + "\n")
            f.write("message=" + text.replace("\n", " ") + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        slug = os.environ.get("SLUG", "").strip()
        msg = str(e) if not isinstance(e, KeyError) else "missing input"
        print("FAILED:", repr(e))
        if re.fullmatch(r"[a-z0-9][a-z0-9-]{1,90}", slug or "") and (ROOT / "properties" / slug).is_dir():
            (ROOT / "properties" / slug / "import.json").write_text(json.dumps(
                {"state": "failed", "at": int(time.time() * 1000), "message": msg}, indent=2) + "\n")
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write("summary=Photo import failed: " + msg.replace("\n", " ") + "\n")
                f.write("message=I couldn't add the photos: " + msg.replace("\n", " ") + "\n")
                f.write("failed=true\n")
        sys.exit(0)  # still commit import.json so the admin page can show the problem
