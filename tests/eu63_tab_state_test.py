"""EU-63 [Ordnance BE] — per-tab cockpit workspace state model (cockpit_state.py).

Asserts the foundation the tabbed-workspace routes/views build on:
  - a Tab pins to ONE concrete project and carries its own view state (ticket/board/runs/needs-you);
  - mutual exclusion: opening an already-open project FOCUSES its tab, never duplicates it;
  - the retired "All projects"/* sentinel is rejected, not special-cased;
  - add/remove/set_active behave like an editor tab strip (active falls back on close);
  - the server-side per-session store hands back the SAME live workspace object;
  - to_dict/from_dict round-trips and rehydrate re-enforces the invariants on untrusted input.
"""
import sys
sys.path.insert(0, ".")

from orchestrator.cockpit_state import (
    ALL_PROJECTS_SENTINEL, Tab, Workspace, rehydrate_workspace, reset_workspaces, workspace_for,
)

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

def _try(f):
    """True iff calling f() raises ValueError (used to assert the model rejects bad input)."""
    try:
        f(); return False
    except ValueError:
        return True


# -- Tab basics ------------------------------------------------------------------------------------
t = Tab(project="automatixy")
chk("tab pins to project", t.project == "automatixy")
chk("tab defaults: no ticket", t.ticket is None)
chk("tab defaults: empty view state", t.board == {} and t.runs == [] and t.needs_you == [])
chk("Tab.from_dict rejects sentinel", _try(lambda: Tab.from_dict({"project": ALL_PROJECTS_SENTINEL})))
chk("Tab.from_dict rejects empty", _try(lambda: Tab.from_dict({"project": "  "})))


# -- mutual exclusion + tab strip ------------------------------------------------------------------
ws = Workspace()
a = ws.add_tab("automatixy")
chk("first add sets active", ws.active == "automatixy")
e = ws.add_tab("Elite-Unit")
chk("second tab appended", ws.projects() == ["automatixy", "Elite-Unit"])
chk("add activates new tab", ws.active == "Elite-Unit")

again = ws.add_tab("automatixy")
chk("re-open focuses, no dup", ws.projects() == ["automatixy", "Elite-Unit"])
chk("re-open returns same object", again is a)
chk("re-open activates existing", ws.active == "automatixy")

chk("add rejects *", _try(lambda: ws.add_tab("*")))
chk("add rejects empty", _try(lambda: ws.add_tab("")))

# per-tab view state is independent
a.ticket = "AUTO-1"; e.ticket = "EU-9"
chk("per-tab ticket independent", ws.get_tab("automatixy").ticket == "AUTO-1"
    and ws.get_tab("Elite-Unit").ticket == "EU-9")

# set_active only for open tabs
chk("set_active open ok", ws.set_active("Elite-Unit") and ws.active == "Elite-Unit")
chk("set_active unknown no-op", ws.set_active("ghost") is False and ws.active == "Elite-Unit")

# remove: active falls back to left neighbour
ws.add_tab("third")                       # tabs: automatixy, Elite-Unit, third  (active=third)
ws.set_active("Elite-Unit")
chk("remove active falls left", ws.remove_tab("Elite-Unit") and ws.active == "automatixy")
chk("remove unknown false", ws.remove_tab("ghost") is False)
ws.remove_tab("automatixy"); ws.remove_tab("third")
chk("empty workspace active None", ws.tabs == [] and ws.active is None)


# -- persistence round-trip ------------------------------------------------------------------------
src = Workspace()
src.add_tab("automatixy"); src.add_tab("Elite-Unit")
src.get_tab("automatixy").board = {"col": "todo"}
snap = src.to_dict()
back = Workspace.from_dict(snap)
chk("round-trip tabs", back.projects() == ["automatixy", "Elite-Unit"])
chk("round-trip active", back.active == src.active)
chk("round-trip view state", back.get_tab("automatixy").board == {"col": "todo"})

# from_dict hardens untrusted input: skip bad, collapse dup, clamp active
dirty = {"tabs": [{"project": "a"}, {"project": "*"}, {"project": "a"}, {"bad": 1}, {"project": "b"}],
         "active": "ghost"}
hardened = Workspace.from_dict(dirty)
chk("from_dict skips bad+dup", hardened.projects() == ["a", "b"])
chk("from_dict clamps active", hardened.active == "a")
chk("from_dict(None) empty", Workspace.from_dict(None).tabs == [])


# -- server-side per-session store -----------------------------------------------------------------
reset_workspaces()
w1 = workspace_for("sess-1")
w1.add_tab("automatixy")
chk("store returns same object", workspace_for("sess-1") is w1)
chk("sessions isolated", workspace_for("sess-2") is not w1 and workspace_for("sess-2").tabs == [])

rehydrated = rehydrate_workspace("sess-1", {"tabs": [{"project": "Elite-Unit"}], "active": "Elite-Unit"})
chk("rehydrate replaces store", workspace_for("sess-1") is rehydrated
    and rehydrated.projects() == ["Elite-Unit"])
reset_workspaces()
chk("reset clears store", workspace_for("sess-1").tabs == [])


# -- tally ---------------------------------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, d in results:
    if not ok:
        print(f"  FAIL: {n} {('— ' + d) if d else ''}")
print(f"{passed}/{len(results)} passed")
print("RESULT:", "PASS" if passed == len(results) else "FAIL")
sys.exit(0 if passed == len(results) else 1)
