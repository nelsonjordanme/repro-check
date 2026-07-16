"""Self-test: run the protocol against the bundled broken fixture.

Verifies the engine (a) auto-repairs the two known breakages, (b) reproduces the
deterministic claims, and (c) grades the unseeded bootstrap CI as NEAR, not a
hard mismatch. Run:  python test_repro.py
"""
import sys
from pathlib import Path

# import the installed package; fall back to the in-repo source tree
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from repro_check import engine as rk


def test_fixture():
    rep = rk.reproduce(ROOT / "fixtures" / "example_paper")
    assert rep["status"] == "COMPLETED", rep["status"]
    patterns = {p["pattern"] for p in rep["patches"]}
    assert patterns == {"PATH_HARDCODED", "DEP_API_CHANGE"}, patterns
    by_id = {c["id"]: c for c in rep["claims"]}
    assert by_id["r_xy"]["status"] == "REPRODUCED"
    assert by_id["treatment_effect"]["status"] == "REPRODUCED"
    assert by_id["r_ci95"]["status"] in ("REPRODUCED_STOCHASTIC", "NEAR_STOCHASTIC")
    assert rep["verdict"]["needs_review"] == 0
    return rep


if __name__ == "__main__":
    rep = test_fixture()
    print("PASS — {reproduced} reproduced, {near} near, {needs_review} need review"
          .format(**rep["verdict"]))
    print("repairs:", [p["pattern"] for p in rep["patches"]])
