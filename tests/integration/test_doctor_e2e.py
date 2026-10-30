"""End-to-end: ``python3 -m theseus.doctor`` against a real browser and a real Chrome debug port."""

from __future__ import annotations

import subprocess
import sys

from tests.integration.test_mcp_stdio import (  # noqa: F401  (fixture import)
    REPO_ROOT,
    _server_env,
    pytestmark,
    throwaway_chrome,
)


def run_doctor(tmp_path, *args, **env):
    environment = _server_env(tmp_path, **env)
    return subprocess.run([sys.executable, "-m", "theseus.doctor", *args], env=environment, cwd=str(tmp_path),
                          capture_output=True, text=True, timeout=180)


def test_the_doctor_passes_on_a_working_setup_and_proves_the_write_block(tmp_path, throwaway_chrome):
    result = run_doctor(tmp_path, THESEUS_CDP_URL=throwaway_chrome)
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    assert "Browser starts:" in out and "Write block self-test - a page's POST was held back, its GET went through" in out
    assert "MCP server starts and lists 15 tools" in out
    assert f"Chrome answers at {throwaway_chrome}" in out and "Chrome gives up its cookies over DevTools" in out
    assert "0 failure(s)" in out


def test_a_missing_browser_is_a_failure_with_the_fix(tmp_path):
    result = run_doctor(tmp_path, THESEUS_BROWSER_CHANNEL="no-such-browser")
    assert result.returncode == 1
    assert "FAIL" in result.stdout and "no-such-browser" in result.stdout and "fix:" in result.stdout


def test_an_unreachable_chrome_prints_the_launch_command(tmp_path):
    result = run_doctor(tmp_path, "--quick", THESEUS_CDP_URL="http://127.0.0.1:9")
    assert result.returncode == 1
    assert "Can't reach your Chrome" in result.stdout and "--remote-debugging-port=9222" in result.stdout
    assert "Browser starts" not in result.stdout  # --quick starts no browser


def test_quick_is_quick_and_the_settings_are_checked(tmp_path):
    result = run_doctor(tmp_path, "--quick", THESEUS_ALLOW_WRITE="1", THESEUS_BLOCK_WRITES="maybe")
    assert result.returncode == 0
    assert "THESEUS_ALLOW_WRITE is not a Theseus setting" in result.stdout and "Did you mean THESEUS_ALLOW_WRITES?" in result.stdout
    assert "THESEUS_BLOCK_WRITES='maybe' is not understood, so writes are blocked" in result.stdout
