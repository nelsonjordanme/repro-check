"""
repro-check — reproduction-attempt engine (kernel helpers).

Loaded automatically when the `repro-check` skill is loaded. Provides a FIXED,
auditable protocol for attempting to reproduce a computational paper's numeric
claims from its code + data, and for reporting *exactly where and how it broke*.

Design contract (see SKILL.md):
  - This is an ASSISTANT, not an oracle. It never issues a public pass/fail
    grade. It emits a structured report: what ran, what was patched to make it
    run, which claims matched within tolerance, and which need a human.
  - The engine handles KNOWN failure patterns (the library below). Novel
    failures are handed back to the agent with the traceback — that is the
    scaffold/agent seam, and it is deliberate.
  - Environment rot is treated as PRODUCT: every patch is recorded as a
    migration step, so the output is a working diff, not just a verdict.

No top-level classes/decorators (kernel-plugin constraint) — state is plain
dicts, behaviour is plain functions.
"""
import json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

PROTOCOL_VERSION = "0.1"

# Default numeric tolerances for the comparison step. Deterministic claims must
# match to ~3 significant figures; callers may override per target.
DEFAULT_TOL = {"rtol": 1e-3, "atol": 1e-6}

# Number of fresh re-runs used to characterise a stochastic claim.
STOCHASTIC_RERUNS = 5

# --- Failure-pattern library ------------------------------------------------
# Grounded in the documented top causes of non-reproducibility: dependency /
# environment issues and API rot are the dominant failure mode for published
# analysis code. Each pattern maps a traceback signature to a repair strategy.
# Grow this dict as new patterns are seen — it is the accumulating asset.

# numpy removed these aliases in >=1.24 / 2.0; a large body of older code breaks.
API_RENAMES = {
    "np.float": "np.float64", "np.int": "np.int64", "np.bool": "np.bool_",
    "np.object": "object", "np.str": "str", "np.long": "np.int64",
}

# Import name -> pip distribution name, for the cases where they differ. The
# dominant real-world failure (78% of the ReScience Python batch) is simply a
# missing package, so restoring runnability means (re)installing declared deps.
# numpy 2.0 removed ndarray.ptp(); it still exists as np.ptp(arr, ...), so the
# uniform a.ptp(...) -> np.ptp(a, ...) rewrite is valid. Only patterns with a
# TRUE top-level np.<name> equivalent belong here. Deliberately NOT included:
# ndarray.itemset (replacement is indexing assignment arr[i]=v, no np.itemset)
# and ndarray.newbyteorder (replacement is arr.dtype.newbyteorder(), no np.
# newbyteorder) — a uniform rewrite would emit calls to functions that do not
# exist. Add a method here only after confirming np.<name>(arr, ...) works.
NDARRAY_METHODS_REMOVED = {"ptp"}

IMPORT_TO_PKG = {
    "yaml": "pyyaml", "cv2": "opencv-python", "sklearn": "scikit-learn",
    "PIL": "pillow", "skimage": "scikit-image", "pyDOE": "pyDOE2",
    "Bio": "biopython", "bs4": "beautifulsoup4", "OpenGL": "pyopengl",
    "yaml_include": "pyyaml-include",
}


