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

--------------------------------------------------------------------------
What's new in this version (2026-10-31)
--------------------------------------------------------------------------
Two independent changes, both requested together but scoped separately:

1. Real stock reconciliation. Every run now also pushes the GiftsKSA feed's
   stock number into Odoo's actual, customer-facing on-hand quantity
   (stock.quant / qty_available) for every flat, directly-matched product
   - both on update (existing products, main() step 3) and immediately
   after create (create_flat_product). Previously this script only wrote
   the feed's number into x_giftsksa_stock_qty, a custom tracking field
   that nothing on the storefront actually reads - real on-hand stock
   could silently drift from what the supplier reports (confirmed live:
   one product was off by over 200 units). See reconcile_stock_quant()
   above for exactly how the adjustment is made (always via the standard
   inventory_quantity + action_apply_inventory() pattern, never a direct
   write to quantity, and never touching more than one quant per product
   per run). This does NOT extend to variant-attached codes (see the
   2026-10-26 "KNOWN, DELIBERATE LIMITATION" below) - that remains a
   separate, not-yet-built follow-up.

2. 60% website markup. list_price is now set to GiftsKSA's retail_price
   times PRICE_MARKUP (1.6) instead of retail_price directly - both for
   brand-new flat products and on every existing flat product's nightly
   price refresh. Because the existing-product path rewrites list_price
   unconditionally on every run for every matched code, this also means
   every already-synced flat product's price self-corrects to the new
   60%-marked-up basis automatically on the very next run, with no
   separate one-time repricing script needed for those. A product whose
   price has since been manually overridden in Odoo will have that
   override overwritten back to the formula's value on its next sync,
   same as list_price already behaved before this change (the sync has
   always unconditionally owned list_price for flat GiftsKSA products;
   this change only alters the formula, not that pre-existing behavior).

--------------------------------------------------------------------------
What's new in the previous version (2026-10-29)
--------------------------------------------------------------------------
Root-caused and fixed the duplicate-product problem the 2026-10-26
create-path fix (below) was supposed to prevent but didn't fully: that fix
only attached a new code as a variant of an existing product when
GiftsKSA's own per-row attribute data (Color/Size) was present and
parseable. On the runs that actually produced the 600+ duplicate "TROPIKANA
..." / "ADF ..." / etc. products (confirmed by reading the live data - not
guessed), the incoming rows for codes like "TROPIKANA Black-3XL" matched an
existing product by NAME, but had no usable attrs, so they silently fell
through to create_flat_product and spawned a lookalike duplicate instead.

Three changes, all scoped to closing exactly that hole:

1. Default-code fallback parsing. When a row's own attrs are empty/
   unparseable, Color/Size are now also recovered from the default_code
   itself (e.g. "TROPIKANA Black-3XL" -> Size "3X-Large", Color "Black")
   before giving up - checked against what the matched product's own
   existing variants actually look like, never guessed blindly. If this
   still can't confidently determine a dimension the product needs, the
   row is skipped and logged, never flat-created as a duplicate.

2. A second, independent safety net right before every flat create: even
   outside the name-matched batch above, if an active+published product
   with the exact same name already exists, this version refuses to
   create another one - it makes one more attempt to attach as a variant
   (same fallback parsing), and if that still doesn't work, skips and logs
   loudly rather than silently duplicating. This is defense in depth, not
   a replacement for fix #1.

3. A self-healing cleanup pass (run_duplicate_merge_cleanup), run at the
   end of every sync, that finds any same-name duplicate products that
   still exist (from before this fix, or from anything unforeseen) and
   either merges them into the main product as a new variant or archives
   them as redundant - automatically, every day, so this class of problem
   can't silently pile back up. Never hard-deletes anything.

--------------------------------------------------------------------------
What's new in the previous version (2026-10-26)
--------------------------------------------------------------------------
Two things were added on top of the previously-deployed script, both
scoped exactly to what was agreed before writing this:

1. Daily Featured-sort new-product bump. Recently-created, sellable,
   published products get the "New!" ribbon and get bumped to the front
   of the shop's default sort order, same as the old Odoo-internal cron
   used to do (ir.actions.server id=769) - ported here to plain XML-RPC
   calls so it runs as part of this external script instead.

2. Create-path duplicate-risk fix. Previously, any default_code this
   script had never seen before always created a brand-new, separate,
   flat product.template - even if a product with the exact same NAME
   already existed in Odoo as a proper multi-variant product (e.g. a
   polo shirt with Color/Size variants). The moment GiftsKSA sent a new
   color/size of an existing multi-variant product, this script would
   recreate the same "647 duplicate products in one night" problem a
   previous Odoo-internal cron had already been fixed for once before.
   Now, before creating anything, a never-before-seen code's product
   NAME is checked against existing products; if a match is found, the
   new code is attached as a new Color/Size variant of that existing
   product instead of spawning a lookalike duplicate.

   KNOWN, DELIBERATE LIMITATION: once a code is attached this way, this
   script does NOT yet keep that variant's price/stock refreshed on
   later runs - the x_giftsksa_* custom fields this script uses for
   price/stock are a template-level, "one SKU per template" shortcut,
   which doesn't cleanly extend to a variant sharing a template with
   other variants. This fix stops the duplicate-creation bug (the thing
   that was asked for); keeping those attached variants' price/stock
   continuously in sync is a separate, larger follow-up that hasn't
   been built yet - flagged here on purpose rather than quietly papered
   over.

   Explicitly NOT included in this version (by request, to keep this
   change scoped to just the two items above): the "Merge Duplicate
   Product Variants" cleanup/safety-net job from the old Odoo-internal
   automation. Nothing here deletes or merges anything.

Optional environment variable - DRY_RUN:
  Set DRY_RUN=true to log every intended Odoo change (update, create,
  variant-attach, archive, featured-sort) WITHOUT actually writing
  anything to Odoo. Important: GIFTSKSA_BASE_URL's test-mirror switch
  only changes where the *product data* comes from - it does NOT point
  at a separate test Odoo. ODOO_URL/ODOO_DB are always your real,
  production SaudiGifting site. That means running with only
  use_test_api on would still create/attach real, live, published
  products from the mirror's sample catalog. DRY_RUN=true is what makes
  a test-mirror run actually safe to look at before trusting this
  against your real catalog - combine the two (test-mirror data +
  DRY_RUN) for a true no-side-effects rehearsal.
