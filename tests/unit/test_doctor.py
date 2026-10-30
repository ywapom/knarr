"""Unit tests for theseus/doctor.py (no browser; the DevTools calls and the session are faked)."""

from __future__ import annotations

import io
import re
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from theseus import doctor
from theseus.doctor import fail, info, ok, warn

REPO_ROOT = Path(__file__).resolve().parents[2]


def statuses(results):
    return [r.status for r in results]


def titles(results):
    return [r.title for r in results]


# -- the lists that must not drift -------------------------------------------------------------

def test_required_versions_match_pyproject():
    deps = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    parsed = {}
    for dep in deps:
        name = re.match(r"[A-Za-z0-9_.-]+", dep).group(0).lower()
        minimum = re.search(r">=\s*([0-9.]+)", dep)
        ceiling = re.search(r"<\s*([0-9.]+)", dep)
        parsed[name] = (tuple(int(x) for x in minimum.group(1).split(".")),
                        tuple(int(x) for x in ceiling.group(1).split(".")) if ceiling else None)
    assert parsed == {k: v for k, v in doctor.REQUIRED.items()}
    requires = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    assert requires == ">=" + ".".join(map(str, doctor.MIN_PYTHON))


def test_every_setting_the_doctor_knows_is_in_the_readme_and_the_other_way_round():
    readme = (REPO_ROOT / "README.md").read_text()
    table = readme.split("## Environment variables", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"`(THESEUS_[A-Z_]+)`", table))
    assert documented == set(doctor.KNOWN_SETTINGS)


def test_the_settings_match_the_names_the_code_reads():
    from theseus.explorer import profiles, session, write_block
    from theseus.mcp import server
    from theseus.mcp import session as mcp_session

    read = {profiles.PROFILES_DIR_ENV, write_block.ALLOW_WRITES_ENV, write_block.BLOCK_WRITES_ENV, write_block.WRITE_ALLOW_ENV,
            write_block.RECORD_FIELDS_ENV, session.TRACE_ENV, session.CDP_URL_ENV, session.STORAGE_STATE_ENV,
            session.BROWSER_CHANNEL_ENV, session.NO_SANDBOX_ENV, server.RUNS_DIR_ENV, mcp_session.MAX_TURNS_ENV,
            mcp_session.MAX_WALL_CLOCK_ENV, mcp_session.IDLE_TIMEOUT_ENV}
    assert read == set(doctor.KNOWN_SETTINGS)
    assert doctor.RUNS_DIR_ENV == server.RUNS_DIR_ENV
    assert doctor.PROFILES_DIR_ENV == profiles.PROFILES_DIR_ENV


# -- python and packages -----------------------------------------------------------------------------

def test_python_that_is_new_enough_and_uvx_on_the_path():
    results = doctor.check_python((3, 12, 1), "/usr/bin/python3", uvx_path="/usr/local/bin/uvx")
    assert statuses(results) == ["ok", "ok"]


def test_python_that_is_too_old_fails():
    result, _ = doctor.check_python((3, 11, 9), "/usr/bin/python3", uvx_path="/usr/local/bin/uvx")
    assert result.status == "fail" and "3.11.9" in result.title and "3.12" in result.detail


def test_no_uvx_on_the_path_is_a_warning_that_names_the_way_without_it():
    results = doctor.check_python((3, 12, 0), "/x/python", uvx_path="")
    assert statuses(results) == ["ok", "warn"] and "uvx" in results[1].title
    assert "claude mcp add theseus -- python3 -m theseus.mcp" in results[1].fix


def good_versions(**changes):
    versions = {"mcp": "1.30.0", "playwright": "1.64.0", "pyyaml": "6.0.2", "websockets": "15.0.1"}
    versions.update(changes)
    return versions


def test_packages_that_fit_pass():
    results = doctor.check_packages(good_versions())
    assert all(r.status in ("ok", "info") for r in results) and sum(r.status == "ok" for r in results) >= 4


