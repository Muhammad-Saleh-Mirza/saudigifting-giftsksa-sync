#!/usr/bin/env python3
"""
Daily GiftsKSA -> Odoo (SaudiGifting) product sync.

Pulls Product / Price / Stock from GiftsKSA's "API Data Exchange Service"
and applies it to Odoo's product.template records over Odoo's external
XML-RPC API. Designed to run unattended from a GitHub Actions schedule.

Required environment variables (set as GitHub Actions secrets):
  GIFTSKSA_TOKEN   - the reseller API token
  ODOO_URL         - e.g. https://saudigifting.odoo.com
  ODOO_DB          - e.g. saudigifting
  ODOO_USERNAME    - the Odoo login/email the API key belongs to
  ODOO_API_KEY     - an Odoo "API Key" generated under
                     Settings (user menu) -> My Profile -> Security -> API Keys

Optional environment variables:
  GIFTSKSA_BASE_URL - override the GiftsKSA API base (default production).
                      Set to https://api-test.jasani.ae for a free, unlimited
                      dry run against the test mirror before trusting this
                      against the real 5-requests/day production quota.

Exit behaviour: never partially applies a run. If any of the three GiftsKSA
API calls fail, the script aborts before touching Odoo at all.
"""
import base64
import os
import sys
import time
import xmlrpc.client
from datetime import datetime, timezone

import requests

GIFTSKSA_TOKEN = os.environ["GIFTSKSA_TOKEN"]
# `or` (not plain .get(..., default)) so an explicitly empty env var (e.g. a
# GitHub Actions expression that resolves to "") still falls back correctly.
GIFTSKSA_BASE_URL = os.environ.get("GIFTSKSA_BASE_URL") or "https://www.giftsksa.com"

