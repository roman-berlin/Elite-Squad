"""EU-465 regression: the whole orchestrator package keeps the EU-441 typing contract — every
PUBLIC def/async def carries a '-> <ReturnType>' and no annotation erodes the type surface with
a bare 'Any' (return, parameter, or variable annotation).

The Reviewer's deterministic typing gate (reviewer._enforce_missing_typing) only ever sees the
CURRENT branch diff — it cannot notice erosion of already-landed code, and on EU-465 the ticket
named reviewer.py while the real gap was ~38 untyped public defs + ~22 explicit-Any sites spread
across 17 sibling modules. This test scans the WHOLE package with the same two rules the gate
enforces, so the baseline the EU-465 sweep established can't quietly regress.

Checks:
  1. Anti-vacuity — the scan actually walks the package (≥60 files, ≥400 defs) AND names the
     specific functions EU-465 fixed, so an empty/renamed tree or a reverted annotation fails
     loudly instead of passing on a technicality.
  2. Zero public def/async def missing a return annotation anywhere under orchestrator/.
  3. Zero bare 'Any' in function signatures (return + parameters) or variable annotations.
  4. The concrete replacements hold: locking is generic (_R/_T), approvals names FilingResult,
     sync names its TypedDict result shapes, server route handlers are annotated, dashboard
     exposes the TaskRow alias.

Offline — pure AST scan; no SDK, no network, no Flask.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "orchestrator"

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: object, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def _has_any(node: ast.expr | None) -> bool:
    """True when a bare 'Any' name appears anywhere inside the annotation expression."""
    return node is not None and any(
        isinstance(sub, ast.Name) and sub.id == "Any" for sub in ast.walk(node))


def scan_package() -> tuple[list[str], list[str], int, int]:
    """(untyped public defs, Any annotations, files scanned, defs scanned)."""
    untyped: list[str] = []
    anys: list[str] = []
    files = defs = 0
    for f in sorted(ROOT.rglob("*.py")):
        files += 1
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        rel = f.relative_to(ROOT)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs += 1
                if not node.name.startswith("_") and node.returns is None:
                    kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    untyped.append(f"{rel}:{node.lineno} {kind} {node.name}")
                if _has_any(node.returns):
                    anys.append(f"{rel}:{node.lineno} {node.name} return")
                args = node.args
                every = [*args.posonlyargs, *args.args, *args.kwonlyargs]
                if args.vararg:
                    every.append(args.vararg)
                if args.kwarg:
                    every.append(args.kwarg)
                for a in every:
                    if _has_any(a.annotation):
                        anys.append(f"{rel}:{node.lineno} {node.name} arg {a.arg}")
            elif isinstance(node, ast.AnnAssign) and _has_any(node.annotation):
                anys.append(f"{rel}:{node.lineno} var {ast.unparse(node.target)}")
    return untyped, anys, files, defs


def _find_def(rel: str, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _ret_src(fn: ast.FunctionDef | ast.AsyncFunctionDef | None) -> str:
    return ast.unparse(fn.returns) if fn is not None and fn.returns is not None else ""


def main() -> int:
    print("=== EU-465 package typing-sweep regression ===\n")
    untyped, anys, files, defs = scan_package()
    print(f"  scanned {files} files, {defs} defs\n")

    # AC 1 — anti-vacuity: the scan really covered the package and the named EU-465 sites.
    chk("AC1 scan non-vacuous: ≥60 orchestrator files walked", files >= 60, f"{files} files")
    chk("AC1 scan non-vacuous: ≥400 defs walked", defs >= 400, f"{defs} defs")
    for rel, name in (("approvals.py", "approve_proposals"), ("locking.py", "locked_call"),
                      ("sync.py", "git_sync"), ("server.py", "create_app"),
                      ("dashboard.py", "load_tasks"), ("backlog/jira.py", "comments")):
        chk(f"AC1 named site exists: {rel}::{name}", _find_def(rel, name) is not None)

    # AC 2 — zero untyped public defs.
    chk("AC2 zero public def/async def missing a return annotation", untyped == [],
        "\n".join(untyped[:10]))

    # AC 3 — zero bare Any in annotations.
    chk("AC3 zero bare-Any annotations (signature or variable)", anys == [], "\n".join(anys[:10]))

    # AC 4 — the concrete EU-465 replacements survived.
    chk("AC4 approvals.approve_proposals names FilingResult (not an omitted return)",
        "FilingResult" in _ret_src(_find_def("approvals.py", "approve_proposals")),
        _ret_src(_find_def("approvals.py", "approve_proposals")))
    lc = _ret_src(_find_def("locking.py", "locked_call"))
    chk("AC4 locking.locked_call is generic via a TypeVar (_R), not a loose type",
        lc == "_R", lc)
    lr = _ret_src(_find_def("locking.py", "locked_rmw"))
    chk("AC4 locking.locked_rmw is generic via a TypeVar (_T), not a loose type",
        lr == "_T", lr)
    for name, shape in (("git_sync", "GitSyncStatus"), ("promote", "PromoteStatus"),
                        ("pull_server_audit", "ServerAuditPullStatus"),
                        ("app_promote_status", "AppPromoteStatus"),
                        ("promote_app", "AppShipStatus")):
        chk(f"AC4 sync.{name} returns its named TypedDict ({shape})",
            _ret_src(_find_def("sync.py", name)) == shape, _ret_src(_find_def("sync.py", name)))
    chk("AC4 server.create_app returns Flask",
        _ret_src(_find_def("server.py", "create_app")) == "Flask",
        _ret_src(_find_def("server.py", "create_app")))
    chk("AC4 server route handlers annotated (sample: index, run_api, needs_page)",
        _ret_src(_find_def("server.py", "index")) == "str"
        and _ret_src(_find_def("server.py", "run_api")) == "Response"
        and _ret_src(_find_def("server.py", "needs_page")) == "str",
        ",".join(_ret_src(_find_def("server.py", n)) for n in ("index", "run_api", "needs_page")))
    chk("AC4 dashboard exposes the TaskRow alias for assembled run rows",
        "TaskRow = dict[str, object]" in (ROOT / "dashboard.py").read_text(encoding="utf-8"))
    chk("AC4 dashboard.load_tasks returns list[TaskRow]",
        _ret_src(_find_def("dashboard.py", "load_tasks")) == "list[TaskRow]",
        _ret_src(_find_def("dashboard.py", "load_tasks")))
    chk("AC4 architect.ADRExtraction.to_dict returns the ADRDict TypedDict",
        _ret_src(_find_def("architect.py", "to_dict")) == "ADRDict",
        _ret_src(_find_def("architect.py", "to_dict")))
    chk("AC4 health.summary returns the HealthSummary TypedDict",
        _ret_src(_find_def("health.py", "summary")) == "HealthSummary",
        _ret_src(_find_def("health.py", "summary")))

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, d) for n, ok, d in results if not ok]
    print(f"{'=' * 60}")
    for n, d in failed:
        print(f"  [FAIL] {n}" + (f"\n         {d}" if d else ""))
    if not failed:
        print("  ALL GREEN")
    print(f"  {passed}/{len(results)} passed")
    print(f"{'=' * 60}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
