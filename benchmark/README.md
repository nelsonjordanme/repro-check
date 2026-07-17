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

## The ReScience corpus (populated, ready to run)

`rescience_manifest.json` ships **populated** with 22 real Python replication
repositories, taken from the ReScience journal bibliography
(`code_url` fields where `language = Python`, source:
`github.com/ReScience/rescience.github.io/_bibliography/published.bib`).

```bash
python benchmark/run_benchmark.py --manifest benchmark/rescience_manifest.json --clone --isolate
```

This clones and evaluates every repo, so it needs **network, disk, and build
tools** — heavy scientific installs (neuroscience simulators, etc.) require real
RAM and cannot run in a memory-constrained sandbox. Run it on a laptop, VM, or
CI runner with a few GB free.

**Always pass `--isolate` for numbers you intend to quote.** Without it, every
repo shares one Python environment, so a dependency installed for one repo stays
present and makes later repos' *as-cloned* baseline pass without a fix — the
"as-cloned" count inflates as installs accumulate (this actually happened: a
shared-venv run drifted from 8 to 11 as-cloned purely from leaked packages).
`--isolate` evaluates each repo in its own throwaway venv (created with
`--system-site-packages`, so the 2026 scientific base resolves without being
reinstalled 22 times, but each repo's *new* installs are discarded afterwards),
so the baseline is a true fixed property of the repo. It is slower — one venv per
repo — but it is the only trustworthy setting. Without `--isolate` the harness
still works and never mutates the corpus, but cross-repo as-cloned counts are not
reliable.

Notes on the corpus:

- `expected` is left `null` for each repo — the outcomes are what you're
  *measuring*, so a fresh run fills them in rather than asserting them.
- This is **22 repos from the current bibliography**, not the exact 43-repo set
  quoted in `RUNNABILITY_STUDY.json` at the project root: that earlier study did
  not record its corpus URLs, so its precise membership can't be reconstructed.
  The bibliography is the canonical, reproducible source, so this manifest is
  built from it and will grow as ReScience publishes more Python replications.
- To refresh the list, re-parse `published.bib` (and `under-review.bib`) for
  `language = Python` entries with a `code_url`.
