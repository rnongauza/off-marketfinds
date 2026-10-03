// Off-Market Finds bot: receives texts from Twilio, powers the admin app, and saves
// everything to the GitHub repo (which rebuilds off-marketfinds.com automatically).
//
// Secrets / vars (set by .github/workflows/deploy-worker.yml from the repo's GitHub secrets):
//   GH_TOKEN, GH_REPO, GH_BRANCH, ADMIN_PASSWORD, SESSION_SECRET,
//   TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, OWNER_PHONES, SITE_URL
// KV binding: STATE  (text conversations, login throttling, short-link clicks, import status)

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };
const MAX_PHOTOS = 10;

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    const origin = req.headers.get("origin") || "";
    const cors = corsHeaders(env, origin);
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    try {
      let res;
      if (url.pathname === "/sms" && req.method === "POST") res = await handleSms(req, env, url);
      else if (url.pathname.startsWith("/c/") && req.method === "POST") res = await countClick(env, url.pathname.slice(3));
      else if (url.pathname.startsWith("/api/")) res = await handleApi(req, env, url);
      else if (url.pathname === "/") res = new Response("Off-Market Finds bot is running.", { headers: { "content-type": "text/plain" } });
      else res = json({ error: "not_found" }, 404);
      const h = new Headers(res.headers);
      for (const [k, v] of Object.entries(cors)) h.set(k, v);
      return new Response(res.body, { status: res.status, headers: h });
    } catch (e) {
      const status = e.status || 500;
      return new Response(JSON.stringify({ error: e.code || "server_error", message: e.publicMessage || (status < 500 ? e.message : "Something went wrong. Try again in a moment.") }),
        { status, headers: { ...JSON_HEADERS, ...cors } });
    }
  },
};

/* ---------------- helpers ---------------- */

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: JSON_HEADERS });
}
function fail(status, code, message) {
  const e = new Error(message || code); e.status = status; e.code = code; e.publicMessage = message; return e;
}
function corsHeaders(env, origin) {
  const site = (env.SITE_URL || "https://off-marketfinds.com").replace(/\/$/, "");
  const allowed = [site, site.replace("https://", "https://www."), "http://localhost:8000", "http://127.0.0.1:8000"];
  return {
    "access-control-allow-origin": allowed.includes(origin) ? origin : site,
    "access-control-allow-methods": "GET,POST,PUT,DELETE,OPTIONS",
    "access-control-allow-headers": "authorization,content-type",
    "access-control-max-age": "86400",
    "vary": "origin",
  };
}
const enc = new TextEncoder();
const dec = new TextDecoder();
function b64encodeBytes(bytes) {
  let s = ""; const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  return btoa(s);
}
function b64decodeToString(b64) {
  const bin = atob(b64.replace(/\s/g, ""));
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return dec.decode(bytes);
}
function b64url(bytes) { return b64encodeBytes(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
async function hmac(alg, key, msg) {
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: alg }, false, ["sign"]);
  return new Uint8Array(await crypto.subtle.sign("HMAC", k, enc.encode(msg)));
}
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const x = enc.encode(a), y = enc.encode(b);
  let d = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) d |= (x[i] || 0) ^ (y[i] || 0);
  return d === 0;
}
function siteUrl(env) { return (env.SITE_URL || "https://off-marketfinds.com").replace(/\/$/, ""); }
function money(n) { return "$" + Math.round(n).toLocaleString("en-US"); }

/* ---------------- GitHub storage ---------------- */

async function gh(env, path, opts = {}) {
  const r = await fetch(`https://api.github.com/repos/${env.GH_REPO}${path}`, {
    ...opts,
    headers: {
      authorization: `Bearer ${env.GH_TOKEN}`, accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28", "user-agent": "off-marketfinds-bot",
      ...(opts.body ? { "content-type": "application/json" } : {}),
    },
  });
  if (r.status === 404 && opts.allow404) return null;
  if (!r.ok) {
    const t = await r.text();
    const e = fail(r.status === 409 || r.status === 422 ? r.status : 502, "github_error", `GitHub ${r.status}: ${t.slice(0, 200)}`);
    e.ghStatus = r.status; throw e;
  }
  return r.status === 204 ? null : r.json();
}
const branch = env => env.GH_BRANCH || "main";

