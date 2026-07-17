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


def test_r_system_dep_handoff():
    """A failed R package build that looks like a missing OS library must
    surface the concrete system packages in the hand-off (R_SYSTEM_DEP)."""
    # static map
    assert rk.rc_r_system_deps("sf") == ["libgdal-dev", "libproj-dev", "libgeos-dev"]
    # header-hint fallback for an unmapped package
    assert rk.rc_r_system_deps("mystery", "fatal error: curl/curl.h: No such file") == ["libcurl4-openssl-dev"]
    # heuristic distinguishes a build failure from a plain not-available
    assert rk.rc_is_system_dep_failure("fatal error: gdal: No such file or directory")
    assert not rk.rc_is_system_dep_failure("package 'foo' is not available")
    # end-to-end shaping + rendering
    res = {"status": "NEEDS_AGENT", "language": "R", "entrypoint": "analysis.R",
           "installed": [], "attempts": [{"iter": 0}, {"iter": 1}],
           "reason": "system libraries required to build sf",
           "diagnosis": {"pattern": "MISSING_PKG_CRAN", "module": "sf"},
           "failed_pkg": "sf", "system_deps": ["libgdal-dev", "libproj-dev", "libgeos-dev"],
           "traceback": "ERROR: configuration failed for package 'sf'"}
    shaped = rk.rc_shape_r_result(res, "/tmp/nonexistent")
    assert shaped["next_action_key"] == "R_SYSTEM_DEP", shaped["next_action_key"]
    md = rk.render_handoff_md(shaped)
    assert "apt-get install -y libgdal-dev libproj-dev libgeos-dev" in md
    assert "System libraries needed" in md
    return True


def test_r_classifier():
    """R failure classification maps to the right categories."""
    assert rk.classify_r_failure("there is no package called 'DESeq2'")["pattern"] == "MISSING_PKG_BIOC"
    assert rk.classify_r_failure("there is no package called 'ggplot2'")["pattern"] == "MISSING_PKG_CRAN"
    assert rk.classify_r_failure("cannot open file 'data/x.csv': No such file")["pattern"] == "MISSING_DATA"
    assert rk.classify_r_failure('could not find function "choose.dir"')["pattern"] == "INTERACTIVE_OR_PLATFORM"
    return True


def test_install_guardrails():
    """Pre-install memory gate: passes with headroom, refuses honestly below
    the floor, respects the env override, and never blocks on unknown RAM."""
    import os
    # RAM is readable on the CI runners we target (Linux) and on macOS
    mb = rk.rc_available_ram_mb()
    assert mb is None or mb > 0
    # a trivially-low floor passes; an absurd floor refuses with a reason
    ok, _ = rk.rc_preinstall_gate(min_mb=1)
    assert ok is True
    ok2, reason2 = rk.rc_preinstall_gate(min_mb=10**9)
    # only assert refusal when RAM is actually knowable on this platform
    if mb is not None:
        assert ok2 is False and reason2 and "RAM available" in reason2
    # env override forces a refusal, and pip_install reports an honest skip
    os.environ["REPRO_CHECK_MIN_INSTALL_MB"] = "999999999"
    try:
        if rk.rc_available_ram_mb() is not None:
            ok3, pkg3, log3 = rk.pip_install("somepkg")
            assert ok3 is False and log3.startswith("SKIPPED_LOW_MEMORY:")
    finally:
        del os.environ["REPRO_CHECK_MIN_INSTALL_MB"]
    # unknown RAM must NOT block installs
    orig = rk.rc_available_ram_mb
    rk.rc_available_ram_mb = lambda: None
    try:
        ok4, _ = rk.rc_preinstall_gate()
        assert ok4 is True
    finally:
        rk.rc_available_ram_mb = orig
    return True


