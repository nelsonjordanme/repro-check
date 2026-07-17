# Changelog

## v0.9.1

Bugfix (found during real-repo benchmark validation):

- A script that exits nonzero but prints nothing to stderr no longer crashes the
  engine (`IndexError` on `splitlines()[-1]`). It now hands off cleanly as a
  GENERIC / NEEDS_AGENT case, with no fabricated error line. Guard added at both
  the main loop and the notebook loop; regression test `test_silent_nonzero_exit`.

## v0.9.0

Coverage + trust: install what the repo declares, be explicit about what a
green result means, and make the runnability numbers reproducible.

- **Install the declared environment first.** When a third-party import is
  missing, repro-check now installs the repo's declared dependencies
  (`requirements.txt`) in one shot before falling back to discovering them one
  failed import at a time. Faster and more faithful to what the authors
  specified.
- **Flagged version-pin relaxation.** A stale exact pin (`numpy==1.16.2`) that
  no longer resolves on a modern interpreter is relaxed to a floor
  (`numpy>=1.16.2`) and re-tried — recorded as a `PIN_RELAXED` patch flagged
  "may change results", because loosening a version is a migration, not a
  silent fix.
- **Explicit reproduction-rung reporting.** A `RAN`/`RAN_AS_IS` result now says
  exactly what it certifies: rung 1 (it runs to completion) — and what it does
  NOT (numeric/scientific correctness, rung 2+). The tool's honesty contract,
  stated in its own output rather than buried in the docs.
- **Re-runnable benchmark harness.** `benchmark/run_benchmark.py` regenerates
  the runnability table (as-cloned vs after-fixes) across a corpus manifest, so
  the quoted percentages are auditable, not a static claim. Runs each repo on
  fresh copies so it never mutates the corpus; ships with a bundled-fixtures
  manifest and a ReScience-C stub for a build-capable machine.
- New regression tests + fixtures for each change.

## v0.8.0

Reach: meet users at their actual moment of need, and fix the most common
"it's a package nobody installed" failure.

- **Run straight from a URL.** `repro-check github.com/owner/repo` (or a full
  `https://` / `git@` URL) now shallow-clones the repo and runs the normal loop
  against it — no manual clone/cd first. Clones use no stored credentials and
  never block on an auth prompt; a private/missing repo, network error, or
  timeout is reported cleanly (exit 1), not a crash. The result records
  `cloned_from`.
- **Install the repo as a package.** When the failing import is the repo's OWN
  package and it ships `pyproject.toml` / `setup.py` / `setup.cfg`, repro-check
  now runs `pip install -e . --no-deps` and re-runs, instead of handing off with
  "package-structure fix needed". This closes the common
  local-package-not-on-PyPI / package-structure / import-name-mismatch cluster.
  `--no-deps` keeps it fast and memory-safe (third-party deps stay on the normal
  install loop), behind the same pre-install memory gate.
- New regression tests for URL detection/clone-failure and the editable-install
  fix.

## v0.7.0

First-run experience and robustness across all three languages.

- **First-run quickstart.** README now leads with a "Try it in 30 seconds"
  before/after (raw `FileNotFoundError` -> `✓ RUNS` with the two auto-fixes)
  plus an honest hand-off example, and a terminal cast in `demo/`.
- **R system-dependency detection.** When a package fails to *build* against a
  missing OS library (GDAL, libxml2, libcurl, ...), the hand-off now names the
  exact system packages to install (`apt-get install -y ...`) instead of a bare
  "install failed" (`R_SYSTEM_DEP`). Covers 19 common packages plus a
  build-log header-hint fallback.
- **Bioconductor version handling.** Refresh BiocManager before install so its
  R->Bioc release map is current, and let `BiocManager::install()` use its
  R-matched release (the correct lockstep-safety) rather than forcing a pin. A
  genuine version-lockstep wall is surfaced honestly (`R_BIOC_VERSION`).
- **Safe dependency installs.** A pre-install memory gate (512 MB floor,
  `REPRO_CHECK_MIN_INSTALL_MB` override; reads psutil / /proc / sysconf /
  macOS vm_stat) and a hard install timeout mean a starved machine gets an
  honest "skipped" instead of an OOM-killed, half-written package.
- **Notebook fidelity.** A notebook last run out of order (non-monotonic
  `execution_count`) is flagged with a caveat that a top-to-bottom run may not
  match the saved outputs, rather than presenting it as a faithful reproduction.
  `nbformat` is now a core dependency.
- CI smoke-tests all three routes (Python, R, notebook); new fixtures and
  regression tests for each change.

## v0.6 — R support
- `attempt_executability` now routes R projects (`.R`/`.Rmd`, or a `DESCRIPTION`,
  with no Python entry point) to an integrated R engine instead of returning an
  out-of-scope `NO_ENTRYPOINT`.
- R engine (`attempt_r_executability`): discovers the `.R`/`.Rmd` entry point,
  runs under `Rscript` (headless graphics, writable user library), classifies
  failures, auto-installs missing CRAN (`install.packages`) and Bioconductor
  (`BiocManager::install`) packages, and re-runs.
- Honest hand-offs for the real R failure modes: `R_MISSING_DATA`,
  `R_INTERACTIVE` (file.choose/choose.dir/readline/menu/View), `R_DEP_INSTALL`
  (system-dep / Bioconductor version failures), `R_FUNCTION_NOT_FOUND`, and
  `R_NOT_AVAILABLE` when no Rscript is found.
- `rc_find_rscript()` locates R via `REPRO_CHECK_RSCRIPT` env var, then PATH,
  then sibling conda envs — so the tool works even when R lives in a separate env.
- `detect_scope` now recognizes `.Rmd`-only repos as R.
- Mixed Python+R repos still route to the Python engine.
- Grounded in an 11-repo biomedical-R feasibility pilot (see `R_PILOT_STUDY.json`
  in the project): most R failures are missing data or environment, not
  source-code rot, so the R fix set is deliberately install-focused.
- `r_runner_pilot.py` is the original standalone pilot, kept for reference.

## v0.5 — notebook support
- Largest `.ipynb` converted to a runnable script (IPython magics / shell
  escapes neutralised) and run through the normal loop.
- Scope-aware `NO_ENTRYPOINT` reasons (notebook / r / no_python).

## v0.3 — CLI-args hand-off + dependency-free Py2 converter
## v0.2 — reframed to scaffold + hand-off (45-repo ReScience-C study)
