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


def test_declared_env_helpers():
    """requirements parsing, source detection, and exact-pin relaxation."""
    import tempfile
    from pathlib import Path
    assert rk.rc_relax_pin("numpy==1.16.2") == ("numpy>=1.16.2", True)
    assert rk.rc_relax_pin("scipy>=1.0") == ("scipy>=1.0", False)
    assert rk.rc_relax_pin("pkg[x]==1.0.0") == ("pkg[x]>=1.0.0", True)
    d = Path(tempfile.mkdtemp())
    (d / "requirements.txt").write_text(
        "# c\nnumpy==1.16.2\nrequests>=2.0  # inline\n-e .\n"
        "git+https://x/y.git\nhttps://f/b.whl\nscipy\n")
    assert rk.rc_find_requirements(d)[0]["kind"] == "requirements"
    assert rk.rc_parse_requirements(d / "requirements.txt") == \
        ["numpy==1.16.2", "requests>=2.0", "scipy"]
    d2 = Path(tempfile.mkdtemp()); (d2 / "environment.yml").write_text("name: x\n")
    assert rk.rc_find_requirements(d2)[0]["kind"] == "conda"
    return True


def test_declared_env_install():
    """A repo whose entry imports a package listed in requirements.txt gets the
    declared set installed in one shot; a stale exact pin is relaxed as a flagged
    PIN_RELAXED patch. Skipped cleanly if the environment refuses the install."""
    import tempfile, subprocess, sys
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    (d / "requirements.txt").write_text("praise==0.0.5\n")  # only 0.1 exists -> must relax
    (d / "run.py").write_text("import praise\nprint(bool(praise.praise()))\n")
    try:
        res = rk.attempt_executability(d, allow_install=True)
    finally:
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "-q", "praise"],
                       capture_output=True)
    if res["status"] == "RAN":
        assert any("requirements" in i for i in res.get("installed", [])), res.get("installed")
        assert any(p.get("pattern") == "PIN_RELAXED" for p in res.get("patches", [])), res.get("patches")
    else:
        # low memory / offline CI -> honest hand-off is acceptable
        assert res["status"] == "NEEDS_AGENT"
    return True


def test_rung_reporting():
    """A RAN/RAN_AS_IS result carries the explicit reproduction-rung fields
    (certifies rung 1 executability, does NOT verify scientific correctness);
    a hand-off reports the rung it stopped at."""
    # run the bundled broken fixture through the real engine, on a temp COPY so
    # the in-place patches never mutate the source fixture
    import os, shutil, tempfile
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    fx = os.path.join(here, "fixtures", "example_paper")
    if os.path.isdir(fx):
        tmp = os.path.join(tempfile.mkdtemp(), "example_paper")
        shutil.copytree(fx, tmp)
        res = rk.attempt_executability(tmp, allow_install=True)
        assert res["status"] in ("RAN", "RAN_AS_IS"), res["status"]
        assert res.get("rung_reached") == 1
        assert "executability" in res.get("rung_certified", "")
        assert "correctness" in res.get("not_verified", "")
    # hand-off path reports a stopping rung (ep must live under target_dir)
    import tempfile as _tf, os as _os
    tdir = _tf.mkdtemp()
    ep = _os.path.join(tdir, "x.py"); open(ep, "w").write("x=1\n")
    ho = rk.build_handoff(tdir, __import__("pathlib").Path(ep),
                          [], [], [{"iter": 0, "returncode": 1, "error": "boom"}],
                          reason="needs a human step")
    assert "stopping_rung" in ho
    return True


def test_py2_name_fix():
    """A removed Py2 builtin (xrange) used at run time is rewritten to its Py3
    equivalent and the script then runs (found on ReScience repo viejo:2016)."""
    import tempfile
    from pathlib import Path
    td = Path(tempfile.mkdtemp())
    (td / "run.py").write_text("total = 0\nfor i in xrange(5):\n    total += i\nprint(total)\n")
    res = rk.attempt_executability(td, allow_install=False, max_iters=4)
    assert res["status"] == "RAN", res
    assert any("xrange" in p.get("change", "") for p in res["patches"]), res["patches"]
    # a NAME inside a string must NOT be rewritten
    assert rk.classify_failure("NameError: name 'foo' is not defined")["pattern"] == "EXEC_ORDER"
    assert rk.classify_failure(
        'File "x.py", line 1\nNameError: name \'xrange\' is not defined')["pattern"] == "PY2_NAME"
    return True


def test_setup_py_only_routes_to_notebook():
    """A repo whose ONLY .py is setup.py should not be run as an entry point;
    if it ships a notebook, scope detection routes there (stollmeier:2017)."""
    import tempfile, json as _json
    from pathlib import Path
    td = Path(tempfile.mkdtemp())
    (td / "setup.py").write_text("from setuptools import setup\nsetup(name='x')\n")
    nb = {"cells": [{"cell_type": "code", "execution_count": 1,
                     "source": "print(1)\n", "outputs": [], "metadata": {}}],
          "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    (td / "analysis.ipynb").write_text(_json.dumps(nb))
    assert rk.detect_scope(td) == "notebook", rk.detect_scope(td)
    # discover_entrypoint must not pick setup.py
    assert rk.discover_entrypoint(td) is None
    return True


def test_silent_nonzero_exit():
    """A script that exits nonzero but prints NOTHING to stderr must hand off
    cleanly, not crash on an empty splitlines()[-1] (regression: found on a real
    ReScience repo during benchmark validation)."""
    import tempfile
    from pathlib import Path
    td = Path(tempfile.mkdtemp())
    (td / "run.py").write_text("import sys; sys.exit(1)\n")  # nonzero, silent
    res = rk.attempt_executability(td, allow_install=False, max_iters=3)
    assert isinstance(res, dict) and res["status"] == "NEEDS_AGENT", res
    assert res["attempts"][-1]["error"] is None  # no fabricated error line
    return True


def test_benchmark_harness():
    """The benchmark harness runs on the bundled fixtures, produces a summary,
    and does NOT mutate the fixtures (it works on fresh copies)."""
    import os, sys, subprocess, importlib.util
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bench = os.path.join(here, "benchmark", "run_benchmark.py")
    if not os.path.exists(bench):
        return True  # harness not present in this checkout
    # snapshot the broken fixture, run the harness, confirm it's unchanged
    fx = os.path.join(here, "fixtures", "example_paper", "analysis.py")
    before = open(fx).read() if os.path.exists(fx) else None
    spec = importlib.util.spec_from_file_location("runbench", bench)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    import json as _json
    manifest = _json.load(open(os.path.join(here, "benchmark", "fixtures_manifest.json")))
    rows = mod.run(manifest, do_clone=False)
    summary = mod.summarize(rows)
    assert summary["n_evaluable"] >= 1
    assert summary["runs_after_repro_check"] >= 1
    if before is not None:
        assert open(fx).read() == before, "benchmark mutated the fixture!"
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
    test_declared_env_helpers()
    print("PASS — declared-env helpers (parse/detect/relax-pin)")
    test_declared_env_install()
    print("PASS — declared-env install + flagged pin relaxation")
    test_rung_reporting()
    print("PASS — explicit reproduction-rung reporting")
    test_py2_name_fix()
    print("PASS — Py2 builtin (xrange) rewritten to Py3, script runs")
    test_setup_py_only_routes_to_notebook()
    print("PASS — setup.py-only repo routes to notebook, not run as entry")
    test_silent_nonzero_exit()
    print("PASS — silent nonzero-exit hands off cleanly (no empty-stderr crash)")
    test_benchmark_harness()
    print("PASS — benchmark harness runs on fixtures without mutating them")
