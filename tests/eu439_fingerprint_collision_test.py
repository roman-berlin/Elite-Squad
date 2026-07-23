"""EU-439: subject-fingerprint dedup must not collide on shared boilerplate.

The bug: filing.subject_fingerprint extracts code anchors from title AND body. Every
forensics.signature_sweep report shares the same opening boilerplate ("Auto-filed by
forensics.signature_sweep (EU-231) ..."), and "forensics.signature_sweep" is the longest
anchor in that text, so it dominated the hash — three unrelated [infra-signature] titles
(EU-422/423/424) all collapsed to one fp- and a genuinely new crash pattern was silently
suppressed as "already filed".

This harness pins the fix and the ACs:
  - AC1/AC2: two different failure signatures never share an fp-; the three colliding
    infra titles + shared boilerplate body yield THREE DISTINCT fingerprints.
  - AC1 pins: reworded reports of ONE subject still share a key (EU-409..414 must
    survive); a different subject differs; prose-only findings still return None.
  - AC3: a label/summary dedup records a `filing_suppressed` audit event (no silent drop).
  - AC4: relabel_fingerprint swaps a mis-stamped colliding fp- for the correct per-title
    one, leaving other labels intact.
"""
import sys, types

# Stub the Agent SDK so importing the orchestrator package never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import filing
from orchestrator.filing import subject_fingerprint
from orchestrator.config import AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# The exact shared boilerplate every forensics.signature_sweep report opens with — the
# text whose "forensics.signature_sweep" anchor used to dominate and collide every title.
BOILERPLATE = (
    "Auto-filed by forensics.signature_sweep (EU-231): the same normalized failure "
    "signature hit N× across M tickets (EU-422, EU-423) within 7 days.\n\n"
    "Normalized signature:\n\n    red base — gate fails on the clean base tree\n\n"
    "Raw evidence lines:\n\n- EU-422 · ...\n\n"
    "This is a cross-ticket pattern — fix the shared cause, not the individual tickets."
)

# The three titles the ticket calls out — the exact label fp-3a817a19b898 they share today.
INFRA_TITLES = [
    "[infra-signature] red base — gate fails on the clean base tree",
    "[infra-signature] command failed with exit code <N>",
    "[infra-signature] control request timeout: initialize",
]

# ── AC2: the three infra titles + shared boilerplate yield THREE DISTINCT non-None fps ──
fps = [subject_fingerprint(t, BOILERPLATE) for t in INFRA_TITLES]
chk("AC2: all three infra fingerprints are non-None", all(f is not None for f in fps),
    str(fps))
chk("AC2: the three infra titles yield THREE DISTINCT fingerprints (regression pinned)",
    len(set(fps)) == 3, f"{len(set(fps))} distinct -> {fps}")

# ── AC1 / crit 3: any two distinct infra titles differ (no two signatures share an fp-) ──
import itertools
pair_ok = all(subject_fingerprint(a, BOILERPLATE) != subject_fingerprint(b, BOILERPLATE)
              for a, b in itertools.combinations(INFRA_TITLES, 2))
chk("AC1: every pair of distinct infra-signature titles gets a different fp-", pair_ok)

# ── AC1 pins: the EU-409..414 'reworded-one-subject -> same key' behaviour must survive ──
_w1 = subject_fingerprint("Fix eu255_env_minimization NATIVE run mislabelled",
                          "tests/eu255_env_minimization_test.py fails")
_w2 = subject_fingerprint("[BLOCKER/tests] gate is RED",
                          "eu255_env_minimization_test.py fails per EU-249")
chk("AC1 pin: rewordings of one subject still share a key",
    _w1 is not None and _w1 == _w2, f"{_w1} vs {_w2}")
chk("AC1 pin: a different subject differs",
    subject_fingerprint("CI deploy", "scripts/deploy-edge-functions.sh on merge") != _w1)
chk("AC1 pin: prose-only findings still return None",
    subject_fingerprint("Improve the error copy", "friendlier") is None)

# ── EU-42 invariant: the SAME title is stable (same title -> same fp) ──
chk("EU-42: the same title always maps to the same fp",
    subject_fingerprint(INFRA_TITLES[0], BOILERPLATE)
    == subject_fingerprint(INFRA_TITLES[0], BOILERPLATE))


