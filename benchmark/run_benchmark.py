#!/usr/bin/env python3
"""Re-runnable runnability benchmark for repro-check.

Runs repro-check across a corpus manifest and regenerates the runnability
table: how many repos run AS-CLONED vs AFTER repro-check's fixes. This is the
auditable, re-executable form of the numbers quoted in the README/study — run
it yourself and check.

    python benchmark/run_benchmark.py                      # bundled fixtures
    python benchmark/run_benchmark.py --manifest my.json   # your own corpus
    python benchmark/run_benchmark.py --manifest rescience.json --clone

A manifest is a JSON list of entries:
    {"name": "...", "path": "fixtures/example_paper"}        # local path
    {"name": "...", "url":  "github.com/owner/repo"}         # cloned if --clone
    optional "expected": "RAN" | "RAN_AS_IS" | "NEEDS_AGENT" | ...

For URL entries you must pass --clone (network + disk); without it they are
skipped and counted as SKIPPED so a no-network run still produces a table for
the local entries.
"""
import argparse, json, shutil, subprocess, sys, tempfile, time
from pathlib import Path


def _fresh_copy(src):
    """Copy a repo to a throwaway temp dir so repro-check's in-place patches
    never mutate the corpus. Each measurement gets its own clean copy."""
    dst = Path(tempfile.mkdtemp(prefix="reprobench_")) / Path(src).name
    shutil.copytree(src, dst)
    return dst

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def _baseline_runs(path):
    """Does the repo run AS-CLONED, with no fixes and no installs? Runs the full
    router (Python/R/notebook) with allow_install=False; the repo counts as
    as-cloned ONLY if it reaches RAN_AS_IS (ran with zero patches and zero
    installs). Anything that needed a fix, an install, or handed off is 'broken
    as-cloned'. Language-agnostic so R and notebook repos are judged the same
    way as Python ones."""
    from repro_check import engine as rk
    res = rk.attempt_executability(Path(path), allow_install=False)
    return (res.get("status") == "RAN_AS_IS"
            and not res.get("patches") and not res.get("installed"))


def _repro_check(path):
    """Run the full repro-check loop; return the result dict."""
    from repro_check import engine as rk
    return rk.attempt_executability(Path(path), allow_install=True)


def run(manifest, do_clone=False):
    from repro_check import engine as rk
    rows = []
    for entry in manifest:
        name = entry.get("name") or entry.get("path") or entry.get("url")
        path = entry.get("path")
        if path is None and entry.get("url"):
            if not do_clone:
                rows.append({"name": name, "status": "SKIPPED",
                             "note": "url entry; pass --clone to fetch"})
                continue
            cloned, info = rk.rc_clone_repo(entry["url"])
            if cloned is None:
                rows.append({"name": name, "status": "CLONE_FAILED",
                             "note": info.get("error", "")[:120]})
                continue
            path = str(cloned)
        p = (ROOT / path) if not Path(path).is_absolute() else Path(path)
        if not p.is_dir():
            rows.append({"name": name, "status": "MISSING", "note": str(p)})
            continue
        t0 = time.time()
        # Each measurement runs on its OWN fresh copy — repro-check patches
        # files in place, so reusing a dir would let the fix pass contaminate
        # the baseline (and mutate the corpus). Baseline and fix pass get
        # separate clean copies.
        base_copy = _fresh_copy(p)
        fix_copy = _fresh_copy(p)
        try:
            baseline = _baseline_runs(base_copy)
            res = _repro_check(fix_copy)
        finally:
            shutil.rmtree(base_copy.parent, ignore_errors=True)
            shutil.rmtree(fix_copy.parent, ignore_errors=True)
        rows.append({"name": name,
                     "as_cloned": bool(baseline),
                     "status": res["status"],
                     "expected": entry.get("expected"),
                     "n_fixes": (len(res.get("patches", [])) + len(res.get("installed", []))
                                 + (1 if res.get("from_notebook") else 0)),
                     "secs": round(time.time() - t0, 1)})
    return rows


def summarize(rows):
    scored = [r for r in rows if "as_cloned" in r]
    n = len(scored)
    as_cloned = sum(1 for r in scored if r["as_cloned"])
    after = sum(1 for r in scored if r["status"] in ("RAN", "RAN_AS_IS"))
    return {"n_evaluable": n,
            "runs_as_cloned": as_cloned,
            "runs_after_repro_check": after,
            "as_cloned_pct": round(100 * as_cloned / n, 1) if n else 0.0,
            "after_pct": round(100 * after / n, 1) if n else 0.0}


def render_table(rows, summary):
    out = []
    out.append("| repo | as-cloned | after repro-check | fixes | expected |")
    out.append("|------|-----------|-------------------|-------|----------|")
    for r in rows:
        if "as_cloned" in r:
            out.append("| %s | %s | %s | %d | %s |" % (
                r["name"], "RUNS" if r["as_cloned"] else "broken",
                r["status"], r["n_fixes"], r.get("expected") or "-"))
        else:
            out.append("| %s | %s | %s | - | - |" % (r["name"], r["status"], r.get("note", "")))
    s = summary
    out.append("")
    out.append("**%d evaluable repos: %d/%d (%.1f%%) run as-cloned -> %d/%d (%.1f%%) "
               "after repro-check.**" % (
        s["n_evaluable"], s["runs_as_cloned"], s["n_evaluable"], s["as_cloned_pct"],
        s["runs_after_repro_check"], s["n_evaluable"], s["after_pct"]))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Regenerate the repro-check runnability table.")
    ap.add_argument("--manifest", default=str(HERE / "fixtures_manifest.json"),
                    help="path to a corpus manifest JSON (default: bundled fixtures)")
    ap.add_argument("--clone", action="store_true", help="clone URL entries (network+disk)")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)
    manifest = json.loads(Path(args.manifest).read_text())
    # Accept either a bare list, or a {"repos": [...]} wrapper carrying metadata
    # (_about/_source/_count) alongside the repo list.
    if isinstance(manifest, dict):
        manifest = manifest.get("repos", [])
    rows = run(manifest, do_clone=args.clone)
    summary = summarize(rows)
    if args.json:
        print(json.dumps({"rows": rows, "summary": summary}, indent=2))
    else:
        print(render_table(rows, summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
