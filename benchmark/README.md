# Benchmark — regenerate the runnability table

The numbers repro-check quotes ("X% run as-cloned → Y% after fixes") are not a
static claim: this harness re-runs the tool across a corpus and regenerates the
table, so you can audit them yourself.

```bash
python benchmark/run_benchmark.py                       # bundled fixtures (fast, offline)
python benchmark/run_benchmark.py --json                # machine-readable
python benchmark/run_benchmark.py --manifest my.json --clone   # your own corpus
```

## How it measures

For each repo it takes **two fresh copies** (repro-check patches files in place,
so the corpus is never mutated):

- **as-cloned** — runs the full router with `allow_install=False`; the repo
  counts as as-cloned only if it reaches `RAN_AS_IS` with zero patches and zero
  installs. Language-agnostic (Python / R / notebook judged the same way).
- **after repro-check** — the full loop with installs enabled.

## Manifest format

A JSON list. Each entry is a local `path` or a `url` (cloned only with
`--clone`), plus an optional `expected` status used for a match audit:

```json
[
  {"name": "my repo", "path": "fixtures/example_paper", "expected": "RAN"},
  {"name": "remote",  "url": "github.com/owner/repo",   "expected": "RAN_AS_IS"}
]
```

## The full ReScience-C corpus

`rescience_manifest.example.json` is a stub. The full 43-repo run needs network,
disk, and build tools (heavy scientific installs), so it is meant for a
build-capable machine, not a memory-constrained sandbox. Populate the manifest
with the corpus repo URLs and run with `--clone`.
