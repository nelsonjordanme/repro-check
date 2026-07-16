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

## The model: scaffold clears the runway, agent flies the plane

`repro-check` owns the mechanical part and stops honestly at the judgment part:

- **entry-point discovery + sane environment** — finds what to run, sets
  `PYTHONPATH`, headless matplotlib, and the OpenMP duplicate-runtime workaround;
- **a small set of *verified* mechanical fixes** — removed numpy/yaml APIs,
  hardcoded paths, missing PyPI packages, Python-2 syntax, relative-import →
  run-as-module. Deliberately not an ever-growing auto-fix library;
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

v0.2 — reframed from a pattern-library to a **scaffold + hand-off** after a
45-repo ReScience-C study (28% run as-cloned → 37% with verified repair). Engine
validated on a synthetic fixture and demonstrated end-to-end on a real repo. The
verified fix set is kept small on purpose; new traceback→repair patterns are
welcome but only after verification on a real case.

## License

MIT (suggested).