@pytest.mark.parametrize("name, version, why", [
    ("mcp", "1.12.9", "Needs 1.13"), ("mcp", "2.0.0", "up to but not including 2"),
    ("playwright", "1.47.1", "Needs 1.48"), ("websockets", "11.0.3", "Needs 12"),
    ("pyyaml", "5.4", "Needs 6.0"), ("mcp", None, "Needs 1.13"),
])
def test_a_package_that_does_not_fit_fails_with_the_install_command(name, version, why):
    results = doctor.check_packages(good_versions(**{name: version}))
    [bad] = [r for r in results if r.status == "fail"]
    assert name in bad.title and why in bad.detail
    assert doctor.INSTALL_COMMAND in bad.fix and doctor.PIP_INSTALL_COMMAND in bad.fix


@pytest.mark.parametrize("text, parsed", [("1.30.0", (1, 30, 0)), ("1.64.0.dev1", (1, 64, 0)), ("6.0", (6, 0)),
                                           ("2.0rc1", (2, 0)), ("x", ())])
def test_version_parsing(text, parsed):
    assert doctor._version_tuple(text) == parsed


# -- settings ---------------------------------------------------------------------------------------------

def settings(tmp_path, **env):
    return doctor.check_settings({"THESEUS_RUNS_DIR": str(tmp_path / "runs"), **env})


def test_default_settings_block_writes(tmp_path):
    results = settings(tmp_path)
    assert results[0].status == "ok" and results[0].title == "Writes are blocked"
    assert not [r for r in results if r.status in ("warn", "fail")]


def test_allowing_writes_is_a_warning_and_block_wins_a_conflict(tmp_path):
    [warning] = [r for r in settings(tmp_path, THESEUS_ALLOW_WRITES="1") if r.status == "warn"]
    assert "ALLOWED" in warning.title and "real permissions" in warning.detail
    both = settings(tmp_path, THESEUS_ALLOW_WRITES="1", THESEUS_BLOCK_WRITES="1")
    assert both[0].status == "ok" and "BLOCK_WRITES wins" in both[0].title


def test_allow_patterns_are_counted_and_bad_ones_fail_with_the_reason(tmp_path):
    good = settings(tmp_path, THESEUS_WRITE_ALLOW="**/graphql, https://api.example.com/search")
    assert "2 request pattern(s)" in good[0].detail
    [bad] = [r for r in settings(tmp_path, THESEUS_WRITE_ALLOW="https://app.example.com") if r.status == "fail"]
    assert bad.title == "THESEUS_WRITE_ALLOW is not valid" and "has no path" in bad.detail


def test_trace_and_no_sandbox_are_warnings(tmp_path):
    results = settings(tmp_path, THESEUS_TRACE="yes", THESEUS_NO_SANDBOX="1")
    warned = [r.title for r in results if r.status == "warn"]
    assert any("THESEUS_TRACE" in t for t in warned) and any("sandbox is off" in t for t in warned)


def test_a_misspelt_setting_is_found_and_the_right_name_suggested(tmp_path):
    [typo] = [r for r in settings(tmp_path, THESEUS_ALLOW_WRITE="1") if r.status == "warn"]
    assert "THESEUS_ALLOW_WRITE is not a Theseus setting" in typo.title and "THESEUS_ALLOW_WRITES" in typo.fix
    [unknown] = [r for r in settings(tmp_path, THESEUS_BANANA="1") if r.status == "warn"]
    assert "README" in unknown.fix
    assert not [r for r in settings(tmp_path, THESEUS_BROWSER_TESTS="1") if r.status == "warn"]


