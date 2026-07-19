"""Seam-double signature guard (2026-07-19 stabilization; BUILD_DOCTRINE.md mechanism 5).

EU-258 added ``ticket_id``/``pass_number`` kwargs to ``run_agent_with_fallback`` and SIX test
doubles with narrower signatures broke with TypeError — and the manual sweep fixed only four
(eu258_reviewer_ledger_test.py's own docstring documents the class). The drift is structural:
a monkeypatched double freezes the seam's signature at the moment it was written, and nothing
re-checks it when production grows a kwarg.

This harness re-checks it on every suite run. For each registered production seam it:
  1. reads the REAL signature from the production source (AST — no imports, no SDK),
  2. finds every test double bound to that seam name (``X.seam = fake``,
     ``patch.object(mod, "seam", fake)``, ``patch("pkg.mod.seam", fake)``),
  3. requires the double to be drift-proof: it declares ``**kwargs`` (the cheap, recommended
     form), or its named parameters are a SUPERSET of the production parameters.

A double that only matches today's signature exactly is accepted (superset rule) — the guard
then fails the moment production grows a kwarg, which is precisely the event that broke EU-258
silently. Guard predicates are pure and self-tested on synthetic fixtures (teeth section).

Pure filesystem + AST — no network, no models, no SDK stub needed."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# seam name -> production source file (repo-relative). Extend when a new seam earns doubles.
SEAMS = {
    "run_agent_with_fallback": "orchestrator/agent.py",
}

results: list[tuple[str, bool, str]] = []


def res(n: str, c: bool, d: str = "") -> None:
    results.append((n, bool(c), d))


def production_params(src: str, name: str) -> list[str] | None:
    """Named parameters of production ``def name(...)`` (positional + kw-only), or None."""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            a = node.args
            return [p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)]
    return None


def _func_defs(tree: ast.AST) -> dict[str, ast.AST]:
    return {n.name: n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _accepts(fn: ast.AST, needed: list[str]) -> bool:
    """Whether a double's signature is drift-proof for ``needed`` production params."""
    a = fn.args
    if a.kwarg is not None:            # **kwargs — the recommended form
        return True
    have = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    if a.vararg is not None:           # *args soaks positionals; kwargs still need names
        have |= set(needed[:0])
    return set(needed) <= have


def seam_bindings(src: str, seam: str) -> list[tuple[int, str | None]]:
    """(lineno, bound-function-name-or-None) for every binding of ``seam`` in test source.

    None means the bound value is a lambda/expression handled inline; the caller checks
    lambdas via their own args. Recognized forms:
      X.seam = NAME | lambda...
      patch.object(mod, "seam", NAME | lambda...)
      patch("pkg.mod.seam", NAME | lambda...)
    """
    out: list[tuple[int, str | None, ast.AST | None]] = []
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Attribute) and tgt.attr == seam:
                    out.append((node.lineno, None, node.value))
        elif isinstance(node, ast.Call):
            fn = node.func
            is_patch_object = (isinstance(fn, ast.Attribute) and fn.attr == "object"
                               and isinstance(fn.value, ast.Name) and fn.value.id == "patch")
            is_patch = isinstance(fn, ast.Name) and fn.id == "patch"
            val = None
            if is_patch_object and len(node.args) >= 3 \
                    and isinstance(node.args[1], ast.Constant) and node.args[1].value == seam:
                val = node.args[2]
            elif is_patch and node.args and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str) \
                    and node.args[0].value.endswith("." + seam) and len(node.args) >= 2:
                val = node.args[1]
            if val is not None:
                out.append((node.lineno, None, val))
    resolved: list[tuple[int, str | None]] = []
    for lineno, _n, val in out:
        if isinstance(val, ast.Name):
            resolved.append((lineno, val.id))
        elif isinstance(val, ast.Lambda):
            resolved.append((lineno, f"<lambda:{val.lineno}>"))
        else:
            resolved.append((lineno, None))   # e.g. Mock()/partial — dynamic, accepts anything
    return resolved


def lint_file(src: str, seam: str, needed: list[str]) -> list[tuple[int, str]]:
    """(lineno, why) for every narrow double bound to ``seam`` in this test source. Pure."""
    tree = ast.parse(src)
    defs = _func_defs(tree)
    lambdas = {f"<lambda:{n.lineno}>": n for n in ast.walk(tree) if isinstance(n, ast.Lambda)}
    bad: list[tuple[int, str]] = []
    for lineno, name in seam_bindings(src, seam):
        if name is None:
            continue                        # dynamic value (Mock, partial) — accepts anything
        fn = defs.get(name) or lambdas.get(name)
        if fn is None:
            continue                        # bound name defined elsewhere/dynamically — skip
        if not _accepts(fn, needed):
            bad.append((lineno, f"double '{name}' is narrower than production "
                                f"({needed}) and has no **kwargs"))
    return bad


# --- 1) production signatures resolve ------------------------------------------------------
NEEDED: dict[str, list[str]] = {}
for seam, rel in SEAMS.items():
    params = production_params((ROOT / rel).read_text(encoding="utf-8"), seam)
    res(f"production seam {seam} found in {rel}", params is not None)
    if params:
        NEEDED[seam] = params

# --- 2) every double in tests/ is drift-proof ----------------------------------------------
for path in sorted(TESTS.glob("*_test.py")):
    if path.name == Path(__file__).name:
        continue
    src = path.read_text(encoding="utf-8")
    for seam, needed in NEEDED.items():
        if seam not in src:
            continue
        try:
            bad = lint_file(src, seam, needed)
        except SyntaxError as e:
            res(f"{path.name} parses", False, str(e))
            continue
        res(f"{path.name}: {seam} doubles accept the full seam", not bad,
            "; ".join(f"line {n}: {w}" for n, w in bad)
            + " — add **kwargs to the double (BUILD_DOCTRINE §5)")

# --- 3) teeth: the predicates catch the EU-258 class on synthetic fixtures -----------------
_needed = ["prompt", "options", "tag", "ticket_id"]
res("teeth: a narrower double is caught",
    lint_file("async def f(prompt, options, tag=''):\n    pass\nmod.seamx = f\n",
              "seamx", _needed) != [])
res("teeth: **kwargs double passes",
    lint_file("async def f(prompt, options, **kw):\n    pass\nmod.seamx = f\n",
              "seamx", _needed) == [])
res("teeth: superset double passes",
    lint_file("async def f(prompt, options, tag='', ticket_id=None, extra=None):\n    pass\n"
              "mod.seamx = f\n", "seamx", _needed) == [])
res("teeth: patch.object binding is scanned",
    lint_file("def f(prompt):\n    pass\npatch.object(m, 'seamx', f)\n",
              "seamx", _needed) != [])
res("teeth: patch('mod.seamx', fake) binding is scanned",
    lint_file("def f(prompt):\n    pass\npatch('orchestrator.agent.seamx', f)\n",
              "seamx", _needed) != [])
res("teeth: narrow lambda is caught",
    lint_file("mod.seamx = lambda prompt, options: None\n", "seamx", _needed) != [])
res("teeth: unrelated assignment is NOT flagged",
    lint_file("mod.other = lambda prompt: None\n", "seamx", _needed) == [])

print("\n========== STUB-SIGNATURE GUARD (BUILD_DOCTRINE §5) ==========")
passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    if not c:
        print(f"  [FAIL] {n}  ({d})")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
