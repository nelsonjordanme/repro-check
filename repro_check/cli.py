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
    else:
        print(rk.render_handoff_md(result))

    # Exit code: 0 = runs, 2 = needs a human/agent step, 1 = hard failure.
    return {"RAN": 0, "RAN_AS_IS": 0, "NEEDS_AGENT": 2,
            "NO_ENTRYPOINT": 1, "FAILED_TO_RUN": 1}.get(result["status"], 1)


if __name__ == "__main__":
    sys.exit(main())
