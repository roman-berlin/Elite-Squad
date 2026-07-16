"""EU-358: 2026-07-16 total-audit fix batch — regression pins.

One harness per audit fix that has a pure/cheap seam to pin:
  backlog/jira.py   — every session call carries a default HTTP timeout (_TimeoutSession); a
                      stalled Atlassian connection used to hang the drain thread forever.
  notify.py         — send() chunks >4096-char messages on newline boundaries (Telegram rejects
                      longer with 400 → the long escalation reports silently vanished) and
                      retries a 429 once, bounded.
  decisions.py      — _validate_question_format only rejects common English openers ("I need
                      to", "Let me", …) at LINE START; as bare substrings they voided legitimate
                      decision questions whose option text contained them.
  usage.py          — daily_token_budget: 0 is the documented "budget OFF": detailed status must
                      not brand it exhausted, and pre_flight_check must not hold every ticket on
                      the margin test (remaining==0 by construction when off).
  scrum.py          — a mid-batch Jira failure while filing split fragments must NOT close the
                      parent / report ok=True (the unfiled fragments only existed in the LLM
                      report — that slice of the feature silently vanished).
  gate.py           — after a gate timeout + killpg, the zombie reap is bounded (a re-daemonized
                      grandchild holding the pipes open froze whole drains) and the failure
                      report keeps the partial-output tail (EU-346 triage evidence).
  loop.py           — _recent_no_changes_ticket_ids is a 48h window, not a life sentence: one
                      historical no_changes used to exclude the ticket from every future drain.
  warroom.py        — the terminal panel's JS regex must contain the two-character sequence
                      backslash-n, not a literal newline (a Python-escape bug rendered a broken
                      regex literal → SyntaxError killed the script block).
  connections.py    — the raw-token store is written atomically (tmp+rename) with 0600 perms.
"""
import asyncio
import json
import os
import stat
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

# ---- SDK stub (must precede orchestrator imports that reach agent.py) ---- #
sdk = types.ModuleType("claude_agent_sdk")


class _Msg:
    def __init__(self, **kw):
        self.__dict__.update(kw)


for _n in ("AssistantMessage", "ResultMessage", "TextBlock", "ToolUseBlock", "UserMessage",
           "SystemMessage", "ThinkingBlock", "ToolResultBlock"):
    setattr(sdk, _n, type(_n, (_Msg,), {}))


class ClaudeAgentOptions:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class ProcessError(Exception):
    def __init__(self, message="", exit_code=None, stderr=None):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class CLINotFoundError(Exception):
    pass


async def _query(*a, **k):
    if False:
        yield None


sdk.ClaudeAgentOptions = ClaudeAgentOptions
sdk.ProcessError = ProcessError
sdk.CLINotFoundError = CLINotFoundError
sdk.query = _query
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, ".")

results: list[tuple[str, bool, str]] = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# ================= backlog/jira.py — default HTTP timeout ================= #
from orchestrator.backlog import jira as jira_mod  # noqa: E402

captured_kw: list[dict] = []


class _FakeSession:
    def request(self, method, url, **kw):
        captured_kw.append(kw)
        return "sentinel"

    def get(self, url, **kw):
        return self.request("GET", url, **kw)


s = jira_mod._with_default_timeout(_FakeSession())
s.request("GET", "http://example.invalid")
s.request("GET", "http://example.invalid", timeout=5)
s.get("http://example.invalid")
chk("jira session injects the default timeout",
    captured_kw and captured_kw[0].get("timeout") == jira_mod._HTTP_TIMEOUT, str(captured_kw))
chk("an explicit per-call timeout still wins", captured_kw[1].get("timeout") == 5)
chk("verb helpers route through the wrapped request",
    captured_kw[2].get("timeout") == jira_mod._HTTP_TIMEOUT, str(captured_kw))
chk("a request-less test stub passes through unwrapped",
    jira_mod._with_default_timeout(SimpleNamespace(post=lambda *a, **k: None)) is not None)
chk("JiraAdapter builds its session through the timeout wrapper",
    "self.session = _with_default_timeout(requests.Session())"
    in Path("orchestrator/backlog/jira.py").read_text(encoding="utf-8"))

# ================= notify.py — chunking + 429 retry ================= #
from orchestrator import notify  # noqa: E402

chunks = notify._tg_chunks("a" * 5000 + "\n" + "b" * 5000)
chk("over-long text splits into >1 chunk", len(chunks) >= 3, str(len(chunks)))
chk("every chunk fits Telegram's ceiling", all(len(c) <= notify._TG_MAX_CHARS for c in chunks))
chk("nothing is lost in the split",
    "".join(chunks).replace("\n", "") == "a" * 5000 + "b" * 5000)