async function readFile(env, path) {
  const d = await gh(env, `/contents/${encodeURI(path)}?ref=${branch(env)}`, { allow404: true });
  if (!d) return null;
  let text;
  if (d.content && d.encoding === "base64") text = b64decodeToString(d.content);
  else { const blob = await gh(env, `/git/blobs/${d.sha}`); text = b64decodeToString(blob.content); }
  return { text, sha: d.sha };
}
async function readJson(env, path, fallback) {
  const f = await readFile(env, path);
  return f ? { data: JSON.parse(f.text), sha: f.sha } : { data: fallback, sha: null };
}

// changes: [{path, text} | {path, b64} | {path, delete:true}]
async function commit(env, changes, message) {
  const blobs = [];
  for (const c of changes) {
    if (c.delete) { blobs.push({ path: c.path, mode: "100644", type: "blob", sha: null }); continue; }
    const b = await gh(env, "/git/blobs", { method: "POST", body: JSON.stringify(c.b64 != null ? { content: c.b64, encoding: "base64" } : { content: c.text, encoding: "utf-8" }) });
    blobs.push({ path: c.path, mode: "100644", type: "blob", sha: b.sha });
  }
  for (let attempt = 0; attempt < 4; attempt++) {
    const ref = await gh(env, `/git/ref/heads/${branch(env)}`);
    const head = await gh(env, `/git/commits/${ref.object.sha}`);
    const tree = await gh(env, "/git/trees", { method: "POST", body: JSON.stringify({ base_tree: head.tree.sha, tree: blobs }) });
    const c = await gh(env, "/git/commits", { method: "POST", body: JSON.stringify({ message, tree: tree.sha, parents: [ref.object.sha] }) });
    try {
      await gh(env, `/git/refs/heads/${branch(env)}`, { method: "PATCH", body: JSON.stringify({ sha: c.sha }) });
      return c.sha;
    } catch (e) {
      if (e.ghStatus !== 422 && e.ghStatus !== 409) throw e; // someone else committed first: retry on top
    }
  }
  throw fail(409, "busy", "The site was busy saving something else. Try again.");
}
async function dispatch(env, type, payload) {
  await gh(env, "/dispatches", { method: "POST", body: JSON.stringify({ event_type: type, client_payload: payload }) });
}

/* ---------------- properties ---------------- */

const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,90}$/;
function slugify(s) {
  return String(s).toLowerCase().normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/#/g, " unit ")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80);
}
function splitAddress(a) {
  a = a.replace(/\s+/g, " ").trim().replace(/[.]$/, "");
  const i = a.indexOf(",");
  if (i > 0) return { street: a.slice(0, i).trim(), cityLine: a.slice(i + 1).trim() };
  const m = a.match(/^(.*?\b(?:st|street|ave|avenue|rd|road|dr|drive|ln|lane|ct|court|blvd|boulevard|way|pl|place|cir|circle|ter|terrace|pkwy|hwy|loop|trl|sq)\.?)\s+(.+)$/i);
  if (m) return { street: m[1].trim(), cityLine: m[2].trim() };
  return { street: a, cityLine: "" };
}
function parsePrice(s) {
  const t = String(s).toLowerCase().replace(/asking|price|is|for|at|usd|obo|firm/g, " ").trim();
  const m = t.match(/^\$?\s*(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)\s*(k|thousand|m|mil|million)?\s*$/);
  if (!m) return null;
  let n = parseFloat(m[1].replace(/[,\s]/g, ""));
  const u = m[2] || "";
  if (u.startsWith("k") || u === "thousand") n *= 1e3;
  else if (u.startsWith("m")) n *= 1e6;
  else if (n < 10000) n *= 1e3; // "350" means $350,000
  n = Math.round(n);
  return n >= 1000 && n < 1e9 ? n : null;
}
async function listProperties(env) {
  const items = await gh(env, `/contents/properties?ref=${branch(env)}`, { allow404: true }) || [];
  const out = [];
  await Promise.all(items.filter(i => i.type === "dir").map(async i => {
    const f = await readJson(env, `properties/${i.name}/property.json`, null);
    if (!f.data) return;
    const d = f.data, P = d.property || {}, ph = (d.photos || [])[0];
    const img = ph && ((ph.modern && ph.modern.src) || (ph.original && ph.original.src));
    out.push({ slug: i.name, street: P.street || i.name, cityLine: P.cityLine || "", price: P.price || 0, arv: P.arv || 0,
      beds: P.beds, baths: P.baths, photos: (d.photos || []).length, listed: d.listed !== false, short: d.short || "",
      createdAt: d.createdAt || 0, updatedAt: d.updatedAt || 0,
      image: img ? (img.startsWith("http") || img.startsWith("/") ? img : `/${i.name}/${img}`) : "" });
  }));
  out.sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
  return out;
}
async function uniqueCode(env, base, links) {
  links = links || (await readJson(env, "links.json", {})).data;
  base = (base || "").toLowerCase().replace(/[^a-z0-9]/g, "").slice(0, 8) || "p";
  if (!links[base]) return base;
  for (let i = 2; i < 50; i++) if (!links[base + i]) return base + i;
  for (;;) { const c = base.slice(0, 4) + Math.random().toString(36).slice(2, 6); if (!links[c]) return c; }
}
function shortBase(street) {
  const m = String(street).match(/^(\d+[a-z]?)\s+(?:[nsew]\.?\s+)?([a-z])/i);
  return m ? m[1] + m[2] : String(street).replace(/[^a-z0-9]/gi, "").slice(0, 5);
}

