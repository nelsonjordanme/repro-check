# repro-check

[![self-test](https://github.com/nelsonjordanme/repro-check/actions/workflows/selftest.yml/badge.svg)](https://github.com/nelsonjordanme/repro-check/actions/workflows/selftest.yml)

**A runnability scaffold for reproducing computational papers: it makes old code
run again, and when it can't, hands back exactly where it stopped and what to do
next.** An agent loads it; it is not a standalone app and not a pass/fail judge.

Most published analysis code does not re-run. In a study bundled with this repo
(`RUNNABILITY_STUDY.json`), of 43 ReScience-C Python repositories only
**28% ran to completion as-cloned** under a 2026 environment; verified mechanical
repair raised that to **37%** (a further 9% rescued); the remaining **63%** needed
case-by-case reasoning. Dependency
rot, removed APIs, environment quirks, and entry-point problems dominate — and
fixing them is tedious, unrewarding, and only recently automatable, because it
takes reasoning, not a fixed script.

## Try it in 30 seconds

```bash
pip install git+https://github.com/nelsonjordanme/repro-check.git
```

Point it at a repo checkout. The bundled fixture is a deliberately-broken
analysis (a hardcoded absolute data path + a removed `np.float` API) — exactly
the kind of rot that stops old code:

**Before** — run the script the way the paper intended, and it dies:

```console
$ python analysis.py
FileNotFoundError: [Errno 2] No such file or directory:
    '/home/researcher/project/data/measurements.csv'
```

**After** — point `repro-check` at the same repo:

```console
$ repro-check fixtures/example_paper
✓ RUNS (RAN)  entry: analysis.py
  fix: repoint data path -> measurements.csv
  fix: np.float -> np.float64
```

It found the entry point, repaired both breakages, and proved it runs — exit
code `0`. When a failure instead needs *your* judgment, it stops honestly and
tells you exactly what to do:

```console
$ repro-check path/to/some_repo
# repro-check hand-off — needs agent
**Stopped at rung:** 0 (does not start)
**Entry point:** `run.py`

## Why it stopped
ran, but exited requiring command-line arguments (argparse)

## Suggested next action (CLI_ARGS)
Script needs run-time arguments. Read the repo README/usage for the required
flags, then re-run with them supplied.

## Run-time arguments this script needs
- required `--input` — path to the input CSV
```

Exit code `2` means *needs a human/agent step* (not a crash); `1` means a hard
failure. That hand-off — not a guess — is the point. A terminal cast replaying
this run (synthesized from the tool's real captured output) is in
[`demo/quickstart.cast`](demo/quickstart.cast) (play with
[asciinema](https://asciinema.org): `asciinema play demo/quickstart.cast`).

## The model: scaffold clears the runway, agent flies the plane

`repro-check` owns the mechanical part and stops honestly at the judgment part:

- **entry-point discovery + sane environment** — finds what to run, sets
  `PYTHONPATH`, headless matplotlib, and the OpenMP duplicate-runtime workaround;
- **a small set of *verified* mechanical fixes** — removed numpy/yaml APIs,
  hardcoded paths, missing PyPI packages, Python-2 syntax, relative-import →
  run-as-module. Deliberately not an ever-growing auto-fix library;
- **R projects too (v0.6)** — a repo with no Python but `.R`/`.Rmd` files routes
  to an R engine that runs it under `Rscript`, installs missing CRAN/Bioconductor
  packages, and hands off honestly on missing data, interactive calls, or install
  failures;
- **an honest hand-off at the seam** — when a failure needs judgment, it returns
  a structured note: the stopping rung, what it already changed (a working
  migration diff), the exact traceback, and a *specific keyed next action*
  (`CLI_ARGS`, `MISSING_DATA`, `DEP_INTERNAL`, …). Acting on that note is the point.

It never emits a public "reproducible ✓/✗" badge. It stops at *it runs* and lets
the human judge whether the science is sound.

## Why this shape (what the study taught us)

We started intending an accumulating failure-pattern library as the core asset.
The data corrected us: mechanical patterns are a **small, quickly-exhausted
set**, and the moment one fix lands the *next* blocker is disproportionately a
genuine human/agent step — missing data, a required run-time argument, an in-code
error. So the durable value is the **scaffold + honest hand-off**, not the
library. The pattern set is kept small and verified on purpose.

## Layout

```
repro-check/
├── SKILL.md              # agent-facing model + usage contract
├── kernel.py             # engine: discover_entrypoint, attempt_executability,
│                         #   classify_failure, apply_patch, build_handoff, reproduce
│                         #   + R engine: attempt_r_executability, classify_r_failure
├── test_repro.py         # self-test against the bundled broken fixture
├── RUNNABILITY_STUDY.json# the 43-repo ReScience-C measurement behind the numbers
├── DEMO_end_to_end.json  # worked trace: scaffold → hand-off → agent → runs
└── fixtures/
    └── example_paper/    # a deliberately broken analysis with known ground truth
        ├── analysis.py       #   seeded bugs: hardcoded path, np.float, no seed
        ├── claims.json       #   the published numbers to check against
        └── data/measurements.csv
```

## Quickstart

```bash
python test_repro.py
# PASS — 2 reproduced, 1 near, 0 need review
# repairs: ['PATH_HARDCODED', 'DEP_API_CHANGE']
```

Inside an agent session, load the skill and attempt any repo checkout:

```python
result = attempt_executability("path/to/paper_checkout")
if result["status"] in ("RAN", "RAN_AS_IS"):
    print("runs —", result["patches"])          # what it took
else:
    print(render_handoff_md(result))             # where it stopped + next action
```

Then **act on the hand-off**: do the one judgment step `suggested_next_action`
names (supply a CLI arg, locate a data file, pin a dependency) and re-run. See
`DEMO_end_to_end.json` for a real worked example.

## Scope & honesty

Rung 1 (runnability) is the default job; rung 2 (numeric reproduction) is opt-in
and needs a human-supplied `claims.json`. It does **not** judge whether the
method is correct (rung 3) or robust (rung 4). **Reproducible ≠ correct**: a
running repo means it executes, not that the science is sound.

## Status

v0.7 — **first-run experience + robustness.** A "Try it in 30 seconds"
before/after now leads the README, with a terminal cast in `demo/`. Three
robustness fixes across languages: R build failures caused by a missing OS
library name the exact system packages to install (`R_SYSTEM_DEP`); a genuine
Bioconductor version-lockstep wall is surfaced honestly (`R_BIOC_VERSION`)
rather than looping; dependency installs are gated by an available-memory
check and a hard timeout, so a starved machine gets an honest "skipped" instead
of an OOM-killed half-written package; and a notebook last run out of order is
flagged with a caveat that a top-to-bottom run may not match the saved outputs.

v0.6 — **R support.** A repo with no Python entry point but `.R`/`.Rmd` files (or
a `DESCRIPTION`) now routes automatically to an R engine: it discovers the entry
script, runs it under `Rscript` (headless, writable user library), installs a
missing CRAN or Bioconductor package, and re-runs — the same
run→classify→fix→re-run→hand-off loop as Python. Grounded in an 11-repo
biomedical-R feasibility pilot (`R_PILOT_STUDY.json`): most R failures are
missing data or environment, not source-code rot, so the R fix set is
install-focused (CRAN/Bioconductor) and hands off on missing data, interactive
calls (`file.choose`/`choose.dir`), and install failures. If R is not installed,
the engine returns an honest `R_NOT_AVAILABLE` note. Mixed Python+R repos still
route to Python.

v0.5 — notebook support (largest `.ipynb` converted and run) and scope-aware
`NO_ENTRYPOINT` reasons.

v0.3 — deepened the two hand-off / repair paths that matter most on real repos:

- **Concrete run-time-argument hand-off.** When a repo runs but exits demanding
  command-line arguments (the single most common wall past the first crash), the
  hand-off no longer just says "read the README" — it parses argparse output, the
  entry point's `add_argument` calls, and README examples to emit a **ready-to-run
  suggested command**, the required flags with their help text, and the data files
  in the repo that can fill path arguments.
- **Dependency-free Python-2 `print` converter.** Replaced the old `lib2to3`
  fixer (removed in Python 3.13, which this project's own CI runs) with a
  self-contained converter that rewrites Py2 `print` statements and refuses to
  write syntactically broken source.

v0.2 — reframed from a pattern-library to a **scaffold + hand-off** after a
45-repo ReScience-C study (28% run as-cloned → 37% with verified repair). Engine
validated on a synthetic fixture and demonstrated end-to-end on a real repo. The
verified fix set is kept small on purpose; new traceback→repair patterns are
welcome but only after verification on a real case.

## License

MIT (suggested).