def classify_failure(stderr: str) -> dict:
    """Map a traceback to {pattern, detail} or {'pattern': 'UNKNOWN'}."""
    last = stderr.strip().splitlines()[-1] if stderr.strip() else ""
    m = re.search(r"No such file or directory: '([^']+)'", stderr)
    if "FileNotFoundError" in stderr and m:
        return {"pattern": "PATH_HARDCODED", "missing_path": m.group(1), "line": last}
    m = re.search(r"module 'numpy' has no attribute '(\w+)'", stderr)
    if m and f"np.{m.group(1)}" in API_RENAMES:
        return {"pattern": "DEP_API_CHANGE", "symbol": f"np.{m.group(1)}", "line": last}
    if "attempted relative import with no known parent package" in stderr:
        return {"pattern": "RELATIVE_IMPORT", "line": last}
    m = re.search(r"No module named '([\w\.]+)'", stderr)
    if m:
        return {"pattern": "DEP_MISSING", "module": m.group(1), "line": last}
    m = re.search(r"name '(\w+)' is not defined", stderr)
    if m:
        return {"pattern": "EXEC_ORDER", "symbol": m.group(1), "line": last}
    # numpy 2.0 removed several ndarray METHODS that still exist as np functions
    # (e.g. a.ptp() -> np.ptp(a)). Distinct from DEP_API_CHANGE (top-level aliases).
    m = re.search(r"type object 'numpy\.ndarray' has no attribute '(\w+)'", stderr)
    if m and m.group(1) in NDARRAY_METHODS_REMOVED:
        mf = re.search(r'File "([^"]+)", line', stderr)
        return {"pattern": "NDARRAY_METHOD", "method": m.group(1),
                "file": (mf.group(1) if mf else None), "line": last}
    # PyYAML >=5.1 made the Loader argument mandatory for yaml.load().
    if re.search(r"load\(\) missing \d+ required positional argument.*Loader", stderr) \
       or "you need to specify a Loader" in stderr.lower():
        return {"pattern": "YAML_LOADER", "line": last}
    # Python-2-only print statement fails at parse time under Python 3.
    if "Missing parentheses in call to 'print'" in stderr \
       or "Missing parentheses in call to 'exec'" in stderr:
        mf = re.search(r'File "([^"]+)", line', stderr)
        return {"pattern": "PY2_SYNTAX", "file": (mf.group(1) if mf else None), "line": last}
    return {"pattern": "UNKNOWN", "line": last}


def rc_locate(target_dir: Path, basename: str):
    hits = list(Path(target_dir).rglob(basename))
    return hits[0] if hits else None


def apply_patch(src: str, diagnosis: dict, target_dir: Path):
    """Return (new_src, patch_record) for a KNOWN pattern, or (None, reason)."""
    p = diagnosis["pattern"]
    if p == "PATH_HARDCODED":
        found = rc_locate(target_dir, Path(diagnosis["missing_path"]).name)
        if not found:
            return None, "data file not found under target"
        new = src.replace(diagnosis["missing_path"], str(found.resolve()))
        return new, {"pattern": p, "change": f"repoint data path -> {found.name}",
                     "from": diagnosis["missing_path"], "to": str(found.resolve())}
    if p == "DEP_API_CHANGE":
        sym = diagnosis["symbol"]; repl = API_RENAMES[sym]
        new = re.sub(re.escape(sym) + r"(?![\w])", repl, src)
        return new, {"pattern": p, "change": f"{sym} -> {repl}"}
    if p == "NDARRAY_METHOD":
        meth = diagnosis["method"]; tgt = diagnosis.get("file")
        if not tgt or not Path(tgt).exists():
            return None, "ndarray method removed but source file not locatable"
        code = Path(tgt).read_text()
        # Rewrite <receiver>.<meth>(<args>) -> np.<meth>(<receiver>, <args>).
        # Conservative: receiver is a dotted/indexed identifier chain, no nested
        # parens, so we don't mis-handle chained calls. Others -> hand to agent.
        rx = re.compile(r"(?<![\w.])([A-Za-z_][\w\.\[\]']*)\." + meth + r"\(([^()]*)\)")
        def _sub(m):
            recv, args = m.group(1), m.group(2).strip()
            return f"np.{meth}({recv}" + (f", {args})" if args else ")")
        new_code, n = rx.subn(_sub, code)
        if n == 0:
            return None, f"could not safely rewrite .{meth}() (complex receiver) — hand to agent"
        if "import numpy" not in new_code and "np." in new_code:
            new_code = "import numpy as np\n" + new_code
        Path(tgt).write_text(new_code)
        return src, {"pattern": p, "change": f"a.{meth}(...) -> np.{meth}(a, ...) x{n} in {Path(tgt).name}",
                     "file": tgt, "side_effect": True}
    if p == "YAML_LOADER":
        # Add the now-required Loader= to bare yaml.load(<one-arg>) calls.
        pat = re.compile(r"yaml\.load\(\s*([^,()]+?)\s*\)")
        n = len(pat.findall(src))
        if n == 0:
            return None, "yaml.load() call not found in this file (may be indirect)"
        new = pat.sub(r"yaml.load(\1, Loader=yaml.SafeLoader)", src)
        return new, {"pattern": p, "change": f"add Loader=yaml.SafeLoader to {n} yaml.load() call(s)"}
    if p == "PY2_SYNTAX":
        # Targeted Python-2 -> 3 conversion, print/exec fixers only. This is a
        # syntactic (not scientific) transform, applied to the FILE named in the
        # traceback rather than the passed entry-point source.
        import lib2to3.refactor as _rt
        tgt = diagnosis.get("file")
        if not tgt or not Path(tgt).exists():
            return None, "py2 syntax error but source file not locatable"
        code = Path(tgt).read_text()
        rtool = _rt.RefactoringTool(["lib2to3.fixes.fix_print",
                                     "lib2to3.fixes.fix_exec"])
        try:
            fixed = str(rtool.refactor_string(code if code.endswith("\n") else code + "\n",
                                              "target"))
        except Exception as e:
            return None, f"2to3 print/exec conversion failed: {type(e).__name__}"
        if fixed == code:
            return None, "no print/exec statements converted (different py2 issue)"
        Path(tgt).write_text(fixed)   # patch the actual failing file in place
        return src, {"pattern": p, "change": f"2to3 print/exec on {Path(tgt).name}",
                     "file": tgt, "side_effect": True}
    return None, f"pattern {p} has no automated repair (hand to agent)"


