"""Live integration test for the War Room SSE stream: run the real Flask server, open an
EventSource-style connection, emit a progress line through the stdout Tee, and measure how
fast the frame carrying it arrives. Proves push-on-change (not just the 2s heartbeat)."""
import logging, sys, time, threading, types, subprocess, urllib.request, tempfile
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

d = Path(tempfile.mkdtemp(prefix="ssetest-"))   # scratch repo in /tmp — never inside the repo tree
d.mkdir(parents=True, exist_ok=True)
(d / "audit.jsonl").write_text("", encoding="utf-8")
repo = d / "app"; repo.mkdir(exist_ok=True)
subprocess.run(["git", "-C", str(repo), "init", "-q"], capture_output=True)

from orchestrator.config import Config, AppConfig
from orchestrator import server
app_cfg = AppConfig(name="automatixy", repo_path=str(repo), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[app_cfg], audit_path=str(d / "audit.jsonl"), use_worktree=False)
cfg.detected_auth = lambda: "claude login (test)"

flask_app = server.create_app(cfg)
sys.stdout = server._Tee(sys.stdout)          # same as serve(): prints now bump log_seq
PORT = 8799
threading.Thread(target=lambda: flask_app.run(host="127.0.0.1", port=PORT, threaded=True,
                                              use_reloader=False), daemon=True).start()
time.sleep(1.3)

def read_frame(resp, timeout=6.0):
    out, deadline = [], time.time() + timeout
    while time.time() < deadline:
        line = resp.readline()
        if line == b"\n":
            if out:
                break
        elif line:
            out.append(line)
    return b"".join(out)

resp = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/stream?app=automatixy", timeout=6)
first = read_frame(resp)
assert b"event: board" in first, first[:160]
sys.__stdout__.write("first frame OK (board pushed on connect)\n")

marker = "SSE-LIVE-MARKER builder: Edit src/widget.tsx"
t0 = time.time()
print(marker, flush=True)                     # a 'unit step' -> Tee -> log_seq++ -> SSE should push
got, lat = False, None
while time.time() - t0 < 5:
    fr = read_frame(resp)
    if marker.encode() in fr:
        got, lat = True, time.time() - t0
        break
resp.close()
sys.__stdout__.write(f"REALTIME_PUSH={got} latency={round(lat,3) if lat else None}s "
                     f"({'push-on-change' if lat and lat < 1.5 else 'slow/heartbeat'})\n")
sys.__stdout__.write("RESULT: " + ("PASS ✅" if got and lat and lat < 1.8 else "FAIL ❌") + "\n")
