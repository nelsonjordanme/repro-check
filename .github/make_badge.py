#!/usr/bin/env python3
"""Emit a shields.io 'endpoint' badge JSON from a repro-check --json result.

Usage:  python make_badge.py repro_check_result.json > badge.json

The badge shows the runnability verdict with an honest label. It certifies
rung 1 (it runs) only — never 'reproducible', which would overclaim. Host the
resulting badge.json anywhere raw-served (a repo branch, Pages, a gist) and
point shields at it:

    https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/OWNER/REPO/BRANCH/badge.json
"""
import json
import sys

# status -> (label suffix, colour). Colours chosen to be honest: green only for
# a genuine run, blue for 'runs after fixes', grey/yellow for an honest hand-off.
STYLE = {
    "RAN_AS_IS":     ("runs as-cloned", "brightgreen"),
    "RAN":           ("runs (after fixes)", "green"),
    "NEEDS_AGENT":   ("needs a human step", "yellow"),
    "NO_ENTRYPOINT": ("no entry point found", "lightgrey"),
    "FAILED_TO_RUN": ("did not run", "red"),
}


def build(result):
    status = result.get("status", "UNKNOWN")
    message, colour = STYLE.get(status, (status.lower(), "lightgrey"))
    return {
        "schemaVersion": 1,
        "label": "repro-check",
        "message": message,
        "color": colour,
    }


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "repro_check_result.json"
    result = json.load(open(path))
    json.dump(build(result), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
