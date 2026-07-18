"""EU-228 — infra/outage/transient error classification: stop charging environment failures to
tickets.

2026-07-10 forensics: a single DNS blip (HTTPSConnectionPool(...NameResolutionError) on a Jira
/transitions call) charged FOUR tickets (EU-234/235/236/237) one error strike each in the same
second — a queue-wide outage, not four broken tickets. Per the Commander's 2026-07-12 re-scope this
ticket covers exactly two classes: (a) missing toolchain binaries HOLD the whole app, and (b)
network/DNS/timeout/5xx errors enter an "offline hold" that touches NO per-ticket counter and
auto-resumes on a connectivity probe — NEVER a turn-limit ("max_turns_reached ...", EU-248's job,
which must stay ticket-attributable and never be blind-retried as if it were transient infra).

Pins (each is a TESTABLE acceptance criterion from the ticket):
  1. infra_classify.classify() tags DNS/network/timeout/5xx text as infra, but explicitly refuses
     to tag a turn-limit message (so turn-cap exhaustion is never treated as infra/retried blind).
  2. autopilot._tally_errored(): an infra-classified ERRORED report leaves error_counts untouched
     for that ticket and is reported separately (`infra`); a non-infra ERRORED report still
     increments the tally toward _MAX_TICKET_ERRORS exactly as before.
  3. autopilot._enter_offline_hold() / _offline_hold_recheck(): entering a hold fires exactly ONE
     alert even for several simultaneously-infra-errored tickets, and once
     infra_classify.connectivity_probe() returns True the hold clears with a resume alert — no
     human /unblock.
  4. autopilot._apply_toolchain_holds(): a missing gate-command binary (shutil.which -> None) HOLDS
     that app (exactly one alert), drops ONLY its tickets from the worklist, and touches no
     error_counts / park state at all. A recovered toolchain resumes the app with one more alert.

No network, no real models — stub the Agent SDK + requests, and monkeypatch the notify/probe seams.
"""
import sys, types, tempfile
from pathlib import Path

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import autopilot, infra_classify
from orchestrator.audit import AuditLog
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Outcome, TicketReport

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


tmp = Path(tempfile.mkdtemp())
audit = AuditLog(str(tmp / "audit.jsonl"))


# captured Telegram sends — monkeypatch the notify seam autopilot calls
sent: list[str] = []
autopilot.notify.send = lambda text, chat_id=None: (sent.append(text), True)[1]


def report(tid, outcome, notes=""):
    return TicketReport(ticket_id=tid, outcome=outcome, iterations=1, cost_usd=0.0, notes=notes)


# ================================================================================================ #
# AC1: infra_classify.classify() — infra tags vs. turn-limit exclusion
# ================================================================================================ #
print("\n=== AC1: classify() tags infra text, refuses turn-limit text ===")
DNS_MSG = ("HTTPSConnectionPool(host='toibis.atlassian.net', port=443): Max retries exceeded "
          "with url: /rest/api/3/issue/EU-234/transitions (Caused by NameResolutionError)")
for msg, label in [
    (DNS_MSG, "Jira DNS/NameResolutionError"),
    ("requests.exceptions.ConnectionError: Connection refused", "ConnectionError"),
    ("socket.timeout: Read timed out", "timeout"),
    ("502 Bad Gateway from upstream", "5xx"),
]:
    tag = infra_classify.classify(msg)
    chk(f"classify() tags {label} as infra", bool(tag), f"got {tag!r}")

TURN_LIMIT_MSG = "max_turns_reached maxTurns:96 turnCount:97"
chk("classify() returns non-infra (falsy) for a turn-limit message",
    not infra_classify.classify(TURN_LIMIT_MSG), repr(infra_classify.classify(TURN_LIMIT_MSG)))
chk("classify() returns non-infra for a plain ticket-logic failure",
    not infra_classify.classify("AssertionError: expected 3 got 4"))

# The turn-limit guard must win even when the SAME text also carries an infra-looking substring —
# otherwise a build that happened to time out on its way to exhausting turns would slip through as
# infra and get blind-retried forever at the same turn budget instead of being scrum-split (EU-248).
# This message contains both "max_turns" (turn-limit) AND "timed out" (an infra marker); without a
# turn-limit-first check this assertion goes RED because "timed out" would otherwise match.
COMBINED_MSG = "Connection timed out while finishing the build; max_turns_reached maxTurns:96"
chk("classify() refuses to tag infra even when an infra-looking substring co-occurs with a "
    "turn-limit marker (turn-limit check must run FIRST and win)",
    not infra_classify.classify(COMBINED_MSG), repr(infra_classify.classify(COMBINED_MSG)))