def rc_module_spec(script: Path, target_dir: Path):
    """If `script` sits inside a package (has __init__.py up the chain to a root
    under target_dir), return (root_dir, 'pkg.sub.mod') for `python -m` execution;
    else (None, None). Used to recover from relative-import-without-parent."""
    script = Path(script).resolve(); target_dir = Path(target_dir).resolve()
    if not (script.parent / "__init__.py").exists():
        return None, None
    parts = [script.stem]; d = script.parent
    while (d / "__init__.py").exists() and d != target_dir and d.parent != d:
        parts.insert(0, d.name); d = d.parent
    return d, ".".join(parts)


def run_script(script: Path, workdir: Path, as_module: str = None,
               module_root: Path = None) -> dict:
    """Run a script in workdir; capture status/stdout/stderr and results.json.

    Default: run the file directly with its own dir on PYTHONPATH (resolves the
    common `import params` sibling pattern). If as_module is given, run
    `python -m as_module` from module_root instead — this recovers packages that
    use relative imports (`from .x import y`) and fail when run as a script.
    MPLBACKEND=Agg forces headless matplotlib; KMP_DUPLICATE_LIB_OK neutralises
    the OpenMP duplicate-runtime abort.
    """
    script = Path(script)
    cwd = Path(module_root) if as_module else script.parent
    env = {**os.environ, "PYTHONPATH": str(cwd), "MPLBACKEND": "Agg",
           "KMP_DUPLICATE_LIB_OK": "TRUE"}
    cmd = [sys.executable, "-m", as_module] if as_module else [sys.executable, str(script)]
    res = subprocess.run(cmd, cwd=str(cwd),
                         capture_output=True, text=True, timeout=600, env=env)
    out = {"returncode": res.returncode, "stdout": res.stdout, "stderr": res.stderr}
    rj = cwd / "results.json"
    if res.returncode == 0 and rj.exists():
        try:
            out["results"] = json.loads(rj.read_text())
        except Exception:
            out["results"] = None
    return out


# Entry-point names tried in priority order when no explicit script is given.
ENTRYPOINT_NAMES = ["analysis.py", "main.py", "run.py", "reproduce.py",
                    "run_all.py", "experiment.py", "figures.py"]