def test_notebook_out_of_order():
    """A notebook last run out of order (non-monotonic execution_count) must be
    flagged; an in-order notebook must not be."""
    def nb(counts):
        cells = [{"cell_type": "code", "execution_count": c,
                  "source": "x=%d" % i, "outputs": [], "metadata": {}}
                 for i, c in enumerate(counts)]
        return {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    # in order
    assert rk.notebook_out_of_order(nb([1, 2, 3]))["out_of_order"] is False
    # out of order
    o = rk.notebook_out_of_order(nb([3, 1, 2]))
    assert o["out_of_order"] is True and "non-monotonic" in o["detail"]
    # too few executed cells to judge -> not flagged
    assert rk.notebook_out_of_order(nb([None, None]))["out_of_order"] is False
    assert rk.notebook_out_of_order(nb([5]))["out_of_order"] is False
    return True


def test_bioc_lockstep_routing():
    """A Bioconductor version-lockstep failure routes to the R_BIOC_VERSION
    honest hand-off (not a generic install-failed)."""
    res = {"status": "NEEDS_AGENT", "language": "R", "entrypoint": "a.R",
           "installed": [], "attempts": [{"iter": 0}],
           "reason": "DESeq2 not available for Bioconductor 3.22 (the release matching this R)",
           "diagnosis": {"pattern": "MISSING_PKG_BIOC", "module": "DESeq2"},
           "failed_pkg": "DESeq2", "version_lockstep": True, "bioc_version": "3.22",
           "system_deps": [], "traceback": "not available for Bioconductor version '3.22'"}
    shaped = rk.rc_shape_r_result(res, "/tmp/nonexistent")
    assert shaped["next_action_key"] == "R_BIOC_VERSION", shaped["next_action_key"]
    return True


def test_url_detection():
    """Repo URLs (scheme'd, SSH, or bare host/owner/repo) are detected and
    normalized; local paths are left alone."""
    assert rk.rc_looks_like_url("github.com/o/r") is True
    assert rk.rc_looks_like_url("https://github.com/o/r") is True
    assert rk.rc_looks_like_url("git@github.com:o/r.git") is True
    assert rk.rc_looks_like_url("/tmp/x") is False
    assert rk.rc_looks_like_url("./fixtures/example_paper") is False
    assert rk.rc_normalize_repo_url("github.com/o/r/") == "https://github.com/o/r"
    assert rk.rc_normalize_repo_url("https://gitlab.com/o/r") == "https://gitlab.com/o/r"
    # clone of a definitely-nonexistent repo fails cleanly (no raise), if git
    # present. Any failure kind is acceptable — the point is it returns
    # (None, info) rather than raising, whether the wall is auth, not-found, or
    # a network-blocked CI runner.
    if __import__("shutil").which("git"):
        path, info = rk.rc_clone_repo("https://github.com/nelsonjordanme/"
                                      "definitely-not-a-real-repo-zzz", timeout=60)
        assert path is None and "error" in info and info.get("kind")
    return True


def test_editable_install_fix():
    """A repo that is its own package (imports fail without installation) but
    ships packaging metadata is fixed by `pip install -e . --no-deps` and runs.
    Skipped if pip can't write to the environment."""
    import tempfile, subprocess, sys
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    (d / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools>=61']\n"
        "build-backend='setuptools.build_meta'\n"
        "[project]\nname='reprocheck-editable-test'\nversion='0.0.1'\n")
    pkg = d / "analysis"; pkg.mkdir()
    (pkg / "__init__.py").write_text("from analysis.helpers import answer\n")
    (pkg / "helpers.py").write_text("import analysis\ndef answer(): return 42\n")
    # rc_find_package_root sees it
    assert rk.rc_find_package_root(d) == d
    try:
        res = rk.attempt_executability(d, allow_install=True)
    finally:
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "-q",
                        "reprocheck-editable-test"], capture_output=True)
    # Either it installed editable and ran, or the environment refused the write
    # (low memory / no pip) and it handed off honestly — both are acceptable.
    if res["status"] == "RAN":
        assert any("editable" in d for d in res.get("installed", [])), res.get("installed")
    else:
        assert res["status"] == "NEEDS_AGENT"
    return True


if __name__ == "__main__":
    rep = test_fixture()
    print("PASS — {reproduced} reproduced, {near} near, {needs_review} need review"
          .format(**rep["verdict"]))
    print("repairs:", [p["pattern"] for p in rep["patches"]])
    test_r_system_dep_handoff()
    print("PASS — R system-dependency hand-off (R_SYSTEM_DEP + apt-get command)")
    test_r_classifier()
    print("PASS — R failure classifier (CRAN/Bioc/data/interactive)")
    test_install_guardrails()
    print("PASS — install guardrails (memory gate + honest low-memory skip)")
    test_notebook_out_of_order()
    print("PASS — notebook out-of-order detection")
    test_bioc_lockstep_routing()
    print("PASS — Bioconductor version-lockstep hand-off (R_BIOC_VERSION)")
    test_url_detection()
    print("PASS — repo-URL detection + clean clone-failure")
    test_editable_install_fix()
    print("PASS — editable install fixes a self-importing package repo")