async function createProperty(env, address, price, via) {
  address = String(address || "").replace(/\s+/g, " ").trim();
  if (address.length < 6) throw fail(400, "bad_address", "Enter the full street address.");
  const slug = slugify(address);
  if (!SLUG_RE.test(slug)) throw fail(400, "bad_address", "That address doesn't look right.");
  const path = `properties/${slug}/property.json`;
  const existing = await readJson(env, path, null);
  if (existing.data) {
    if (price && existing.data.property.price !== price) {
      existing.data.property.price = price; existing.data.updatedAt = Date.now();
      await commit(env, [{ path, text: JSON.stringify(existing.data, null, 2) + "\n" }], `Update price for ${existing.data.property.street} (${via})`);
    }
    return { slug, short: existing.data.short, existed: true, data: existing.data };
  }
  const { street, cityLine } = splitAddress(address);
  const linksF = await readJson(env, "links.json", {});
  const short = await uniqueCode(env, shortBase(street), linksF.data);
  linksF.data[short] = { url: `/${slug}/`, label: street, slug, kind: "property", createdAt: Date.now() };
  const now = Date.now();
  const data = {
    slug, short, listed: true, createdAt: now, updatedAt: now, createdVia: via, needsEnrich: true,
    property: { street, cityLine, price: price || 0, arv: 0, beds: "", baths: "", sqft: 0, lot: "", yearBuilt: "",
      propertyType: "Single Family", occupancy: "", overview: [] },
    photos: [], calc: {}, links: { more: "", moreShort: "", showMore: true },
  };
  await commit(env, [
    { path, text: JSON.stringify(data, null, 2) + "\n" },
    { path: "links.json", text: JSON.stringify(linksF.data, null, 2) + "\n" },
  ], `New property: ${street} (${via})`);
  return { slug, short, existed: false, data };
}

// how a photo sits in its frame, from the admin photo editor
function photoLook(p) {
  const o = {}, num = v => v !== "" && v != null && Number.isFinite(+v);
  for (const k of ["focus", "x", "y", "cx", "cy"]) if (num(p[k])) o[k] = Math.max(0, Math.min(100, Math.round(+p[k] * 10) / 10));
  for (const k of ["z", "cz"]) if (num(p[k]) && +p[k] > 0) o[k] = Math.min(4, Math.round(+p[k] * 1000) / 1000);
  if (p.frame === "photo") o.frame = "photo";
  else if (num(p.frame) && +p.frame >= 0.3 && +p.frame <= 3.5) o.frame = Math.round(+p.frame * 10000) / 10000;
  return o;
}