def discover_entrypoint(target_dir):
    """Find the most likely entry script in a repo checkout.

    Strategy: prefer a known entry-point NAME; among matches prefer shallower
    paths and a `code/`/`src/` location; fall back to the top-level .py with the
    most `if __name__ == '__main__'` / plotting signal. Returns a Path or None.
    """
    target_dir = Path(target_dir)
    pys = [p for p in target_dir.rglob("*.py")
           if not any(seg in {".git", "__pycache__", "article", "figures", "paper"}
                      for seg in p.relative_to(target_dir).parts[:-1])]
    if not pys:
        return None

    def score(p):
        rel = p.relative_to(target_dir)
        name_rank = (ENTRYPOINT_NAMES.index(p.name)
                     if p.name in ENTRYPOINT_NAMES else len(ENTRYPOINT_NAMES))
        in_code = 0 if any(s in {"code", "src", "scripts"} for s in rel.parts[:-1]) else 1
        try:
            txt = p.read_text(errors="ignore")
        except Exception:
            txt = ""
        has_main = 0 if "__main__" in txt else 1
        return (name_rank, in_code, has_main, len(rel.parts), len(rel.as_posix()))

    return sorted(pys, key=score)[0]


def looks_local(module, target_dir):
    """True if `module` is the repo's OWN code (a sibling .py file or package
    dir under target_dir), not a third-party dependency. Such modules must NOT
    be pip-installed — a failing local import means a package-structure / working
    -directory problem for the agent, and blindly `pip install <name>` on a
    local name is exactly how a typo or a malicious lookalike package would get
    pulled in. This is the real boundary the erfit case only accidentally hit.
    """
    top = module.split(".")[0]
    target_dir = Path(target_dir)
    for base in {target_dir, *[p.parent for p in target_dir.rglob("*.py")]}:
        if (base / f"{top}.py").exists() or (base / top / "__init__.py").exists():
            return True
    return False


def pip_install(module, timeout=300):
    """Install the pip distribution for an import name. Returns (ok, pkg, log)."""
    pkg = IMPORT_TO_PKG.get(module, module)
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", pkg],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0, pkg, (r.stderr or r.stdout).strip()[-300:]


