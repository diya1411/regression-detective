"""Build the ground-truth corpus: one synthetic git repository per test case from the design (§10).

Each case is a small but realistic "checkout-service" history with a defect (or non-defect) injected
at a known commit, plus exported CI runs, PR/issue history and a CI failure archive for the
LocalForge. Ground truth lands in truth.json next to the repo.

    python -m eval.build_fixtures            # all cases into eval/fixtures/
    python -m eval.build_fixtures --only c1_single_defect
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "fixtures"
TEST_ID = "tests/test_cart.py::test_checkout_total"
AUTHORS = ["r.mehta", "a.okafor", "j.lindqvist", "s.tanaka", "m.rossi", "p.nair"]

# ------------------------------------------------------------------------------------------ sources
CATALOG = '''"""Product catalog: SKU -> current unit price."""
from decimal import Decimal

import attrs


@attrs.define
class Product:
    sku: str
    price: Decimal


class Catalog:
    def __init__(self, prices: dict | None = None):
        self._products = {sku: Product(sku, Decimal(p)) for sku, p in (prices or {}).items()}

    def price(self, sku: str) -> Decimal:
        return self._products[sku].price

    def set_price(self, sku: str, price) -> None:
        self._products[sku] = Product(sku, Decimal(price))
'''

CART = '''"""Shopping cart."""
from dataclasses import dataclass
from decimal import Decimal

from . import pricing
from .catalog import Catalog


@dataclass
class LineItem:
    sku: str
    unit_price: Decimal
    qty: int


class Cart:
    def __init__(self, catalog: Catalog, tax_rate: Decimal = Decimal("0")):
        self.catalog = catalog
        self.tax_rate = Decimal(tax_rate)
        self.items: list[LineItem] = []

    def add(self, sku: str, qty: int = 1) -> None:
        self.items.append(LineItem(sku, self.catalog.price(sku), qty))

    def reprice(self) -> None:
        """Refresh unit prices from the catalog (PR #412)."""
        for item in self.items:
            item.unit_price = self.catalog.price(item.sku)

    def skus(self) -> list[str]:
        return [i.sku for i in self.items]

    def total(self) -> Decimal:
        return pricing.order_total(self.items, tax_rate=self.tax_rate)
'''

PRICING = '''"""Pricing rules. Rounding is applied once, at order level (see docs/pricing.md)."""
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def line_total(item) -> Decimal:
    return item.unit_price * item.qty


def tax_amount(amount: Decimal, rate: Decimal) -> Decimal:
    return amount * rate


def order_total(items, tax_rate: Decimal = Decimal("0")) -> Decimal:
    subtotal = sum((line_total(i) for i in items), Decimal("0"))
    total = subtotal + tax_amount(subtotal, tax_rate)
    return total.quantize(CENT, rounding=ROUND_HALF_UP)
'''

PRICING_DEP = PRICING.replace("from decimal import ROUND_HALF_UP, Decimal",
                              "from decimal import Decimal\n\nfrom rdfixture_money import round_money").replace(
    "    return total.quantize(CENT, rounding=ROUND_HALF_UP)", "    return round_money(total)")

FORMATTING = '''"""Money formatting for receipts."""
from decimal import Decimal


def format_money(amount: Decimal, currency: str = "EUR") -> str:
    return f"{amount:.2f} {currency}"
'''

REPORTS = '''"""Back-office reports (not used at checkout)."""


def daily_summary(orders: list) -> dict:
    return {"orders": len(orders)}
'''

TEST_CART_HEAD = '''from decimal import Decimal

from checkout.cart import Cart
from checkout.catalog import Catalog


def make_cart(**prices):
    return Cart(Catalog(prices))


def test_empty_cart_total_is_zero():
    assert make_cart().total() == Decimal("0.00")


def test_single_item():
    cart = make_cart(PEN="2.50")
    cart.add("PEN", 2)
    assert cart.total() == Decimal("5.00")

'''

TEST_REPRICE = TEST_CART_HEAD + '''
def test_checkout_total():
    catalog = Catalog({"MUG": "39.60"})
    cart = Cart(catalog)
    cart.add("MUG", 3)
    assert cart.total() == Decimal("118.80")
    catalog.set_price("MUG", "40.00")
    cart.reprice()
    assert cart.total() == Decimal("120.00")


def test_two_lines():
    cart = make_cart(MUG="39.60", PEN="2.50")
    cart.add("MUG", 1)
    cart.add("PEN", 4)
    assert cart.total() == Decimal("49.60")
'''

TEST_HALF_CENT = TEST_CART_HEAD + '''
def test_checkout_total():
    cart = make_cart(MUG="40.335")
    cart.add("MUG", 3)
    assert cart.total() == Decimal("121.01")
'''

TEST_TAX = TEST_CART_HEAD + '''
def test_checkout_total():
    cart = Cart(Catalog({"MUG": "40.00"}), tax_rate=Decimal("0.0825"))
    cart.add("MUG", 3)
    assert cart.total() == Decimal("129.90")
'''

TEST_FLAKY = TEST_CART_HEAD + '''
def test_checkout_total():
    cart = make_cart(MUG="40.00", PEN="2.50")
    cart.add("MUG", 3)
    cart.add("PEN", 2)
    assert cart.skus()[0] == "MUG"
    assert cart.total() == Decimal("125.00")
'''

TEST_FORMATTING = '''from decimal import Decimal

from checkout.formatting import format_money


def test_format_money():
    assert format_money(Decimal("3.5")) == "3.50 EUR"
'''

TEST_REPORTS = '''from checkout.reports import daily_summary


def test_daily_summary():
    assert daily_summary([1, 2]) == {"orders": 2}
'''

README = """# checkout-service

Cart, catalog and pricing for the shop backend.

Run the tests with `pytest`.
"""

PRICING_DOC = """# Pricing

## Totals
Cart totals must always reflect the *current* catalog price of every line. `Cart.reprice()` refreshes
unit prices after a catalog update (PR #412).

## Rounding
Rounding is applied once, at order level, to the cent with ROUND_HALF_UP. Line totals are never
rounded individually.

## Tax
Tax is computed on the order subtotal at the cart's tax rate and rounded with the total.
"""

CACHE_BUG = '''_line_cache: dict = {}


def line_total(item) -> Decimal:
    """Cached per SKU: line totals are hot in large carts (PR #431)."""
    if item.sku not in _line_cache:
        _line_cache[item.sku] = item.unit_price * item.qty
    return _line_cache[item.sku]
'''

BULK_BUG = '''BULK_QTY = 3
BULK_DISCOUNT = Decimal("0.05")
BULK_ENABLED = False  # finance has not approved bulk pricing yet


def line_total(item) -> Decimal:
    total = item.unit_price * item.qty
    if item.qty >= BULK_QTY:
        total -= total * BULK_DISCOUNT
    return total
'''

LINE_TOTAL = '''def line_total(item) -> Decimal:
    return item.unit_price * item.qty
'''


# ------------------------------------------------------------------------------------------ builder
class Builder:
    def __init__(self, path: Path, start: datetime):
        self.path = path
        self.t = start
        self.n = 0
        self.shas: list[str] = []
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        self._git("init", "-q", "-b", "main")

    def _git(self, *args, env=None):
        return subprocess.run(["git", "-C", str(self.path), *args], check=True, capture_output=True, text=True,
                              env={**os.environ, **(env or {})}).stdout.strip()

    def write(self, rel: str, text: str) -> None:
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def read(self, rel: str) -> str:
        return (self.path / rel).read_text()

    def edit(self, rel: str, old: str, new: str) -> None:
        txt = self.read(rel)
        assert txt.count(old) == 1, f"{rel}: {old[:40]!r} occurs {txt.count(old)} times"
        self.write(rel, txt.replace(old, new, 1))

    def append(self, rel: str, text: str) -> None:
        p = self.path / rel
        self.write(rel, (p.read_text() if p.exists() else "") + text)

    def commit(self, msg: str, hours: float = 20) -> str:
        self.t += timedelta(hours=hours)
        author = AUTHORS[self.n % len(AUTHORS)]
        date = self.t.isoformat()
        self._git("add", "-A")
        self._git("-c", f"user.name={author}", "-c", f"user.email={author}@acme.test", "commit", "-q",
                  "--allow-empty", "-m", msg, env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
        sha = self._git("rev-parse", "HEAD")
        self.shas.append(sha)
        self.n += 1
        return sha

    # harmless, realistic changes that never touch pricing.py
    def filler(self, k: int) -> str:
        kind = k % 6
        if kind == 0:
            self.append("docs/changelog.md", f"- {self.t.date()}: housekeeping #{k}\n")
            return self.commit(f"docs: changelog entry {k}")
        if kind == 1:
            self.append("checkout/reports.py", f"\n\ndef report_{k}(orders: list) -> int:\n    return len(orders) * {k}\n")
            return self.commit(f"reports: add report_{k}")
        if kind == 2:
            self.append("checkout/formatting.py",
                        f"\n\ndef format_code_{k}(code: str) -> str:\n    return code.upper().strip()[:{k + 3}]\n")
            return self.commit(f"formatting: helper for code style {k}")
        if kind == 3:
            self.append("tests/test_reports.py",
                        f"\n\ndef test_report_{k}():\n    from checkout import reports\n    "
                        f"assert reports.daily_summary([]) == {{'orders': 0}}\n")
            return self.commit(f"tests: cover reports ({k})")
        if kind == 4:
            self.append("checkout/catalog.py",
                        f"\n\ndef sku_slug_{k}(sku: str) -> str:\n    return sku.lower().replace(' ', '-')\n")
            return self.commit(f"catalog: sku slug helper {k}")
        self.append("README.md", f"\nNote {k}: see docs/pricing.md for pricing rules.\n")
        return self.commit(f"readme: note {k}")


def base_project(b: Builder, test_src: str, requirements: str, pricing: str = PRICING) -> str:
    b.write("checkout/__init__.py", '"""checkout-service."""\n')
    b.write("checkout/catalog.py", CATALOG)
    b.write("checkout/cart.py", CART)
    b.write("checkout/pricing.py", pricing)
    b.write("checkout/formatting.py", FORMATTING)
    b.write("checkout/reports.py", REPORTS)
    b.write("tests/__init__.py", "")
    b.write("tests/test_cart.py", test_src)
    b.write("tests/test_formatting.py", TEST_FORMATTING)
    b.write("tests/test_reports.py", TEST_REPORTS)
    b.write("README.md", README)
    b.write("docs/pricing.md", PRICING_DOC)
    b.write("requirements.txt", requirements)
    b.write(".github/workflows/ci.yml", "name: ci\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-22.04\n"
            "    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n"
            "        with: {python-version: '3.12'}\n      - run: pip install -r requirements.txt pytest\n"
            "      - run: pytest\n")
    return b.commit("initial checkout-service")


def meta(case_dir: Path, b: Builder, lg: str, head: str, history: list, archive: list, ci_extra: list | None = None,
         env_head: dict | None = None):
    env = {"runner_image": "ubuntu-22.04", "image_digest": "sha256:9f1c0d2e7a", "python": "3.12.4"}
    runs = [{"id": "2840", "sha": lg, "status": "success", "started_at": "2024-01-01T00:00:00Z", "env": env},
            {"id": "2841", "sha": head, "status": "failure", "started_at": "2024-02-01T00:00:00Z",
             "env": env_head or env, "failed_tests": [TEST_ID]}] + (ci_extra or [])
    m = case_dir / "meta"
    m.mkdir(parents=True, exist_ok=True)
    (m / "ci_runs.json").write_text(json.dumps(runs, indent=1))
    (m / "history.json").write_text(json.dumps(history, indent=1))
    (m / "ci_archive.json").write_text(json.dumps(archive, indent=1))


COMMON_HISTORY = [
    {"kind": "pr", "number": 412, "title": "Reprice carts when catalog prices change",
     "body": "Cart totals must always reflect current catalog prices. Adds `Cart.reprice()` which refreshes "
             "unit prices from the catalog; `order_total` recomputes every line from the current unit price. "
             "Rounding stays at order level (ROUND_HALF_UP).",
     "files": ["checkout/cart.py", "checkout/pricing.py", "tests/test_cart.py"], "closed_at": "2023-11-02"},
    {"kind": "issue", "number": 377, "title": "Receipt shows EUR twice",
     "body": "format_money appends the currency and the template adds it again.", "files": ["checkout/formatting.py"],
     "closed_at": "2023-10-11"},
    {"kind": "pr", "number": 398, "title": "Daily summary report", "body": "Back-office daily order counts.",
     "files": ["checkout/reports.py"], "closed_at": "2023-10-20"},
]

COMMON_ARCHIVE = [
    {"id": "2311", "test_id": "tests/test_cart.py::test_single_item", "date": "2023-09-14",
     "message": "AssertionError: assert Decimal('4.99') == Decimal('5.00')",
     "log": "checkout/pricing.py:14: in order_total", "resolution": "commit",
     "fix_commit": "1a2b3c4", "notes": "per-line rounding introduced in pricing; reverted to order-level rounding"},
    {"id": "2398", "test_id": "tests/test_cart.py::test_checkout_total", "date": "2023-10-03",
     "message": "AssertionError: assert Decimal('118.79') == Decimal('118.80')",
     "log": "checkout/pricing.py:16: in order_total", "resolution": "commit", "fix_commit": "5d6e7f8",
     "notes": "pricing defect: subtotal summed floats"},
    {"id": "2502", "test_id": "tests/test_formatting.py::test_format_money", "date": "2023-11-21",
     "message": "AssertionError: assert '3.5 EUR' == '3.50 EUR'", "log": "", "resolution": "commit",
     "fix_commit": "9a8b7c6", "notes": "format string regression"},
    {"id": "2577", "test_id": "tests/test_reports.py::test_daily_summary", "date": "2023-12-02",
     "message": "ImportError: cannot import name 'frozen' from 'attrs'", "log": "", "resolution": "dependency",
     "fix_commit": "0f1e2d3", "notes": "attrs downgrade in a lockfile refresh"},
]


def truth(case_dir: Path, **kw) -> None:
    (case_dir / "truth.json").write_text(json.dumps({"test": TEST_ID, **kw}, indent=1))


# ------------------------------------------------------------------------------------------ cases
def c1_single_defect(d: Path):
    b = Builder(d / "repo", datetime(2023, 12, 20, 9, tzinfo=timezone.utc))
    lg = base_project(b, TEST_REPRICE, "attrs==23.1.0\n")
    culprit = None
    for i in range(1, 33):
        if i == 11:
            b.write("requirements.txt", "attrs==23.2.0\n")
            b.commit("deps: bump attrs to 23.2.0")
        elif i == 19:
            b.edit("checkout/pricing.py", LINE_TOTAL, CACHE_BUG)
            culprit = b.commit("cache line totals per item")
        elif i == 8:
            b.edit("checkout/pricing.py", '"""Pricing rules.', '"""Pricing rules for the checkout.')
            b.commit("pricing: clarify module docstring")
        else:
            b.filler(i)
    head = b.shas[-1]
    meta(d, b, lg, head, COMMON_HISTORY + [
        {"kind": "pr", "number": 431, "title": "Cache line totals per item",
         "body": "Profiling large carts showed `line_total` is hot. Cache the computed line total per SKU. "
                 "Pure performance change, no behaviour change intended.",
         "files": ["checkout/pricing.py"], "closed_at": "2024-01-05", "commit": culprit}], COMMON_ARCHIVE)
    truth(d, case="c1_single_defect", title="Single defect commit, clean 32-commit range",
          expect="resolved", culprits=[culprit], last_good=lg, head=head, max_probes=6,
          classification="defect")
    return lg, head


def c2_flaky(d: Path):
    b = Builder(d / "repo", datetime(2024, 2, 1, 9, tzinfo=timezone.utc))
    cart = CART.replace("        return [i.sku for i in self.items]",
                        "        return list({i.sku for i in self.items})")
    base_project(b, TEST_FLAKY, "attrs==23.2.0\n")
    b.write("checkout/cart.py", cart)
    lg = b.commit("cart: dedupe skus()")
    for i in range(1, 17):
        b.filler(i)
    head = b.shas[-1]
    meta(d, b, lg, head, COMMON_HISTORY, COMMON_ARCHIVE + [
        {"id": "2611", "test_id": TEST_ID, "date": "2024-01-12",
         "message": "AssertionError: assert 'PEN' == 'MUG'", "log": "tests/test_cart.py:24: AssertionError",
         "resolution": "flaky", "fix_commit": None, "notes": "passed on re-run; not investigated"}])
    truth(d, case="c2_flaky", title="Genuinely flaky test, no real regression", expect="flaky", culprits=[],
          last_good=lg, head=head)
    return lg, head


def _wheel(version: str, rounding: str) -> tuple[str, bytes]:
    name = "rdfixture_money"
    files = {
        f"{name}/__init__.py": (f'"""Money helpers (fixture package)."""\nfrom decimal import {rounding}, Decimal\n\n'
                                f'__version__ = "{version}"\nCENT = Decimal("0.01")\n\n\n'
                                f'def round_money(amount: Decimal) -> Decimal:\n'
                                f'    return Decimal(amount).quantize(CENT, rounding={rounding})\n').encode(),
        f"{name}-{version}.dist-info/METADATA": (f"Metadata-Version: 2.1\nName: rdfixture-money\nVersion: {version}\n"
                                                 f"Summary: money helpers\n").encode(),
        f"{name}-{version}.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: rd-fixtures\nRoot-Is-Purelib: true\n"
                                             b"Tag: py3-none-any\n",
    }
    record_lines = []
    for p, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record_lines.append(f"{p},sha256={digest},{len(data)}")
    record_lines.append(f"{name}-{version}.dist-info/RECORD,,")
    files[f"{name}-{version}.dist-info/RECORD"] = ("\n".join(record_lines) + "\n").encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p, data in files.items():
            z.writestr(p, data)
    return f"{name}-{version}-py3-none-any.whl", buf.getvalue()


def c3_dependency(d: Path):
    reg = d / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    for ver, rnd in (("1.0.0", "ROUND_HALF_UP"), ("1.1.0", "ROUND_HALF_EVEN")):
        fn, data = _wheel(ver, rnd)
        (reg / fn).write_bytes(data)
    (reg / "index.json").write_text(json.dumps({"rdfixture-money": [
        {"version": "1.0.0", "upload_time": "2024-03-01T10:00:00Z"},
        {"version": "1.1.0", "upload_time": "2024-06-10T10:00:00Z"}]}, indent=1))
    b = Builder(d / "repo", datetime(2024, 6, 1, 9, tzinfo=timezone.utc))
    lg = base_project(b, TEST_HALF_CENT, "attrs==23.2.0\nrdfixture-money>=1.0\n", pricing=PRICING_DEP)
    for i in range(1, 17):
        b.filler(i)
    head = b.shas[-1]
    meta(d, b, lg, head, COMMON_HISTORY, COMMON_ARCHIVE)
    truth(d, case="c3_dependency", title="Dependency minor release breaks the test; repository code unchanged",
          expect="dependency", culprits=[], packages=["rdfixture-money"], last_good=lg, head=head)
    return lg, head


def c4_unbuildable(d: Path):
    b = Builder(d / "repo", datetime(2024, 3, 1, 9, tzinfo=timezone.utc))
    lg = base_project(b, TEST_REPRICE, "attrs==23.2.0\n")
    culprit = None
    for i in range(1, 17):
        if i == 13:
            b.write("requirements.txt", "attrs==23.2.9\n")   # no such release: install fails
            b.commit("deps: bump attrs")
        elif i == 14:
            b.edit("checkout/pricing.py", LINE_TOTAL, CACHE_BUG)
            culprit = b.commit("cache line totals per item")
        elif i == 15:
            b.write("requirements.txt", "attrs==23.2.0\n")
            b.edit("checkout/cart.py", "    def skus(self) -> list[str]:", "    def skus(self) -> list[str]")
            b.commit("deps: fix attrs pin; cart: wip skus refactor")      # syntax error: collection fails
        elif i == 16:
            b.edit("checkout/cart.py", "    def skus(self) -> list[str]", "    def skus(self) -> list[str]:")
            b.commit("cart: fix syntax")
        else:
            b.filler(i)
    head = b.shas[-1]
    meta(d, b, lg, head, COMMON_HISTORY, COMMON_ARCHIVE)
    truth(d, case="c4_unbuildable", title="Defect commit adjacent to two commits that fail to build",
          expect="resolved", culprits=[culprit], last_good=lg, head=head, classification="defect")
    return lg, head


def c5_stale_test(d: Path):
    b = Builder(d / "repo", datetime(2024, 4, 1, 9, tzinfo=timezone.utc))
    lg = base_project(b, TEST_HALF_CENT, "attrs==23.2.0\n")
    culprit = None
    for i in range(1, 17):
        if i == 10:
            b.edit("checkout/pricing.py", "from decimal import ROUND_HALF_UP, Decimal",
                   "from decimal import ROUND_HALF_EVEN, Decimal")
            b.edit("checkout/pricing.py", "    return total.quantize(CENT, rounding=ROUND_HALF_UP)",
                   "    return total.quantize(CENT, rounding=ROUND_HALF_EVEN)")
            b.edit("docs/pricing.md", "Rounding is applied once, at order level, to the cent with ROUND_HALF_UP.",
                   "Rounding is applied once, at order level, to the cent with ROUND_HALF_EVEN (banker's rounding, "
                   "see ADR-0007).")
            b.write("docs/adr/0007-bankers-rounding.md",
                    "# ADR-0007: Banker's rounding for order totals\n\n## Status\nAccepted\n\n## Decision\n"
                    "Order totals are rounded with ROUND_HALF_EVEN, as required by finance for reconciliation.\n\n"
                    "## Consequences\nThis intentionally changes totals that end in exactly half a cent "
                    "(e.g. 121.005 now rounds to 121.00). Tests asserting the old ROUND_HALF_UP results must be "
                    "updated.\n")
            culprit = b.commit("pricing: switch order rounding to banker's rounding (ADR-0007)")
        else:
            b.filler(i)
    head = b.shas[-1]
    meta(d, b, lg, head, COMMON_HISTORY + [
        {"kind": "pr", "number": 457, "title": "Banker's rounding for order totals (ADR-0007)",
         "body": "Finance requires ROUND_HALF_EVEN for reconciliation with the ledger. This is an intentional "
                 "behaviour change for half-cent totals; follow-up: update the tests that assert HALF_UP values.",
         "files": ["checkout/pricing.py", "docs/pricing.md", "docs/adr/0007-bankers-rounding.md"],
         "closed_at": "2024-04-10", "commit": culprit}], COMMON_ARCHIVE)
    truth(d, case="c5_stale_test", title="Intentional behaviour change; the test is stale", expect="resolved",
          culprits=[culprit], classification="stale_test", last_good=lg, head=head)
    return lg, head


def c6_multi_cause(d: Path):
    b = Builder(d / "repo", datetime(2024, 5, 1, 9, tzinfo=timezone.utc))
    lg = base_project(b, TEST_TAX, "attrs==23.2.0\n")
    culprits = []
    for i in range(1, 33):
        if i == 9:
            b.edit("checkout/pricing.py", LINE_TOTAL, BULK_BUG)
            culprits.append(b.commit("pricing: add bulk pricing (behind BULK_ENABLED)"))
        elif i == 21:
            b.edit("checkout/pricing.py", "    return amount * rate",
                   '    return (amount * rate).to_integral_value(rounding="ROUND_DOWN")')
            culprits.append(b.commit("pricing: whole-unit tax for the new invoice format"))
        else:
            b.filler(i)
    head = b.shas[-1]
    meta(d, b, lg, head, COMMON_HISTORY, COMMON_ARCHIVE)
    truth(d, case="c6_multi_cause", title="Two independent defects in the range", expect="multi_cause",
          culprits=culprits, last_good=lg, head=head)
    return lg, head


def c7_window_invalid(d: Path):
    b = Builder(d / "repo", datetime(2024, 6, 15, 9, tzinfo=timezone.utc))
    base_project(b, TEST_REPRICE, "attrs==23.2.0\n")
    real = None
    for i in range(1, 29):
        if i == 5:
            b.edit("checkout/pricing.py", LINE_TOTAL, CACHE_BUG)
            real = b.commit("cache line totals per item")
        else:
            b.filler(i)
    lg = b.shas[12]     # CI claimed this was green (the job was skipping tests/test_cart.py)
    head = b.shas[-1]
    meta(d, b, lg, head, COMMON_HISTORY, COMMON_ARCHIVE)
    truth(d, case="c7_window_invalid", title="Culprit outside the search window (bad last-good assumption)",
          expect="window_invalid", culprits=[], true_culprit_outside_window=real, last_good=lg, head=head)
    return lg, head


def c8_budget(d: Path):
    b = Builder(d / "repo", datetime(2024, 7, 1, 9, tzinfo=timezone.utc))
    lg = base_project(b, TEST_REPRICE, "attrs==23.2.0\n")
    culprit = None
    for i in range(1, 65):
        if i == 41:
            b.edit("checkout/pricing.py", LINE_TOTAL, CACHE_BUG)
            culprit = b.commit("cache line totals per item")
        else:
            b.filler(i)
    head = b.shas[-1]
    meta(d, b, lg, head, COMMON_HISTORY, COMMON_ARCHIVE)
    truth(d, case="c8_budget", title="Budget exhausted mid-bisection", expect="budget_exhausted",
          culprits=[], true_culprit=culprit, last_good=lg, head=head, settings={"max_probes": 3})
    return lg, head


CASES = {f.__name__: f for f in (c1_single_defect, c2_flaky, c3_dependency, c4_unbuildable, c5_stale_test,
                                  c6_multi_cause, c7_window_invalid, c8_budget)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--out", default=str(ROOT))
    a = ap.parse_args(argv)
    out = Path(a.out)
    for name, fn in CASES.items():
        if a.only and name not in a.only:
            continue
        d = out / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        lg, head = fn(d)
        print(f"{name:<22} last_good={lg[:7]} head={head[:7]} -> {d}")


if __name__ == "__main__":
    main()
