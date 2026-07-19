"""Vacuous-assertion guard (2026-07-19 stabilization; BUILD_DOCTRINE.md mechanism 3).

The 3-day sprint produced three tests that were green while asserting nothing, and all of them
share two STATIC signatures this harness lints every tests/*_test.py for, forever:

  CLASS A — unguarded ``.find()`` comparison: ``src.find(x) < src.find(y)`` (or ``<`` a variable
  position). A missing anchor makes ``find`` return ``-1``, so the precedence claim silently
  passes exactly when the token it depends on disappears (eu68's roster-nav check only passed
  while the duplicate it tolerated existed; eu375/eu376 shipped the same shape and were fixed by
  the stabilization sweep). A find-comparison is legitimate ONLY alongside a ``!= -1`` (or
  ``== -1`` / ``>= 0``) presence guard in the SAME check condition.

  CLASS B — an UNCONDITIONAL literal ``True`` as a check condition:
  ``check("...", True)  # by construction`` asserts nothing (shipreview_test.py:64 claimed the
  read-only tool contract without inspecting anything). A check that checks nothing is worse
  than no check — it documents confidence that does not exist. A literal-True check INSIDE an
  if/elif branch is the house branch-recording idiom (the enclosing condition IS the claim —
  retired_subsystems_test's allowlist bookkeeping) and is deliberately not flagged.

Registered check-call names: ok / chk / check (the house harness idioms). ALLOW below exempts a
(file, line-ish) pair with a written reason; a stale entry FAILS so the list can't rot — the
retired_subsystems_test.py registry pattern. Guard predicates are pure functions self-tested on
synthetic fixtures (teeth section), so the mutation-proof re-runs on every suite.

Pure filesystem + AST — no network, no models, no SDK stub needed."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"
SELF = Path(__file__).name

CHECK_NAMES = {"ok", "chk", "check"}

# (file, snippet-substring) -> reason. The snippet must appear on the flagged line; a stale entry
# (nothing flagged matches it any more) FAILS so the allowlist can never silently bless new debt.
ALLOW: dict[tuple[str, str], str] = {}

results: list[tuple[str, bool, str]] = []


def res(n: str, c: bool, d: str = "") -> None:
    results.append((n, bool(c), d))


def _is_find_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "find")


def _find_compares(expr: ast.AST) -> list[ast.Compare]:
    """Compare nodes ordering a .find() result (Lt/Gt/LtE/GtE against anything but a -1/0 guard)."""
    out = []
    for node in ast.walk(expr):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        if not any(_is_find_call(o) for o in operands):
            continue
        if any(isinstance(op, (ast.Lt, ast.Gt, ast.LtE, ast.GtE)) for op in node.ops):
            # a pure presence guard like find(x) >= 0 is not an ordering claim — skip those
            # (only when a non-find constant operand exists; find-vs-find must stay flagged)
            non_find = [o for o in operands if not _is_find_call(o)]
            if non_find and all(isinstance(o, ast.Constant) and o.value in (-1, 0)
                                for o in non_find):
                continue
            out.append(node)
    return out


def _has_presence_guard(expr: ast.AST) -> bool:
    """True when the expression also compares something against -1 (or >= 0) — the anchor guard."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            for o in operands:
                if isinstance(o, ast.UnaryOp) and isinstance(o.op, ast.USub) \
                        and isinstance(o.operand, ast.Constant) and o.operand.value == 1:
                    return True
                if isinstance(o, ast.Constant) and o.value == -1:
                    return True
            if any(isinstance(op, ast.GtE) for op in node.ops) \
                    and any(isinstance(o, ast.Constant) and o.value == 0 for o in operands):
                return True
    return False


def lint_source(src: str) -> list[tuple[int, str]]:
    """(lineno, kind) for every vacuous-assertion hit in ``src``. Pure — the teeth test this."""
    hits: list[tuple[int, str]] = []
    tree = ast.parse(src)
    # parent map so a literal-True check inside an if/elif branch (branch-recording idiom —
    # the enclosing condition is the claim) is distinguishable from an unconditional one.
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def _inside_branch(n: ast.AST) -> bool:
        cur = parents.get(n)
        while cur is not None:
            if isinstance(cur, (ast.If, ast.IfExp, ast.Try, ast.ExceptHandler)):
                return True
            cur = parents.get(cur)
        return False

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in CHECK_NAMES and len(node.args) >= 2):
            continue
        cond = node.args[1]
        if isinstance(cond, ast.Constant) and cond.value is True:
            if not _inside_branch(node):
                hits.append((node.lineno, "unconditional literal-True condition"))
            continue
        if _find_compares(cond) and not _has_presence_guard(cond):
            hits.append((node.lineno, "unguarded .find() ordering comparison"))
    return hits


# --- 1) lint every harness -----------------------------------------------------------------
seen_allow: set[tuple[str, str]] = set()
for path in sorted(TESTS.glob("*_test.py")):
    if path.name == SELF:
        continue
    src = path.read_text(encoding="utf-8")
    try:
        hits = lint_source(src)
    except SyntaxError as e:
        res(f"{path.name} parses", False, str(e))
        continue
    lines = src.splitlines()
    real = []
    for lineno, kind in hits:
        line_text = lines[lineno - 1] if lineno - 1 < len(lines) else ""
        allowed = next((k for k in ALLOW if k[0] == path.name and k[1] in line_text), None)
        if allowed:
            seen_allow.add(allowed)
            continue
        real.append((lineno, kind))
    res(f"{path.name}: no vacuous assertions", not real,
        "; ".join(f"line {n}: {k}" for n, k in real)
        + " — guard the anchor with != -1, or make the check inspect something real")

for pair in sorted(set(ALLOW) - seen_allow):
    res(f"ALLOW entry is stale — remove it: {pair}", False, ALLOW[pair])

# --- 2) teeth: the linter catches the defect classes on synthetic fixtures -----------------
res("teeth: unguarded find-vs-find compare is caught",
    lint_source('ok("t", s.find("a") < s.find("b"))') ==
    [(1, "unguarded .find() ordering comparison")])
res("teeth: unguarded find-vs-variable compare is caught",
    lint_source('ok("t", s.find("a") < pub_at)') ==
    [(1, "unguarded .find() ordering comparison")])
res("teeth: a != -1 guard in the same condition passes",
    lint_source('ok("t", s.find("a") != -1 and s.find("a") < s.find("b"))') == [])
res("teeth: a >= 0 presence guard passes",
    lint_source('ok("t", s.find("a") >= 0 and s.find("a") < s.find("b"))') == [])
res("teeth: a plain presence check find(x) >= 0 alone is NOT an ordering claim",
    lint_source('ok("t", s.find("a") >= 0)') == [])
res("teeth: unconditional literal True condition is caught",
    lint_source('check("by construction", True)') ==
    [(1, "unconditional literal-True condition")])
res("teeth: literal True inside an if-branch (branch-recording idiom) is NOT flagged",
    lint_source('if allowed:\n    check("allowlisted", True)') == [])
res("teeth: a real boolean expression is NOT flagged",
    lint_source('check("t", x == 1 and y in z)') == [])
res("teeth: equality on find results is NOT flagged (== is a value claim, not ordering)",
    lint_source('ok("t", s.find("a") == 7)') == [])

print("\n========== VACUOUS-ASSERTION GUARD (BUILD_DOCTRINE §3) ==========")
passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    if not c:
        print(f"  [FAIL] {n}  ({d})")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
