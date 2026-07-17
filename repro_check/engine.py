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
        # Targeted Python-2 -> 3 conversion of the dominant breakage: the Py2
        # `print` STATEMENT (`print "x"` / `print >>f, x`). Applied to the FILE
        # named in the traceback. lib2to3 (the old 2to3 engine) was removed in
        # Python 3.13, so this is a self-contained, dependency-free converter —
        # it wraps only unambiguous print-statement lines and refuses anything
        # it can't rewrite safely rather than corrupt the source.
        tgt = diagnosis.get("file")
        if not tgt or not Path(tgt).exists():
            return None, "py2 syntax error but source file not locatable"
        code = Path(tgt).read_text()
        out, n = [], 0
        # `print >>sys.stderr, x`  and  `print x`  (statement forms only; a call
        # `print(...)` already has '(' right after the keyword and is left alone)
        chevron = re.compile(r'^(\s*)print\s*>>\s*([^,]+),\s*(.*?)\s*$')
        stmt = re.compile(r'^(\s*)print\s+(?!\()(.*?)\s*$')
        # A Py2 print statement can span physical lines via backslash continuation
        # (`print a, \`  /  `      b`). Join such continuations into one logical
        # line BEFORE matching, so the wrap captures the whole statement — else we
        # skip it and wrongly report "no print statements converted".
        raw = code.splitlines()
        lines, i = [], 0
        while i < len(raw):
            cur = raw[i]
            while cur.rstrip().endswith("\\") and i + 1 < len(raw):
                cur = cur.rstrip()[:-1].rstrip() + " " + raw[i + 1].strip()
                i += 1
            lines.append(cur); i += 1
        for line in lines:
            mc = chevron.match(line)
            ms = stmt.match(line)
            if mc:
                indent, stream, rest = mc.groups()
                out.append(f'{indent}print({rest}, file={stream})'); n += 1
            elif ms:
                indent, rest = ms.groups()
                # skip if it's clearly not a statement (e.g. `print` alone, or a comment)
                if rest and not rest.startswith("#"):
                    out.append(f'{indent}print({rest})'); n += 1
                else:
                    out.append(line)
            else:
                out.append(line)
        if n == 0:
            return None, "no Py2 print statements converted (different py2 issue — hand to agent)"
        fixed = "\n".join(out) + ("\n" if code.endswith("\n") else "")
        try:                                    # never write syntactically broken source
            compile(fixed, tgt, "exec")
        except SyntaxError as e:
            return None, f"Py2 print rewrite left a syntax error ({e.msg}) — hand to agent"
        Path(tgt).write_text(fixed)   # patch the actual failing file in place
        return src, {"pattern": p, "change": f"Py2 print statements -> print() in {Path(tgt).name} ({n})",
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


def notebook_out_of_order(nb):
    """Inspect a parsed notebook's saved execution_count metadata and decide
    whether its cells were last run OUT OF DOCUMENT ORDER. If so, the saved
    outputs reflect a different execution path than a top-to-bottom run, so a
    linear reproduction may legitimately produce different results — an honest
    caveat, not a silent 'reproduced'.

    Returns a dict {out_of_order: bool, detail: str, counts: [...]}. Cells with
    no execution_count (never run / cleared) are ignored for the ordering test;
    if fewer than two executed cells carry counts, we cannot judge and report
    out_of_order=False.
    """
    counts = []
    cells = nb["cells"] if isinstance(nb, dict) else nb.cells
    for cell in cells:
        if cell.get("cell_type") != "code":
            continue
        if not (cell.get("source") or "").strip():
            continue
        counts.append(cell.get("execution_count"))
    executed = [c for c in counts if isinstance(c, int)]
    if len(executed) < 2:
        return {"out_of_order": False, "detail": "", "counts": counts}
    # Non-monotonic execution_count in document order => ran out of order.
    ascending = all(executed[i] < executed[i + 1] for i in range(len(executed) - 1))
    if ascending:
        return {"out_of_order": False, "detail": "", "counts": counts}
    n_uncleared = sum(1 for c in counts if c is None)
    detail = ("cell execution_count is non-monotonic in document order "
              "(%s) — the notebook was last run out of order, so its saved "
              "outputs may not match a top-to-bottom run" % executed)
    if n_uncleared:
        detail += ("; %d code cell(s) were never run (no execution_count)" % n_uncleared)
    return {"out_of_order": True, "detail": detail, "counts": counts}


def notebook_to_script(nb_path, out_path=None):
    """Convert a Jupyter notebook's code cells to a runnable .py file, reusing the
    whole existing run/diagnose/fix loop instead of a parallel notebook executor.

    IPython-only constructs are neutralised so the result is plain-Python:
      - line magics (`%matplotlib inline`)      -> commented out
      - cell magics (`%%time`)                   -> whole cell's magic line dropped
      - shell escapes (`!pip install x`)         -> commented out
      - `display(...)` / `get_ipython()`         -> commented out
    Returns (Path, order_info) where order_info is the notebook_out_of_order()
    dict, or (None, None) if the notebook has no code or nbformat is
    unavailable (caller falls back to NO_ENTRYPOINT).
    """
    try:
        import nbformat
    except Exception:
        return None, None
    try:
        nb = nbformat.read(str(nb_path), as_version=4)
    except Exception:
        return None, None
    order_info = notebook_out_of_order(nb)
    lines = []
    if order_info["out_of_order"]:
        lines.append("# [repro-check: WARNING] " + order_info["detail"])
        lines.append("# repro-check runs cells in DOCUMENT order; results may differ "
                     "from the saved outputs.")
        lines.append("")
    for cell in nb.cells:
        if cell.get("cell_type") != "code":
            continue
        for ln in (cell.get("source") or "").splitlines():
            s = ln.lstrip()
            if s.startswith(("%", "!", "get_ipython(")) or s.startswith("display("):
                lines.append("# [repro-check: notebook-only] " + ln)
            else:
                lines.append(ln)
        lines.append("")  # cell boundary
    if not any(l.strip() and not l.startswith("#") for l in lines):
        return None, order_info
    out = Path(out_path) if out_path else Path(nb_path).with_suffix(".repro_nb.py")
    out.write_text("\n".join(lines) + "\n")
    return out, order_info


# Files that signal a repo is NOT a runnable-Python project (so NO_ENTRYPOINT
# should be reported honestly as "out of scope", not as a tool failure).
def detect_scope(target_dir):
    """Classify a repo with no Python entry point, so the hand-off can say WHY.
    Returns one of: 'notebook' (has .ipynb, convertible), 'r' (R project),
    'no_python' (no .py/.ipynb at all), or None (has .py — not this branch's job).
    """
    td = Path(target_dir)
    def has(glob):
        return any(p for p in td.rglob(glob) if ".git" not in p.parts)
    py = has("*.py"); ipynb = has("*.ipynb")
    r = has("*.R") or has("*.r") or has("*.Rmd") or has("*.rmd") or (td / "DESCRIPTION").exists()
    if py:
        return None
    if ipynb:
        return "notebook"
    if r:
        return "r"
    return "no_python"


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


# Minimum free RAM (MB) below which we refuse to start a dependency install.
# Installs of scientific wheels (and especially any source build) can spike
# memory; on a starved machine pip gets OOM-killed mid-build, leaving a
# half-written package that is worse than a clean "skipped". Overridable via
# REPRO_CHECK_MIN_INSTALL_MB.
MIN_INSTALL_RAM_MB = 512


def rc_available_ram_mb():
    """Best-effort available RAM in MB, or None if it can't be determined.
    Uses psutil if present, else /proc/meminfo (Linux), else sysconf. Never
    raises — an unknown value must not block installs on platforms we can't
    read."""
    try:
        import psutil
        return int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f:
            info = {}
            for line in f:
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
        for key in ("MemAvailable", "MemFree"):
            if key in info:
                return int(info[key].split()[0]) / 1024  # kB -> MB
    except Exception:
        pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and page_size > 0:
            return int(pages * page_size / (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        pass
    # macOS: no SC_AVPHYS_PAGES; parse `vm_stat` (free + inactive + speculative).
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
        page_size = 4096
        m = re.search(r"page size of (\d+) bytes", out)
        if m:
            page_size = int(m.group(1))
        free = spec = inactive = 0
        for line in out.splitlines():
            if "Pages free:" in line:
                free = int(re.sub(r"\D", "", line.split(":")[1]))
            elif "Pages speculative:" in line:
                spec = int(re.sub(r"\D", "", line.split(":")[1]))
            elif "Pages inactive:" in line:
                inactive = int(re.sub(r"\D", "", line.split(":")[1]))
        avail_pages = free + spec + inactive
        if avail_pages > 0:
            return int(avail_pages * page_size / (1024 * 1024))
    except Exception:
        pass
    return None


def rc_preinstall_gate(min_mb=None):
    """Decide whether it is safe to start an install. Returns (ok, reason).
    ok=True with reason=None when memory is sufficient OR unknown (we don't
    block on an unreadable value); ok=False with a human reason when memory is
    known and below the floor."""
    if min_mb is None:
        try:
            min_mb = int(os.environ.get("REPRO_CHECK_MIN_INSTALL_MB", MIN_INSTALL_RAM_MB))
        except (TypeError, ValueError):
            min_mb = MIN_INSTALL_RAM_MB
    avail = rc_available_ram_mb()
    if avail is not None and avail < min_mb:
        return False, ("only %d MB RAM available (< %d MB floor) — install skipped "
                       "to avoid an OOM-killed, half-written package. Free memory or "
                       "set REPRO_CHECK_MIN_INSTALL_MB to override." % (int(avail), min_mb))
    return True, None


def pip_install(module, timeout=300):
    """Install the pip distribution for an import name. Returns (ok, pkg, log).
    Gated by a pre-install memory check and a hard timeout: on a starved
    machine or a hung build it returns a clean, honest failure log rather than
    crashing or leaving a half-written package. A memory skip is flagged in the
    log with the marker 'SKIPPED_LOW_MEMORY:'."""
    pkg = IMPORT_TO_PKG.get(module, module)
    safe, reason = rc_preinstall_gate()
    if not safe:
        return False, pkg, "SKIPPED_LOW_MEMORY: " + reason
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", pkg],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, pkg, "TIMEOUT: pip install %s exceeded %ss" % (pkg, timeout)
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
    notebook_converted = None
    notebook_order_warning = None
    if ep is None:
        # No .py entry point. Before giving up, see what kind of repo this is.
        scope = detect_scope(target_dir)
        if scope == "notebook":
            # Convert the most substantial notebook to a script and run THAT
            # through the normal loop — notebooks are ~a third of no-entry repos.
            nbs = sorted((p for p in target_dir.rglob("*.ipynb") if ".git" not in p.parts),
                         key=lambda p: p.stat().st_size, reverse=True)
            for nb in nbs:
                converted, order_info = notebook_to_script(nb, nb.with_suffix(".repro_nb.py"))
                if converted:
                    ep = converted
                    notebook_converted = str(nb.relative_to(target_dir))
                    if order_info and order_info.get("out_of_order"):
                        notebook_order_warning = order_info["detail"]
                    break
        if ep is None:
            # R project with no Python entry point: route to the R engine (v0.6)
            # instead of a bare out-of-scope verdict. If R is unavailable, the R
            # engine returns an honest R_NOT_AVAILABLE hand-off.
            if scope == "r":
                r_res = attempt_r_executability(target_dir, allow_install=allow_install)
                return rc_shape_r_result(r_res, target_dir)
            reason = {
                "notebook": "repo is notebook-based but no notebook could be converted to a runnable script",
                "no_python": "no Python or notebook files found — nothing for this tool to run",
                None: "no runnable Python entry point found",
            }.get(scope, "no runnable Python entry point found")
            return {"status": "NO_ENTRYPOINT", "target": str(target_dir),
                    "scope": scope, "reason": reason}

    patches, installed, attempts = [], [], []
    prev_err, tried_install = None, set()
    module_mode = None; module_root = None   # set when we recover via `python -m`
    for i in range(max_iters):
        r = run_script(ep, target_dir, as_module=module_mode, module_root=module_root)
        err_line = (r["stderr"].strip().splitlines()[-1] if r["returncode"] else None)
        attempts.append({"iter": i, "returncode": r["returncode"], "error": err_line,
                         **({"mode": f"-m {module_mode}"} if module_mode else {})})
        if r["returncode"] == 0:
            return {"status": "RAN" if (patches or installed or notebook_converted) else "RAN_AS_IS",
                    "entrypoint": str(ep.relative_to(target_dir)),
                    "patches": patches, "installed": installed, "attempts": attempts,
                    **({"run_as_module": module_mode} if module_mode else {}),
                    **({"from_notebook": notebook_converted} if notebook_converted else {}),
                    **({"notebook_warning": notebook_order_warning} if notebook_order_warning else {})}

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
                if log.startswith("SKIPPED_LOW_MEMORY:"):
                    reason = (f"install of {pkg} skipped — {log.split(':',1)[1].strip()}")
                elif log.startswith("TIMEOUT:"):
                    reason = f"install of {pkg} timed out — {log.split(':',1)[1].strip()}"
                else:
                    reason = f"pip install {pkg} failed"
                return build_handoff(target_dir, ep, patches, installed, attempts, diag=diag,
                        reason=reason, traceback=(r["stderr"] or "") + "\n[install log] " + log,
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


def extract_cli_spec(entrypoint, target_dir, attempts=None):
    """When a repo stops on missing run-time arguments, gather everything needed
    to suggest the exact invocation — instead of just telling the human to read
    the README. Three independent sources, best-effort and never raising:

      1. the argparse usage line + 'the following arguments are required' line
         that argparse itself printed to stderr on the failing run;
      2. an AST scan of the entry point for `add_argument(...)` calls (flag
         names, required=, help=, choices=, default=);
      3. example invocations found in the repo's README(s).

    Also scans the repo for data files whose names resemble a path-type argument,
    so a concrete command can be proposed. Returns a dict (possibly with empty
    fields); the caller decides how much to surface.
    """
    import ast
    ep = Path(entrypoint)
    td = Path(target_dir)
    spec = {"required": [], "optional": [], "usage_line": None,
            "readme_examples": [], "candidate_data_files": [], "suggested_command": None}

    # 1. argparse output from the failing run's stderr
    if attempts:
        for a in reversed(attempts):
            err = a.get("stderr") or ""
            m = re.search(r"^usage:.*$", err, re.M)
            if m:
                spec["usage_line"] = m.group(0).strip()
            m2 = re.search(r"the following arguments are required:\s*(.+)", err)
            if m2:
                # keep the SAME dict shape the AST scan produces, so downstream
                # code can treat every entry uniformly (flags list + help)
                spec["required"] = [{"flags": [x.strip()], "help": None}
                                    for x in re.split(r"[,\s]+", m2.group(1))
                                    if x.strip().startswith("-")]
            if spec["usage_line"] or spec["required"]:
                break

    # 2. AST scan of the entry point for add_argument calls
    try:
        tree = ast.parse(ep.read_text(errors="ignore"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"):
                flags = [a.value for a in node.args
                         if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if not flags:
                    continue
                kw = {k.arg: k.value for k in node.keywords}
                required = isinstance(kw.get("required"), ast.Constant) and kw["required"].value is True
                helptxt = kw["help"].value if isinstance(kw.get("help"), ast.Constant) else None
                entry = {"flags": flags, "help": helptxt}
                (spec["required"] if required else spec["optional"]).append(entry)
    except Exception:
        pass

    # 3. README example invocations. Reject TEMPLATE/placeholder lines — a README
    # that shows `python main.py --[keyword1] [argument1] ...` is documenting the
    # SHAPE of the call, not a runnable command; surfacing it as "try this" hands
    # the user a command that cannot run. Only keep lines that look concrete.
    def is_placeholder(s):
        return bool(
            re.search(r"--\[[^\]]+\]", s)            # --[keyword1]
            or re.search(r"[<\[](arg|keyword|value|option|param|name|path|input|output)", s, re.I)
            or "..." in s or "…" in s
            or re.search(r"<[^>]+>", s)              # <path>, <value>
            or re.search(r"\{[^}]+\}", s)            # {input}
        )
    for readme in list(td.glob("README*")) + list(td.glob("*/README*")):
        try:
            for line in readme.read_text(errors="ignore").splitlines():
                s = line.strip().lstrip("$ ").strip()
                if re.match(r"python[3]?\s", s) and ("--" in s or ep.name in s):
                    if not is_placeholder(s):
                        spec["readme_examples"].append(s)
        except Exception:
            pass
    spec["readme_examples"] = spec["readme_examples"][:5]

    # 4. candidate data files (to fill path-type args)
    for p in td.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".csv", ".json", ".npy", ".npz", ".txt", ".h5", ".pkl", ".tsv"}:
            rel = p.relative_to(td).as_posix()
            if "/.git/" not in "/" + rel:
                spec["candidate_data_files"].append(rel)
    spec["candidate_data_files"] = spec["candidate_data_files"][:12]

    # 5. best suggested command: prefer a README example, else synthesise from usage
    if spec["readme_examples"]:
        spec["suggested_command"] = spec["readme_examples"][0]
    elif spec["usage_line"]:
        spec["suggested_command"] = "python " + str(ep.relative_to(td)) + "  " + \
            " ".join(f["flags"][0] + " <value>" for f in spec["required"])
    return spec


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
    # For a CLI-args stop, gather the concrete invocation instead of "read the README".
    cli_spec = None
    if key == "CLI_ARGS":
        try:
            cli_spec = extract_cli_spec(ep, target_dir, attempts)
        except Exception:
            cli_spec = None
        # the classifier tags argparse's SystemExit as UNKNOWN; give a real reason
        if not reason or (isinstance(diag, dict) and diag.get("pattern") == "UNKNOWN"):
            reason = "ran, but exited requiring command-line arguments (argparse)"
    return {
        "status": "NEEDS_AGENT",
        "stopping_rung": "1 (executability) — advanced but not running" if advanced
                         else "0 (does not start)",
        "entrypoint": str(Path(ep).relative_to(target_dir)),
        "run_as_module": module_mode,
        # Top-level patches/installed mirror the RAN-result shape so any consumer
        # can read res["patches"] uniformly regardless of outcome. `already_applied`
        # keeps the flattened change-strings for the human-readable hand-off note.
        "patches": patches,
        "installed": installed,
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
        "cli_spec": cli_spec,
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
         f"## Suggested next action ({h['next_action_key']})\n{h['suggested_next_action']}"]
    sysdeps = h.get("system_deps")
    if sysdeps:
        pkg = h.get("failed_pkg", "the package")
        L += ["", "## System libraries needed",
              f"`{pkg}` needs OS-level libraries that `install.packages()` cannot provide. "
              "Install these first (Debian/Ubuntu package names shown), then re-run:",
              "```bash",
              "apt-get install -y " + " ".join(sysdeps),
              "```",
              "On macOS use the Homebrew equivalents; in a conda env, the `-dev`/`-devel` "
              "packages are usually on conda-forge."]
    cs = h.get("cli_spec")
    if cs:
        L += ["", "## Run-time arguments this script needs"]
        if cs.get("suggested_command"):
            L += [f"Try:\n```\n{cs['suggested_command']}\n```"]
        if cs.get("usage_line"):
            L += [f"argparse usage: `{cs['usage_line']}`"]
        for e in cs.get("required", []):
            if isinstance(e, dict):
                L.append(f"- required `{' / '.join(e['flags'])}`"
                         + (f" — {e['help']}" if e.get("help") else ""))
        if cs.get("candidate_data_files"):
            L.append("- data files present in the repo that may fill path args: "
                     + ", ".join(f"`{f}`" for f in cs["candidate_data_files"][:6]))
        if cs.get("readme_examples") and not cs.get("suggested_command"):
            L += ["README shows:"] + [f"  `{x}`" for x in cs["readme_examples"][:3]]
    L += ["", "## Traceback (tail)", "```", h["traceback"].strip()[-1000:], "```"]
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


# ---------------------------------------------------------------------------
# R runnability engine (v0.6). Routed to from attempt_executability() when
# detect_scope() reports an R project with no Python entry point. Same
# run -> classify -> fix -> re-run -> hand-off loop as the Python engine, but
# scoped to R's real failure modes: the mechanical win in R is INSTALL
# robustness (CRAN / Bioconductor), NOT source patching. Most R stops are
# missing data or interactive/platform calls, which are honest hand-offs.
# ---------------------------------------------------------------------------

CRAN_REPO = "https://cloud.r-project.org"

R_ENTRY_NAMES = ["analysis.R", "main.R", "run.R", "reproduce.R", "run_all.R",
                 "make.R", "figures.R", "master.R", "00_main.R"]

# import names that live on Bioconductor, not CRAN (install.packages fails)
BIOC_PKGS = {
    "DESeq2", "edgeR", "limma", "Biobase", "BiocGenerics", "S4Vectors",
    "IRanges", "GenomicRanges", "SummarizedExperiment", "Biostrings",
    "GenomicFeatures", "AnnotationDbi", "org.Hs.eg.db", "ComplexHeatmap",
    "clusterProfiler", "DEXSeq", "tximport", "scran", "scater",
    "SingleCellExperiment", "fgsea", "GEOquery", "sva", "biomaRt",
    "RiboCrypt", "ORFik", "multinichenetr", "InSituType",
}

R_NEXT_ACTION = {
    "R_MISSING_DATA":       "A data file the R code expects is absent from the repo. Locate "
                            "it (README, data DOI, external download) and place it where the "
                            "code looks.",
    "R_INTERACTIVE":        "The script calls an interactive/platform-only function "
                            "(file.choose, choose.dir, readline, menu, View) that cannot run "
                            "headless. Refactor it to take the path/input as a variable or "
                            "command-line argument, then re-run.",
    "R_DEP_INSTALL":        "A CRAN/Bioconductor package failed to install \u2014 usually a missing "
                            "system dependency (compilers, libxml2/libcurl/GDAL headers) or "
                            "Bioconductor version lockstep. Install the system libraries, or "
                            "match the Bioconductor release to your R version, then retry.",
    "R_FUNCTION_NOT_FOUND": "A function could not be found \u2014 likely an uninstalled package's "
                            "export or an API change. Identify which package provides it and "
                            "install/pin that package.",
    "R_NOT_AVAILABLE":      "R (Rscript) is not installed or not on PATH, so this R project "
                            "cannot be run. Install R, or set the REPRO_CHECK_RSCRIPT "
                            "environment variable to your Rscript path, then re-run.",
    "R_BIOC_VERSION":       "A Bioconductor package is not available for the Bioconductor "
                            "release that matches your R version (a version-lockstep wall). "
                            "Either install the R version whose Bioconductor release ships this "
                            "package, or find the package in the Bioconductor archive for your "
                            "release. repro-check will not silently install a mismatched build.",
    "R_SYSTEM_DEP":         "A package failed to build because a system (OS-level) library or "
                            "header is missing \u2014 R can't install this with install.packages() "
                            "alone. Install the named system packages (e.g. via apt/brew/conda), "
                            "then re-run so the R package can compile.",
    "R_GENERIC":            "Novel R failure outside the known pattern set. Read the traceback "
                            "and repair directly, then re-run.",
}

# R packages whose install COMPILES against an OS-level library. When the build
# fails, install.packages() can't fix it — the user needs the system package.
# Maps the R package -> the system libraries it needs, with common apt names.
R_SYSTEM_DEPS = {
    "xml2":        ["libxml2-dev"],
    "curl":        ["libcurl4-openssl-dev"],
    "openssl":     ["libssl-dev"],
    "sf":          ["libgdal-dev", "libproj-dev", "libgeos-dev"],
    "rgdal":       ["libgdal-dev", "libproj-dev"],
    "rgeos":       ["libgeos-dev"],
    "terra":       ["libgdal-dev", "libproj-dev", "libgeos-dev"],
    "units":       ["libudunits2-dev"],
    "systemfonts": ["libfontconfig1-dev", "libfreetype6-dev"],
    "textshaping": ["libharfbuzz-dev", "libfribidi-dev"],
    "ragg":        ["libfontconfig1-dev", "libfreetype6-dev", "libpng-dev", "libtiff5-dev", "libjpeg-dev"],
    "magick":      ["libmagick++-dev"],
    "pdftools":    ["libpoppler-cpp-dev"],
    "rJava":       ["default-jdk"],
    "V8":          ["libv8-dev"],
    "gert":        ["libgit2-dev"],
    "hdf5r":       ["libhdf5-dev"],
    "RMySQL":      ["libmariadb-dev"],
    "RPostgreSQL": ["libpq-dev"],
}

# Substrings that appear in a compiler/linker failure when an OS library header
# is missing, mapped to the system package that provides it. Used when the
# failing R package isn't in R_SYSTEM_DEPS but the build log names the header.
R_SYSLIB_HINTS = {
    "libxml/":              "libxml2-dev",
    "curl/curl.h":          "libcurl4-openssl-dev",
    "openssl/":             "libssl-dev",
    "gdal":                 "libgdal-dev",
    "proj_api.h":           "libproj-dev",
    "geos_c.h":             "libgeos-dev",
    "udunits2":             "libudunits2-dev",
    "fontconfig":           "libfontconfig1-dev",
    "ft2build.h":           "libfreetype6-dev",
    "hb.h":                 "libharfbuzz-dev",
    "png.h":                "libpng-dev",
    "jpeglib.h":            "libjpeg-dev",
    "Magick++.h":           "libmagick++-dev",
    "poppler":              "libpoppler-cpp-dev",
    "hdf5.h":               "libhdf5-dev",
    "libpq-fe.h":           "libpq-dev",
    "jni.h":                "default-jdk",
}


def rc_r_system_deps(pkg, build_log=""):
    """Given a package that failed to install and its build log, return the
    list of system (apt-style) packages needed to compile it, or [] if none
    can be inferred. Combines the static map with header hints from the log."""
    deps = list(R_SYSTEM_DEPS.get(pkg, []))
    if build_log:
        for needle, sysdep in R_SYSLIB_HINTS.items():
            if needle in build_log and sysdep not in deps:
                deps.append(sysdep)
    return deps


def rc_is_system_dep_failure(build_log):
    """Heuristic: does a failed R package build look like a MISSING SYSTEM
    LIBRARY (as opposed to a plain package-not-found or a network error)?"""
    if not build_log:
        return False
    markers = [
        "cannot find -l", "No such file or directory", "fatal error:",
        "unable to load shared object", "configuration failed",
        "libraries required but not found", "Cannot find ", "not found. ",
        "ERROR: dependency", "compilation failed", "C++ compiler",
    ]
    lo = build_log
    return any(m in lo for m in markers)


def rc_find_rscript():
    """Locate an Rscript binary. Order: explicit REPRO_CHECK_RSCRIPT override,
    PATH (shutil.which), then sibling conda envs of the current prefix (R is
    commonly in its own env). Returns the path str, or None if R is unavailable."""
    import glob
    override = os.environ.get("REPRO_CHECK_RSCRIPT")
    if override and Path(override).exists():
        return override
    w = shutil.which("Rscript")
    if w:
        return w
    prefix = os.environ.get("CONDA_PREFIX")
    if prefix:
        for cand in glob.glob(str(Path(prefix).parent / "*" / "bin" / "Rscript")):
            return cand
    return None


def rc_r_libs_user():
    """Writable user library for runtime installs (a conda R lib is read-only)."""
    p = Path(os.environ.get("R_LIBS_USER") or (Path.cwd() / ".Rlib")).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def discover_r_entrypoint(target_dir):
    """Find the most likely R entry script. Prefers a known entry NAME, then
    shallower paths / code|src|scripts|R|analysis dirs. .R and .Rmd both count;
    .Rmd is only chosen if no .R exists (rendering it needs rmarkdown)."""
    td = Path(target_dir)
    rs = [p for p in td.rglob("*.R")
          if not any(seg in {".git", "man", "tests", "testthat", "vignettes"}
                     for seg in p.relative_to(td).parts[:-1])]
    pool = rs
    if not pool:
        pool = [p for p in td.rglob("*.Rmd") if ".git" not in p.parts]
    if not pool:
        return None

    def score(p):
        rel = p.relative_to(td)
        name_rank = R_ENTRY_NAMES.index(p.name) if p.name in R_ENTRY_NAMES else len(R_ENTRY_NAMES)
        in_code = 0 if any(s in {"code", "src", "scripts", "R", "analysis"} for s in rel.parts[:-1]) else 1
        return (name_rank, in_code, len(rel.parts), len(rel.as_posix()))

    return sorted(pool, key=score)[0]


def rc_run_r(script, target_dir, rscript, timeout=180):
    """Run an R script (or render an .Rmd) with the writable user lib on the
    path and a headless graphics device. Returns {returncode, stdout, stderr}."""
    script = Path(script)
    libs = rc_r_libs_user()
    env = {**os.environ, "R_LIBS_USER": libs,
           "R_DEFAULT_DEVICE": "png", "MPLBACKEND": "Agg"}
    prelude = '.libPaths(c("%s", .libPaths())); ' % libs
    if script.suffix.lower() == ".rmd":
        cmd = [rscript, "-e", prelude + 'rmarkdown::render("%s")' % script.name]
    else:
        cmd = [rscript, "-e", prelude + 'source("%s", echo=FALSE)' % script.name]
    try:
        r = subprocess.run(cmd, cwd=str(script.parent), capture_output=True,
                           text=True, timeout=timeout, env=env)
        return {"returncode": r.returncode, "stdout": r.stdout, "stderr": r.stderr}
    except subprocess.TimeoutExpired:
        return {"returncode": 124, "stdout": "", "stderr": "TIMEOUT after %ss" % timeout}


def classify_r_failure(stderr):
    """Map an R error tail to a fix category. Returns {pattern, module?}."""
    m = re.search(r"there is no package called [`'\"]([\w.]+)[`'\"]", stderr)
    if m:
        pkg = m.group(1)
        return {"pattern": "MISSING_PKG_BIOC" if pkg in BIOC_PKGS else "MISSING_PKG_CRAN",
                "module": pkg}
    m = re.search(r"Error in library\(([\w.]+)\)", stderr) or \
        re.search(r"Error in require\(([\w.]+)\)", stderr)
    if m:
        pkg = m.group(1)
        return {"pattern": "MISSING_PKG_BIOC" if pkg in BIOC_PKGS else "MISSING_PKG_CRAN",
                "module": pkg}
    # data / path not found — R has many phrasings across base, readxl, here, fs
    if re.search(r"cannot open file|No such file or directory|cannot open the connection"
                 r"|`?path`? does not exist|does not exist:|cannot find the file"
                 r"|file.*not found|Error.*reading", stderr, re.I):
        return {"pattern": "MISSING_DATA", "module": None}
    # interactive / platform-only functions that can't run headless (real hand-off)
    m = re.search(r'could not find function ["\u201c]?(choose\.dir|choose\.files|winDialog|'
                  r'file\.choose|readline|menu|View)["\u201d]?', stderr)
    if m:
        return {"pattern": "INTERACTIVE_OR_PLATFORM", "module": m.group(1)}
    if re.search(r"could not find function", stderr):
        return {"pattern": "FUNCTION_NOT_FOUND", "module": None}
    return {"pattern": "UNKNOWN", "module": None}


def rc_install_r_pkg(pkg, rscript, bioc=False, timeout=600):
    """Install a CRAN or Bioconductor package to the writable user lib, then
    verify it loads. Returns (ok_bool, info) where info is a dict:
    {log, system_deps}. system_deps is a non-empty list ONLY when the build
    failed and looks like a missing OS-level library.

    Bioconductor installs pin to the release matching the running R version
    (BiocManager picks the correct Bioc release for this R), avoiding the
    version-lockstep failures where a newer Bioc refuses an older R."""
    libs = rc_r_libs_user()
    lp = '.libPaths(c("%s", .libPaths())); ' % libs
    if bioc:
        # BiocManager::install() already resolves the Bioconductor release that
        # matches the RUNNING R version — that R-matched resolution IS the
        # lockstep-safety, so we do NOT force an explicit version= (forcing
        # version() is redundant and actively breaks when the installed
        # BiocManager is stale and maps R to an unpopulated release).
        # We first refresh BiocManager from CRAN so its R->Bioc map is current,
        # then print the resolved release for transparency in the log.
        expr = (lp
                + 'install.packages("BiocManager",repos="%s",quiet=TRUE); ' % CRAN_REPO
                + 'bv <- tryCatch(as.character(BiocManager::version()), error=function(e) "?"); '
                + 'cat("REPRO_CHECK_BIOC_VERSION=", bv, "\\n", sep=""); '
                + 'BiocManager::install("%s",update=FALSE,ask=FALSE)' % pkg)
    else:
        expr = lp + 'install.packages("%s",repos="%s",quiet=TRUE)' % (pkg, CRAN_REPO)
    env = {**os.environ, "R_LIBS_USER": libs}
    try:
        r = subprocess.run([rscript, "-e", expr], capture_output=True,
                           text=True, timeout=timeout, env=env)
        log = (r.stderr or "") + (r.stdout or "")
    except subprocess.TimeoutExpired:
        return False, {"log": "install timeout (%ss)" % timeout, "system_deps": [],
                       "bioc_version": None, "version_lockstep": False}
    chk = subprocess.run([rscript, "-e", lp + 'cat(requireNamespace("%s",quietly=TRUE))' % pkg],
                         capture_output=True, text=True, env=env)
    ok = chk.stdout.strip() == "TRUE"
    bioc_version = None
    mv = re.search(r"REPRO_CHECK_BIOC_VERSION=([\d.]+)", log)
    if mv:
        bioc_version = mv.group(1)
    system_deps = []
    if not ok and rc_is_system_dep_failure(log):
        system_deps = rc_r_system_deps(pkg, log)
    # A genuine version-lockstep wall: the resolved Bioc release has no such
    # package (usually the running R is too old/new for the package's release).
    version_lockstep = bool(not ok and re.search(
        r"not available for Bioconductor version|package .* is not available", log))
    return ok, {"log": log[-500:], "system_deps": system_deps,
                "bioc_version": bioc_version, "version_lockstep": version_lockstep}


def attempt_r_executability(target_dir, max_iters=12, allow_install=True):
    """Rung-1 for R: discover the .R/.Rmd entry point and try to make it run,
    installing missing CRAN/Bioconductor packages. Returns a result dict
    analogous to the Python engine (shaped for render_handoff_md by
    rc_shape_r_result)."""
    target_dir = Path(target_dir).resolve()
    rscript = rc_find_rscript()
    if rscript is None:
        return {"status": "NEEDS_AGENT", "language": "R", "target": str(target_dir),
                "entrypoint": "(no R interpreter)", "installed": [], "attempts": [],
                "reason": "R_NOT_AVAILABLE", "traceback": ""}
    ep = discover_r_entrypoint(target_dir)
    if ep is None:
        return {"status": "NO_ENTRYPOINT", "language": "R", "target": str(target_dir),
                "scope": "r", "reason": "R project but no .R/.Rmd entry script was found"}

    installed, attempts, tried = [], [], set()
    for i in range(max_iters):
        r = rc_run_r(ep, target_dir, rscript)
        err_tail = (r["stderr"].strip().splitlines()[-1] if r["returncode"] and r["stderr"].strip() else None)
        attempts.append({"iter": i, "returncode": r["returncode"], "error": err_tail,
                         "stderr": r["stderr"][-1500:]})
        if r["returncode"] == 0:
            return {"status": "RAN" if installed else "RAN_AS_IS", "language": "R",
                    "entrypoint": str(ep.relative_to(target_dir)),
                    "installed": installed, "attempts": attempts}
        diag = classify_r_failure(r["stderr"])
        pkg = diag.get("module")
        if allow_install and diag["pattern"] in ("MISSING_PKG_CRAN", "MISSING_PKG_BIOC") \
                and pkg and pkg not in tried:
            tried.add(pkg)
            ok, info = rc_install_r_pkg(pkg, rscript, bioc=(diag["pattern"] == "MISSING_PKG_BIOC"))
            if ok:
                installed.append({"pkg": pkg, "source": "bioc" if diag["pattern"] == "MISSING_PKG_BIOC" else "cran"})
                continue
            system_deps = info.get("system_deps") or []
            if system_deps:
                reason = "system libraries required to build %s" % pkg
            elif info.get("version_lockstep"):
                reason = ("%s not available for Bioconductor %s (the release matching this R)"
                          % (pkg, info.get("bioc_version") or "?"))
            else:
                reason = "install of %s failed" % pkg
            return {"status": "NEEDS_AGENT", "language": "R",
                    "entrypoint": str(ep.relative_to(target_dir)),
                    "installed": installed, "attempts": attempts,
                    "reason": reason, "diagnosis": diag,
                    "failed_pkg": pkg, "system_deps": system_deps,
                    "version_lockstep": info.get("version_lockstep", False),
                    "bioc_version": info.get("bioc_version"),
                    "install_log": info.get("log", ""),
                    "traceback": r["stderr"][-1500:]}
        return {"status": "NEEDS_AGENT", "language": "R",
                "entrypoint": str(ep.relative_to(target_dir)),
                "installed": installed, "attempts": attempts,
                "reason": diag["pattern"], "diagnosis": diag,
                "traceback": r["stderr"][-1500:]}
    return {"status": "NEEDS_AGENT", "language": "R",
            "entrypoint": str(ep.relative_to(target_dir)),
            "installed": installed, "attempts": attempts,
            "reason": "max_iters reached", "traceback": ""}


def rc_shape_r_result(res, target_dir):
    """Add the hand-off fields render_handoff_md expects, so an R NEEDS_AGENT
    stop renders the same way a Python one does, with R-specific next actions."""
    res.setdefault("language", "R")
    if res.get("status") != "NEEDS_AGENT":
        return res
    reason = res.get("reason", "") or ""
    diag = res.get("diagnosis") or {}
    pat = diag.get("pattern")
    if reason == "R_NOT_AVAILABLE":
        key = "R_NOT_AVAILABLE"
    elif res.get("system_deps"):
        key = "R_SYSTEM_DEP"
    elif res.get("version_lockstep") or "not available for Bioconductor" in reason:
        key = "R_BIOC_VERSION"
    elif "install of" in reason or "install timeout" in reason or "system libraries" in reason:
        key = "R_DEP_INSTALL"
    elif pat == "MISSING_DATA":
        key = "R_MISSING_DATA"
    elif pat == "INTERACTIVE_OR_PLATFORM":
        key = "R_INTERACTIVE"
    elif pat == "FUNCTION_NOT_FOUND":
        key = "R_FUNCTION_NOT_FOUND"
    else:
        key = "R_GENERIC"
    installed = res.get("installed", [])
    attempts = res.get("attempts", [])
    advanced = len(attempts) > 1 or bool(installed)
    res["next_action_key"] = key
    res["suggested_next_action"] = R_NEXT_ACTION[key]
    res["already_applied"] = {"patches": [],
                              "installed": [d.get("pkg") for d in installed]}
    res.setdefault("entrypoint", "(no R entry point resolved)")
    res.setdefault("run_as_module", None)
    res.setdefault("cli_spec", None)
    res.setdefault("traceback", "")
    res.setdefault("patches", [])
    res["stopping_rung"] = ("1 (executability) \u2014 advanced but not running" if advanced
                            else "0 (does not start)")
    res["environment"] = {"Rscript": rc_find_rscript() or "(not found)"}
    return res