chk("short text passes through as one chunk", notify._tg_chunks("hi") == ["hi"])
nl_chunks = notify._tg_chunks("x" * 3000 + "\n" + "y" * 3000)
chk("split prefers the newline boundary", nl_chunks[0] == "x" * 3000, str(len(nl_chunks[0])))


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


posts: list[str] = []
_responses: list[_Resp] = []


def _fake_post(url, json=None, timeout=None):  # noqa: A002 - mirrors requests.post signature
    posts.append(json["text"])
    return _responses.pop(0) if _responses else _Resp(200)


_orig_post = notify.requests.post
os.environ["TELEGRAM_BOT_TOKEN"] = "t"
os.environ["TELEGRAM_CHAT_ID"] = "c"
try:
    notify.requests.post = _fake_post
    posts.clear()
    ok = notify.send("m" * 9000)
    chk("send() delivers an over-long message in parts", ok and len(posts) == 3, str(len(posts)))
    chk("each delivered part is under the ceiling", all(len(p) <= 4096 for p in posts))
    posts.clear()
    _responses[:] = [_Resp(429, {"parameters": {"retry_after": 0}}), _Resp(200)]
    ok = notify.send("rate-limited once")
    chk("a 429 gets one retry and succeeds", ok and len(posts) == 2, f"ok={ok} posts={len(posts)}")
finally:
    notify.requests.post = _orig_post

# ================= decisions.py — question validator ================= #
from orchestrator import decisions  # noqa: E402

ok, _ = decisions._validate_question_format(
    "Which auth method should we use?\nOptions:\n1. JWT — but I need to know if prod uses sessions\n2. Cookies")
chk("a legit question with 'I need to' mid-line is ACCEPTED", ok)
ok, _ = decisions._validate_question_format("Let me analyze the failure modes first...")
chk("a line STARTING with 'Let me' is still rejected", not ok)
ok, _ = decisions._validate_question_format("Which env?\n## ANALYSIS\nblah")
chk("'## ANALYSIS' anywhere is still rejected", not ok)
ok, _ = decisions._validate_question_format("Problem: X fails.\nBased on the logs, retry?\nOptions:\n1. yes\n2. no")
chk("a line starting 'Based on the' is still rejected", not ok)
ok, _ = decisions._validate_question_format("Should we cap retries at 3 based on the audit data?")
chk("'based on the' mid-sentence is ACCEPTED", ok)

# ================= usage.py — budget off is not budget exhausted ================= #
from orchestrator import usage  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="eu358_"))
cfg_off = SimpleNamespace(daily_token_budget=0, audit_path=str(_tmp / "audit.jsonl"))
st = usage.claude_budget_status_detailed(cfg_off)
chk("cap=0 → budget reported OFF, not exhausted", st["on"] is False and st["over"] is False, str(st))
pf = usage.pre_flight_check(cfg_off)
chk("pre_flight_check GOES when the budget is off", pf.get("go") is True, str(pf))

# ================= scrum.py — partial split must not close the parent ================= #
from orchestrator import scrum  # noqa: E402
from orchestrator import recon as recon_mod  # noqa: E402
from orchestrator import models as models_mod  # noqa: E402
from orchestrator.backlog import base as backlog_base  # noqa: E402

REPORT = ("=== TICKET ===\nTITLE: Frag A\nBody A\n"
          "=== TICKET ===\nTITLE: Frag B\nBody B\n"
          "=== TICKET ===\nTITLE: Frag C\nBody C\n")


async def _fake_officer(**kw):
    return REPORT


class _FakeBacklog:
    def __init__(self, fail_on_call=None):
        self.fail_on_call = fail_on_call
        self.created: list[str] = []
        self.comments: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        self._n = 0

    def create_task(self, title, body, labels=None):
        self._n += 1
        if self.fail_on_call and self._n == self.fail_on_call:
            raise RuntimeError("jira 500")
        key = f"EU-90{self._n}"
        self.created.append(key)
        return key

    def add_comment(self, ticket, text):
        self.comments.append(text)

    def set_status(self, ticket, status):
        self.statuses.append((getattr(ticket, "key", getattr(ticket, "id", "?")), status))


_orig_officer = recon_mod.run_officer
_orig_for_officer = models_mod.for_officer
_orig_make_backlog = backlog_base.make_backlog
cfg_s = SimpleNamespace(app=lambda n: SimpleNamespace(name=n, repo_path="."),
                        discussion_model="m", reviewer_model="m", auto_model=False)
parent = SimpleNamespace(id="EU-999", key="EU-999", summary="Big feature",
                         description="", ephemeral=False)
