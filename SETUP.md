# Off-Market Finds: text-to-listing automation

## What it does

1. Text a property address to your Twilio number. The bot replies "What's the asking price?"
2. Reply with the price, for example `350k`.
3. The bot creates:
   - the public page `off-marketfinds.com/<address>/`
   - a short link such as `off-marketfinds.com/go/123m`
   - the admin page `off-marketfinds.com/admin/#<address>`

   It texts you the public and admin links. RentCast fills in beds, baths, square feet, year built, a market value (used as the starting ARV) and comps.
4. Add photos. You can:
   - text a share link: a Google Drive folder, Dropbox folder, iCloud shared album or Google Photos album
   - send the photos by text
   - add them in the admin page, from a link or from your computer or phone

   Gemini picks the best exterior shot as photo #1. The rest go in this order: living room, kitchen, bathroom, bedroom, garage, backyard, pool, then other good shots, 10 photos max. It skips photos with people in them, blurry shots and duplicates. The folder link appears as "See all photos" under the photo grid, using its own short link.
5. Edit everything in the admin. Saving publishes to off-marketfinds.com in about a minute.

Other text commands:
- `PRICE 365k` changes the price of the last property you texted about.
- `LIST` shows your newest properties with their short links.
- `CANCEL` stops the current address.
- `HELP` lists the commands.

You can also send the address and price in one text: `123 Main St, Vallejo CA 94590 $350k`.

## How it is built

| Piece | Where | What it does |
|---|---|---|
| `properties/<slug>/property.json` + `img/` | this repo | One folder per property: the data and photos. |
| `links.json` | this repo | Short links: `off-marketfinds.com/go/<code>`. |
| `tools/build.py` | GitHub Actions | Builds the whole site: home page, property pages, short links and admin. |
| `tools/rentcast.py` | GitHub Actions | Fills in new properties, then adds the weekly value, rent and comps. |
| `tools/photos.py` | GitHub Actions | Downloads the photos, sorts them with Gemini and keeps the best 10. |
| `worker/` | Cloudflare Workers (free) | Text bot and admin API. It saves changes to this repo. |
| `site/admin/` | off-marketfinds.com/admin | The admin app, which needs a password. |

## One-time setup

Add each of these under **GitHub repo → Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Where to get it |
|---|---|
| `GH_PAT` | GitHub → Settings → Developer settings → Fine-grained tokens → Generate. Choose **only this repository**, give it **Contents: Read and write**, and set it to expire in 1 year. |
| `ADMIN_PASSWORD` | Make up a long password for the admin page. |
| `CLOUDFLARE_API_TOKEN` | Cloudflare → My Profile → API Tokens → Create Token → use the **Edit Cloudflare Workers** template. |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare dashboard → Workers & Pages → Account ID, shown in the right sidebar. |
| `GEMINI_API_KEY` | aistudio.google.com → Get API key. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` | Twilio Console home page. |
| `TWILIO_PHONE_NUMBER` | Your Twilio number, for example `+17075550100`. |
| `OWNER_PHONES` | The cell numbers allowed to text the bot, for example `+15104593029`. Separate several numbers with commas. |
| `RENTCAST_API_KEY` | Already set. |
| `GOOGLE_API_KEY` *(optional)* | A Google Cloud API key with the Drive API enabled. It makes large Drive folders more reliable. Without it the importer reads the public folder view. |

Then go to **Actions → Deploy text bot → Run workflow**. The workflow:
- creates the Cloudflare Worker and its storage
- loads the secrets into the Worker
- points the admin page at the Worker
- points your Twilio number's incoming messages at `https://off-marketfinds-bot.<you>.workers.dev/sms`

Run it again any time you change a secret.

**Twilio note:** US carriers only deliver replies after your A2P 10DLC (Sole Proprietor) registration is approved. Until then the bot receives your texts, but its replies are blocked. The admin page works the whole time. If your number belongs to a Messaging Service, set the service's incoming messages to "Defer to sender's webhook".

## Costs

- **Cloudflare Workers:** free plan.
- **GitHub Actions and Pages:** free for this public repo.
- **Twilio:**
  - number: about $1.15 a month
  - Sole Proprietor brand: $4.50 one-time
  - campaign vetting: $15
  - campaign: about $2 a month
  - texts: about a penny each
- **Gemini:** a free tier is available. Sorting one property is one request per 6 photos.
- **RentCast:** the free plan has 50 requests a month. A new property uses 3, and the weekly refresh uses 2 per property.

## Run things by hand

- **Re-import or re-sort photos:** Actions → Import photos → Run workflow (slug, link, mode).
- **Refresh RentCast now:** Actions → Publish site → Run workflow.