def test_a_storage_state_file_must_exist(tmp_path):
    [bad] = [r for r in settings(tmp_path, THESEUS_STORAGE_STATE=str(tmp_path / "no.json")) if r.status == "fail"]
    assert "THESEUS_STORAGE_STATE" in bad.title
    (tmp_path / "state.json").write_text("{}")
    assert not [r for r in settings(tmp_path, THESEUS_STORAGE_STATE=str(tmp_path / "state.json")) if r.status == "fail"]


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "1.5"])
def test_a_limit_that_is_not_a_positive_whole_number_is_called_out(tmp_path, raw):
    [warning] = [r for r in settings(tmp_path, THESEUS_MAX_TURNS=raw) if r.status == "warn"]
    assert "THESEUS_MAX_TURNS" in warning.title and "ignored" in warning.detail


def test_the_browser_setting_is_reported(tmp_path):
    assert "Playwright's Chromium" in [r.title for r in settings(tmp_path) if r.status == "info"][0]
    assert "installed chrome" in [r.title for r in settings(tmp_path, THESEUS_BROWSER_CHANNEL="chrome") if r.status == "info"][0]


def test_a_runs_directory_that_cannot_be_written_fails(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    [bad] = [r for r in doctor.check_settings({"THESEUS_RUNS_DIR": str(blocker / "runs")}) if r.status == "fail"]
    assert "runs directory" in bad.title and "THESEUS_RUNS_DIR" in bad.fix


def test_a_runs_directory_others_can_read_is_a_warning_with_the_command(tmp_path):
    root = tmp_path / "runs"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    [warning] = [r for r in doctor.check_settings({"THESEUS_RUNS_DIR": str(root)}) if r.status == "warn"]
    assert "readable by others" in warning.title and warning.fix == f"chmod 700 {root}"


def test_a_new_runs_directory_is_private(tmp_path):
    doctor.check_settings({"THESEUS_RUNS_DIR": str(tmp_path / "a" / "runs")})
    assert ((tmp_path / "a").stat().st_mode & 0o077) == 0


# -- Chrome you borrow a login from ---------------------------------------------------------------------------

@pytest.fixture
def chrome(monkeypatch):
    from theseus.explorer import borrow

    state = SimpleNamespace(version={"Browser": "Chrome/156.0", "webSocketDebuggerUrl": "ws://browser"},
                            tabs=[{"type": "page"}, {"type": "page"}, {"type": "service_worker"}],
                            cookies=[{"name": "a"}, {"name": "b"}], down=False, ws_error=None)

    def get_json(base, path, timeout):
        if state.down:
            raise borrow.BorrowError(f"could not reach Chrome at {base} (ConnectionRefusedError)")
        return state.version if path == "/json/version" else state.tabs

    def call(ws_url, method, params, timeout):
        if state.ws_error:
            raise state.ws_error
        return {"id": 1, "result": {"cookies": state.cookies}}

    monkeypatch.setattr(borrow, "_get_json", get_json)
    monkeypatch.setattr(borrow, "_call", call)
    return state


def test_without_a_devtools_url_it_explains_how_and_nothing_fails(chrome):
    chrome.down = True
    [result] = doctor.check_devtools({})
    assert result.status == "info" and "sessions start logged out" in result.title
    assert "--remote-debugging-port=9222" in result.fix and "THESEUS_CDP_URL=http://127.0.0.1:9222" in result.fix
    assert "--user-data-dir" in result.fix


def test_without_a_devtools_url_but_a_chrome_on_the_usual_port_it_says_so(chrome):
    [result] = doctor.check_devtools({})
    assert result.status == "info" and "answers on http://127.0.0.1:9222" in result.title


def test_a_working_devtools_url_passes_and_prints_no_cookie_or_url(chrome):
    results = doctor.check_devtools({"THESEUS_CDP_URL": "http://127.0.0.1:9222"})
    assert statuses(results) == ["ok", "ok", "ok"]
    text = "\n".join(f"{r.title} {r.detail}" for r in results)
    assert "Chrome/156.0" in text and "2 tab(s)" in text and "2 in total; none are printed" in text


def test_the_websocket_form_of_the_url_works(chrome):
    assert statuses(doctor.check_devtools({"THESEUS_CDP_URL": "ws://127.0.0.1:9222/devtools/browser/abc"}))[0] == "ok"


def test_an_unreachable_chrome_fails_with_the_launch_command(chrome):
    chrome.down = True
    [result] = doctor.check_devtools({"THESEUS_CDP_URL": "http://127.0.0.1:9222"})
    assert result.status == "fail" and "Can't reach your Chrome" in result.title and "--remote-debugging-port" in result.fix


def test_a_malformed_devtools_url_fails(chrome):
    [result] = doctor.check_devtools({"THESEUS_CDP_URL": "9222"})
    assert result.status == "fail" and "isn't valid" in result.title


@pytest.mark.parametrize("address", [
    "http://chrome-box.example.com:9222", "http://192.168.1.20:9222", "ws://10.0.0.5:9222/devtools/browser/x",
    "http://0.0.0.0:9222", "http://user:pw@127.0.0.1:9222",
])
def test_a_chrome_that_is_not_on_this_machine_fails_and_nothing_is_asked_of_it(chrome, address):
    [result] = doctor.check_devtools({"THESEUS_CDP_URL": address})
    assert result.status == "fail" and result.title == "THESEUS_CDP_URL isn't valid"
    assert "this machine" in result.detail + result.fix


def test_a_refused_websocket_fails(chrome):
    chrome.ws_error = ConnectionRefusedError("refused")
    results = doctor.check_devtools({"THESEUS_CDP_URL": "http://127.0.0.1:9222"})
    [bad] = [r for r in results if r.status == "fail"]
    assert "DevTools connection failed" in bad.title and "ConnectionRefusedError" in bad.detail


@pytest.mark.parametrize("system, binary", [("Darwin", "/Applications/Google Chrome.app"), ("Linux", "google-chrome")])
def test_the_launch_command_fits_the_platform(system, binary):
    command = doctor.chrome_launch_command(system)
    assert binary in command and "--remote-debugging-port=9222" in command and '--user-data-dir="$HOME/.theseus-chrome"' in command


# -- the browser and the write block ---------------------------------------------------------------------------

class FakeSession:
    """Stands in for plain_session: runs the test page's behaviour against the probe server's record."""

    def __init__(self, *, post_gets_through=False, block_sees_it=True, get_blocked=False, raises=None):
        self.post_gets_through, self.block_sees_it, self.get_blocked, self.raises = (
            post_gets_through, block_sees_it, get_blocked, raises)
        self.args = None

    def __call__(self, url, **kwargs):
        self.args = (url, kwargs)
        if self.raises:
            raise self.raises
        import urllib.request

        if not self.get_blocked:
            urllib.request.urlopen(url + "read", timeout=5).read()
        if self.post_gets_through:
            urllib.request.urlopen(urllib.request.Request(url + "write", data=b"x=1", method="POST"), timeout=5).read()
        if self.block_sees_it:
            kwargs["write_block"]._record({"method": "POST", "outcome": "blocked"})
        page = MagicMock()
        context = MagicMock()
        context.browser.version = "130.0.0.1"
        return _Cm(SimpleNamespace(page=page, context=context))


class _Cm:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        return False


def test_a_browser_that_holds_the_write_back_passes():
    fake = FakeSession()
    results = doctor.check_browser({"THESEUS_BROWSER_CHANNEL": " chrome "}, session=fake)
    assert statuses(results) == ["ok", "ok"] and "installed chrome 130.0.0.1" in results[0].title
    assert fake.args[1]["channel"] == "chrome" and fake.args[1]["write_block"].active  # the block is on, as in a session


def test_a_write_that_reaches_the_server_is_a_failure():
    results = doctor.check_browser({}, session=FakeSession(post_gets_through=True))
    [bad] = [r for r in results if r.status == "fail"]
    assert "let a write through" in bad.title and "Don't use Theseus on a real site" in bad.fix


def test_a_block_that_saw_nothing_is_a_failure():
    results = doctor.check_browser({}, session=FakeSession(block_sees_it=False))
    assert [r.title for r in results if r.status == "fail"] == ["The write block didn't see the test page's POST"]


def test_a_blocked_get_is_a_failure():
    results = doctor.check_browser({}, session=FakeSession(get_blocked=True))
    assert "stopped a plain GET" in [r.title for r in results if r.status == "fail"][0]


@pytest.mark.parametrize("message, channel, title, fix", [
    ("BrowserType.launch: Executable doesn't exist at /x/chrome-mac/Chromium\nLooks like Playwright was just installed",
     None, "Playwright's Chromium isn't installed", "playwright install chromium"),
    ("BrowserType.launch: Chromium distribution 'chrome' is not found at /Applications/Google Chrome.app",
     "chrome", "The browser 'chrome' isn't installed", "unset THESEUS_BROWSER_CHANNEL"),
    ("FATAL: No usable sandbox!", None, "The browser started and stopped at once", "THESEUS_NO_SANDBOX=1"),
    ("net::ERR_CONNECTION_REFUSED", None, "The browser couldn't start or load the test page", ""),
])
def test_browser_failures_are_explained_with_the_fix(message, channel, title, fix):
    env = {"THESEUS_BROWSER_CHANNEL": channel} if channel else {}
    [result] = doctor.check_browser(env, session=FakeSession(raises=RuntimeError(message)))
    assert result.status == "fail" and result.title == title and fix in result.fix


# -- the MCP server --------------------------------------------------------------------------------------------

ALL_TOOLS = ["session_start", "session_end", "session_status", "read_page", "click_index", "assert_state", "note"]


def test_a_server_that_lists_its_tools_passes():
    async def tools():
        return ALL_TOOLS

    [result] = doctor.check_server(tools)
    assert result.status == "ok" and "lists 7 tools" in result.title


def test_a_server_missing_tools_fails():
    async def tools():
        return ["note"]

    [result] = doctor.check_server(tools)
    assert result.status == "fail" and "session_start" in result.detail


def test_a_server_that_will_not_start_fails_with_the_reason():
    async def tools():
        raise RuntimeError("Connection closed")

    [result] = doctor.check_server(tools)
    assert result.status == "fail" and "RuntimeError: Connection closed" in result.detail and f"{sys.executable} -m theseus.mcp" in result.fix


def test_a_server_that_does_not_answer_times_out(monkeypatch):
    import asyncio

    async def tools():
        await asyncio.sleep(10)

    monkeypatch.setattr(doctor, "SERVER_TEST_SECONDS", 0.05)
    [result] = doctor.check_server(tools)
    assert result.status == "fail" and "didn't answer" in result.title


# -- the report ----------------------------------------------------------------------------------------------------

def test_the_report_shows_status_detail_and_fix_and_a_verdict():
    text = doctor.render([("Section", [ok("fine", "all good"), warn("hmm", "look", "do this"),
                                       fail("broken", "why", "line one\nline two"), info("note")])])
    assert "ok    fine - all good" in text and "WARN  hmm - look" in text and "fix: do this" in text
    assert "FAIL  broken - why" in text and "fix: line one\n        line two" in text and "info  note" in text
    assert "1 ok, 1 warning(s), 1 failure(s). Theseus can't run here yet" in text


def test_the_verdicts():
    assert doctor.render([("S", [ok("a")])]).endswith("Everything Theseus needs works here.")
    assert doctor.render([("S", [ok("a"), warn("b")])]).endswith("Theseus can run here; read the warnings.")


def test_quick_skips_the_browser_and_the_server(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "check_browser", lambda env: pytest.fail("browser started"))
    monkeypatch.setattr(doctor, "check_server", lambda: pytest.fail("server started"))
    sections = doctor.run_checks({"THESEUS_RUNS_DIR": str(tmp_path / "r")}, quick=True)
    assert [name for name, _ in sections] == ["Python and uv", "Packages", "Settings", "Site profiles", "Chrome login"]


def test_broken_packages_skip_the_browser_with_a_reason(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "check_packages", lambda: [fail("playwright is not installed")])
    monkeypatch.setattr(doctor, "check_browser", lambda env: pytest.fail("browser started"))
    monkeypatch.setattr(doctor, "check_server", lambda: pytest.fail("server started"))
    sections = doctor.run_checks({"THESEUS_RUNS_DIR": str(tmp_path / "r")})
    assert sections[-1][1][0].title == "Skipped: fix the packages first"


def test_main_exit_status_follows_failures_not_warnings(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(doctor, "run_checks", lambda env, quick=False: [("S", [warn("w")])])
    assert doctor.main(["--quick"], env={}, out=out) == 0
    monkeypatch.setattr(doctor, "run_checks", lambda env, quick=False: [("S", [warn("w"), fail("f")])])
    assert doctor.main([], env={}, out=io.StringIO()) == 1


def test_main_rejects_unknown_options_and_prints_help():
    out = io.StringIO()
    assert doctor.main(["--fast"], env={}, out=out) == 2 and "--fast" in out.getvalue()
    out = io.StringIO()
    assert doctor.main(["--help"], env={}, out=out) == 0 and doctor.DOCTOR_COMMAND in out.getvalue()


@pytest.mark.parametrize("name", ["THESEUS_BLOCK_WRITES", "THESEUS_ALLOW_WRITES", "THESEUS_TRACE", "THESEUS_NO_SANDBOX"])
@pytest.mark.parametrize("value", ["2", "enable", "maybe", "onn"])
def test_a_switch_set_to_something_that_is_not_understood_is_called_out(tmp_path, name, value):
    [warning] = [r for r in settings(tmp_path, **{name: value}) if r.status == "warn" and "not understood" in r.title]
    assert name in warning.title and "Say 1 or 0" in warning.detail and f"{name}=1" in warning.fix
    # A value nobody can read blocks writes (BLOCK_WRITES) or leaves the setting off; it never allows or enables.
    assert warning.title.endswith("writes are blocked" if name == "THESEUS_BLOCK_WRITES" else "it is off")


@pytest.mark.parametrize("value", ["1", "true", "YES", "on", "y", "t", "enabled", "0", "false", "no", "off", "n", " 1 "])
def test_the_values_it_understands_are_not_called_out(tmp_path, value):
    assert not [r for r in settings(tmp_path, THESEUS_BLOCK_WRITES=value) if "not understood" in r.title]


# -- site profiles -------------------------------------------------------------------------------------

def _profile_env(tmp_path, files=None, mode=0o700, where=".theseus/sites"):
    """The profiles directory, by default where ``~/.theseus/sites`` is when the home is ``tmp_path``."""
    d = tmp_path / where
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(mode)
    for name, text in (files or {}).items():
        (d / name).write_text(text)
    return d, {"THESEUS_PROFILES_DIR": str(d)}


def _claude_settings(home, permissions):
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(__import__("json").dumps({"permissions": permissions}))


def test_no_profiles_directory_is_only_a_note(tmp_path):
    results = doctor.check_profiles({"THESEUS_PROFILES_DIR": str(tmp_path / "nope")}, home=tmp_path, cwd=tmp_path)
    assert statuses(results) == ["info"] and results[0].title == "No site profiles"


def test_a_good_profile_is_listed_with_what_it_changes(tmp_path):
    d, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\nallow: ['https://a.example.com/api/**']\n"})
    results = doctor.check_profiles(env, home=tmp_path, cwd=tmp_path)
    good = next(r for r in results if r.title == "Site profile 'm'")
    assert good.status == "ok" and "a.example.com" in good.detail and "1 request pattern(s) are let through" in good.detail


def test_a_live_profile_is_a_warning(tmp_path):
    _, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\nwrites: live\nauthorized_by: me\n"})
    results = doctor.check_profiles(env, home=tmp_path, cwd=tmp_path)
    live = next(r for r in results if "lets every write through" in r.title)
    assert live.status == "warn" and "authorized by me" in live.detail


def test_a_broken_profile_and_two_for_one_origin_are_warnings_not_failures(tmp_path):
    _, env = _profile_env(tmp_path, {
        "bad.yaml": "origins: [https://a.example.com]\ntypo: 1\n",
        "x.yaml": "origins: [https://b.example.com]\n",
        "y.yaml": "origins: [https://b.example.com]\n",
    })
    results = doctor.check_profiles(env, home=tmp_path, cwd=tmp_path)
    assert "fail" not in statuses(results)
    assert any(r.title == "A site profile is ignored" and r.detail.startswith("bad.yaml: unknown setting") for r in results)
    assert any(r.title == "Two site profiles name the same origin" and "x.yaml, y.yaml" in r.detail for r in results)


def test_an_empty_directory_says_so(tmp_path):
    _, env = _profile_env(tmp_path)
    results = doctor.check_profiles(env, home=tmp_path, cwd=tmp_path)
    assert results[0].title == "No site profiles" and "holds none" in results[0].detail


def test_nothing_guarding_the_directory_is_a_warning_that_says_what_to_do(tmp_path):
    _, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\n"})
    results = doctor.check_profiles(env, home=tmp_path, cwd=tmp_path)
    warning = next(r for r in results if r.title.startswith("Nothing makes Claude Code ask"))
    assert warning.status == "warn" and "/permissions" in warning.fix and "shell command" in warning.fix


def _asks_or_refuses_edits(results):
    return any(r.status == "ok" and r.title.startswith("Claude Code is set to ask or refuse") for r in results)


def _warns_nothing_guards(results):
    return next((r for r in results if r.title.startswith("Nothing makes Claude Code ask")), None)


@pytest.mark.parametrize("kind, rule", [
    ("ask", "Edit(~/.theseus/sites/**)"),
    ("deny", "Edit(~/.theseus/sites/**)"),
    ("deny", "Write(~/.theseus/sites/**)"),
    ("ask", "Edit(~/.theseus/**)"),
    ("ask", "Edit(~/**)"),
    ("ask", "Edit(~/.theseus/sites)"),
    ("ask", "Edit(~/.theseus/sites/)"),
    ("ask", "Edit(~/.theseus/sites/*)"),
    ("ask", "Edit(~/.theseus/**/*)"),
    ("deny", "Edit(~/.theseus/sites/*.y*ml)"),
    ("ask", "Edit"),  # the whole tool
])
def test_a_rule_about_editing_files_in_the_directory_counts(tmp_path, kind, rule):
    _, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\n"})
    _claude_settings(tmp_path, {kind: [rule]})
    results = doctor.check_profiles(env, home=tmp_path, cwd=tmp_path)
    assert _asks_or_refuses_edits(results) and not _warns_nothing_guards(results)


def test_an_absolute_path_rule_counts(tmp_path):
    d, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\n"})
    _claude_settings(tmp_path, {"ask": [f"Edit(/{d}/**)"]})  # "//path" is absolute
    assert _asks_or_refuses_edits(doctor.check_profiles(env, home=tmp_path, cwd=tmp_path))


@pytest.mark.parametrize("kind, rule", [
    ("allow", "Edit(~/.theseus/sites/**)"),  # permits, doesn't ask
    ("deny", "Edit(~/.ssh/**)"),
    ("deny", "Read(~/.theseus/sites/**)"),  # reading is not editing
    ("deny", "Read(~/.theseus/runs/**)"),
    ("deny", "Bash(rm -rf ~/.theseus/runs)"),
    ("deny", "WebFetch(domain:.theseus.example)"),
    ("ask", "Edit(~/.theseus/other/**)"),
    ("ask", "Edit(~/.theseus/runs/**)"),
    ("ask", "Edit(~/.theseus/sites/nested/**)"),  # a subdirectory only
    ("ask", "Edit(~/.theseus/sites-backup/**)"),  # starts the same, is another directory
    ("ask", "Edit(~/.theseus/sites/*.yaml)"),  # leaves .yml files open
    ("ask", "Edit(~/.theseus/sites/*.json)"),
    ("ask", "Edit(.theseus/sites/**)"),  # relative: which directory it means isn't known here
    ("ask", "Edit(/.theseus/sites/**)"),
    ("ask", "Edit(*)"),
    ("ask", "Edit(~/.theseus/sites/**"),  # not a well-formed rule
    ("ask", "not a rule"),
])
def test_a_rule_that_is_not_about_editing_files_in_the_directory_does_not_count(tmp_path, kind, rule):
    _, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\n"})
    _claude_settings(tmp_path, {kind: [rule]})
    results = doctor.check_profiles(env, home=tmp_path, cwd=tmp_path)
    assert _warns_nothing_guards(results) and not _asks_or_refuses_edits(results)


def test_the_warning_names_a_rule_that_mentions_the_directory_but_does_not_guard_it(tmp_path):
    _, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\n"})
    _claude_settings(tmp_path, {"deny": ["Read(~/.theseus/sites/**)"]})
    warning = _warns_nothing_guards(doctor.check_profiles(env, home=tmp_path, cwd=tmp_path))
    assert "Read(~/.theseus/sites/**)" in warning.detail and "not an ask or deny rule for editing" in warning.detail


def test_the_suggested_rule_matches_the_directory_in_use(tmp_path):
    _, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\n"}, where="work/profiles")
    warning = _warns_nothing_guards(doctor.check_profiles(env, home=tmp_path, cwd=tmp_path))
    assert "Edit(~/work/profiles/**)" in warning.fix
    default = _profile_env(tmp_path / "other", {"m.yaml": "origins: [https://a.example.com]\n"})[1]
    warning = _warns_nothing_guards(doctor.check_profiles(default, home=tmp_path / "other", cwd=tmp_path))
    assert "Edit(~/.theseus/sites/**)" in warning.fix
    outside = _warns_nothing_guards(doctor.check_profiles(env, home=tmp_path / "elsewhere", cwd=tmp_path))
    assert "an Edit rule that covers that directory" in outside.fix and "Edit(~" not in outside.fix


def test_a_rule_for_the_default_directory_does_not_guard_a_directory_set_by_the_environment(tmp_path):
    _, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\n"}, where="work/profiles")
    _claude_settings(tmp_path, {"ask": ["Edit(~/.theseus/sites/**)"]})
    assert _warns_nothing_guards(doctor.check_profiles(env, home=tmp_path, cwd=tmp_path))
    _claude_settings(tmp_path, {"ask": ["Edit(~/.theseus/sites/**)", "Edit(~/work/profiles/**)"]})
    assert _asks_or_refuses_edits(doctor.check_profiles(env, home=tmp_path, cwd=tmp_path))


def test_unreadable_claude_settings_are_ignored(tmp_path):
    _, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\n"})
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{not json")
    (tmp_path / ".claude" / "settings.local.json").write_text('{"permissions": "oops"}')
    results = doctor.check_profiles(env, home=tmp_path, cwd=tmp_path)
    assert any(r.title.startswith("Nothing makes Claude Code ask") for r in results)


def test_project_settings_count_too(tmp_path):
    _, env = _profile_env(tmp_path, {"m.yaml": "origins: [https://a.example.com]\n"})
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "settings.json").write_text('{"permissions": {"deny": ["Write(~/.theseus/**)"]}}')
    results = doctor.check_profiles(env, home=tmp_path, cwd=project)
    assert any(r.status == "ok" and "set to ask or refuse" in r.title for r in results)