def attempt_executability(target_dir, max_iters=10, allow_install=False):
    """Rung-1 check: discover the entry point and try to make it RUN under the
    current environment, auto-repairing known failures. No claims needed.

    allow_install: if True, a missing top-level package is pip-installed (env
    mutation — opt in explicitly) and recorded as a setup step. This targets the
    dominant real-world failure (missing dependency). Local/relative-import
    modules and packages that are not on PyPI are NOT installed — they go to the
    agent instead.

    Returns {status, entrypoint, patches, installed, attempts, [traceback]}.
    status: RAN | RAN_AS_IS | FAILED_TO_RUN | NEEDS_AGENT | NO_ENTRYPOINT.
    """
    target_dir = Path(target_dir).resolve()
    ep = discover_entrypoint(target_dir)
    if ep is None:
        return {"status": "NO_ENTRYPOINT", "target": str(target_dir)}

    patches, installed, attempts = [], [], []
    prev_err, tried_install = None, set()
    module_mode = None; module_root = None   # set when we recover via `python -m`
    for i in range(max_iters):
        r = run_script(ep, target_dir, as_module=module_mode, module_root=module_root)
        err_line = (r["stderr"].strip().splitlines()[-1] if r["returncode"] else None)
        attempts.append({"iter": i, "returncode": r["returncode"], "error": err_line,
                         **({"mode": f"-m {module_mode}"} if module_mode else {})})
        if r["returncode"] == 0:
            return {"status": "RAN" if (patches or installed) else "RAN_AS_IS",
                    "entrypoint": str(ep.relative_to(target_dir)),
                    "patches": patches, "installed": installed, "attempts": attempts,
                    **({"run_as_module": module_mode} if module_mode else {})}

        diag = classify_failure(r["stderr"])

        # RELATIVE_IMPORT: the entry file uses `from .x import y` but was run as a
        # script. Re-run it as a module (`python -m pkg.mod`) from the package
        # root. Structural fix, no source edit. Only attempt once.
        if diag["pattern"] == "RELATIVE_IMPORT" and module_mode is None:
            root, spec = rc_module_spec(ep, target_dir)
            if spec:
                module_mode, module_root = spec, root
                patches.append({"pattern": "RELATIVE_IMPORT",
                                "change": f"run as module: python -m {spec}"})
                prev_err = None
                continue
            return build_handoff(target_dir, ep, patches, installed, attempts, diag=diag,
                    reason="relative import but entry file is not inside a package",
                    traceback=r["stderr"], module_mode=module_mode)

        # DEP_MISSING: (re)install the declared dependency if permitted.
        if diag["pattern"] == "DEP_MISSING" and allow_install:
            # A submodule import (e.g. `skimage.filters`) failing usually means
            # the top-level package is absent, so install the top-level name.
            top = diag["module"].split(".")[0]
            # Boundary: never pip-install the repo's own module. A failing local
            # import is a package-structure problem for the agent, not a missing
            # dependency — and installing a local name risks pulling a lookalike.
            if looks_local(diag["module"], target_dir):
                return build_handoff(target_dir, ep, patches, installed, attempts, diag=diag,
                        reason=f"'{top}' is a local module (found in repo), not a PyPI dependency "
                               f"— install refused; package-structure fix needed",
                        traceback=r["stderr"], module_mode=module_mode)
            if top in tried_install:
                return build_handoff(target_dir, ep, patches, installed, attempts, diag=diag,
                        reason=f"install of {top} did not resolve import",
                        traceback=r["stderr"], module_mode=module_mode)
            ok, pkg, log = pip_install(top)
            tried_install.add(top)
            if not ok:
                return build_handoff(target_dir, ep, patches, installed, attempts, diag=diag,
                        reason=f"pip install {pkg} failed", traceback=r["stderr"],
                        module_mode=module_mode)
            installed.append({"import": top, "pkg": pkg})
            prev_err = None  # environment changed; a repeated error is now meaningful
            continue

        # No-progress guard: an unchanged error after a source patch means the
        # repair was ineffective (real bug: ramp-metering looped 8x on a path).
        if patches and err_line == prev_err:
            return build_handoff(target_dir, ep, patches, installed, attempts,
                                 diag=diag, reason="last patch made no progress (ineffective repair)",
                                 traceback=r["stderr"], module_mode=module_mode)
        prev_err = err_line

        if diag["pattern"] in ("UNKNOWN", "DEP_MISSING"):
            return build_handoff(target_dir, ep, patches, installed, attempts,
                                 diag=diag, reason=(diag.get("reason") if isinstance(diag, dict) else None),
                                 traceback=r["stderr"], module_mode=module_mode)
        new_src, rec = apply_patch(ep.read_text(), diag, target_dir)
        if new_src is None:
            return build_handoff(target_dir, ep, patches, installed, attempts,
                                 diag=diag, reason=rec, traceback=r["stderr"], module_mode=module_mode)
        # A side-effect patch already edited a file on disk (e.g. 2to3 on the
        # file named in the traceback, which may not be the entry point).
        if isinstance(rec, dict) and rec.get("side_effect"):
            patches.append(rec)
            continue
        if new_src == ep.read_text():
            return build_handoff(target_dir, ep, patches, installed, attempts,
                                 diag=diag, reason="patch was a no-op", traceback=r["stderr"],
                                 module_mode=module_mode)
        ep.write_text(new_src)
        patches.append(rec)
    return build_handoff(target_dir, ep, patches, installed, attempts,
                         reason=f"still failing after {max_iters} repair iterations",
                         traceback=(attempts[-1].get("error") if attempts else ""),
                         module_mode=module_mode)


# --- Agent hand-off -------------------------------------------------------
# When the scaffold stops, it does not fail silently or emit a bare status. It
# produces a structured hand-off so the loading agent can continue: what rung it
# reached, the exact traceback, what it already changed, the environment it used,
# and a specific next action keyed to the failure. This is the "agent flies the
# plane" half of the design made concrete.