# Sanity: with the turn-limit marker stripped out, the SAME infra substring alone DOES classify —
# proving the exclusion above is doing real work, not just accidentally missing "timed out".
chk("sanity: the same infra substring alone (no turn-limit marker) DOES classify as infra",
    bool(infra_classify.classify("Connection timed out while finishing the build")),
    repr(infra_classify.classify("Connection timed out while finishing the build")))


# ================================================================================================ #
# AC2: autopilot._tally_errored — infra ERRORED reports leave error_counts untouched
# ================================================================================================ #
print("\n=== AC2: _tally_errored — infra ERRORED tickets get no strike, non-infra still counts ===")
counts = {}
reports = [
    report("EU-234", Outcome.ERRORED, DNS_MSG),
    report("EU-235", Outcome.ERRORED, DNS_MSG),
    report("AUTO-9", Outcome.ERRORED, "TypeError: cannot read property 'x' of undefined"),
]
park_now, errored, retrying, infra_ids, changed = autopilot._tally_errored(reports, counts)
chk("infra-classified tickets left OUT of error_counts entirely",
    "EU-234" not in counts and "EU-235" not in counts, str(counts))
chk("infra-classified tickets reported in `infra`", infra_ids == {"EU-234", "EU-235"}, str(infra_ids))
chk("infra-classified tickets never added to park_now",
    "EU-234" not in park_now and "EU-235" not in park_now, str(park_now))
chk("non-infra ERRORED ticket DOES increment error_counts (unchanged behaviour)",
    counts.get("AUTO-9") == 1, str(counts))
chk("non-infra ERRORED ticket reported in `retrying`", "AUTO-9" in retrying, str(retrying))
chk("`errored` includes ALL ERRORED tickets (infra + non-infra), for the tracker-park filter",
    errored == {"EU-234", "EU-235", "AUTO-9"}, str(errored))

# a non-infra ticket that reaches the strike threshold still parks exactly as before
counts2 = {"AUTO-9": autopilot._MAX_TICKET_ERRORS - 1}
park_now2, _, _, infra2, _ = autopilot._tally_errored(
    [report("AUTO-9", Outcome.ERRORED, "TypeError: boom")], counts2)
chk("non-infra ticket at the strike threshold still parks (no regression)",
    "AUTO-9" in park_now2 and "AUTO-9" not in counts2, f"park={park_now2} counts={counts2}")
chk("that threshold-park case reports no infra ids", infra2 == set())

# a fail-first control: an EMPTY infra classifier (nothing ever classifies as infra) would have
# charged EU-234/EU-235 a strike each — pin that the real classifier is actually consulted.
counts3 = {}
_orig_classify = infra_classify.classify
infra_classify.classify = lambda text: ""   # simulate "no infra classification exists" (pre-fix)
try:
    park3, errored3, retrying3, infra3, _ = autopilot._tally_errored(
        [report("EU-236", Outcome.ERRORED, DNS_MSG)], counts3)
finally:
    infra_classify.classify = _orig_classify
chk("control: with classify() stubbed to never match, the same DNS report WOULD strike "
    "(proves _tally_errored actually delegates to infra_classify.classify, not its own logic)",
    counts3.get("EU-236") == 1 and infra3 == set(), f"counts={counts3} infra={infra3}")


# ================================================================================================ #
# AC3: offline-hold enter/recheck — one alert, no counters, auto-resume on connectivity_probe
# ================================================================================================ #
print("\n=== AC3: offline-hold — one alert for a multi-ticket blip, auto-resume on probe ===")
sent.clear()
active = autopilot._enter_offline_hold(None, audit, {"EU-234", "EU-235"}, False)
chk("_enter_offline_hold activates the hold", active is True)
chk("_enter_offline_hold sends exactly ONE alert for 2 simultaneously-infra-errored tickets",
    len(sent) == 1, str(sent))
chk("the alert mentions both tickets", "EU-234" in sent[0] and "EU-235" in sent[0], sent[0] if sent else "")

# a SECOND cycle's infra errors while already active must NOT send a second alert
sent.clear()
active2 = autopilot._enter_offline_hold(None, audit, {"EU-236"}, active)
chk("_enter_offline_hold stays active but sends NO extra alert once already active",
    active2 is True and sent == [], str(sent))