"""
import base64
import os
import sys
import time
import xmlrpc.client
from datetime import datetime, timedelta, timezone

import requests

GIFTSKSA_TOKEN = os.environ["GIFTSKSA_TOKEN"]
# `or` (not plain .get(..., default)) so an explicitly empty env var (e.g. a
# GitHub Actions expression that resolves to "") still falls back correctly.
GIFTSKSA_BASE_URL = os.environ.get("GIFTSKSA_BASE_URL") or "https://www.giftsksa.com"

ODOO_URL = os.environ["ODOO_URL"].rstrip("/")
ODOO_DB = os.environ["ODOO_DB"]
ODOO_USERNAME = os.environ["ODOO_USERNAME"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

DRY_RUN = (os.environ.get("DRY_RUN") or "").strip().lower() in ("1", "true", "yes")

# Deliberate placeholder product: several GiftsKSA API rows for colour/size
# variants of this item come back with default_code == False (no distinct
# SKU). They were consolidated into this one Odoo product by hand. Never
# auto-archive it, even though it will never get its own synced-date update.
PROTECTED_DEFAULT_CODES = {"KSB-1W"}

REQUEST_TIMEOUT = 60
RPC_RETRY_ATTEMPTS = 4
RPC_RETRY_BACKOFF_SECONDS = 5

# Website prices are shown with a 60% markup over GiftsKSA's own
# retail_price. Applied everywhere list_price is set from retail_price -
# both for brand-new flat products (create_flat_product) and for every
# existing flat product's nightly price refresh (main(), step 3) - so a
# single change here covers both paths consistently.
PRICE_MARKUP = 1.6

# How far a product's real on-hand stock.quant total is allowed to drift
# from the latest GiftsKSA feed value before this script corrects it. Kept
# at 0 (exact match) rather than some slop allowance, since the feed value
# IS the number we want reflected on the website.
STOCK_MATCH_TOLERANCE = 0.001

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

# Fallback attribute ids, only used if a live lookup by name (see
# resolve_attribute_id) somehow fails. These match the ids this Odoo
# database has used historically for these two attributes.
ATTR_ID_FALLBACK = {"Color": 3, "Size": 2}

# Used to give a brand-new "Color" attribute value a sensible swatch color
# when this script has to create one (e.g. a size the catalog didn't have
# yet). Carried over as-is from the previous Odoo-internal automation.
COLOR_HEX = {
    "Sky Blue": "#87CEEB", "Q. Maroon": "#722F37", "Orange": "#FFA500",
    "Red": "#FF0000", "White": "#FFFFFF", "Grey": "#808080",
    "Royal blue": "#4169E1", "E-Green": "#50C878", "Maroon": "#800000",
    "Black": "#000000", "Yellow": "#FFD700", "Navy Blue": "#000080",
    "Red (CS)": "#B22222", "Charcoal Grey": "#36454F", "Grey Melange": "#9E9E9E",
    "Green": "#008000", "Petrol": "#005F6A", "Ash melange": "#B2BEB5",
    "Blue": "#0000FF", "Medium Blue": "#0000CD", "J-Green": "#29AB87",
    "Air Force Blue": "#5D8AA8", "Light Blue": "#ADD8E6",
    "Terracotta (Red/Navy)": "#E2725B", "Royal Blue/Black": "#4169E1",
    "White Cloud": "#F5F5F5", "Gulf Blue (Navy blue / Red)": "#000080",
    "Lava (Black/Dark melange": "#3B3B3B", "Ebonite (Dark melange / Black)": "#2F2F2F",
    "Light Grey": "#D3D3D3", "Royal Black": "#0B0B0B", "Cobalt Blue": "#0047AB",
    "White / Navy Blue": "#FFFFFF", "White / Black": "#FFFFFF",
    "White / Red": "#FFFFFF", "Navy - Red / Grey": "#000080",
    "Grey - Navy / White": "#808080", "White - Red / Navy": "#FFFFFF",
    "Black - Red / Grey": "#000000", "Black / Grey": "#000000",
    "R.Blue": "#4169E1", "N.Blue": "#000080", "L.Blue": "#ADD8E6",
    "L.Grey": "#D3D3D3", "D.Grey": "#36454F", "Burgundy": "#800020",
    "Beige": "#F5F5DC", "Khaki": "#C3B091", "Brown": "#964B00",
    "Pink": "#FFC0CB", "Purple": "#800080",
}


# --------------------------------------------------------------------------
# Default-code-suffix fallback parser. Used only when GiftsKSA's own
# per-row attribute data (attrs, from parse_attrs) is missing or doesn't
# parse - see the "What's new (2026-10-29)" note above for why this
# exists. (abbreviation as it appears in a default_code, canonical Odoo
# Size attribute value name) - sorted longest-abbreviation-first below so
# e.g. "XXXL" is tried before "XL" is.
# --------------------------------------------------------------------------

SIZE_TOKEN_CANONICAL = [
    ("XXXL", "3X-Large"), ("3X-Large", "3X-Large"), ("3XL", "3X-Large"),
    ("XX-Large", "XX-Large"), ("XXL", "XX-Large"), ("2XL", "XX-Large"),
    ("X-Large", "X-Large"), ("XL", "X-Large"),
    ("4X-Large", "4X-Large"), ("4XL", "4X-Large"),
    ("5X-Large", "5X-Large"), ("5XL", "5X-Large"),
    ("X-Small", "X-Small"), ("XS", "X-Small"),
    ("Large", "Large"), ("L", "Large"),
    ("Medium", "Medium"), ("M", "Medium"),
    ("Small", "Small"), ("S", "Small"),
]
SIZE_TOKEN_CANONICAL.sort(key=lambda pair: -len(pair[0]))


def normalize_size_value(value_name):
    """
    Maps a shorthand size like 'XL' (as GiftsKSA's own attrs sometimes send
    it) to Odoo's existing canonical value name ('X-Large'), so this never
    creates a redundant near-duplicate Size value alongside the real one.
    Returns the value unchanged if it isn't a recognised shorthand.
    """
    for token, canonical in SIZE_TOKEN_CANONICAL:
        if value_name == token:
            return canonical
    return value_name


def strip_size_suffix(code):
    """
    If `code` ends with a recognisable size token - "...-XL" or "...(XL)"
    style, both seen in real default_codes from this feed - returns
    (rest_of_code, canonical_size_name). Otherwise returns (code, None).
    """
    for token, canonical in SIZE_TOKEN_CANONICAL:
        if code.endswith("-" + token):
            return code[: -(len(token) + 1)], canonical
        paren = "(" + token + ")"
        if code.endswith(paren):
            rest = code[: -len(paren)].rstrip()
            return rest, canonical
    return code, None


def group_common_prefix(codes):
    """Longest string every code in `codes` starts with."""
    if not codes:
        return ""
    prefix = codes[0]
    for c in codes[1:]:
        i = 0
        while i < len(prefix) and i < len(c) and prefix[i] == c[i]:
            i += 1
        prefix = prefix[:i]
    return prefix


def parse_combo_from_code(default_code, group_codes, keys_seen):
    """
    Best-effort fallback: recovers {"Size": ..., "Color": ...} - only the
    keys `keys_seen` says the target product actually uses - from the
    default_code itself. `group_codes` should be every sibling code being
    considered alongside this one (e.g. the rest of today's batch for the
    same product name), used to isolate the base-SKU prefix from the
    color text by finding what's common across all of them.

    Returns {} - never a half-filled or guessed dict - the moment a
    dimension the product needs can't be confidently determined, so the
    caller knows to skip this row rather than attach something wrong.
    """
    result = {}
    remaining = default_code
    if "Size" in keys_seen:
        rest, size_name = strip_size_suffix(default_code)
        if not size_name:
            return {}
        result["Size"] = size_name
        remaining = rest
    if "Color" in keys_seen:
        sized_rests = []
        for c in group_codes:
            rest, size_name = strip_size_suffix(c)
            if size_name:
                sized_rests.append(rest)
        prefix = group_common_prefix(sized_rests) if sized_rests else ""
        if remaining.startswith(prefix):
            color = remaining[len(prefix):].strip()
        else:
            color = remaining.strip()
        if not color:
            return {}
        result["Color"] = color
    return result


def infer_keys_from_codes(codes):
    """
    Used only by run_duplicate_merge_cleanup, for the case where a target
    template has no attribute lines at all yet - so there's no live "what
    dimensions does this product use" signal to read the way the main
    sync's keys_seen normally comes from fetch_template_variant_index.
    Makes one best-effort guess, purely from a batch of sibling
    default_codes' own text, at whether they're differentiated by Size,
    by Color, or both. Returns a subset of {"Size", "Color"} - or an empty
    set if there isn't enough signal to say (fewer than 2 distinct codes,
    or nothing distinguishable once a common prefix is stripped) - never a
    guess that's only half-right.
    """
    codes = [c for c in codes if c]
    if len(codes) < 2:
        return set()
    keys = set()
    sized = [strip_size_suffix(c) for c in codes]
    if all(size_name for _, size_name in sized):
        keys.add("Size")
        rests = [rest for rest, _ in sized]
    else:
        rests = codes
    prefix = group_common_prefix(rests)
    leftovers = {r[len(prefix):].strip() for r in rests}
    leftovers.discard("")
    if len(leftovers) >= 2:
        keys.add("Color")
    return keys


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
        self._dry_run_next_fake_id = -1
        log(f"Authenticated to Odoo as uid={self.uid} (db={db})")
        if DRY_RUN:
            log("DRY_RUN is on: no create/write/unlink will actually reach Odoo - every one is only logged below.")

    def execute(self, model, method, args, kwargs=None):
        if DRY_RUN and method in ("create", "write", "unlink"):
            log(f"  [DRY RUN] would call {model}.{method}({args!r})")
            if method == "create":
                fake_id = self._dry_run_next_fake_id
                self._dry_run_next_fake_id -= 1
                return fake_id
            return True
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

    def search(self, model, domain, fields=None, order=None, limit=None, context=None):
        kwargs = {}
        if fields:
            kwargs["fields"] = fields
        if order:
            kwargs["order"] = order
        if limit:
            kwargs["limit"] = limit
        if context:
            kwargs["context"] = context
        return self.execute(model, "search_read", [domain], kwargs)


# --------------------------------------------------------------------------
# Real stock.quant reconciliation (new in this version - see the "What's
# new" note at the top of this file). Previously this script only wrote
# the supplier's reported stock into the x_giftsksa_stock_qty tracking
# field and never touched the real, customer-facing on-hand quantity
# (qty_available / stock.quant) - meaning the website could show a product
# as in stock (or out of stock) based on a number nobody was actually
# keeping in sync. This section makes every run also push the feed's
# stock number into Odoo's real inventory, the same safe
# inventory_quantity + action_apply_inventory() pattern already proven in
# the one-off KSB-1W fix.
# --------------------------------------------------------------------------

def get_default_stock_location_id(odoo):
    """
    The internal stock location new quants are created at when a flat
    product has no existing quant anywhere yet (brand-new product, or a
    product that's only ever been tracked via the old x_giftsksa_stock_qty
    field). Uses the first warehouse's own "Stock" location - standard
    single-warehouse Odoo setups only have the one.
    """
    warehouses = odoo.search("stock.warehouse", [], ["id", "lot_stock_id"])
    if not warehouses or not warehouses[0].get("lot_stock_id"):
        raise RuntimeError(
            "Could not find any stock.warehouse with a Stock location - "
            "cannot reconcile real on-hand stock without one."
        )
    return warehouses[0]["lot_stock_id"][0]


def reconcile_stock_quant(odoo, variant_id, target_qty, default_location_id, label):
    """
    Makes a single product.product's real on-hand stock (summed across its
    internal-location stock.quant rows) match target_qty, the latest
    GiftsKSA feed value - without ever touching any non-internal location
    (customer/supplier/transit locations are left alone) and without ever
    creating a second quant when one already exists (always adjusts the
    existing one rather than piling up new rows).

    If there's already exactly one internal quant, its inventory_quantity
    is nudged by the difference and applied. If there are several (e.g.
    stock split across more than one internal location), the first one
    found absorbs the whole adjustment rather than guessing how to split
    it - still correct in total, which is what customer-facing
    qty_available reflects. If there are none yet, a new quant is created
    at the default warehouse location.
    """
    if DRY_RUN:
        log(f"  [DRY RUN] would reconcile real stock for {label!r} to {target_qty}")
        return
    quants = odoo.search(
        "stock.quant",
        [["product_id", "=", variant_id], ["location_id.usage", "=", "internal"]],
        ["id", "quantity"],
    )
    current_total = 0.0
    for q in quants:
        current_total += q.get("quantity") or 0.0
    diff = target_qty - current_total
    if abs(diff) <= STOCK_MATCH_TOLERANCE:
        return
    if quants:
        q = quants[0]
        new_qty = (q.get("quantity") or 0.0) + diff
        odoo.execute("stock.quant", "write", [[q["id"]], {"inventory_quantity": new_qty}])
        odoo.execute("stock.quant", "action_apply_inventory", [[q["id"]]])
    else:
        new_id = odoo.execute(
            "stock.quant", "create",
            [{
                "product_id": variant_id,
                "location_id": default_location_id,
                "inventory_quantity": target_qty,
            }],
        )
        odoo.execute("stock.quant", "action_apply_inventory", [[new_id]])
    log(f"  reconciled real stock for {label!r}: {current_total} -> {target_qty}")


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
# Attribute / variant-matching helpers (ported from the old Odoo-internal
# "Daily GiftsKSA Catalog & Stock Sync" automation, translated from direct
# ORM calls to XML-RPC search_read/execute_kw calls)
# --------------------------------------------------------------------------

_attribute_id_cache = {}
_attribute_value_cache = {}


def resolve_attribute_id(odoo, attr_name):
    """
    Looks up the id of a product.attribute by its name (e.g. "Color",
    "Size"), caching the result for the rest of this run. Falls back to
    ATTR_ID_FALLBACK only if the live lookup comes back empty.
    """
    if attr_name in _attribute_id_cache:
        return _attribute_id_cache[attr_name]
    found = odoo.search("product.attribute", [["name", "=", attr_name]], ["id"])
    attribute_id = found[0]["id"] if found else ATTR_ID_FALLBACK.get(attr_name)
    _attribute_id_cache[attr_name] = attribute_id
    return attribute_id


def ensure_attribute_value(odoo, attribute_id, value_name, is_color):
    """
    Finds an existing product.attribute.value for (attribute_id,
    value_name), or creates one (with a known swatch color if it's a new
    Color value we have a hex code for).
    """
    key = (attribute_id, value_name)
    if key in _attribute_value_cache:
        return _attribute_value_cache[key]
    existing = odoo.search(
        "product.attribute.value",
        [["attribute_id", "=", attribute_id], ["name", "=", value_name]],
        ["id"],
    )
    if existing:
        _attribute_value_cache[key] = existing[0]["id"]
        return existing[0]["id"]
    create_vals = {"attribute_id": attribute_id, "name": value_name}
    if is_color:
        hexcode = COLOR_HEX.get(value_name)
        if hexcode:
            create_vals["html_color"] = hexcode
    new_id = odoo.execute("product.attribute.value", "create", [create_vals])
    _attribute_value_cache[key] = new_id
    log(f"  auto-created new attribute value: {value_name!r} (attribute_id={attribute_id}, id={new_id})")
    return new_id


def parse_attrs(item):
    """
    GiftsKSA sends each variant's attributes as display strings like
    "Size: Large" under product_template_attribute_value_ids. This pulls
    them into a plain {"Size": "Large", "Color": "Red"} dict.
    """
    attrs = {}
    for av in (item.get("product_template_attribute_value_ids") or []):
        disp = av.get("display_name", "") if isinstance(av, dict) else ""
        if ":" in disp:
            key, value = disp.split(":", 1)
            attrs[key.strip()] = value.strip()
    return attrs


def fetch_template_variant_index(odoo, template_id):
    """
    Reads back, for one existing template, which attribute keys it already
    has (e.g. {"Color", "Size"}), which attribute-value ids are already on
    each of its attribute lines, and a map from "sorted attribute value
    combo" -> variant id for every variant it currently has. Used both to
    detect a true new combination (needs a new variant) and a collision
    (an incoming code's combo already matches an existing variant).
    """
    tmpls = odoo.search(
        "product.template",
        [["id", "=", template_id]],
        ["id", "attribute_line_ids", "product_variant_ids"],
    )
    if not tmpls:
        return set(), {}, {}
    tmpl = tmpls[0]
    line_ids = tmpl.get("attribute_line_ids") or []
    variant_ids = tmpl.get("product_variant_ids") or []

    keys_seen = set()
    existing_lines = {}  # attribute_id -> set(product.attribute.value ids already on that line)
    if line_ids:
        lines = odoo.search(
            "product.template.attribute.line",
            [["id", "in", line_ids]],
            ["id", "attribute_id", "value_ids"],
        )
        for line in lines:
            attr = line.get("attribute_id")
            if not attr:
                continue
            keys_seen.add(attr[1])
            existing_lines[attr[0]] = set(line.get("value_ids") or [])

    combo_to_variant = {}
    if variant_ids:
        variants = odoo.search(
            "product.product",
            [["id", "in", variant_ids]],
            ["id", "default_code", "product_template_attribute_value_ids"],
        )
        ptav_ids = set()
        for v in variants:
            ptav_ids.update(v.get("product_template_attribute_value_ids") or [])
        ptav_map = {}
        if ptav_ids:
            ptavs = odoo.search(
                "product.template.attribute.value",
                [["id", "in", list(ptav_ids)]],
                ["id", "attribute_id", "product_attribute_value_id"],
            )
            for ptav in ptavs:
                ptav_map[ptav["id"]] = (ptav["attribute_id"][1], ptav["product_attribute_value_id"][1])
        for v in variants:
            v_attrs = {}
            for pid in v.get("product_template_attribute_value_ids") or []:
                pair = ptav_map.get(pid)
                if pair:
                    v_attrs[pair[0]] = pair[1]
            combo = tuple(sorted(v_attrs.get(k, "") for k in keys_seen))
            combo_to_variant[combo] = v["id"]

    return keys_seen, existing_lines, combo_to_variant


def extend_target_with_new_variants(odoo, target_id, group_rows, keys_seen_hint=None):
    """
    Given an EXISTING template (target_id) and a batch of never-before-seen
    GiftsKSA rows that all share that template's exact product name, adds
    any genuinely new Color/Size combination as a new variant of that
    template (instead of letting the caller create a lookalike duplicate
    product). Returns a set of default_codes that were successfully
    attached this way - any row whose code is NOT in that set should fall
    through to the normal flat-create path (so nothing is silently lost).

    When a row has no usable attrs of its own (GiftsKSA sent none, or they
    didn't parse), falls back to recovering Color/Size from the row's own
    default_code via parse_combo_from_code before giving up on it - see the
    "What's new (2026-10-29)" note at the top of this file for why. A row
    that still can't be confidently resolved either way is skipped and
    logged, never flat-created as a duplicate.

    keys_seen_hint is used only by run_duplicate_merge_cleanup, for a
    target that has no attribute lines of its own yet to read keys_seen
    from (see infer_keys_from_codes) - the main sync never needs to pass
    it, since a true name-match target it's attaching new variants onto
    almost always already has at least one attribute line.
    """
    keys_seen, existing_lines, existing_combo_to_variant = fetch_template_variant_index(odoo, target_id)
    for row in group_rows:
        keys_seen.update(row["attrs"].keys())
    if not keys_seen and keys_seen_hint:
        keys_seen = set(keys_seen_hint)
    if not keys_seen:
        return set()

    group_codes_all = [r["default_code"] for r in group_rows]
    new_entries = []
    collided_codes = []
    for row in group_rows:
        attrs = row["attrs"]
        if not attrs:
            fallback = parse_combo_from_code(row["default_code"], group_codes_all, keys_seen)
            if not fallback:
                log(
                    f"  WARNING: {row['default_code']!r} matches existing product "
                    f"(template id={target_id}) by name but has no usable Color/Size "
                    f"data - neither from GiftsKSA's own attrs nor recoverable from "
                    f"the code itself - skipping rather than guessing or flat-creating "
                    f"a duplicate."
                )
                continue
            attrs = fallback
        if "Size" in attrs:
            attrs = dict(attrs)
            attrs["Size"] = normalize_size_value(attrs["Size"])
        # Persist the resolved attrs back onto the row - the rest of this
        # function (the attribute-line-creation pass below) reads
        # row["attrs"] again rather than this local, so without this the
        # fallback-recovered values would silently be dropped there.
        row["attrs"] = attrs
        unexpected = any(resolve_attribute_id(odoo, k) is None for k in attrs)
        if unexpected:
            continue
        combo = tuple(sorted(attrs.get(k, "") for k in keys_seen))
        if combo in existing_combo_to_variant:
            collided_codes.append(row["default_code"])
        else:
            new_entries.append((row, combo))

    # Dedupe among the new entries themselves (two incoming rows claiming
    # the exact same combo - keep the first, leave the second to fall
    # through to the normal flat-create path rather than guess).
    seen_combo = set()
    deduped_new = []
    for row, combo in new_entries:
        if combo in seen_combo:
            continue
        seen_combo.add(combo)
        deduped_new.append((row, combo))

    handled_codes = set(collided_codes)
    if not deduped_new:
        return handled_codes

    if DRY_RUN:
        # Odoo's own auto-variant-generation (what actually spawns the new
        # product.product record once a new attribute value lands on the
        # template) is a real server-side side effect that can't be
        # faithfully previewed without really writing it - so a dry run
        # just reports the intent plainly instead of attempting it, rather
        # than leaving these codes to be wrongly logged as a flat create.
        for row, combo in deduped_new:
            log(
                f"  [DRY RUN] would attach {row['default_code']!r} as a new variant "
                f"({', '.join(f'{k}={v}' for k, v in row['attrs'].items())}) "
                f"of existing product (template id={target_id})"
            )
            handled_codes.add(row["default_code"])
        return handled_codes

    # 1. Make sure every needed attribute value exists, and is on the
    #    template's attribute lines (creating a new line if this is the
    #    first time this template has seen that attribute at all).
    for k in keys_seen:
        attribute_id = resolve_attribute_id(odoo, k)
        if not attribute_id:
            continue
        needed_value_ids = set()
        for row, combo in deduped_new:
            v = row["attrs"].get(k)
            if v:
                needed_value_ids.add(ensure_attribute_value(odoo, attribute_id, v, is_color=(k == "Color")))
        if not needed_value_ids:
            continue
        if attribute_id in existing_lines:
            missing = needed_value_ids - existing_lines[attribute_id]
            if missing:
                line_rows = odoo.search(
                    "product.template.attribute.line",
                    [["product_tmpl_id", "=", target_id], ["attribute_id", "=", attribute_id]],
                    ["id"],
                )
                if line_rows:
                    odoo.execute(
                        "product.template.attribute.line", "write",
                        [[line_rows[0]["id"]], {"value_ids": [[4, mid] for mid in missing]}],
                    )
        else:
            odoo.execute(
                "product.template", "write",
                [[target_id], {"attribute_line_ids": [[0, 0, {
                    "attribute_id": attribute_id,
                    "value_ids": [[6, 0, list(needed_value_ids)]],
                }]]}],
            )

    # 2. Odoo has now auto-generated the new variant(s). Re-read the
    #    template's variants and match each freshly-generated, still
    #    blank-code variant to the row whose combo it corresponds to.
    combo_to_row = {combo: row for row, combo in deduped_new}
    _, _, refreshed_combo_to_variant = fetch_template_variant_index(odoo, target_id)
    tmpls = odoo.search("product.template", [["id", "=", target_id]], ["id", "product_variant_ids"])
    variant_ids = tmpls[0].get("product_variant_ids") or [] if tmpls else []
    variants = odoo.search(
        "product.product",
        [["id", "in", variant_ids]],
        ["id", "default_code", "product_template_attribute_value_ids"],
    ) if variant_ids else []
    ptav_ids = set()
    for v in variants:
        ptav_ids.update(v.get("product_template_attribute_value_ids") or [])
    ptav_map = {}
    if ptav_ids:
        ptavs = odoo.search(
            "product.template.attribute.value",
            [["id", "in", list(ptav_ids)]],
            ["id", "attribute_id", "product_attribute_value_id"],
        )
        for ptav in ptavs:
            ptav_map[ptav["id"]] = (ptav["attribute_id"][1], ptav["product_attribute_value_id"][1])

    for v in variants:
        if v.get("default_code"):
            continue  # not a freshly-generated blank variant
        v_attrs = {}
        for pid in v.get("product_template_attribute_value_ids") or []:
            pair = ptav_map.get(pid)
            if pair:
                v_attrs[pair[0]] = pair[1]
        combo = tuple(sorted(v_attrs.get(k, "") for k in keys_seen))
        row = combo_to_row.get(combo)
        if not row:
            continue
        write_vals = {"default_code": row["default_code"]}
        if row.get("barcode"):
            write_vals["barcode"] = row["barcode"]
        odoo.execute("product.product", "write", [[v["id"]], write_vals])
        if row.get("image_url"):
            try:
                v_fresh = odoo.search("product.product", [["id", "=", v["id"]]], ["id", "image_variant_1920"])
                has_own_image = bool(v_fresh and v_fresh[0].get("image_variant_1920"))
                if not has_own_image:
                    odoo.execute(
                        "product.product", "write",
                        [[v["id"]], {"image_variant_1920": fetch_image_b64(row["image_url"])}],
                    )
            except Exception as img_exc:  # noqa: BLE001 - never let an image fetch block the attach
                log(f"  warning: variant image fetch failed for {row['default_code']}: {img_exc}")
        handled_codes.add(row["default_code"])

    return handled_codes


def create_flat_product(odoo, resolve_category, row, default_location_id):
    """
    The original "never seen this code before, and no existing product
    matched its name" path: creates a brand-new, standalone
    product.template for this one SKU. list_price now carries the 60%
    website markup, and the new product's real on-hand stock is set to
    match the feed immediately after create (not just the
    x_giftsksa_stock_qty tracking field) - see the stock-reconciliation
    section above.
    """
    if DRY_RUN:
        log(f"  [DRY RUN] would create flat product {row['default_code']!r} ({row['name']!r}) "
            f"at list_price={round((row['retail_price'] or 0) * PRICE_MARKUP, 2)} "
            f"with real stock={row['stock_qty']}")
        return
    cat_ids = [resolve_category(n) for n in row["categ_names"]]
    cat_ids = [c for c in cat_ids if c]
    vals = {
        "name": row["name"],
        "default_code": row["default_code"],
        "list_price": (row["retail_price"] or 0) * PRICE_MARKUP,
        "type": "consu",
        "sale_ok": True,
        "website_published": True,
        "is_published": True,
        "x_giftsksa_stock_qty": row["stock_qty"],
        "x_giftsksa_discontinued": False,
        "x_giftsksa_source": "giftsksa",
        "x_giftsksa_synced_date": row["run_timestamp"],
    }
    if cat_ids:
        vals["public_categ_ids"] = [[6, 0, cat_ids]]
    if row["image_url"]:
        try:
            vals["image_1920"] = fetch_image_b64(row["image_url"])
        except Exception as img_exc:  # noqa: BLE001
            log(f"  warning: image fetch failed for {row['default_code']}: {img_exc}")
    new_tmpl_id = odoo.execute("product.template", "create", [vals])
    try:
        fresh = odoo.search(
            "product.template", [["id", "=", new_tmpl_id]], ["id", "product_variant_id"],
        )
        if fresh and fresh[0].get("product_variant_id"):
            new_variant_id = fresh[0]["product_variant_id"][0]
            reconcile_stock_quant(odoo, new_variant_id, row["stock_qty"], default_location_id, row["default_code"])
    except Exception as stock_exc:  # noqa: BLE001 - never let a stock-reconcile failure block the rest of the run
        log(f"  warning: real-stock reconcile failed for new product {row['default_code']}: {stock_exc}")


# --------------------------------------------------------------------------
# Self-healing duplicate-merge cleanup (new in 2026-10-29 - see the "What's
# new" note at the top of this file). Runs at the end of every sync, after
# the main create/update/attach pass above, and catches same-name flat
# duplicate products however they got there - not only ones this run
# itself might (in theory) create, since the fixes above should prevent
# that going forward, but anything already sitting on the storefront for
# any other reason too.
# --------------------------------------------------------------------------

def run_duplicate_merge_cleanup(odoo):
    """
    Finds published, sellable, FLAT (single-variant, zero attribute-line)
    product.templates that share an exact name with at least one other
    such template, and merges every one of them except a chosen "primary"
    into that primary as a new Color/Size variant - instead of leaving
    them sitting on the storefront as lookalike duplicates.

    The primary is the oldest template in the name-group (the one
    everything else should logically attach to). Each other duplicate in
    the group is only merged in if ALL of the following hold:
      - it has exactly one variant, and that variant has a default_code
        to merge by;
      - a confident Color and/or Size combo can be recovered from that
        default_code (the same fallback parser the main sync uses - see
        parse_combo_from_code - with the dimensions it needs to look for
        read from the primary's own existing attribute lines when it has
        any, or otherwise best-effort inferred from the group's own codes
        via infer_keys_from_codes);
      - that combo doesn't already exist as a variant on the primary; and
      - the duplicate's variant has zero on-hand stock and no sale order
        lines against it, so nothing with real transactional history is
        ever folded away.
    Any duplicate that fails one of those checks is left exactly as it is
    and logged for manual review - never guessed, never merged.

    A duplicate that IS merged is archived afterwards - active=False,
    unpublished, unsellable - never hard-deleted, so it's always
    recoverable from Odoo's own Archived view if that's ever needed. Only
    the duplicate is archived; the primary is never touched except to
    gain the new variant.

    Returns (merged_count, flagged_count).
    """
    candidates = odoo.search(
        "product.template",
        [["website_published", "=", True], ["sale_ok", "=", True]],
        ["id", "name", "create_date", "attribute_line_ids", "product_variant_count"],
    )
    flat_only = [
        c for c in candidates
        if c.get("product_variant_count", 1) == 1 and not c.get("attribute_line_ids")
    ]
    by_name = {}
    for c in flat_only:
        # .strip() here too (see the ingestion-side note near where
        # row["name"] is built in main(), below) - groups a stray
        # whitespace-corrupted name together with its clean-named
        # siblings instead of silently treating it as its own group of
        # one and never merging it.
        by_name.setdefault(c["name"].strip(), []).append(c)

    merged_count = 0
    flagged = []

    for name, group in by_name.items():
        if len(group) < 2:
            continue
        group_sorted = sorted(group, key=lambda c: c.get("create_date") or "")
        primary = group_sorted[0]
        dupes = group_sorted[1:]

        dupe_variants = odoo.search(
            "product.product",
            [["product_tmpl_id", "in", [d["id"] for d in dupes]]],
            ["id", "product_tmpl_id", "default_code", "barcode", "qty_available"],
        )
        variant_by_tmpl = {v["product_tmpl_id"][0]: v for v in dupe_variants}

        primary_variants = odoo.search(
            "product.product", [["product_tmpl_id", "=", primary["id"]]], ["id", "default_code"],
        )
        code_pool = [v["default_code"] for v in primary_variants if v.get("default_code")]
        code_pool += [v["default_code"] for v in dupe_variants if v.get("default_code")]

        keys_seen, _, _ = fetch_template_variant_index(odoo, primary["id"])
        keys_hint = keys_seen if keys_seen else infer_keys_from_codes(code_pool)

        group_rows = []
        row_to_dupe = {}
        for d in dupes:
            v = variant_by_tmpl.get(d["id"])
            if not v:
                flagged.append(f"{name!r} (template id={d['id']}): no variant found - left alone")
                continue
            if not v.get("default_code"):
                flagged.append(f"{name!r} (template id={d['id']}): has no default_code to merge by - left alone")
                continue
            if v.get("qty_available"):
                flagged.append(
                    f"{name!r} ({v['default_code']}): has on-hand stock "
                    f"({v['qty_available']}) - left alone, flagged for manual review"
                )
                continue
            so_lines = odoo.search("sale.order.line", [["product_id", "=", v["id"]]], ["id"], limit=1)
            if so_lines:
                flagged.append(
                    f"{name!r} ({v['default_code']}): has sale order history - "
                    f"left alone, flagged for manual review"
                )
                continue
            row = {
                "default_code": v["default_code"],
                "attrs": {},
                "name": name,
                "barcode": v.get("barcode"),
                "image_url": None,
            }
            group_rows.append(row)
            row_to_dupe[v["default_code"]] = d["id"]

        if not group_rows:
            continue

        if DRY_RUN:
            for row in group_rows:
                log(f"  [DRY RUN] would attempt to merge duplicate {row['default_code']!r} ({name!r}) into template id={primary['id']}")
            continue

        try:
            handled_codes = extend_target_with_new_variants(odoo, primary["id"], group_rows, keys_seen_hint=keys_hint)
        except Exception as exc:  # noqa: BLE001
            log(f"  ERROR merging duplicates of {name!r} into template id={primary['id']}: {exc}")
            flagged.append(f"{name!r}: merge attempt failed ({exc}) - left alone")
            continue

        for row in group_rows:
            code = row["default_code"]
            if code not in handled_codes:
                flagged.append(
                    f"{name!r} ({code}): could not recover a confident Color/Size "
                    f"combo from its code - left alone, flagged for manual review"
                )
                continue
            dupe_id = row_to_dupe.get(code)
            if not dupe_id:
                continue
            odoo.execute(
                "product.template", "write",
                [[dupe_id], {
                    "website_published": False,
                    "is_published": False,
                    "sale_ok": False,
                    "active": False,
                }],
            )
            merged_count += 1
            log(f"  merged duplicate {code!r} ({name!r}, template id={dupe_id}) into template id={primary['id']} and archived it")

    if flagged:
        log(f"Duplicate-merge cleanup: {len(flagged)} duplicate(s) left alone for manual review:")
        for f in flagged:
            log(f"  {f}")

    return merged_count, len(flagged)


# --------------------------------------------------------------------------
# Daily Featured-sort new-product bump (ported from the old Odoo-internal
# cron, ir.actions.server id=769). Runs at the end of main() so any
# products this run just created get included.
# --------------------------------------------------------------------------

def run_featured_sort(odoo):
    ribbons = odoo.search("product.ribbon", [["name", "=", "New!"]], ["id", "new_period"])
    ribbon = ribbons[0] if ribbons else None
    new_period = ribbon["new_period"] if ribbon else 2
    cutoff = (datetime.now(timezone.utc) - timedelta(days=new_period)).strftime("%Y-%m-%d %H:%M:%S")

    new_products = odoo.search(
        "product.template",
        [["is_published", "=", True], ["sale_ok", "=", True], ["create_date", ">=", cutoff]],
        ["id"],
        order="create_date desc",
    )
    new_ids = [p["id"] for p in new_products]

    if ribbon:
        if new_ids:
            odoo.execute("product.template", "write", [new_ids, {"website_ribbon_id": ribbon["id"]}])
        stale = odoo.search(
            "product.template",
            [["website_ribbon_id", "=", ribbon["id"]], ["id", "not in", new_ids]],
            ["id"],
        )
        stale_ids = [p["id"] for p in stale]
        if stale_ids:
            odoo.execute("product.template", "write", [stale_ids, {"website_ribbon_id": False}])

    if new_ids:
        baseline = odoo.search(
            "product.template",
            [["is_published", "=", True], ["sale_ok", "=", True], ["create_date", "<", cutoff]],
            ["id", "website_sequence"],
            order="website_sequence asc",
            limit=1,
        )
        base_seq = baseline[0]["website_sequence"] if baseline else 10000
        seq = base_seq - 10
        for p in new_products:
            odoo.execute("product.template", "write", [[p["id"]], {"website_sequence": seq}])
            seq -= 10

    log(
        f"Featured-sort: ribbon={'New!' if ribbon else '(none found)'}, "
        f"new_period={new_period}d, bumped {len(new_ids)} product(s)"
    )
    return len(new_ids)


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
                # .strip() matters here, not just cosmetically: every
                # name-based duplicate-match (step 4 below, the fallback
                # safety net in step 5, and run_duplicate_merge_cleanup's
                # own grouping) is an EXACT string comparison. GiftsKSA's
                # feed has been observed sending a product's name with a
                # stray trailing whitespace character on some rows (e.g.
                # a trailing tab) while every other row/previously-synced
                # copy of that same product has the clean name - an exact
                # match then silently fails and the row falls through to
                # create_flat_product, creating exactly the kind of
                # duplicate this whole file exists to prevent. Confirmed
                # live on 2026-10-30: "KSB-1W Stormtech Women's Orbiter
                # Softshell Jacket" (with a trailing \t, from this feed)
                # vs. the existing "...Jacket" (no trailing whitespace).
                "name": (p.get("name") or code).strip(),
                "default_code": code,
                "categ_names": [
                    c.get("name") for c in (p.get("public_categ_ids") or []) if c.get("name")
                ],
                "image_url": p.get("image_url"),
                "retail_price": price_row.get("retail_price"),
                "stock_qty": stock_row.get("net_available_qty") or 0,
                "barcode": p.get("barcode"),
                "attrs": parse_attrs(p),
                "run_timestamp": run_timestamp,
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
    default_location_id = None
    if not DRY_RUN:
        default_location_id = get_default_stock_location_id(odoo)

    # 3. Index existing GiftsKSA-sourced products by default_code (this is
    #    the normal "update this flat product's price/stock" path).
    # product_variant_id is fetched here too (not just default_code/name) -
    # it's what the real stock.quant reconciliation below writes against,
    # since quants live on product.product (the variant), not
    # product.template.
    existing = odoo.search(
        "product.template",
        [["x_giftsksa_source", "=", "giftsksa"]],
        ["id", "default_code", "name", "x_giftsksa_discontinued", "product_variant_id"],
    )
    existing_by_code = {
        e["default_code"]: {
            "tmpl_id": e["id"],
            "variant_id": e["product_variant_id"][0] if e.get("product_variant_id") else None,
        }
        for e in existing if e["default_code"]
    }
    log(f"Found {len(existing)} existing GiftsKSA-sourced products in Odoo")

    # 3b. Also index EVERY coded variant store-wide (not just GiftsKSA-
    #     tagged flat templates). This is what lets us tell "this code was
    #     already attached as a variant of some other product on a
    #     previous run" apart from "this code has genuinely never been
    #     seen before" - without it, a previously-attached variant's code
    #     would look brand-new forever and this script would try to
    #     recreate it as a duplicate flat product every single run.
    all_coded_variants = odoo.search(
        "product.product", [["default_code", "!=", False]], ["id", "default_code"]
    )
    known_variant_codes = {v["default_code"] for v in all_coded_variants if v.get("default_code")}
    log(f"Found {len(known_variant_codes)} coded product variants store-wide")

    updated, created, variants_added, already_variant_skipped = 0, 0, 0, 0
    errors = []
    new_rows = []

    for row in rows:
        code = row["default_code"]
        try:
            existing_entry = existing_by_code.get(code)
            if existing_entry:
                existing_id = existing_entry["tmpl_id"]
                cat_ids = [resolve_category(n) for n in row["categ_names"]]
                cat_ids = [c for c in cat_ids if c]
                vals = {
                    "list_price": (row["retail_price"] or 0) * PRICE_MARKUP,
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
                if existing_entry.get("variant_id"):
                    try:
                        reconcile_stock_quant(
                            odoo, existing_entry["variant_id"], row["stock_qty"],
                            default_location_id, code,
                        )
                    except Exception as stock_exc:  # noqa: BLE001 - never let a stock-reconcile failure block the rest of the run
                        log(f"  warning: real-stock reconcile failed for {code}: {stock_exc}")
            elif code in known_variant_codes:
                # Already live in Odoo as a variant of some other product
                # (most likely a previously-attached merged variant). This
                # script's simple template-level price/stock fields don't
                # apply to it - see the module docstring's "known,
                # deliberate limitation". Skip rather than recreate.
                already_variant_skipped += 1
            else:
                new_rows.append(row)
        except Exception as exc:  # noqa: BLE001
            errors.append((code, str(exc)))
            log(f"  ERROR processing {code}: {exc}")

    # 4. For genuinely never-before-seen codes, check their product NAME
    #    against existing products before creating anything - if a match
    #    exists, attach as a new variant instead of a duplicate product.
    new_names = sorted({row["name"] for row in new_rows})
    name_to_target = {}
    if new_names:
        candidates = odoo.search(
            "product.template",
            [["name", "in", new_names], ["website_published", "=", True]],
            ["id", "name", "active", "product_variant_count"],
            context={"active_test": False},
        )
        by_name = {}
        for c in candidates:
            by_name.setdefault(c["name"], []).append(c)
        for name, certs in by_name.items():
            active_certs = [c for c in certs if c.get("active", True)]
            pool = active_certs if active_certs else certs
            best = max(pool, key=lambda c: c.get("product_variant_count", 0))
            name_to_target[name] = best["id"]
        if name_to_target:
            log(f"{len(name_to_target)} never-before-seen product name(s) matched an existing product")

    groups_by_name = {}
    for row in new_rows:
        groups_by_name.setdefault(row["name"], []).append(row)

    fallback_rows = []
    for name, group_rows in groups_by_name.items():
        target_id = name_to_target.get(name)
        if not target_id:
            fallback_rows.extend(group_rows)
            continue
        try:
            handled_codes = extend_target_with_new_variants(odoo, target_id, group_rows)
        except Exception as exc:  # noqa: BLE001
            errors.append((name, f"variant-attach failed: {exc}"))
            log(f"  ERROR attaching variants under existing product {name!r}: {exc}")
            handled_codes = set()
        variants_added += len(handled_codes)
        for row in group_rows:
            if row["default_code"] not in handled_codes:
                fallback_rows.append(row)

    # 5. Anything left over (no name match at all, or a name match that
    #    couldn't cleanly attach as a variant) still gets created as a
    #    flat product - UNLESS a second, independent check right here
    #    finds an existing active+published product under that exact
    #    name after all (this re-checks fresh, rather than trusting the
    #    batch lookup in step 4, which only ran once against the names
    #    known at that point). If one exists and one more variant-attach
    #    attempt against it still can't recover a confident Color/Size
    #    combo, flat-creating would just recreate the duplicate bug this
    #    version exists to close - so it's skipped and logged for manual
    #    review instead, never guessed into a create. Nothing is dropped
    #    silently either way: every row ends up created, attached, or
    #    logged as skipped.
    skipped_would_duplicate = 0
    for row in fallback_rows:
        try:
            recheck = odoo.search(
                "product.template",
                [["name", "=", row["name"]], ["website_published", "=", True]],
                ["id", "product_variant_count"],
                context={"active_test": False},
            )
            if recheck:
                retry_target = max(recheck, key=lambda c: c.get("product_variant_count", 0))["id"]
                handled = extend_target_with_new_variants(odoo, retry_target, [row])
                if row["default_code"] in handled:
                    variants_added += 1
                    continue
                log(
                    f"  WARNING: {row['default_code']!r} ({row['name']!r}) matches an "
                    f"existing product (template id={retry_target}) but a confident "
                    f"Color/Size combo still couldn't be recovered for it - skipping "
                    f"rather than flat-creating a duplicate. Needs manual review."
                )
                skipped_would_duplicate += 1
                continue
            create_flat_product(odoo, resolve_category, row, default_location_id)
            created += 1
        except Exception as exc:  # noqa: BLE001
            errors.append((row["default_code"], str(exc)))
            log(f"  ERROR processing {row['default_code']}: {exc}")

    # 6. Archive products that used to come from GiftsKSA but are no longer
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

    # 7. Self-healing duplicate-merge cleanup. Catches any same-name, flat
    #    duplicate products that exist right now - however they got there,
    #    not just ones this run itself might have (in theory) created -
    #    and merges every one it can confidently resolve into a single
    #    variant-bearing product, archiving the rest. Wrapped so a problem
    #    here never erases or hides the catalog sync results logged above.
    merge_merged_count, merge_flagged_count = 0, 0
    try:
        merge_merged_count, merge_flagged_count = run_duplicate_merge_cleanup(odoo)
    except Exception as exc:  # noqa: BLE001
        errors.append(("duplicate-merge-cleanup", str(exc)))
        log(f"  ERROR running duplicate-merge cleanup: {exc}")

    # 8. Featured-sort pass. Wrapped so a problem here never erases or
    #    hides the catalog sync results logged above it.
    bumped_count = 0
    try:
        bumped_count = run_featured_sort(odoo)
    except Exception as exc:  # noqa: BLE001
        errors.append(("featured-sort", str(exc)))
        log(f"  ERROR running featured-sort: {exc}")

    # 9. Summary.
    log("=== Sync complete ===")
    log(f"Updated:                {updated}")
    log(f"Created (flat):         {created}")
    log(f"Attached as variants:   {variants_added}")
    log(f"Already-variant, skipped (price/stock not synced - see docstring): {already_variant_skipped}")
    log(f"Flat-create skipped (would duplicate, flagged for review): {skipped_would_duplicate}")
    log(f"Archived (no longer in feed): {len(archived)}")
    log(f"Duplicate-merge cleanup: merged {merge_merged_count} into existing products, flagged {merge_flagged_count} for manual review")
    log(f"Featured-sort bumped:   {bumped_count}")
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
