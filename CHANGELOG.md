# Changelog

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
