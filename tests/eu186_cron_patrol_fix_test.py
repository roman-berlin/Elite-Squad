"""EU-186 (history) + EU-432 (current): the weekly patrol cron.

EU-186 (2026-07-10): the server cron ran `./general patrol` without the required positional app
argument, failing every Monday. EU-186 fixed it to `./general patrol automatixy`.

EU-432 (2026-07-22) SUPERSEDES that fix for the SERVER: patrol is NO LONGER scheduled there at all.
The server's automatixy.repo_path is a PLACEHOLDER pointing at the orchestrator's OWN source (the box
has no product repo), so `patrol automatixy` would patrol the orchestrator code and file AUTO tickets
against the product backlog. The server's role is "discusses, never builds/patrols"
(config.server.example.yaml). So the patrol cron is removed from install-server-cron.sh. The patrol
SUBCOMMAND is unchanged — it still requires the app argument (the EU-186 parser guard below), and can
be run manually on the Mac against a real repo.
"""
import subprocess
import sys
from pathlib import Path

# 2026-07-22: drive the parser through THIS interpreter, never through the ./general wrapper. The
# wrapper sources .env and `exec python -m orchestrator.main --config config.yaml` — and .env,
# config.yaml and a `python` on PATH are all absent in a clean checkout, so CI failed with
# `./general: line 31: exec: python: not found` instead of the argparse error under test. The
# contract being pinned belongs to orchestrator.main's parser, not to the shell wrapper.
_MAIN = [sys.executable, "-m", "orchestrator.main"]

def test_patrol_not_scheduled_on_server():
    """EU-432: patrol must NOT be scheduled on the server (no real product target there)."""

    script_content = Path("scripts/install-server-cron.sh").read_text(encoding="utf-8")

    assert "./general patrol" not in script_content, (
        "EU-432: patrol must NOT be scheduled on the server — automatixy.repo_path there is a "
        "placeholder pointing at the orchestrator's own source, so it would self-patrol and file "
        "AUTO tickets against the product backlog. Run patrol manually on the Mac against a real repo.")

    print("✓ EU-432: patrol is not scheduled on the server (no real product target)")

def test_patrol_command_requires_app_argument():
    """Verify that the patrol command requires the app argument (this proves the bug existed)."""
    # Try to run patrol without the app argument - it should fail
    result = subprocess.run(_MAIN + ["patrol"], capture_output=True, text=True, cwd=".")

    # Should fail with error message about required argument
    assert result.returncode != 0, "patrol without app argument should fail"
    assert "required: app" in result.stderr.lower() or "the following arguments are required: app" in result.stderr.lower(), \
        f"Expected error about required 'app' argument, got: {result.stderr}"

    print("✓ patrol command correctly requires the app argument")

def test_patrol_command_works_with_automatixy_argument():
    """The parser ACCEPTS an app argument — the other half of the EU-186 contract.

    2026-07-22: this test previously ran a command and asserted NOTHING — it could not fail, so it
    pinned nothing (the vacuous-assertion class BUILD_DOCTRINE.md exists to catch). It now asserts
    the parse succeeds, which is the actual complement to the missing-arg test above."""
    result = subprocess.run(_MAIN + ["patrol", "automatixy", "--help"],
                            capture_output=True, text=True, cwd=".")

    assert result.returncode == 0, (
        f"patrol WITH an app argument must parse (--help exits 0); got rc={result.returncode}, "
        f"stderr={result.stderr[:300]}")
    assert "required: app" not in result.stderr.lower(), \
        "the app argument was supplied — the parser must not still demand it"

    print("✓ patrol command accepts the app argument")

if __name__ == "__main__":
    print("\n================ EU-186 / EU-432 PATROL CRON TEST ================")

    try:
        test_patrol_not_scheduled_on_server()
        test_patrol_command_requires_app_argument()
        test_patrol_command_works_with_automatixy_argument()

        print("\n✅ All tests passed!")
        print("----------------------------------------------------")
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        print("----------------------------------------------------")
        raise