NEXT_ACTION = {
    "CLI_ARGS":       "Script needs run-time arguments. Read the repo README/usage for the "
                      "required flags, then re-run with them supplied.",
    "MISSING_DATA":   "A data file the code expects is absent from the repo. Locate it (README, "
                      "data DOI, external download) and place it where the code looks.",
    "DEP_MISSING":    "A third-party package is missing and could not be auto-installed. Check the "
                      "repo's requirements for the exact distribution/version and install it.",
    "DEP_INTERNAL":   "The error is raised INSIDE a dependency (not the repo's code) under a newer "
                      "runtime. Pin/upgrade that dependency, or patch it in the environment.",
    "IN_CODE_LOGIC":  "A genuine error in the analysis code (shape/value/index). This is a "
                      "substantive fix — read the surrounding logic; not a plumbing repair.",
    "PKG_STRUCTURE":  "A package-structure/import problem the run-as-module fallback didn't resolve. "
                      "Inspect the package layout and how the entry point is meant to be launched.",
    "INEFFECTIVE":    "An auto-patch stopped making progress. Inspect the traceback; the assumed "
                      "fix does not apply here.",
    "GENERIC":        "Novel failure outside the known pattern set. Read the traceback and repair "
                      "directly, then re-run.",
}


def rc_next_action_key(diag, reason, traceback):
    tb = (traceback or "") + " " + (reason or "")
    if re.search(r"arguments are required|error: argument|SystemExit: 2", tb): return "CLI_ARGS"
    if re.search(r"FileNotFoundError|No such file", tb) or "data file" in (reason or ""): return "MISSING_DATA"
    if "local module" in (reason or "") or "package-structure" in (reason or ""): return "PKG_STRUCTURE"
    if "site-packages" in tb and "AttributeError" in tb: return "DEP_INTERNAL"
    if (diag or {}).get("pattern") == "DEP_MISSING" or "pip install" in (reason or ""): return "DEP_MISSING"
    if re.search(r"ValueError|IndexError|KeyError|ShapeError|not enough values", tb): return "IN_CODE_LOGIC"
    if "no progress" in (reason or "") or "no-op" in (reason or ""): return "INEFFECTIVE"
    return "GENERIC"


def build_handoff(target_dir, ep, patches, installed, attempts, *, diag=None,
                  reason=None, traceback=None, module_mode=None):
    """Assemble the structured agent hand-off returned on every NEEDS_AGENT stop."""
    key = rc_next_action_key(diag, reason, traceback)
    # rung reached: 0 = didn't run at all, 1 = advanced but not to completion
    ran_any = any(a["returncode"] == 0 for a in attempts)
    advanced = len(attempts) > 1 or patches or installed
    return {
        "status": "NEEDS_AGENT",
        "stopping_rung": "1 (executability) — advanced but not running" if advanced
                         else "0 (does not start)",
        "entrypoint": str(Path(ep).relative_to(target_dir)),
        "run_as_module": module_mode,
        "already_applied": {
            "patches": [p.get("change") for p in patches],
            "installed": [d["pkg"] for d in installed],
        },
        "diagnosis": diag,
        "reason": reason,
        "traceback": (traceback or "")[-1500:],
        "environment": {"python": sys.version.split()[0],
                        "MPLBACKEND": "Agg", "KMP_DUPLICATE_LIB_OK": "TRUE"},
        "next_action_key": key,
        "suggested_next_action": NEXT_ACTION[key],
        "attempts": attempts,
    }


def render_handoff_md(h):
    """Human/agent-readable hand-off note (used in the report and live sessions)."""
    if h["status"] != "NEEDS_AGENT":
        return f"# repro-check: {h['status']}"
    L = ["# repro-check hand-off — needs agent",
         f"**Stopped at rung:** {h['stopping_rung']}",
         f"**Entry point:** `{h['entrypoint']}`"
         + (f"  (run as `python -m {h['run_as_module']}`)" if h.get("run_as_module") else ""),
         "",
         "## Already applied automatically",
         *( [f"- patch: {c}" for c in h["already_applied"]["patches"]]
            + [f"- installed: {p}" for p in h["already_applied"]["installed"]]
            or ["- (nothing — failed on first run)"]),
         "",
         f"## Why it stopped\n{h.get('reason') or (h.get('diagnosis') or {}).get('pattern','')}",
         "",
         f"## Suggested next action ({h['next_action_key']})\n{h['suggested_next_action']}",
         "",
         "## Traceback (tail)", "```", h["traceback"].strip()[-1000:], "```"]
    return "\n".join(L)


