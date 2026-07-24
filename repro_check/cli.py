#!/usr/bin/env python3
"""repro-check — standalone runnability scaffold (no agent required).

Point it at a checkout of a computational paper's code. It discovers the entry
point, sets up a sane environment, applies a small set of verified mechanical
fixes, and either reports the repo now runs or prints a structured hand-off
telling you exactly where it stopped and what to do next.

    python repro_check_cli.py path/to/repo
    python repro_check_cli.py path/to/repo --no-install --json

It deliberately stops at "does it run" — it does NOT judge whether the science
is correct. See SKILL.md / README.md for the full model.
"""
import argparse, json, sys
from pathlib import Path

# The engine lives in engine.py next to this file.
from . import engine as rk


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Make an old computational repo run again, or say exactly why it can't.")
    ap.add_argument("target", help="path to the repo checkout, or a git repo URL "
                    "(github.com/owner/repo, https://…, or git@…) to clone and run")
    ap.add_argument("--no-install", action="store_true",
                    help="do not pip-install missing declared dependencies")
    ap.add_argument("--max-iters", type=int, default=14,
                    help="max repair/re-run iterations (default 14)")
    ap.add_argument("--json", action="store_true",
                    help="emit the full result as JSON instead of a human report")
    ap.add_argument("--ai-suggest", action="store_true",
                    help="on a hand-off, ask an LLM to draft a suggested fix using "
                         "YOUR OWN API key (ANTHROPIC_API_KEY or OPENAI_API_KEY). "
                         "Off by default; the suggestion is flagged, never applied, "
                         "and never counted as a fix. No key set = feature unavailable.")
    args = ap.parse_args(argv)

    cloned_from = None
    if rk.rc_looks_like_url(args.target):
        target, info = rk.rc_clone_repo(args.target)
        if target is None:
            msg = {"status": "CLONE_FAILED", "target": args.target,
                   "reason": info.get("error"), "kind": info.get("kind")}
            if args.json:
                print(json.dumps(msg, indent=2))
            else:
                print(f"✗ could not clone {args.target}")
                print(f"  {info.get('kind')}: {info.get('error')}")
                if info.get("kind") == "AUTH":
                    print("  (private repo? this tool clones with no stored credentials)")
            return 1
        cloned_from = info["url"]
    else:
        target = Path(args.target).expanduser().resolve()
        if not target.is_dir():
            ap.error(f"{target} is not a directory")

    result = rk.attempt_executability(target, max_iters=args.max_iters,
                                      allow_install=not args.no_install)
    if cloned_from:
        result["cloned_from"] = cloned_from

    # Opt-in AI suggestion: only on a hand-off, only with the user's own key.
    # Attached under a separate `ai_suggestion` field — it never touches
    # patches/installed or the status, so the runnability verdict is unaffected.
    if args.ai_suggest and result.get("status") == "NEEDS_AGENT":
        result["ai_suggestion"] = rk.rc_ai_suggest(result, target)

    if args.json:
        print(json.dumps(result, indent=2))
    elif result["status"] in ("RAN", "RAN_AS_IS"):
        if cloned_from:
            print(f"cloned: {cloned_from}")
        print(f"✓ RUNS ({result['status']})  entry: {result.get('entrypoint')}")
        for p in result.get("patches", []):
            print(f"  fix: {p.get('change')}")
        for d in result.get("installed", []):
            print(f"  installed: {d['pkg']}")
        if result.get("from_notebook"):
            print(f"  note: ran converted notebook {result['from_notebook']} in document order")
        if result.get("notebook_warning"):
            print(f"  ⚠ caveat: {result['notebook_warning']}")
        # Honesty contract: say exactly what a green result certifies, and what
        # it does NOT. RAN means rung 1 (it runs) — not that the science is right.
        if result.get("rung_certified"):
            print(f"  rung: certifies {result['rung_certified']}; "
                  f"does NOT verify {result.get('not_verified', 'scientific correctness')}")
    else:
        print(rk.render_handoff_md(result))
        ai = result.get("ai_suggestion")
        if ai is not None:
            print()
            if ai.get("available"):
                print(f"## AI suggestion ({ai['provider']} / {ai['model']})")
                print(f"_{ai['disclaimer']}_\n")
                print(ai["suggestion"])
            else:
                print(f"## AI suggestion unavailable\n{ai.get('reason')}")

    # Exit code: 0 = runs, 2 = needs a human/agent step, 1 = hard failure.
    return {"RAN": 0, "RAN_AS_IS": 0, "NEEDS_AGENT": 2,
            "NO_ENTRYPOINT": 1, "FAILED_TO_RUN": 1}.get(result["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