try:
    recon_mod.run_officer = _fake_officer
    models_mod.for_officer = lambda cfg, effort=None, ceiling_model=None: ("m", "why")

    bl_partial = _FakeBacklog(fail_on_call=2)
    backlog_base.make_backlog = lambda app: bl_partial
    sp = asyncio.run(scrum.split(cfg_s, "eu", parent))
    chk("partial split reports ok=False", sp["ok"] is False, str(sp))
    chk("partial split records the error", "filed 1/3" in (sp.get("error") or ""), str(sp.get("error")))
    chk("partial split does NOT close the parent",
        ("EU-999", "Done") not in bl_partial.statuses, str(bl_partial.statuses))
    chk("partial split leaves an INCOMPLETE comment naming what landed",
        any("Split INCOMPLETE" in c and "EU-901" in c for c in bl_partial.comments),
        str(bl_partial.comments))

    bl_full = _FakeBacklog()
    backlog_base.make_backlog = lambda app: bl_full
    sp = asyncio.run(scrum.split(cfg_s, "eu", parent))
    chk("full split still reports ok=True with all keys", sp["ok"] and len(sp["keys"]) == 3, str(sp))
    chk("full split still closes the parent", ("EU-999", "Done") in bl_full.statuses, str(bl_full.statuses))
finally:
    recon_mod.run_officer = _orig_officer
    models_mod.for_officer = _orig_for_officer
    backlog_base.make_backlog = _orig_make_backlog

# ================= gate.py — bounded reap + partial output on timeout ================= #
from orchestrator import gate  # noqa: E402

app_g = SimpleNamespace(name="eu", workdir=None, repo_path=str(_tmp), gate_env={},
                        gate_timeout_sec=1)
t0 = datetime.now()
res = gate.run_commands(app_g, ["echo partial-evidence-marker; sleep 30"])
elapsed = (datetime.now() - t0).total_seconds()
chk("gate timeout fails the command", not res.passed)
chk("gate timeout reap is bounded (no pipe-hang)", elapsed < 15, f"{elapsed:.1f}s")
chk("gate timeout report names the timeout", "timed out after 1s" in res.report, res.report[:200])
chk("gate timeout report keeps the partial output tail",
    "partial-evidence-marker" in res.report, res.report[:200])

# ================= loop.py — no_changes guard is a 48h window ================= #
from orchestrator import loop as loop_mod  # noqa: E402

audit_p = _tmp / "audit.jsonl"
now_iso = datetime.now(timezone.utc).isoformat()
old_iso = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
audit_p.write_text(
    json.dumps({"ts": old_iso, "event": "no_changes", "ticket_id": "EU-OLD"}) + "\n"
    + json.dumps({"ts": now_iso, "event": "no_changes", "ticket_id": "EU-FRESH"}) + "\n"
    + json.dumps({"event": "no_changes", "ticket_id": "EU-NOTS"}) + "\n",
    encoding="utf-8")
ids = loop_mod._recent_no_changes_ticket_ids(SimpleNamespace(audit_path=str(audit_p)))
chk("a fresh no_changes still excludes its ticket", "EU-FRESH" in ids, str(ids))
chk("a >48h-old no_changes no longer excludes its ticket", "EU-OLD" not in ids, str(ids))
chk("an event with no timestamp is not treated as recent", "EU-NOTS" not in ids, str(ids))

# ================= warroom.py — JS regex survives Python escaping ================= #
from orchestrator import warroom  # noqa: E402

chk("terminal JS regex carries backslash-n (not a literal newline)",
    "replace(/\\n/g" in warroom._PAGE)
chk("no JS regex in the page contains a literal newline",
    "replace(/\n/g" not in warroom._PAGE)

# ================= connections.py — atomic 0600 token-store writes ================= #
from orchestrator import connections  # noqa: E402

_store = _tmp / "jira_connections.json"
_orig_file = connections._file
try:
    connections._file = lambda cfg=None: _store
    connections._save(None, {"connections": [{"id": "x", "token": "secret"}], "by_project": {}})
    chk("token store is written", _store.exists())
    chk("token store round-trips", json.loads(_store.read_text())["connections"][0]["id"] == "x")
    mode = stat.S_IMODE(_store.stat().st_mode)
    chk("token store is owner-only (0600)", mode == 0o600, oct(mode))
    chk("no stray tmp sidecar left behind", not (_tmp / "jira_connections.json.tmp").exists())
    connections._file = lambda cfg=None: _tmp / "no-such-dir" / "x.json"
    connections._save(None, {"connections": [], "by_project": {}})   # must warn, not raise
    chk("a failed save never raises", True)
finally:
    connections._file = _orig_file

# ================= tally ================= #
print("\n============ EU-358 TOTAL-AUDIT FIX BATCH QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
