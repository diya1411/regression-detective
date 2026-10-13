from detective.agents.explanation import intent_hits
from detective.agents.triage import _parse_symptom
from detective.rag.index import BM25, Doc, HashingEmbedder, HybridIndex, tokenize
from detective.redact import redact
from detective.sandbox.deps import discover
from detective.tools.ast_tools import chunk_python, defs_overlapping, external_io_scan, import_closure
from detective.tools.git import parse_hunks
from detective.tools.pytest_report import parse_run, signature

DIFF = """diff --git a/p.py b/p.py
--- a/p.py
+++ b/p.py
@@ -1,9 +1,12 @@
 CENT = 1


+_cache = {}
+
+
 def line_total(item):
-    return item.price * item.qty
+    return _cache.setdefault(item.sku, item.price * item.qty)


 def tax_amount(a, r):
"""

SRC_AFTER = """CENT = 1


_cache = {}


def line_total(item):
    return _cache.setdefault(item.sku, item.price * item.qty)


def tax_amount(a, r):
    return a * r
"""


def test_parse_hunks_ignores_context_lines():
    h = parse_hunks(DIFF)["p.py"]
    touched = [c.qualname for c in defs_overlapping(SRC_AFTER, "p.py", h["new"])]
    assert touched == ["line_total"]          # tax_amount is only context


def test_chunk_python_semantic_units():
    src = "class A:\n    def m(self):\n        return 1\n\n\ndef f(x):\n    '''doc'''\n    return x\n"
    q = {c.qualname: c for c in chunk_python(src, "a.py")}
    assert {"A", "A.m", "f"} <= set(q)
    assert q["f"].docstring == "doc"


def test_import_closure_follows_packages():
    files = {"pkg/__init__.py": "", "pkg/a.py": "from . import b\n", "pkg/b.py": "import json\n",
             "pkg/c.py": "x = 1\n", "tests/test_a.py": "from pkg.a import thing\n"}
    closure = import_closure(["tests/test_a.py"], list(files), files.get)
    assert {"tests/test_a.py", "pkg/a.py", "pkg/b.py", "pkg/__init__.py"} <= closure
    assert "pkg/c.py" not in closure


def test_external_io_scan():
    f = external_io_scan({"t.py": "import requests\nimport os\nx = os.environ['HOME']\nopen('/etc/hosts')\n"})
    kinds = {x["kind"] for x in f}
    assert {"network-import", "env-read", "absolute-path-read"} <= kinds


def test_tokenize_splits_identifiers_and_bm25_prefers_exact_symbol():
    assert {"line_total", "line", "total"} <= set(tokenize("line_total"))
    assert {"linetotal", "line", "total"} <= set(tokenize("lineTotal"))
    docs = [tokenize("def order_total(items): return sum(items)"), tokenize("def line_total(item): return x"),
            tokenize("readme about totals")]
    s = BM25(docs).scores(tokenize("line_total"))
    assert s.argmax() == 1


def test_hybrid_index_rrf_and_filters():
    idx = HybridIndex(HashingEmbedder())
    idx.add([Doc("a", "code", "pricing.py :: line_total", "def line_total(item): cache per sku"),
             Doc("b", "docs", "pricing — Totals", "Totals must reflect current catalog prices"),
             Doc("c", "history", "PR #412", "Reprice carts when catalog prices change")])
    idx.build()
    hits = idx.search("catalog prices reprice", k=3)
    assert hits and hits[0][0].id in ("b", "c")
    only_code = idx.search("catalog prices", k=3, where=lambda d: d.corpus == "code")
    assert [h[0].id for h in only_code] == ["a"]


def test_deps_discovery_static(tmp_path):
    files = {"requirements.txt": "-r base.txt\nattrs==23.2.0  # pinned\n-e .\n",
             "base.txt": "httpx>=0.27\n",
             "pyproject.toml": "[project]\ndependencies=['packaging>=23']\n[project.optional-dependencies]\ntest=['pytest']\n",
             "setup.py": "import os\nos.system('rm -rf /')\nsetup(install_requires=['six'])\n"}
    spec = discover(files.get)
    assert {"httpx>=0.27", "attrs==23.2.0", "packaging>=23", "pytest"} <= set(spec.requirements)
    assert "six" in spec.requirements       # install_requires read from setup.py's AST; the file is never run
    pinned = spec.with_pins({"attrs": "23.1.0"})
    assert "attrs==23.1.0" in pinned.lines() and "attrs==23.2.0" not in pinned.lines()
    assert spec.key("3.12") != pinned.key("3.12")


def test_redaction():
    s = redact("token=ghp_" + "a" * 36 + " key AIza" + "b" * 35 + " API_KEY=supersecret123 https://u:pw@host/x")
    assert "ghp_" not in s and "AIza" not in s and "supersecret123" not in s and "u:pw@" not in s


def test_pytest_report_parsing():
    rep = {"tests": [{"nodeid": "tests/t.py::test_x", "outcome": "failed",
                      "call": {"outcome": "failed", "crash": {"message": "AssertionError: assert 5 == 4"}}}],
           "collectors": []}
    r = parse_run(rep, 1, "tests/t.py::test_x", 0.3)
    assert r.outcome == "failed" and r.signature == "AssertionError: assert 5 == 4"
    col = {"tests": [], "collectors": [{"nodeid": "tests/t.py", "outcome": "failed",
                                         "longrepr": "E   SyntaxError: invalid syntax"}]}
    assert parse_run(col, 2, "tests/t.py::test_x", 0.1).outcome == "collection_error"
    assert parse_run(None, 4, "tests/t.py::test_x", 0.1, "ERROR: file or directory not found").outcome == "missing"
    assert signature("obj at 0x7f00ab12 failed") == "obj at 0x? failed"


def test_symptom_parse():
    s = _parse_symptom("t::t", "AssertionError: assert Decimal('118.80') == Decimal('120.00')\n"
                       "checkout/pricing.py:14: in order_total\n/usr/lib/python3.12/x.py:1: in y")
    assert s["observed"] == "Decimal('118.80')" and s["expected"] == "Decimal('120.00')"
    assert abs(s["delta"] - 1.2) < 1e-9
    assert s["suspect_modules"] == ["checkout/pricing.py"]


def test_intent_detection_respects_negation():
    assert intent_hits("Pure performance change, no behaviour change intended.") == []
    assert intent_hits("This intentionally changes totals; tests must be updated.")