# ── AC3: a dedup must not be silent — it records a `filing_suppressed` audit event ──────
# The bug went unnoticed because filing.py dropped a deduped finding with NO trace. AC3 makes
# every suppression emit an audit event carrying the matched key, the suppressed title, how it
# matched (label vs summary) and the fingerprint — so a future silent suppression is visible.
import json as _json
ns = types.SimpleNamespace


class _DedupBacklog:
    """Stub backlog: configurable label/summary dedup hits; records creates + lookups."""
    def __init__(self, label_hit=None, summary_hit=None):
        self._label_hit, self._summary_hit = label_hit, summary_hit
        self.created, self.label_lookups, self.summary_lookups = [], [], []
    def find_open_by_label(self, label):
        self.label_lookups.append(label); return self._label_hit
    def find_open_by_summary(self, summary):
        self.summary_lookups.append(summary); return self._summary_hit
    def create_task(self, summary, description, labels=None, issue_type="Task", priority=None):
        key = f"EU-{900 + len(self.created)}"; self.created.append(summary); return key


def _audit_sink():
    ev = []
    return ev, ns(record=lambda k, **kw: ev.append((k, kw)))


def _one(title, body):
    return ("===TICKETS===\n" + _json.dumps(
        [{"title": title, "type": "Bug", "severity": "HIGH", "body": body}]) + "\n===END===")


_app = AppConfig(name="Elite-Unit", repo_path="/tmp/x", base_branch="dev",
                 protected_branch="main", backlog_backend="none")

# label-hit dedup -> exactly one filing_suppressed, matched_by='label', nothing filed
ev, fa = _audit_sink()
_bl = _DedupBacklog(label_hit="EU-422")
filing.make_backlog = lambda a: _bl
_r = filing.file_findings(_app, "infra-signature", _one(INFRA_TITLES[0], BOILERPLATE), audit=fa)
_sup = [(k, kw) for k, kw in ev if k == "filing_suppressed"]
chk("AC3: a label-hit dedup records exactly one filing_suppressed", len(_sup) == 1, str(ev))
if _sup:
    _k, _kw = _sup[0]
    chk("AC3: filing_suppressed carries key + title + matched_by='label' + fp",
        _kw.get("key") == "EU-422" and _kw.get("title") == INFRA_TITLES[0]
        and _kw.get("matched_by") == "label" and str(_kw.get("fp")).startswith("fp-"), str(_kw))
chk("AC3: a label-hit dedup files nothing (no create)", not _r.filed and not _bl.created)

# no open ticket -> a real filing records NO filing_suppressed
ev2, fa2 = _audit_sink()
_bl2 = _DedupBacklog(label_hit=None, summary_hit=None)
filing.make_backlog = lambda a: _bl2
filing.file_findings(_app, "infra-signature", _one(INFRA_TITLES[0], BOILERPLATE), audit=fa2)
chk("AC3: a newly-filed finding records NO filing_suppressed",
    not any(k == "filing_suppressed" for k, _ in ev2), str(ev2))

# summary-hit dedup -> filing_suppressed with matched_by='summary'
ev3, fa3 = _audit_sink()
_bl3 = _DedupBacklog(label_hit=None, summary_hit="EU-500")
filing.make_backlog = lambda a: _bl3
filing.file_findings(_app, "review", _one("Some distinct prose title", "plain body"), audit=fa3)
_sup3 = [(k, kw) for k, kw in ev3 if k == "filing_suppressed"]
chk("AC3: a summary-hit dedup records filing_suppressed with matched_by='summary'",
    len(_sup3) == 1 and _sup3[0][1].get("matched_by") == "summary", str(ev3))

# audit omitted (default None) -> dedup still works, never raises (backward compat)
_bl4 = _DedupBacklog(label_hit="EU-422")
filing.make_backlog = lambda a: _bl4
_r4 = filing.file_findings(_app, "infra-signature", _one(INFRA_TITLES[0], BOILERPLATE))
chk("AC3: audit defaults to None — dedup works with no audit sink (backward compat)",
    _r4.deduped == ["EU-422"] and not _r4.filed)

# ── AC3 (forensics path): a suppressed re-file through _file_one emits filing_suppressed ──
# forensics._file_one is the infra-signature / postmortem filer — the bug's home. EU-439 threads
# `audit` through it, so a dedup in THAT path is no longer a silent drop either.
from orchestrator import forensics


