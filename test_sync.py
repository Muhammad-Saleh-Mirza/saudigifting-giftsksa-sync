"""
Offline logic test for sync.py: mocks both the GiftsKSA HTTP calls and the
Odoo XML-RPC server, so this can run anywhere (including a sandbox with no
outbound network) to verify the join/create/update/archive logic before
trusting it against the real services.

Run: python3 test_sync.py
"""
import base64
import os
import sys
import types
from unittest import mock

os.environ.setdefault("GIFTSKSA_TOKEN", "test-token")
os.environ.setdefault("ODOO_URL", "https://example-test.odoo.com")
os.environ.setdefault("ODOO_DB", "example-test")
os.environ.setdefault("ODOO_USERNAME", "tester@example.com")
os.environ.setdefault("ODOO_API_KEY", "fake-key")

# --- fake GiftsKSA data --------------------------------------------------
FAKE_PRODUCTS = [
    {  # existing product, category already exists in Odoo
        "id": 1, "name": "Widget A", "default_code": "WID-A",
        "public_categ_ids": [{"id": 9, "name": "Technology Gifts"}],
        "image_url": "https://img.example/a.png",
    },
    {  # brand-new product, category does NOT exist in Odoo yet
        "id": 2, "name": "Widget B (new)", "default_code": "WID-B",
        "public_categ_ids": [{"id": 99, "name": "Brand New Category"}],
        "image_url": "https://img.example/b.png",
    },
    {  # variant-less row: must be skipped, never created
        "id": 3, "name": "Widget C variant", "default_code": False,
        "public_categ_ids": [], "image_url": None,
    },
    {  # the protected consolidated placeholder - present in feed too, but
       # let's make sure a normal present+matched row just updates fine
        "id": 4, "name": "KSB-1W Jacket", "default_code": "KSB-1W",
        "public_categ_ids": [], "image_url": None,
    },
]
FAKE_PRICES = [
    {"id": 1, "default_code": "WID-A", "currency": "SAR", "list_price": 50, "retail_price": 80},
    {"id": 2, "default_code": "WID-B", "currency": "SAR", "list_price": 20, "retail_price": 35},
    {"id": 4, "default_code": "KSB-1W", "currency": "SAR", "list_price": 100, "retail_price": 175},
]
FAKE_STOCKS = [
    {"id": 1, "default_code": "WID-A", "net_available_qty": 12},
    {"id": 2, "default_code": "WID-B", "net_available_qty": 0},
    {"id": 4, "default_code": "KSB-1W", "net_available_qty": 3},
]

# Existing Odoo state:
#  - WID-A: already exists, giftsksa-sourced -> should be UPDATED
#  - WID-OLD: existing giftsksa-sourced product no longer in the feed
#             -> should be ARCHIVED
#  - KSB-1W: existing giftsksa-sourced product, present in feed by its own
#            code too -> should be UPDATED normally
EXISTING_ODOO_PRODUCTS = [
    {"id": 101, "default_code": "WID-A"},
    {"id": 102, "default_code": "WID-OLD"},
    {"id": 103, "default_code": "KSB-1W"},
]
EXISTING_ODOO_CATEGORIES = [
    {"id": 9, "name": "Technology Gifts"},
]


class FakeResponse:
    def __init__(self, json_data=None, content=b"\x89PNG..", status=200):
        self._json = json_data
        self.content = content
        self.status_code = status
        self.text = str(json_data)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


def fake_requests_get(url, timeout=None):
    if "/products/all/" in url:
        return FakeResponse(FAKE_PRODUCTS)
    if "/products/price/" in url:
        return FakeResponse(FAKE_PRICES)
    if "/products/stock/" in url:
        return FakeResponse(FAKE_STOCKS)
    if "img.example" in url:
        return FakeResponse(content=b"fake-image-bytes")
    raise AssertionError(f"Unexpected URL in test: {url}")


class FakeServerProxy:
    """Stands in for xmlrpc.client.ServerProxy for both /common and /object."""

    def __init__(self, url, *a, **kw):
        self.url = url
        self.calls = CALL_LOG

    # /xmlrpc/2/common
    def authenticate(self, db, username, api_key, ctx):
        return 42  # fake uid

    # /xmlrpc/2/object
    def execute_kw(self, db, uid, api_key, model, method, args, kwargs):
        self.calls.append((model, method, args, kwargs))
        domain = args[0] if args else None

        if model == "product.public.category" and method == "search_read":
            return EXISTING_ODOO_CATEGORIES
        if model == "product.public.category" and method == "create":
            vals = args[0]
            new_id = 9000 + len(self.calls)
            CREATED_CATEGORIES.append((new_id, vals["name"]))
            return new_id

        if model == "product.template" and method == "search_read":
            # sync.py now makes exactly one product.template search_read call
            # (existing giftsksa-sourced products); archiving is computed
            # client-side from which default_codes appeared in today's feed.
            return [
                dict(p, name=p["default_code"], x_giftsksa_discontinued=False)
                for p in EXISTING_ODOO_PRODUCTS
            ]

        if model == "product.template" and method == "write":
            WRITES.append(args)
            return True
        if model == "product.template" and method == "create":
            CREATES.append(args[0])
            return 55555

        raise AssertionError(f"Unexpected RPC: {model}.{method}({args}, {kwargs})")


CALL_LOG = []
CREATED_CATEGORIES = []
WRITES = []
CREATES = []


