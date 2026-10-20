"""End-to-end: build a fixture repo and run a full offline investigation in the (local) sandbox."""
import json

import pytest


@pytest.mark.parametrize("case,expect", [("c1_single_defect", "resolved"), ("c2_flaky", "flaky")])
def test_investigation(tmp_path, monkeypatch, case, expect):
    from eval.build_fixtures import CASES
    from detective.config import Settings
    from detective.graph import Params, run_investigation

    d = tmp_path / case
    d.mkdir()
    CASES[case](d)
    truth = json.loads((d / "truth.json").read_text())
    s = Settings().with_(offline=True, home=tmp_path / "home")
    inv = run_investigation(Params(repo=str(d / "repo"), test=truth["test"], last_good=truth["last_good"],
                                   head=truth["head"], meta=str(d / "meta")), s)
    rep = inv.report
    assert rep["status"] == expect
    assert [c["sha"] for c in rep["culprits"]] == truth["culprits"]
    assert all(cl["verdict"] != "unsupported" for cl in rep["verification"]["claims"])
    events = (s.investigations_dir / inv.id / "events.jsonl").read_text().splitlines()
    assert json.loads(events[-1])["type"] == "investigation.finished"
