---
name: repro-check
description: >-
  Get old or broken Python (or R) code running again. Load this when code that
  used to work won't run, an old repo or paper's analysis fails on a modern
  setup, or someone wants to reproduce, replicate, re-run, revive, or just get a
  GitHub repo or paper's code running, or to diagnose why a script errors on
  launch. Handles common breakages verbatim: ModuleNotFoundError / No module
  named, packages that won't install, removed numpy APIs ("no attribute float"),
  yaml.load() missing-Loader, hardcoded paths / FileNotFoundError, OpenMP
  runtime aborts, "attempted relative import", which-file-do-I-run confusion,
  and Python-2 syntax. It finds the entry point, fixes the environment,
  auto-applies verified repairs, re-runs to prove it, and when a failure needs
  judgment hands back a structured note on where it stopped and what to do next.
  Also routes R projects (.R/.Rmd) to an R engine that installs missing
  CRAN/Bioconductor packages and hands off honestly. A runnability scaffold,
  never a pass/fail verdict on the science.
---

# repro-check — a runnability scaffold for reproducing computational papers

## The one-line model

**The scaffold clears the runway; the agent flies the plane.** repro-check does
the mechanical, repetitive work of getting old code to *start* — find the entry
point, fix the environment, apply known cheap repairs — and the moment it hits
something that needs judgment, it **stops and hands you a structured note** with
everything needed to continue. It is not a pattern library that tries to fix
everything, and it is **not** a judge of whether the science is correct.

This design is grounded in a real measurement (see `RUNNABILITY_STUDY.json`): on
43 ReScience-C Python repos, **28% ran to completion as-cloned** under a 2026
stack; verified mechanical repair raised that to **37%** (a further 9% rescued);
the remaining **63%** needed case-by-case reasoning. The lesson that shaped this
tool: mechanical fixes are a
small, quickly-exhausted set, and *the moment one lands the next blocker is
disproportionately a genuine agent/human step* (missing data, run-time args, an
in-code error). So the product is the **honest hand-off at that seam**, not an
ever-growing auto-fix library.

## What it does / does not do

**Does:** discover the entry point in an arbitrary repo checkout — a `.py`
script *or*, when there is none, the largest Jupyter notebook (converted to a
runnable script, IPython magics/shell-escapes neutralised); run it in a sane
environment (own dir on `PYTHONPATH`, headless matplotlib, OpenMP
duplicate-runtime workaround); on failure, classify the traceback and apply a
**verified** mechanical repair if one fits; re-run; and either report it now runs
or emit a hand-off. When a repo has no Python at all, it either RUNS it (an R project — see
"R support" below) or reports *why* it cannot (a data-only repo) instead of a
bare "NO_ENTRYPOINT".

**Does not:** decide whether the analysis is *correct* (rung 3), fabricate a fix
for a novel failure, install a package that is actually the repo's own local
module, or ever emit a public "reproducible ✓/✗" badge on the authors. It stops
at "it runs" and lets the human judge the science.

## Primary API (rung 1 — runnability)

The kernel plugin (`kernel.py`) auto-loads these. The main entry is:

```python
result = attempt_executability(target_dir, max_iters=14, allow_install=True)
```

It loops run → classify → repair → re-run. On success:
`{"status": "RAN"|"RAN_AS_IS", "entrypoint", "patches", "installed", ...}`.
When it stops, it returns a **structured hand-off** (see below). `allow_install`
gates environment mutation (pip-installing a declared dependency) — opt-in and
recorded. Render a hand-off for a human/agent with `render_handoff_md(result)`.

### The hand-off object (the core deliverable)

On any honest stop, `attempt_executability` returns `status="NEEDS_AGENT"` with:

- `stopping_rung` — how far it got (`0 (does not start)` / `1 (advanced but not
  running)`);
- `entrypoint` and `run_as_module` — what to launch and how;
- `already_applied` — the patches and installs it made (a working migration diff);
- `traceback` — the exact tail;
- `next_action_key` + `suggested_next_action` — a specific instruction keyed to
  the failure (`CLI_ARGS`, `MISSING_DATA`, `DEP_MISSING`, `DEP_INTERNAL`,
  `IN_CODE_LOGIC`, `PKG_STRUCTURE`, `INEFFECTIVE`, `GENERIC`);
- `environment` — the interpreter/env vars used.

**Acting on a hand-off is the point.** Read `suggested_next_action`, do that one
judgment step (e.g. `CLI_ARGS` → read the repo README for required flags and
re-run; `MISSING_DATA` → locate the data file; `DEP_INTERNAL` → pin/upgrade the
offending dependency), and the repo typically runs. A worked end-to-end example
is in `DEMO_end_to_end.json` (a real repo: relative-import crash auto-fixed →
stopped at a `--input` wall → agent supplied the documented args → ran to
completion).

## Verified mechanical fixes (the small, deliberate set)

Kept intentionally small — only repairs that are unambiguous and verified:
- **DEP_MISSING** → pip-install the declared distribution (never a local module —
  `looks_local()` refuses that and hands off);
- **DEP_API_CHANGE** → removed numpy aliases (`np.float`→`np.float64`, etc.);
- **NDARRAY_METHOD** → `a.ptp()`→`np.ptp(a)` (ptp only — verified to have a
  top-level equivalent; `itemset`/`newbyteorder` deliberately excluded);