function cleanProperty(input, current) {
  // Keep only the fields the admin edits; never trust the client for slug/short/createdAt.
  const d = JSON.parse(JSON.stringify(current));
  const P = input.property || {};
  const keep = ["eyebrow", "headline", "street", "cityLine", "price", "arv", "beds", "baths", "sqft", "lot", "yearBuilt",
    "occupancy", "propertyType", "overview", "offer", "deal", "hideStreet"];
  for (const k of keep) if (k in P) d.property[k] = P[k];
  if ("heroText" in P) {  // where the headline block sits on the cover (admin photo editor)
    const t = P.heroText, ok = v => v !== "" && v != null && Number.isFinite(+v);
    if (t && ok(t.x) && ok(t.y)) d.property.heroText = { x: Math.max(0, Math.min(100, Math.round(+t.x * 10) / 10)), y: Math.max(0, Math.min(100, Math.round(+t.y * 10) / 10)) };
    else delete d.property.heroText;
  }
  if (Array.isArray(input.photos)) {
    d.photos = input.photos.slice(0, 40).map((p, i) => ({
      id: String(p.id || `p${i + 1}`).slice(0, 40), caption: String(p.caption || "").slice(0, 120), room: String(p.room || "").slice(0, 30),
      ...(p.original && p.original.src ? { original: { src: String(p.original.src) } } : {}),
      ...(p.modern && p.modern.src ? { modern: { src: String(p.modern.src) } } : {}),
      ...(p.ai ? { ai: p.ai } : {}),
      ...photoLook(p),
    }));
  }
  if (input.calc && typeof input.calc === "object") d.calc = input.calc;
  if (input.links && typeof input.links === "object") d.links = { ...d.links, ...input.links };
  if ("listed" in input) d.listed = !!input.listed;
  d.updatedAt = Date.now();
  return d;
}
function imagePathsInUse(d) {
  const s = new Set();
  for (const p of d.photos || []) for (const k of ["original", "modern"]) if (p[k] && p[k].src && p[k].src.startsWith("img/")) s.add(p[k].src);
  return s;
}

/* ---------------- admin API ---------------- */

async function sessionSecret(env) {
  if (env.SESSION_SECRET) return env.SESSION_SECRET;
  return b64url(await hmac("SHA-256", env.GH_TOKEN || "x", "session|" + (env.ADMIN_PASSWORD || "")));
}
async function makeToken(env) {
  const body = b64url(enc.encode(JSON.stringify({ exp: Date.now() + 30 * 86400e3 })));
  return body + "." + b64url(await hmac("SHA-256", await sessionSecret(env), body));
}
async function checkToken(env, req) {
  const t = (req.headers.get("authorization") || "").replace(/^Bearer\s+/i, "");
  const [body, sig] = t.split(".");
  if (!body || !sig) return false;
  if (!safeEqual(sig, b64url(await hmac("SHA-256", await sessionSecret(env), body)))) return false;
  try { return JSON.parse(atob(body.replace(/-/g, "+").replace(/_/g, "/"))).exp > Date.now(); } catch { return false; }
}