class _ForensicsBacklog:
    """A backlog where the infra-signature label ALREADY exists on EU-422 — the next signature
    report collides and is suppressed (exactly the EU-422/423/424 scenario)."""
    def __init__(self):
        self.created = []
    def find_open_by_label(self, label):
        return "EU-422"
    def find_open_by_summary(self, summary):
        return None
    def create_task(self, summary, description, labels=None, issue_type="Task", priority=None):
        self.created.append(summary); return None


_fev, _ffa = _audit_sink()
_fbl = _ForensicsBacklog()
filing.make_backlog = lambda a: _fbl
_fk = forensics._file_one(_app, "infra-signature",
                          {"title": INFRA_TITLES[1], "type": "Bug", "severity": "HIGH",
                           "body": BOILERPLATE}, audit=_ffa)
chk("AC3 forensics: a suppressed _file_one returns None (not newly filed)", _fk is None)
chk("AC3 forensics: the suppressed re-file emitted filing_suppressed",
    any(k == "filing_suppressed" for k, _ in _fev), str(_fev))
chk("AC3 forensics: nothing was created (the suppression dropped it)", not _fbl.created)


# ── AC4: relabel_fingerprint corrects a mis-stamped colliding fp- (EU-422/423/424) ───────
# The three tickets were all stamped fp-3a817a19b898 (the collision). relabel_fingerprint
# re-reads each ticket's title+body, recomputes the CORRECT per-subject fp, and swaps the wrong
# label for it — non-clobbering (infra-signature/autofiled labels stay). The three get DISTINCT
# new labels, so the collision stops shadowing new reports.
from orchestrator.contracts import Ticket

COLLIDING = "fp-3a817a19b898"


class _RelabelBacklog:
    """get_task returns the stored Ticket; set_labels records the op and mutates the label set."""
    def __init__(self):
        self.tickets = {}
        self.label_ops = []
    def get_task(self, key):
        return self.tickets[key]
    def set_labels(self, key, add=(), remove=()):
        self.label_ops.append((key, list(add or []), list(remove or [])))
        t = self.tickets[key]
        cur = [l for l in (t.labels or []) if l not in (remove or [])]
        cur += [l for l in (add or []) if l not in cur]
        t.labels = cur
        return True


_rbl = _RelabelBacklog()
for _i, _t in enumerate(INFRA_TITLES):
    _k = f"EU-{422 + _i}"
    _rbl.tickets[_k] = Ticket(id=_k, key=_k, summary=_t, description=BOILERPLATE,
                              labels=["infra-signature", "autofiled", COLLIDING])
    filing.relabel_fingerprint(_rbl, _k)

_new_fps = []
for _i, _t in enumerate(INFRA_TITLES):
    _k = f"EU-{422 + _i}"
    _correct = subject_fingerprint(_t, BOILERPLATE)
    _new_fps.append(_correct)
    _labels = _rbl.tickets[_k].labels
    chk(f"AC4 {_k}: the colliding fp- removed", COLLIDING not in _labels, str(_labels))
    chk(f"AC4 {_k}: the correct per-title fp added", _correct in _labels, str(_labels))
    chk(f"AC4 {_k}: infra-signature + autofiled labels left untouched",
        "infra-signature" in _labels and "autofiled" in _labels, str(_labels))
chk("AC4: the three tickets get THREE DISTINCT new fp labels",
    len(set(_new_fps)) == 3, str(_new_fps))
_eu422_op = [op for op in _rbl.label_ops if op[0] == "EU-422"]
chk("AC4 EU-422: set_labels removed the colliding label and added the correct fp",
    _eu422_op and COLLIDING in _eu422_op[0][2] and _new_fps[0] in _eu422_op[0][1],
    str(_rbl.label_ops))

# idempotent: relabelling an already-correct ticket is a no-op (no stray set_labels op)
_pre_ops = len(_rbl.label_ops)
filing.relabel_fingerprint(_rbl, "EU-422")
chk("AC4: relabel is idempotent — a correctly-labelled ticket triggers no set_labels",
    len(_rbl.label_ops) == _pre_ops, str(_rbl.label_ops))

print("\n=========== EU-439 FINGERPRINT COLLISION QA ===========")
passed = sum(1 for _, ok_, _ in results if ok_)
for n, ok_, det in results:
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok_ else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