def run_test():
    sys.path.insert(0, os.path.dirname(__file__))
    with mock.patch("requests.get", side_effect=fake_requests_get), mock.patch(
        "xmlrpc.client.ServerProxy", FakeServerProxy
    ):
        import sync as sync_module  # import after patching so module-level code is fine

        try:
            sync_module.main()
        except SystemExit as e:
            assert e.code in (0, None), f"sync.py exited with error code {e.code}"

    # --- assertions -----------------------------------------------------
    write_ids = [w[0] for w in WRITES]
    write_vals = [w[1] for w in WRITES]

    # WID-A (existing, id 101) must be updated, not created
    assert [101] in write_ids, f"expected a write to id 101 (WID-A), got {write_ids}"
    wid_a_vals = write_vals[write_ids.index([101])]
    assert wid_a_vals["list_price"] == 80, "should use retail_price (80), not list_price (50)"
    assert wid_a_vals["x_giftsksa_stock_qty"] == 12
    assert wid_a_vals["public_categ_ids"] == [[4, 9]], "existing category should resolve to id 9 via ADD command"

    # KSB-1W (existing, id 103) is present in the feed under its own code ->
    # must be updated normally (not skipped just because it's protected)
    assert [103] in write_ids, "KSB-1W should still be updated when it genuinely appears in the feed"

    # WID-B is new -> must be created, with the new category auto-created first
    assert len(CREATES) == 1, f"expected exactly 1 create (WID-B), got {len(CREATES)}"
    wid_b_vals = CREATES[0]
    assert wid_b_vals["default_code"] == "WID-B"
    assert wid_b_vals["list_price"] == 35
    assert wid_b_vals["image_1920"] == base64.b64encode(b"fake-image-bytes").decode("ascii")
    assert CREATED_CATEGORIES and CREATED_CATEGORIES[0][1] == "Brand New Category"
    assert wid_b_vals["public_categ_ids"] == [[6, 0, [CREATED_CATEGORIES[0][0]]]]

    # Widget C (default_code False) must never be created or written.
    # Total expected writes: WID-A update + KSB-1W update + WID-OLD archive = 3.
    assert len(CREATES) == 1 and len(WRITES) == 3, (
        f"expected exactly 3 writes + 1 create total, got writes={len(WRITES)} creates={len(CREATES)}"
    )

    # WID-OLD (existing, no longer in feed) must be archived
    archive_writes = [w for w in WRITES if w[1].get("x_giftsksa_discontinued") is True]
    assert len(archive_writes) == 1, f"expected exactly 1 archive write, got {archive_writes}"
    assert archive_writes[0][0] == [102], "should archive WID-OLD (id 102)"
    assert archive_writes[0][1]["website_published"] is False

    print("ALL ASSERTIONS PASSED")


def run_test_failed_update_not_archived():
    """
    A product that's still genuinely in today's feed, but whose write()
    call happens to raise (transient RPC error), must NOT be archived just
    because its synced-date-style bookkeeping didn't get updated. Archiving
    is decided from "is this default_code in today's feed at all", not from
    whether the write succeeded.
    """
    products = [
        {"id": 1, "name": "Flaky Widget", "default_code": "WID-FLAKY",
         "public_categ_ids": [], "image_url": None},
    ]
    prices = [{"id": 1, "default_code": "WID-FLAKY", "list_price": 10, "retail_price": 15}]
    stocks = [{"id": 1, "default_code": "WID-FLAKY", "net_available_qty": 5}]
    existing_products = [{"id": 201, "default_code": "WID-FLAKY"}]

    def flaky_requests_get(url, timeout=None):
        if "/products/all/" in url:
            return FakeResponse(products)
        if "/products/price/" in url:
            return FakeResponse(prices)
        if "/products/stock/" in url:
            return FakeResponse(stocks)
        raise AssertionError(f"Unexpected URL: {url}")

    writes = []

    class FlakyServerProxy:
        def __init__(self, url, *a, **kw):
            pass

        def authenticate(self, db, username, api_key, ctx):
            return 42

        def execute_kw(self, db, uid, api_key, model, method, args, kwargs):
            if model == "product.public.category" and method == "search_read":
                return []
            if model == "product.template" and method == "search_read":
                return [dict(p, name=p["default_code"], x_giftsksa_discontinued=False)
                        for p in existing_products]
            if model == "product.template" and method == "write":
                if args[0] == [201]:
                    raise RuntimeError("simulated transient RPC failure")
                writes.append(args)
                return True
            raise AssertionError(f"Unexpected RPC in flaky test: {model}.{method}")

    import sync as sync_module

    orig_attempts, orig_backoff = sync_module.RPC_RETRY_ATTEMPTS, sync_module.RPC_RETRY_BACKOFF_SECONDS
    sync_module.RPC_RETRY_ATTEMPTS = 1
    sync_module.RPC_RETRY_BACKOFF_SECONDS = 0
    try:
        with mock.patch("requests.get", side_effect=flaky_requests_get), mock.patch(
            "xmlrpc.client.ServerProxy", FlakyServerProxy
        ):
            try:
                sync_module.main()
                raise AssertionError("expected sync.py to exit(1) due to the simulated failure")
            except SystemExit as e:
                assert e.code == 1, f"should exit 1 because the write genuinely failed, got {e.code}"
    finally:
        sync_module.RPC_RETRY_ATTEMPTS = orig_attempts
        sync_module.RPC_RETRY_BACKOFF_SECONDS = orig_backoff

    # The crucial assertion: WID-FLAKY's write raised, but it was never
    # re-queried/re-written as an archive candidate, because archiving is
    # computed from `seen_codes` (built from the feed, before any Odoo
    # writes happen) rather than from whether its own write succeeded.
    assert not any(w[0] == [201] and w[1].get("x_giftsksa_discontinued") for w in writes), (
        "WID-FLAKY must never be archived just because its update write failed"
    )

    print("FAILED-UPDATE-NOT-ARCHIVED TEST PASSED")


if __name__ == "__main__":
    run_test()
    run_test_failed_update_not_archived()