async function handleApi(req, env, url) {
  const parts = url.pathname.split("/").filter(Boolean).slice(1); // after "api"
  const body = req.method === "GET" || req.method === "DELETE" ? {} : await req.json().catch(() => ({}));

  if (parts[0] === "login" && req.method === "POST") {
    const ip = req.headers.get("cf-connecting-ip") || "?";
    const key = "fail:" + ip;
    const fails = parseInt(await env.STATE.get(key) || "0", 10);
    if (fails >= 10) throw fail(429, "locked", "Too many attempts. Wait 15 minutes and try again.");
    if (!env.ADMIN_PASSWORD || !safeEqual(String(body.password || ""), env.ADMIN_PASSWORD)) {
      await env.STATE.put(key, String(fails + 1), { expirationTtl: 900 });
      throw fail(401, "wrong_password", "That password isn't right.");
    }
    return json({ token: await makeToken(env) });
  }
  if (!(await checkToken(env, req))) throw fail(401, "login_required", "Please log in again.");

  if (parts[0] === "me") return json({ ok: true, repo: env.GH_REPO, site: siteUrl(env), sms: !!env.TWILIO_FROM, phone: env.TWILIO_FROM || "" });

  if (parts[0] === "properties" && parts.length === 1) {
    if (req.method === "GET") return json({ properties: await listProperties(env) });
    if (req.method === "POST") {
      const price = typeof body.price === "number" ? Math.round(body.price) : parsePrice(body.price || "");
      if (!price) throw fail(400, "bad_price", "Enter the asking price, like 350000 or 350k.");
      const r = await createProperty(env, body.address, price, "admin");
      return json({ slug: r.slug, short: r.short, existed: r.existed });
    }
  }

  if (parts[0] === "properties" && parts[1]) {
    const slug = parts[1];
    if (!SLUG_RE.test(slug)) throw fail(400, "bad_slug");
    const path = `properties/${slug}/property.json`;

    if (parts.length === 2 && req.method === "GET") {
      const f = await readJson(env, path, null);
      if (!f.data) throw fail(404, "not_found", "That property doesn't exist.");
      const imp = await readJson(env, `properties/${slug}/import.json`, null);
      const queued = JSON.parse(await env.STATE.get("import:" + slug) || "null");
      return json({ data: f.data, sha: f.sha, import: imp.data, queued });
    }
    if (parts.length === 2 && req.method === "PUT") {
      const f = await readJson(env, path, null);
      if (!f.data) throw fail(404, "not_found");
      if (body.sha && body.sha !== f.sha) return json({ error: "conflict", message: "This property changed somewhere else (maybe the photo AI finished). Reload to see the latest, then save again.", data: f.data, sha: f.sha }, 409);
      const next = cleanProperty(body.data || {}, f.data);
      // remove image files no longer referenced
      const before = imagePathsInUse(f.data), after = imagePathsInUse(next);
      const changes = [{ path, text: JSON.stringify(next, null, 2) + "\n" }];
      for (const p of before) if (!after.has(p)) changes.push({ path: `properties/${slug}/${p}`, delete: true });
      // keep links.json in step with the "more photos" short link
      if (next.links && next.links.more) {
        if (!/^https:\/\/\S+$/.test(next.links.more)) throw fail(400, "bad_url", "The photo folder link must start with https://");
        const lf = await readJson(env, "links.json", {});
        let code = next.links.moreShort;
        if (!code || !lf.data[code] || lf.data[code].slug !== slug) code = await uniqueCode(env, (next.short || "p") + "pics", lf.data);
        if (!lf.data[code] || lf.data[code].url !== next.links.more) {
          lf.data[code] = { url: next.links.more, label: `More photos: ${next.property.street}`, slug, kind: "photos", createdAt: Date.now() };
          changes.push({ path: "links.json", text: JSON.stringify(lf.data, null, 2) + "\n" });
        }
        next.links.moreShort = code;
        changes[0].text = JSON.stringify(next, null, 2) + "\n";
      }
      await commit(env, changes, `Update ${next.property.street} (admin)`);
      const nf = await readJson(env, path, null);
      return json({ data: nf.data, sha: nf.sha });
    }
    if (parts.length === 2 && req.method === "DELETE") {
      const ref = await gh(env, `/git/ref/heads/${branch(env)}`);
      const head = await gh(env, `/git/commits/${ref.object.sha}`);
      const tree = await gh(env, `/git/trees/${head.tree.sha}?recursive=1`);
      const files = tree.tree.filter(t => t.type === "blob" && t.path.startsWith(`properties/${slug}/`));
      if (!files.length) throw fail(404, "not_found");
      const lf = await readJson(env, "links.json", {});
      for (const [k, v] of Object.entries(lf.data)) if (v && v.slug === slug) delete lf.data[k];
      await commit(env, [...files.map(f => ({ path: f.path, delete: true })), { path: "links.json", text: JSON.stringify(lf.data, null, 2) + "\n" }], `Remove property ${slug} (admin)`);
      return json({ ok: true });
    }
    // upload photos: {files:[{name, b64, photoId?, kind?}], sha}
    if (parts[2] === "photos" && req.method === "POST") {
      const f = await readJson(env, path, null);
      if (!f.data) throw fail(404, "not_found");
      const files = (body.files || []).slice(0, 12);
      if (!files.length) throw fail(400, "no_files");
      const d = f.data; d.photos = d.photos || [];
      const changes = [];
      const stamp = Date.now().toString(36);
      files.forEach((x, i) => {
        if (!/^[A-Za-z0-9+/=]+$/.test(x.b64 || "")) throw fail(400, "bad_file");
        const file = `img/u${stamp}${i}.jpg`;
        changes.push({ path: `properties/${slug}/${file}`, b64: x.b64 });
        const target = x.photoId && d.photos.find(p => p.id === x.photoId);
        if (target) target[x.kind === "modern" ? "modern" : "original"] = { src: file };
        else d.photos.push({ id: "u" + stamp + i, caption: String(x.caption || "").slice(0, 120), room: "", original: { src: file } });
      });
      d.updatedAt = Date.now();
      changes.unshift({ path, text: JSON.stringify(d, null, 2) + "\n" });
      await commit(env, changes, `Add ${files.length} photo(s) to ${d.property.street} (admin)`);
      const nf = await readJson(env, path, null);
      return json({ data: nf.data, sha: nf.sha });
    }
    // create Modern versions with Gemini for some or all photos
    if (parts[2] === "modern" && req.method === "POST") {
      const f = await readJson(env, path, null);
      if (!f.data) throw fail(404, "not_found");
      const ids = (Array.isArray(body.ids) ? body.ids : []).map(String).filter(x => /^[A-Za-z0-9_-]{1,40}$/.test(x)).slice(0, 20);
      const queued = { state: "queued", mode: "modern", at: Date.now(), source: "modern" };
      await env.STATE.put("import:" + slug, JSON.stringify(queued), { expirationTtl: 86400 });
      await dispatch(env, "import_photos", { slug, mode: "modern", only: ids.join(","), redo: body.redo === true });
      return json({ queued });
    }
    // AI import from a folder link, or re-sort what's there
    if ((parts[2] === "import" || parts[2] === "sort") && req.method === "POST") {
      const f = await readJson(env, path, null);
      if (!f.data) throw fail(404, "not_found");
      const payload = { slug, mode: parts[2] === "sort" ? "sort" : (body.mode === "add" ? "add" : "replace"), ai: body.ai !== false, max: MAX_PHOTOS, modern: body.modern === true };
      if (parts[2] === "import") {
        const link = String(body.url || "").trim();
        if (!/^https:\/\/\S+$/.test(link)) throw fail(400, "bad_url", "Paste the full share link, starting with https://");
        payload.urls = [link];
        payload.setMore = body.setMore !== false;
      }
      const queued = { state: "queued", mode: payload.mode, at: Date.now(), source: (payload.urls || [""])[0] };
      await env.STATE.put("import:" + slug, JSON.stringify(queued), { expirationTtl: 86400 });
      await dispatch(env, "import_photos", payload);
      return json({ queued });
    }
  }

  if (parts[0] === "links") {
    const lf = await readJson(env, "links.json", {});
    if (req.method === "GET") {
      const clicks = {};
      await Promise.all(Object.keys(lf.data).map(async k => { clicks[k] = parseInt(await env.STATE.get("clicks:" + k) || "0", 10); }));
      return json({ links: lf.data, clicks, base: siteUrl(env) + "/go/" });
    }
    if (req.method === "POST") {
      const target = String(body.url || "").trim();
      if (!/^(https:\/\/\S+|\/[a-z0-9-\/]*)$/i.test(target)) throw fail(400, "bad_url", "Enter a full link starting with https://");
      let code = String(body.code || "").toLowerCase().replace(/[^a-z0-9-]/g, "").slice(0, 30);
      if (code && lf.data[code]) throw fail(409, "taken", `off-marketfinds.com/go/${code} is already used.`);
      if (!code) code = await uniqueCode(env, Math.random().toString(36).slice(2, 6), lf.data);
      lf.data[code] = { url: target, label: String(body.label || "").slice(0, 80), kind: "custom", createdAt: Date.now() };
      await commit(env, [{ path: "links.json", text: JSON.stringify(lf.data, null, 2) + "\n" }], `Short link /go/${code} (admin)`);
      return json({ code, short: siteUrl(env) + "/go/" + code });
    }
    if (req.method === "DELETE" && parts[1]) {
      if (!lf.data[parts[1]]) throw fail(404, "not_found");
      if (lf.data[parts[1]].kind === "property") throw fail(400, "in_use", "That link belongs to a property page.");
      delete lf.data[parts[1]];
      await commit(env, [{ path: "links.json", text: JSON.stringify(lf.data, null, 2) + "\n" }], `Remove short link /go/${parts[1]} (admin)`);
      return json({ ok: true });
    }
  }
  throw fail(404, "not_found");
}

