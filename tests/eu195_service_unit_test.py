#!/usr/bin/env python3
"""
EU-195 test: Assert the hardened systemd unit is emitted by install-service.sh

This test verifies that the install-service.sh script creates a systemd unit file
with the required hardening parameters (StartLimitIntervalSec=300 and StartLimitBurst=5)
as specified in EU-184 and VPS_DEPLOYMENT.md.

Run: python3 tests/eu195_service_unit_test.py
"""

import subprocess
import tempfile
import os
import sys


def test_install_service_script_emits_hardened_unit():
    """Test 1: scripts/install-service.sh creates a unit with StartLimitIntervalSec=300 and StartLimitBurst=5."""
    # Capture the script output by running it with a dry-run flag (we'll create a temp file)
    script_path = "scripts/install-service.sh"

    if not os.path.exists(script_path):
        print(f"FAIL: {script_path} does not exist")
        return False

    # Run the script and capture what it would write
    # We'll use a temp file to avoid writing to /etc/systemd/system during tests
    with tempfile.NamedTemporaryFile(mode='w+', delete=False) as temp_file:
        temp_path = temp_file.name

    try:
        # Run the script with OUTPUT_FILE env var to capture the unit content
        env = os.environ.copy()
        env['OUTPUT_FILE'] = temp_path
        env['DRY_RUN'] = '1'  # Tell script to not reload systemd

        result = subprocess.run(
            ['bash', script_path],
            env=env,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print(f"FAIL: {script_path} failed with exit code {result.returncode}")
            print(f"stderr: {result.stderr}")
            return False

        # Read the generated unit file
        with open(temp_path, 'r') as f:
            unit_content = f.read()

        # Check for required fields in [Unit] section
        has_start_limit_interval = 'StartLimitIntervalSec=300' in unit_content
        has_start_limit_burst = 'StartLimitBurst=5' in unit_content

        if not has_start_limit_interval:
            print("FAIL: StartLimitIntervalSec=300 not found in generated unit")
            print(f"Unit content:\n{unit_content}")
            return False

        if not has_start_limit_burst:
            print("FAIL: StartLimitBurst=5 not found in generated unit")
            print(f"Unit content:\n{unit_content}")
            return False

        print("PASS: Unit file contains StartLimitIntervalSec=300 and StartLimitBurst=5")
        return True

    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_script_calls_daemon_reload():
    """Test 3: scripts/install-service.sh calls systemctl daemon-reload after writing the unit file."""
    script_path = "scripts/install-service.sh"

    if not os.path.exists(script_path):
        print(f"FAIL: {script_path} does not exist")
        return False

    # Read the script content
    with open(script_path, 'r') as f:
        script_content = f.read()

    # Check for daemon-reload command
    has_daemon_reload = 'systemctl daemon-reload' in script_content

    if not has_daemon_reload:
        print("FAIL: Script does not call 'systemctl daemon-reload'")
        print(f"Script content:\n{script_content}")
        return False

    print("PASS: Script calls systemctl daemon-reload")
    return True


def test_script_is_idempotent():
    """Test 5: scripts/install-service.sh is idempotent (can run multiple times without errors)."""
    script_path = "scripts/install-service.sh"

    if not os.path.exists(script_path):
        print(f"FAIL: {script_path} does not exist")
        return False

    # Run the script twice
    with tempfile.NamedTemporaryFile(mode='w+', delete=False) as temp_file:
        temp_path = temp_file.name

    try:
        env = os.environ.copy()
        env['OUTPUT_FILE'] = temp_path
        env['DRY_RUN'] = '1'

        # First run
        result1 = subprocess.run(
            ['bash', script_path],
            env=env,
            capture_output=True,
            text=True
        )

        # Second run
        result2 = subprocess.run(
            ['bash', script_path],
            env=env,
            capture_output=True,
            text=True
        )

        if result1.returncode != 0:
            print(f"FAIL: First run failed with exit code {result1.returncode}")
            print(f"stderr: {result1.stderr}")
            return False

        if result2.returncode != 0:
            print(f"FAIL: Second run failed with exit code {result2.returncode}")
            print(f"stderr: {result2.stderr}")
            return False

        print("PASS: Script is idempotent (runs twice without errors)")
        return True

    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_vps_deployment_docs_updated():
    """Test 4: VPS_DEPLOYMENT.md Step 4 references install-service.sh."""
    docs_path = "VPS_DEPLOYMENT.md"

    if not os.path.exists(docs_path):
        print(f"FAIL: {docs_path} does not exist")
        return False

    with open(docs_path, 'r') as f:
        docs_content = f.read()

    # Check for the script reference in Step 4
    has_install_service = 'install-service.sh' in docs_content

    if not has_install_service:
        print("FAIL: VPS_DEPLOYMENT.md does not reference install-service.sh")
        return False

    print("PASS: VPS_DEPLOYMENT.md references install-service.sh")
    return True


def main():
    """Run all tests."""
    print("=" * 60)
    print("EU-195 Test Suite: Hardened systemd unit deployment")
    print("=" * 60)

    tests = [
        ("Test 1: Unit file contains hardening parameters", test_install_service_script_emits_hardened_unit),
        ("Test 3: Script calls daemon-reload", test_script_calls_daemon_reload),
        ("Test 5: Script is idempotent", test_script_is_idempotent),
        ("Test 4: VPS_DEPLOYMENT.md updated", test_vps_deployment_docs_updated),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        print(f"\n{name}")
        try:
            if test_func():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"FAIL: {test_func.__name__} raised exception: {e}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