# recheck: probe still failing -> stays active, no alert
_orig_probe = infra_classify.connectivity_probe
infra_classify.connectivity_probe = lambda cfg, **kw: False
sent.clear()
still = autopilot._offline_hold_recheck(None, audit, True)
chk("_offline_hold_recheck stays active while the probe still fails", still is True)
chk("_offline_hold_recheck sends no alert while still down", sent == [], str(sent))

# recheck: probe now passes -> clears automatically, ONE resume alert, no human /unblock involved
infra_classify.connectivity_probe = lambda cfg, **kw: True
sent.clear()
cleared = autopilot._offline_hold_recheck(None, audit, True)
chk("_offline_hold_recheck clears the hold once connectivity_probe() returns True", cleared is False)
chk("_offline_hold_recheck sends exactly one resume alert", len(sent) == 1, str(sent))
infra_classify.connectivity_probe = _orig_probe

# recheck when not active is a pure no-op (no alert, stays inactive)
sent.clear()
chk("_offline_hold_recheck(active=False) is a no-op",
    autopilot._offline_hold_recheck(None, audit, False) is False and sent == [])


# ================================================================================================ #
# AC4: toolchain preflight — a missing binary holds the APP, never parks a ticket
# ================================================================================================ #
print("\n=== AC4: _apply_toolchain_holds — missing binary holds the app, not the ticket ===")
tmp2 = Path(tempfile.mkdtemp())
app_ok = AppConfig(name="automatixy", repo_path=str(tmp2), base_branch="dev", protected_branch="main",
                   backlog_backend="jira", gate_commands=["bun test"],
                   backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
app_bad = AppConfig(name="eu-broken", repo_path=str(tmp2), base_branch="dev", protected_branch="main",
                    backlog_backend="jira", gate_commands=["totally-not-a-real-binary --check"],
                    backlog={"base_url": "https://y.atlassian.net", "project_key": "EU"})
cfg = Config(apps=[app_ok, app_bad], audit_path=str(tmp2 / "audit.jsonl"), use_worktree=False)

ns = types.SimpleNamespace
worklist = [
    (app_ok, ns(id="AUTO-1")),
    (app_bad, ns(id="EU-9")),
    (app_bad, ns(id="EU-10")),
]

_orig_which = infra_classify.shutil.which
def _fake_which(binary, path=None):   # EU-322: the probe now passes path= (the gate's effective PATH)
    if binary == "totally-not-a-real-binary":
        return None
    return _orig_which(binary) or "/usr/bin/" + binary   # 'bun' need not really exist for this test
infra_classify.shutil.which = _fake_which

error_counts = {}
sent.clear()
try:
    filtered, held = autopilot._apply_toolchain_holds(cfg, audit, worklist, frozenset())
finally:
    infra_classify.shutil.which = _orig_which

chk("the healthy app's ticket survives the filter", ("AUTO-1" in [t.id for _, t in filtered]))
chk("BOTH of the broken app's tickets are dropped from the worklist (held, not parked)",
    "EU-9" not in [t.id for _, t in filtered] and "EU-10" not in [t.id for _, t in filtered],
    str([t.id for _, t in filtered]))
chk("exactly one alert for the broken app (not one per dropped ticket)", len(sent) == 1, str(sent))
chk("the alert names the missing binary", "totally-not-a-real-binary" in sent[0], sent[0] if sent else "")
chk("held-set now contains the broken app", held == frozenset({"eu-broken"}), str(held))
chk("no error_counts / park state touched by a toolchain hold", error_counts == {})

# recovery: toolchain fixed -> app drops out of held, with a resume alert
sent.clear()
infra_classify.shutil.which = _orig_which  # 'totally-not-a-real-binary' still missing on the real
                                            # machine too, so simulate recovery explicitly instead:
_fixed_which = lambda binary, path=None: "/usr/bin/" + binary   # EU-322: path= kwarg tolerated
infra_classify.shutil.which = _fixed_which
try:
    filtered2, held2 = autopilot._apply_toolchain_holds(cfg, audit, worklist, held)
finally:
    infra_classify.shutil.which = _orig_which
chk("a fixed toolchain clears the app from the held-set", held2 == frozenset(), str(held2))
chk("a fixed toolchain sends exactly one resume alert", len(sent) == 1, str(sent))
chk("previously-held tickets are back in the worklist once fixed",
    {"EU-9", "EU-10"}.issubset({t.id for _, t in filtered2}), str([t.id for _, t in filtered2]))


print("\n================ EU-228 INFRA CLASSIFICATION QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