- **YAML_LOADER** → add the now-required `Loader=` to `yaml.load()`;
- **PATH_HARDCODED** → repoint a hardcoded data path to the file in the repo;
- **PY2_SYNTAX** → dependency-free Py2 `print`-statement conversion (no lib2to3, which is gone in 3.13);
- **RELATIVE_IMPORT** → re-run as `python -m pkg.mod`;
- **OpenMP** → `KMP_DUPLICATE_LIB_OK=TRUE` (set for every run; cleared 6/43 crashes).

Add a new one *only* after confirming the fix is unambiguous and verified on a
real case — do not speculatively enumerate patterns. When in doubt, hand off.

## R support (v0.6)

When a repo has **no Python entry point but is an R project** (`.R`/`.Rmd`
files, or a `DESCRIPTION`), `attempt_executability` automatically routes to the
R engine — you call the same function, no separate API. It mirrors the Python
loop: discover the `.R`/`.Rmd` entry point, run it under `Rscript` (headless
graphics, a writable user library for installs), classify any failure, install a
missing package, and re-run.

- **What it auto-fixes:** a missing **CRAN** package (`install.packages`) or a
  missing **Bioconductor** package (`BiocManager::install`, auto-detected from a
  known Bioc name list). These are the mechanical R wins.
- **What it hands off (honestly):** missing data files (`R_MISSING_DATA`),
  interactive/platform-only calls like `file.choose`/`choose.dir`
  (`R_INTERACTIVE`), a build that fails on a **missing system library** — the
  hand-off names the exact OS packages to install (`R_SYSTEM_DEP`), a
  **Bioconductor version-lockstep** wall where the package has no build for the
  release matching your R (`R_BIOC_VERSION`), other install failures
  (`R_DEP_INSTALL`), and unresolved `could not find function`
  (`R_FUNCTION_NOT_FOUND`). Per the R pilot, **most R failures are missing data
  or environment, not source-code rot** — so the R fix set is deliberately
  install-focused, not source-patching.
- **If R is not installed:** the engine returns an honest `R_NOT_AVAILABLE`
  hand-off rather than crashing. It finds `Rscript` via the
  `REPRO_CHECK_RSCRIPT` env var, then `PATH`, then sibling conda envs.
- **Mixed repos** (both `.py` and `.R`) always route to the Python engine.

Hand-offs render through the same `render_handoff_md(result)` and carry
`language: "R"`.

## Robustness (v0.7)

- **Safe dependency installs.** Before any pip/CRAN/Bioc install, a memory gate
  checks available RAM (512 MB floor, override with `REPRO_CHECK_MIN_INSTALL_MB`)
  and installs run under a hard timeout. On a starved machine you get an honest
  "install skipped — low memory" hand-off, never an OOM-killed half-written
  package. Unknown RAM never blocks.
- **Notebook fidelity.** A converted notebook whose cells were last run **out of
  order** (non-monotonic `execution_count`) still runs, but the result carries a
  `notebook_warning` caveat: a top-to-bottom run may not match the saved
  outputs. Don't present an out-of-order notebook as a faithful reproduction.

## Coverage + trust (v0.9)

- **Declared environment first.** On the first missing third-party import,
  repro-check installs the repo's declared deps (`requirements.txt`) in one
  shot before discovering deps one failed import at a time. A stale exact pin
  that no longer resolves is relaxed to a floor (`numpy==1.16.2` →
  `numpy>=1.16.2`) and recorded as a `PIN_RELAXED` patch flagged "may change
  results" — a migration, not a silent fix.
- **Explicit rung in the result.** A `RAN`/`RAN_AS_IS` result carries
  `rung_reached: 1`, `rung_certified`, and `not_verified`, and the CLI prints a
  one-liner: it certifies the code RUNS, not that the science is correct.
- **Re-runnable benchmark.** `benchmark/run_benchmark.py` regenerates the
  runnability table across a corpus manifest (each repo on a fresh copy, never
  mutating the corpus). The quoted percentages are auditable, not a static
  claim.

## The reproduction ladder (know which rung you are on)

1. **Executability** — does it run at all? ← **this tool's job**
2. **Computational reproducibility** — same numbers/figures? (opt-in, below)
3. **Analytical correctness** — is the method right? (human judgment)
4. **Robustness** — does the conclusion survive reasonable changes?

**Reproducible ≠ correct.** A running repo means "it executes," not "the science
is sound." Say so; never let runnability read as endorsement.

## Optional API (rung 2 — numeric reproduction)

When a human supplies expected numbers, check them:

```python
report = reproduce(target_dir, claims_path="claims.json", tol={"rtol":1e-3,"atol":1e-6})
```

Requires an entry script writing `results.json` and a `claims.json`:

```json
{
  "paper": "Author et al. (year) — short title",
  "claims": [
    {"id": "r_xy", "desc": "Pearson r(x,y)", "kind": "deterministic", "value": 0.566},
    {"id": "ci",   "desc": "bootstrap 95% CI", "kind": "stochastic", "value": [0.461, 0.656]}
  ]
}
```
`id` must be a key the analysis writes into `results.json`. `deterministic` is
compared with `np.allclose` at `tol`; `stochastic` is characterised across
`STOCHASTIC_RERUNS` fresh runs and graded REPRODUCED / NEAR / MISMATCH against
the re-run spread. Note: most real repos do **not** ship this contract, so rung 2
is opt-in and author-supplied, not the default path.

## Reporting rules

- State the ladder rung reached and that reproducible ≠ correct.
- Report the migration path (`already_applied`) as a first-class result.
- On a hand-off, surface `suggested_next_action` — don't just say "it failed."
- A `NEAR_STOCHASTIC` claim is *not* a failure — name the likely cause (usually a
  missing seed) and recommend the fix.
- Never phrase output as a public verdict on the authors.
