#!/usr/bin/env python3
"""Post-process a repro-check --json result inside the GitHub Action.

Reads the JSON result and the CLI exit code, then:
  - writes `status` and `json_path` to $GITHUB_OUTPUT,
  - renders a readable panel to $GITHUB_STEP_SUMMARY,
  - emits a ::notice/::warning/::error annotation,
  - re-exits with the CLI's own exit code (honouring fail-on-handoff).

Kept as a real script (not inline shell) so there are no nested-heredoc or
quoting hazards, and so it is unit-testable on its own.
"""
import json
import os
import sys


def main():
    result_path = sys.argv[1] if len(sys.argv) > 1 else "repro_check_result.json"
    code = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    fail_on_handoff = os.environ.get("RC_FAIL_ON_HANDOFF", "true") == "true"

    try:
        d = json.load(open(result_path))
    except Exception as ex:  # malformed / missing result — surface, don't mask
        print(f"::error::repro-check produced no readable JSON result ({ex})")
        return code or 1

    status = d.get("status", "UNKNOWN")
    entry = d.get("entrypoint") or "-"

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"status={status}\n")
            f.write(f"json_path={result_path}\n")

    lines = ["### repro-check runnability", "",
             "| field | value |", "|---|---|",
             f"| status | `{status}` |",
             f"| entry point | `{entry}` |",
             f"| exit code | `{code}` |"]
    patches = d.get("patches", [])
    installed = d.get("installed", [])
    if patches or installed:
        lines += ["", "**Fixes applied:**", ""]
        lines += [f"- {p.get('change', p.get('pattern'))}" for p in patches]
        lines += [f"- installed `{i.get('pkg')}`" for i in installed]
    if status not in ("RAN", "RAN_AS_IS"):
        na = d.get("next_action") or {}
        reason = na.get("reason") or d.get("reason") or "needs a human step"
        lines += ["", f"**Hand-off:** {reason}"]
    if d.get("rung_certified"):
        lines += ["", f"> Certifies **{d['rung_certified']}** — does NOT verify "
                  f"{d.get('not_verified', 'scientific correctness')}."]
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write("\n".join(lines) + "\n")

    if code == 0:
        print(f"::notice::repro-check: repo runs ({status})")
        return 0
    if code == 2:
        print(f"::warning::repro-check: hand-off ({status}) — needs a human step")
        return 2 if fail_on_handoff else 0
    print(f"::error::repro-check: {status} (exit {code})")
    return code


if __name__ == "__main__":
    sys.exit(main())