ODOO_URL = os.environ["ODOO_URL"].rstrip("/")
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USERNAME = os.environ["ODOO_USERNAME"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

# Deliberate placeholder product: several GiftsKSA API rows for colour/size
# variants of this item come back with default_code == False (no distinct
# SKU). They were consolidated into this one Odoo product by hand. Never
# auto-archive it, even though it will never get its own synced-date update.
PROTECTED_DEFAULT_CODES = {"KSB-1W"}

REQUEST_TIMEOUT = 60
RPC_RETRY_ATTEMPTS = 4
RPC_RETRY_BACKOFF_SECONDS = 5

# Some bot-filters in front of these sites reject the default
# `python-requests/x.y` User-Agent with a bare 403, even for a legitimate,
# documented API endpoint. A normal browser-looking User-Agent (this is not
# bypassing any auth or consent - it's just matching what a browser sends)
# clears that.
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------
# GiftsKSA API
# --------------------------------------------------------------------------

def fetch_giftsksa(path):
    url = f"{GIFTSKSA_BASE_URL}{path}/{GIFTSKSA_TOKEN}"
    log(f"GET {url}")
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
    if resp.status_code >= 400:
        # Diagnostic dump: a WAF/CDN block page usually says so explicitly
        # (e.g. "Cloudflare", "Access denied", "Ray ID") - print it so the
        # real cause is visible in the Actions log instead of just "403".
        log(f"  non-OK response: HTTP {resp.status_code}")
        log(f"  response headers: {dict(resp.headers)}")
        log(f"  response body (first 1000 chars): {resp.text[:1000]!r}")
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected response shape from {path}: {type(data)}")
    log(f"  -> {len(data)} rows")
    return data


def diagnostic_homepage_ping():
    """
    Harmless, token-free, zero-quota GET to the bare domain root (no API
    path at all). Purely diagnostic: tells us whether the whole domain is
    blocked from this runner's IP, or only the specific gated API paths
    are. Never raises - a failure here must never abort the real sync.
    """
    url = GIFTSKSA_BASE_URL + "/"
    log(f"[diagnostic] GET {url} (plain homepage ping, no token)")
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
        log(f"[diagnostic]   -> HTTP {resp.status_code}")
        if resp.status_code >= 400:
            log(f"[diagnostic]   response headers: {dict(resp.headers)}")
            log(f"[diagnostic]   response body (first 500 chars): {resp.text[:500]!r}")
        else:
            log("[diagnostic]   homepage reachable - block (if any) is specific to the API path(s)")
    except Exception as exc:  # noqa: BLE001 - diagnostic only, must never abort the run
        log(f"[diagnostic]   request itself failed: {exc}")


def fetch_image_b64(image_url):
    resp = requests.get(image_url, timeout=REQUEST_TIMEOUT, headers=HTTP_HEADERS)
    resp.raise_for_status()
    return base64.b64encode(resp.content).decode("ascii")


# --------------------------------------------------------------------------
# Odoo external API (XML-RPC)
# --------------------------------------------------------------------------

class Odoo:
    def __init__(self, url, db, username, api_key):
        self.db = db
        self.common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        self.uid = self.common.authenticate(db, username, api_key, {})
        if not self.uid:
            raise RuntimeError(
                "Odoo authentication failed - check ODOO_USERNAME/ODOO_API_KEY/ODOO_DB"
            )
        self.models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        self.api_key = api_key
        log(f"Authenticated to Odoo as uid={self.uid} (db={db})")

    def execute(self, model, method, args, kwargs=None):
        kwargs = kwargs or {}
        last_err = None
        for attempt in range(1, RPC_RETRY_ATTEMPTS + 1):
            try:
                return self.models.execute_kw(
                    self.db, self.uid, self.api_key, model, method, args, kwargs
                )
            except Exception as exc:  # noqa: BLE001 - genuinely want to retry anything
                last_err = exc
                log(
                    f"  RPC {model}.{method} failed (attempt {attempt}/"
                    f"{RPC_RETRY_ATTEMPTS}): {exc}"
                )
                if attempt < RPC_RETRY_ATTEMPTS:
                    time.sleep(RPC_RETRY_BACKOFF_SECONDS * attempt)
        raise last_err

    def search(self, model, domain, fields=None):
        kwargs = {"fields": fields} if fields else {}
        return self.execute(model, "search_read", [domain], kwargs)


# --------------------------------------------------------------------------
# Category resolution (self-healing: never blocks on an unseen name)
# --------------------------------------------------------------------------

def build_category_resolver(odoo):
    cats = odoo.search("product.public.category", [], ["id", "name"])
    by_name = {c["name"].strip().lower(): c["id"] for c in cats}
    created = []

    def resolve(name):
        if not name:
            return None
        key = name.strip().lower()
        if key in by_name:
            return by_name[key]
        new_id = odoo.execute(
            "product.public.category", "create", [{"name": name.strip(), "parent_id": False}]
        )
        by_name[key] = new_id
        created.append(name.strip())
        log(f"  auto-created new category: {name!r} (id={new_id})")
        return new_id

    return resolve, created


# --------------------------------------------------------------------------
# Main sync
# --------------------------------------------------------------------------

def main():
    run_started = datetime.now(timezone.utc)
    run_timestamp = run_started.strftime("%Y-%m-%d %H:%M:%S")
    log(f"=== GiftsKSA -> Odoo sync starting (run timestamp {run_timestamp}) ===")
    log(f"GiftsKSA base URL: {GIFTSKSA_BASE_URL}")

    # 0. Diagnostic only (temporary - remove once the 403 is root-caused):
    #    a plain, token-free ping to the bare domain, to tell a blanket
    #    domain-wide block apart from a block on just the API path(s).
    diagnostic_homepage_ping()

    # 1. Pull all three GiftsKSA datasets FIRST. If any fails, abort before
    #    touching Odoo at all (never apply a partial/inconsistent pull).
    try:
        products = fetch_giftsksa("/products/all")
        prices = fetch_giftsksa("/products/price")
        stocks = fetch_giftsksa("/products/stock")
    except Exception as exc:  # noqa: BLE001
        log(f"FATAL: could not fetch GiftsKSA data, aborting without touching Odoo: {exc}")
        sys.exit(1)

    price_by_id = {p["id"]: p for p in prices if "id" in p}
    stock_by_id = {s["id"]: s for s in stocks if "id" in s}

    rows = []
    skipped_no_code = 0
    for p in products:
        pid = p.get("id")
        code = p.get("default_code")
        if not code:
            skipped_no_code += 1
            continue
        price_row = price_by_id.get(pid, {})
        stock_row = stock_by_id.get(pid, {})
        rows.append(
            {
                "id": pid,
                "name": p.get("name") or code,
                "default_code": code,
                "categ_names": [
                    c.get("name") for c in (p.get("public_categ_ids") or []) if c.get("name")
                ],
                "image_url": p.get("image_url"),
                "retail_price": price_row.get("retail_price"),
                "stock_qty": stock_row.get("net_available_qty") or 0,
            }
        )
    log(
        f"Joined {len(rows)} usable rows from {len(products)} product rows "
        f"({skipped_no_code} skipped: no default_code)"
    )
    seen_codes = {row["default_code"] for row in rows}

    # 2. Connect to Odoo.
    odoo = Odoo(ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_API_KEY)
    resolve_category, newly_created_categories = build_category_resolver(odoo)

    # 3. Index existing GiftsKSA-sourced products by default_code.
    existing = odoo.search(
        "product.template",
        [["x_giftsksa_source", "=", "giftsksa"]],
        ["id", "default_code", "name", "x_giftsksa_discontinued"],
    )
    existing_by_code = {e["default_code"]: e["id"] for e in existing if e["default_code"]}
    log(f"Found {len(existing)} existing GiftsKSA-sourced products in Odoo")

    updated, created, errors = 0, 0, []

    for row in rows:
        try:
            cat_ids = [resolve_category(n) for n in row["categ_names"]]
            cat_ids = [c for c in cat_ids if c]

            existing_id = existing_by_code.get(row["default_code"])
            if existing_id:
                vals = {
                    "list_price": row["retail_price"] or 0,
                    "x_giftsksa_stock_qty": row["stock_qty"],
                    "x_giftsksa_discontinued": False,
                    "x_giftsksa_synced_date": run_timestamp,
                    "x_giftsksa_source": "giftsksa",
                    "website_published": True,
                    "is_published": True,
                    "sale_ok": True,
                }
                if cat_ids:
                    vals["public_categ_ids"] = [[4, c] for c in cat_ids]
                odoo.execute("product.template", "write", [[existing_id], vals])
                updated += 1
            else:
                vals = {
                    "name": row["name"],
                    "default_code": row["default_code"],
                    "list_price": row["retail_price"] or 0,
                    "type": "consu",
                    "sale_ok": True,
                    "website_published": True,
                    "is_published": True,
                    "x_giftsksa_stock_qty": row["stock_qty"],
                    "x_giftsksa_discontinued": False,
                    "x_giftsksa_source": "giftsksa",
                    "x_giftsksa_synced_date": run_timestamp,
                }
                if cat_ids:
                    vals["public_categ_ids"] = [[6, 0, cat_ids]]
                if row["image_url"]:
                    try:
                        vals["image_1920"] = fetch_image_b64(row["image_url"])
                    except Exception as img_exc:  # noqa: BLE001
                        log(f"  warning: image fetch failed for {row['default_code']}: {img_exc}")
                odoo.execute("product.template", "create", [vals])
                created += 1
        except Exception as exc:  # noqa: BLE001
            errors.append((row["default_code"], str(exc)))
            log(f"  ERROR processing {row['default_code']}: {exc}")

    # 4. Archive products that used to come from GiftsKSA but are no longer
    #    in today's feed (never hard-delete; just unpublish + mark discontinued).
    #    Based on which default_codes actually appeared in today's feed
    #    (`seen_codes`), NOT on synced_date - a product whose update happened
    #    to error out this run (see `errors` above) is still present upstream
    #    and must never be archived just because its write failed.
    stragglers = [e for e in existing if e["default_code"] and e["default_code"] not in seen_codes]
    archived = []
    for s in stragglers:
        if s["default_code"] in PROTECTED_DEFAULT_CODES:
            continue
        if s["x_giftsksa_discontinued"]:
            continue  # already archived in a previous run
        odoo.execute(
            "product.template",
            "write",
            [
                [s["id"]],
                {
                    "website_published": False,
                    "is_published": False,
                    "sale_ok": False,
                    "x_giftsksa_discontinued": True,
                },
            ],
        )
        archived.append(f"{s['default_code']} ({s['name']})")

    # 5. Summary.
    log("=== Sync complete ===")
    log(f"Updated:  {updated}")
    log(f"Created:  {created}")
    log(f"Archived: {len(archived)}")
    for a in archived:
        log(f"  archived: {a}")
    if newly_created_categories:
        log(f"New categories auto-created this run: {newly_created_categories}")
    if errors:
        log(f"Errors ({len(errors)}):")
        for code, err in errors:
            log(f"  {code}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
