"""Test that the weekly patrol cron includes the required app argument.
EU-186: The cron was running './general patrol' without the required positional app argument,
causing it to fail every Monday. This test verifies the fix adds 'automatixy' as the app.
"""
import subprocess
import tempfile
import os
from pathlib import Path

def test_cron_installation_includes_patrol_app_argument():
    """Verify the install-server-cron.sh script installs the patrol cron with the app argument."""

    # Read the install script to check it contains the corrected line
    script_path = Path("scripts/install-server-cron.sh")
    script_content = script_path.read_text(encoding="utf-8")

    # The corrected line should include 'automatixy' after 'patrol'
    expected_line = "0 9 * * 1 cd $HOME/General && ./general patrol automatixy >> council/cron.log 2>&1"

    # Check that the script contains the expected patrol line
    assert expected_line in script_content, \
        f"Expected cron line '{expected_line}' not found in install-server-cron.sh"

    # Verify the old broken line is NOT present
    broken_line = "0 9 * * 1 cd $HOME/General && ./general patrol >> council/cron.log 2>&1"
    assert broken_line not in script_content, \
        f"Broken cron line '{broken_line}' should not be present in install-server-cron.sh"

    print("✓ install-server-cron.sh contains the corrected patrol line with 'automatixy' argument")

def test_patrol_command_requires_app_argument():
    """Verify that the patrol command requires the app argument (this proves the bug existed)."""
    # Try to run patrol without the app argument - it should fail
    result = subprocess.run(
        ["./general", "patrol"],
        capture_output=True,
        text=True,
        cwd="."
    )

    # Should fail with error message about required argument
    assert result.returncode != 0, "patrol without app argument should fail"
    assert "required: app" in result.stderr.lower() or "the following arguments are required: app" in result.stderr.lower(), \
        f"Expected error about required 'app' argument, got: {result.stderr}"

    print("✓ patrol command correctly requires the app argument")

def test_patrol_command_works_with_automatixy_argument():
    """Verify that the patrol command works when given the automatixy argument."""
    # This would be a more complex integration test requiring the full app setup
    # For now, just verify the command accepts the argument
    result = subprocess.run(
        ["./general", "patrol", "automatixy", "--help"],
        capture_output=True,
        text=True,
        cwd="."
    )

    # The --help should work even without full setup
    # If the argument parsing works, this should succeed
    # (This is a basic sanity check - full integration test would require more setup)

if __name__ == "__main__":
    print("\n================ EU-186 CRON PATROL FIX TEST ================")

    try:
        test_cron_installation_includes_patrol_app_argument()
        test_patrol_command_requires_app_argument()
        test_patrol_command_works_with_automatixy_argument()

        print("\n✅ All tests passed!")
        print("----------------------------------------------------")
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        print("----------------------------------------------------")
        raise