async function countClick(env, code) {
  code = code.replace(/[^a-z0-9-]/gi, "").slice(0, 30);
  if (code) {
    const k = "clicks:" + code;
    const n = parseInt(await env.STATE.get(k) || "0", 10);
    await env.STATE.put(k, String(n + 1));
  }
  return new Response(null, { status: 204 });
}

/* ---------------- text messages (Twilio) ---------------- */

async function validTwilio(req, env, url, params) {
  const sig = req.headers.get("x-twilio-signature") || "";
  if (!env.TWILIO_TOKEN) return false;
  const keys = [...params.keys()].sort();
  let data = url.toString();
  for (const k of keys) data += k + params.get(k);
  return safeEqual(sig, b64encodeBytes(await hmac("SHA-1", env.TWILIO_TOKEN, data)));
}
function twiml(msg) {
  const x = String(msg).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return new Response(`<?xml version="1.0" encoding="UTF-8"?><Response>${msg ? `<Message>${x}</Message>` : ""}</Response>`,
    { headers: { "content-type": "text/xml; charset=utf-8" } });
}
const owners = env => String(env.OWNER_PHONES || "").split(/[,\s]+/).map(p => p.replace(/[^\d+]/g, "")).filter(Boolean)
  .map(p => p.startsWith("+") ? p : (p.length === 10 ? "+1" + p : "+" + p));