def compare_value(claimed, observed, kind, tol=None):
    """Compare one claim to its reproduced value; return a status dict."""
    if tol is None:
        tol = DEFAULT_TOL
    if observed is None:
        return {"status": "UNAVAILABLE", "note": "not produced by run"}
    if kind == "deterministic":
        import numpy as np
        ok = np.allclose(np.asarray(claimed, float), np.asarray(observed, float),
                         rtol=tol["rtol"], atol=tol["atol"])
        return {"status": "REPRODUCED" if ok else "MISMATCH",
                "claimed": claimed, "observed": observed}
    return {"status": "STOCHASTIC", "claimed": claimed, "observed": observed}


def characterize_stochastic(script, workdir, claim_id, claimed, n=None,
                            band=0.05):
    """Re-run n times and test the claimed value against the re-run spread.

    A min/max band over few re-runs UNDER-estimates the true spread, so exact
    containment is too brittle for stochastic claims (this was the lesson the
    fixture surfaced). We grade three ways:
      REPRODUCED_STOCHASTIC : claim inside the observed [min,max]
      NEAR_STOCHASTIC       : claim within `band` (relative) of the range — a
                              seed/resample difference, not a discrepancy
      MISMATCH_STOCHASTIC   : outside even the widened band — needs a human
    """
    import numpy as np
    if n is None:
        n = STOCHASTIC_RERUNS
    vals = []
    for _ in range(n):
        r = run_script(script, workdir)
        if r["returncode"] == 0:
            vals.append(r["results"][claim_id])
    arr = np.asarray(vals, float)                      # shape (n, ...)
    lo, hi = arr.min(axis=0), arr.max(axis=0)
    cl = np.asarray(claimed, float)
    scale = np.maximum(np.abs(hi - lo), np.abs(cl))    # relative widening
    pad = band * np.where(scale > 0, scale, 1.0)
    inside = bool(np.all(cl >= lo - 1e-9) and np.all(cl <= hi + 1e-9))
    near = bool(np.all(cl >= lo - pad) and np.all(cl <= hi + pad))
    status = ("REPRODUCED_STOCHASTIC" if inside
              else "NEAR_STOCHASTIC" if near else "MISMATCH_STOCHASTIC")
    note = ("claim within re-run spread" if inside else
            "claim within {:.0%} of re-run spread — consistent with a missing "
            "random seed, not a discrepancy".format(band) if near else
            "claim outside re-run spread even widened — needs human review")
    return {"status": status, "claimed": claimed,
            "observed_range": [lo.tolist(), hi.tolist()], "n_reruns": len(vals),
            "note": note + "; source ran WITHOUT a seed — recommend authors pin one"}


