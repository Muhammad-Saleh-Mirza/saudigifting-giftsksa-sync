# SaudiGifting <- GiftsKSA daily sync

Pulls Product / Price / Stock from GiftsKSA's API Data Exchange Service and
applies it to Odoo (`saudigifting.odoo.com`) once a day, automatically, via
a GitHub Actions scheduled workflow. No server to maintain, no AI usage, no
manual steps after the one-time setup below.

What it does each run:
- Updates existing products' price (`retail_price`), stock count
  (`net_available_qty`) and categories.
- Creates any genuinely new products (skips rows with no `default_code` -
  those are unresolvable variant rows).
- Auto-creates any category name GiftsKSA uses that Odoo doesn't have yet
  (self-healing - never blocks on an unrecognised category).
- Archives (unpublishes, never deletes) any product that used to come from
  GiftsKSA but no longer appears in the feed - except the `KSB-1W`
  consolidated placeholder product, which is intentionally protected.
- Uses exactly 3 of GiftsKSA's 5-requests/day quota (Product, Price, Stock -
  one call each).

Verified with an offline mocked test (`test_sync.py`, mocks both the
GiftsKSA HTTP calls and Odoo's XML-RPC so it runs with no network access)
before being handed over - see "Testing" below to re-run it yourself.

## One-time setup

### 1. Add these two files to the repo

Put `sync.py`, `requirements.txt` and `.github/workflows/sync.yml` (keep the
`.github/workflows/` folder structure) at the root of
`Muhammad-Saleh-Mirza/saudigifting-giftsksa-sync`. Easiest way: on
github.com, use "Add file -> Upload files" and drag all three in (GitHub
preserves the folder path from the zip/drag).

### 2. Generate an Odoo API key

This is a credential, so please do this step yourself rather than handing
me a password:

1. Log into `saudigifting.odoo.com` as the user you want the sync to run
   as (e.g. the admin account).
2. Click your avatar (top right) -> **My Profile** -> **Security** tab.
3. Under **API Keys**, click **Add API Key**.
4. Name it something like `GitHub Actions - GiftsKSA Daily Sync`.
5. Scope: **RPC**. Duration: **Persistent Key** (or a long duration like
   1 Year, if you'd rather rotate it periodically).
6. Odoo will show you the generated key **once** - copy it immediately.

### 3. Add 5 GitHub repository secrets

On the repo: **Settings -> Secrets and variables -> Actions -> New
repository secret**. Add each of these (again, please enter these
yourself rather than pasting them to me):

| Secret name | Value |
|---|---|
| `GIFTSKSA_TOKEN` | `1cb27d810f454b93b21d696f697a2c3d` |
| `ODOO_URL` | `https://saudigifting.odoo.com` |
| `ODOO_DB` | `saudigifting` |
| `ODOO_USERNAME` | `nabeelsiddiqui2001@gmail.com` (or whichever user owns the API key) |
| `ODOO_API_KEY` | the key you copied in step 2 |

### 4. Test it before trusting it with production quota

Go to the repo's **Actions** tab -> **GiftsKSA daily sync** -> **Run
workflow** -> tick **"Run against the free test mirror"** -> **Run
workflow**. This runs the exact same logic against GiftsKSA's free,
unlimited test mirror (`api-test.jasani.ae`), so it costs no production
quota and is safe to run as many times as you like. Open the run and read
the log - it prints exactly what it updated/created/archived.

Once that looks right, run it again with the box **unticked** to do one
real run against production (uses 3 of today's 5 quota requests), then
check a product or two in Odoo to confirm.

After that, it just runs on its own every day at 23:07 UTC
(02:07 Saudi time) - nothing further to do.

## Changing the schedule

Edit the `cron:` line in `.github/workflows/sync.yml`
(currently `7 23 * * *` = 23:07 UTC daily). GitHub Actions cron is UTC
only.

## Testing

```
pip install requests
python3 test_sync.py
```

This runs entirely offline (mocks both GiftsKSA's HTTP API and Odoo's
XML-RPC API) and checks: price comes from `retail_price` not `list_price`,
stock comes from `net_available_qty`, unknown categories get auto-created,
existing categories are added to (not replacing prior manual category
assignments), new products get created with their image, rows with no
`default_code` are skipped entirely, products no longer in the feed get
archived (unpublished, not deleted), the `KSB-1W` placeholder is never
touched, and - importantly - a product whose update happens to fail this
run is never mistakenly archived just because of that failure.