function linksText(env, r) {
  const base = siteUrl(env).replace(/^https?:\/\//, "");
  return `Public page: ${base}/go/${r.short}\nAdmin: ${base}/admin/#${r.slug}`;
}
const HELP = "Text an address to add a property. I'll ask for the price.\n" +
  "Then text a Google Drive, Dropbox, iCloud shared album or Google Photos link, or send photos, and I'll pick and order the best 10.\n" +
  "Other commands: PRICE 365k (change the last property's price), LIST, CANCEL.";

async function handleSms(req, env, url) {
  const form = await req.formData();
  const params = new URLSearchParams();
  for (const [k, v] of form.entries()) params.append(k, String(v));
  if (!(await validTwilio(req, env, url, params))) return new Response("forbidden", { status: 403 });
  const from = params.get("From") || "";
  if (!owners(env).includes(from)) return twiml(""); // ignore texts from anyone else
  const text = (params.get("Body") || "").trim();
  const numMedia = parseInt(params.get("NumMedia") || "0", 10);
  const stateKey = "sms:" + from, lastKey = "last:" + from;
  const state = JSON.parse(await env.STATE.get(stateKey) || "null");
  const last = await env.STATE.get(lastKey);
  const lower = text.toLowerCase();

  if (/^(help|\?|info|commands)$/.test(lower)) return twiml(HELP);
  if (/^(cancel|nevermind|never mind|reset)$/.test(lower)) { await env.STATE.delete(stateKey); return twiml("OK, cancelled. Text an address whenever you're ready."); }

  if (lower === "list") {
    const list = (await listProperties(env)).slice(0, 6);
    if (!list.length) return twiml("No properties yet. Text an address to add one.");
    const base = siteUrl(env).replace(/^https?:\/\//, "");
    return twiml(list.map(p => `${p.street} ${p.price ? money(p.price) : ""}\n${base}/go/${p.short}`).join("\n\n"));
  }

  // photos sent by text (MMS) -> add to the last property
  if (numMedia > 0) {
    if (!last) return twiml("Text the property address first, then send the photos.");
    const media = [];
    for (let i = 0; i < Math.min(numMedia, 10); i++) {
      const u = params.get("MediaUrl" + i), t = params.get("MediaContentType" + i) || "";
      if (u && t.startsWith("image/")) media.push(u);
    }
    if (!media.length) return twiml("I can only use photos (JPEG, PNG or HEIC).");
    await queueImport(env, last, { media, mode: "add", notify: from });
    return twiml(`Got ${media.length} photo${media.length > 1 ? "s" : ""}. I'll add ${media.length > 1 ? "them" : "it"} to ${await streetOf(env, last)}, re-pick the best 10 and text you when it's done.`);
  }

  // a share link -> import photos into the last property
  const link = text.match(/https?:\/\/\S+/);
  if (link) {
    if (!last) return twiml("Text the property address first, then the photo link.");
    const f = await readJson(env, `properties/${last}/property.json`, null);
    const mode = f.data && (f.data.photos || []).length ? "add" : "replace";
    await queueImport(env, last, { urls: [link[0]], mode, notify: from, setMore: true });
    return twiml(`Thanks. I'm pulling the photos for ${f.data ? f.data.property.street : last} now. Gemini will pick the best cover photo and order the rest (max 10). I'll text you when they're on the page.`);
  }

  // "price 365k" -> change the last property's price
  const pm = text.match(/^price\s+(.+)$/i);
  if (pm && last) {
    const price = parsePrice(pm[1]);
    if (!price) return twiml("I couldn't read that price. Try: PRICE 365k");
    const path = `properties/${last}/property.json`;
    const f = await readJson(env, path, null);
    if (!f.data) return twiml("I couldn't find the last property. Text its address again.");
    f.data.property.price = price; f.data.updatedAt = Date.now();
    await commit(env, [{ path, text: JSON.stringify(f.data, null, 2) + "\n" }], `Update price for ${f.data.property.street} (text)`);
    return twiml(`Updated ${f.data.property.street} to ${money(price)}. The page refreshes in about a minute.`);
  }

  // waiting for the price
  if (state && state.step === "price") {
    const price = parsePrice(text);
    if (price) {
      await env.STATE.delete(stateKey);
      const r = await createProperty(env, state.address, price, "text");
      await env.STATE.put(lastKey, r.slug, { expirationTtl: 60 * 86400 });
      if (r.existed) return twiml(`${r.data.property.street} already exists, so I set its price to ${money(price)}.\n${linksText(env, r)}`);
      return twiml(`Done! ${splitAddress(state.address).street} is set up at ${money(price)}. It goes live in about 2 minutes.\n${linksText(env, r)}\n\nNext, text a Google Drive, Dropbox or iCloud shared album link (or send photos) and I'll pick and order the best 10.`);
    }
    if (!/^\d+\s+\S+/.test(text)) return twiml(`What's the asking price for ${state.address}? (for example 350k)`);
  }

  // an address (optionally with the price in the same text)
  if (/^\d+[a-z]?\s+\S+.{3,}/i.test(text)) {
    const both = text.match(/^(.*?)[\s,;]+(?:asking\s+|price\s+|for\s+|at\s+)?(\$\s*[\d,.]+\s*(?:k|m)?|[\d,.]+\s*(?:k|m))\s*$/i);
    if (both && parsePrice(both[2])) {
      const price = parsePrice(both[2]);
      const r = await createProperty(env, both[1].replace(/[,\s]+$/, ""), price, "text");
      await env.STATE.put(lastKey, r.slug, { expirationTtl: 60 * 86400 });
      await env.STATE.delete(stateKey);
      return twiml(`Done! Set up at ${money(price)}. It goes live in about 2 minutes.\n${linksText(env, r)}\n\nText a photo folder link or send photos next.`);
    }
    await env.STATE.put(stateKey, JSON.stringify({ step: "price", address: text, at: Date.now() }), { expirationTtl: 86400 });
    return twiml(`Got it: ${text}\nWhat's the asking price?`);
  }
  return twiml("Text a property address to get started, or HELP for options.");
}

async function streetOf(env, slug) {
  const f = await readJson(env, `properties/${slug}/property.json`, null);
  return f.data ? f.data.property.street : slug;
}
async function queueImport(env, slug, opts) {
  const payload = { slug, mode: opts.mode || "add", ai: true, max: MAX_PHOTOS, notify: opts.notify || "", setMore: !!opts.setMore, modern: true };
  if (opts.urls) payload.urls = opts.urls;
  if (opts.media) payload.media = opts.media;
  await env.STATE.put("import:" + slug, JSON.stringify({ state: "queued", mode: payload.mode, at: Date.now(), source: (opts.urls || ["text message photos"])[0] }), { expirationTtl: 86400 });
  await dispatch(env, "import_photos", payload);
}

export const _test = { parsePrice, splitAddress, slugify, shortBase, cleanProperty };