def reproduce(target_dir, claims_path=None, max_iters=6, tol=None):
    """Full protocol. Returns the structured report dict (also written to disk).

    target_dir : path to the paper's code+data (a repo checkout).
    claims_path: JSON of claimed numeric results; defaults to <target>/claims.json.
    """
    import numpy as np, pandas as pd, scipy
    if tol is None:
        tol = DEFAULT_TOL
    target_dir = Path(target_dir).resolve()
    claims = json.loads(Path(claims_path or target_dir / "claims.json").read_text())

    work = Path(tempfile.mkdtemp(prefix="repro_"))
    shutil.copytree(target_dir, work, dirs_exist_ok=True)
    script = work / "analysis.py"

    attempts, patches = [], []
    final = None
    for i in range(max_iters):
        r = run_script(script, work)
        attempts.append({"iter": i, "returncode": r["returncode"],
                         "error": (r["stderr"].strip().splitlines()[-1]
                                   if r["returncode"] else None)})
        if r["returncode"] == 0:
            final = r; break
        diag = classify_failure(r["stderr"])
        if diag["pattern"] == "UNKNOWN":
            return {"status": "NEEDS_AGENT", "protocol_version": PROTOCOL_VERSION,
                    "target": str(target_dir), "attempts": attempts,
                    "patches": patches, "traceback": r["stderr"],
                    "note": "novel failure — hand traceback to the agent to repair"}
        new_src, rec = apply_patch(script.read_text(), diag, work)
        if new_src is None:
            return {"status": "NEEDS_AGENT", "protocol_version": PROTOCOL_VERSION,
                    "target": str(target_dir), "attempts": attempts,
                    "patches": patches, "diagnosis": diag, "reason": rec}
        script.write_text(new_src); patches.append(rec)

    if final is None:
        return {"status": "FAILED_TO_RUN", "protocol_version": PROTOCOL_VERSION,
                "target": str(target_dir), "attempts": attempts, "patches": patches}

    # --- compare each claim to the reproduced output ---
    claim_reports = []
    for c in claims["claims"]:
        obs = final["results"].get(c["id"])
        if c["kind"] == "stochastic":
            cr = characterize_stochastic(script, work, c["id"], c["value"], tol.get("n", STOCHASTIC_RERUNS))
        else:
            cr = compare_value(c["value"], obs, c["kind"], tol)
        claim_reports.append({"id": c["id"], "desc": c["desc"], "kind": c["kind"], **cr})

    ok = sum(r["status"].startswith("REPRODUCED") for r in claim_reports)
    near = sum(r["status"].startswith("NEAR") for r in claim_reports)
    report = {
        "status": "COMPLETED", "protocol_version": PROTOCOL_VERSION,
        "target": str(target_dir), "paper": claims.get("paper"),
        "tolerance": tol, "required_repair": bool(patches),
        "environment": {"python": sys.version.split()[0], "numpy": np.__version__,
                        "pandas": pd.__version__, "scipy": scipy.__version__},
        "attempts": attempts, "patches": patches, "claims": claim_reports,
        "verdict": {"n_claims": len(claim_reports), "reproduced": ok,
                    "near": near, "needs_review": len(claim_reports) - ok - near},
    }
    (target_dir / "repro_report.json").write_text(json.dumps(report, indent=2))
    (target_dir / "repro_report.md").write_text(render_markdown(report))
    return report


def render_markdown(rep: dict) -> str:
    L = [f"# Reproduction report — {rep.get('paper','(target)')}",
         f"\n*Protocol v{rep['protocol_version']} · assistant output, not a verdict*\n",
         f"**Overall:** ran successfully"
         + (" after automated repair" if rep.get("required_repair") else " as-is")
         + f"; {rep['verdict']['reproduced']}/{rep['verdict']['n_claims']} claims reproduced within tolerance.\n"]
    if rep.get("patches"):
        L.append("## Repairs applied (migration path)")
        for p in rep["patches"]:
            L.append(f"- **{p['pattern']}** — {p['change']}")
        L.append("")
    L.append("## Claims")
    L.append("| claim | kind | status | claimed | reproduced |")
    L.append("|---|---|---|---|---|")
    for c in rep["claims"]:
        obs = c.get("observed", c.get("observed_range", "—"))
        L.append(f"| {c['id']} | {c['kind']} | {c['status']} | {c['claimed']} | {obs} |")
    L.append("\n## Environment")
    L.append(", ".join(f"{k} {v}" for k, v in rep["environment"].items()))
    L.append("\n## Scope")
    L.append("Reproduction ladder rung reached: **2 — computational reproducibility** "
             "(does the code run and produce the same numbers). This report does NOT "
             "assess analytical correctness (rung 3) or robustness (rung 4).")
    L.append("\n> **Reproducible ≠ correct.** A reproduced result means *consistent*, "
             "not *true*: buggy or mis-specified analyses can reproduce perfectly. "
             "This is assistant output to accelerate a human reviewer, not a verdict "
             "on the authors.")
    return "\n".join(L)
