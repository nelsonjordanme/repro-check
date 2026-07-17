# Changelog

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
