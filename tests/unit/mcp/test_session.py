"""Unit tests for theseus/mcp/session.py — one exploration driven by a client."""

from __future__ import annotations

import json
import re
import stat

import pytest
from unittest.mock import MagicMock

from theseus.explorer.caps import RunCaps
from theseus.mcp.session import (
    IDLE_TIMEOUT_ENV,
    MAX_TURNS_ENV,
    MAX_WALL_CLOCK_ENV,
    REASON_CLIENT_ENDED,
    ActiveClock,
    McpSession,
    SessionConfig,
    SessionError,
    caps_from_env,
    idle_timeout_from_env,
    validate_start_url,
)

from tests.unit.mcp.fakes import FakeBrowser, make_page

EXPECTED_TOOLS = sorted([
    "navigate", "read_page", "inspect_error", "screenshot", "note", "click",
    "click_index", "fill", "submit", "wait_for", "assert_state", "give_up",
])
CRITERION = "the page shows 'Order placed'"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _session(tmp_path, *, browser=None, caps=None, clock=None, **config):
    browser = browser or FakeBrowser()
    cfg = SessionConfig(
        start_url=config.pop("start_url", "https://example.com/start"),
        success_criterion=config.pop("success_criterion", CRITERION),
        **config,
    )
    s = McpSession(
        cfg,
        run_dir=tmp_path / "run",
        caps=caps or RunCaps.for_explore(max_turns=10),
        session_cm_factory=browser.factory,
        time_provider=clock or FakeClock(),
    )
    return s, browser


# -- start URL and criterion ---------------------------------------------------

@pytest.mark.parametrize("url, origin", [
    ("https://example.com/start", "https://example.com"),
    ("http://127.0.0.1:8080/x?y=1", "http://127.0.0.1:8080"),
    ("  https://Example.COM/path  ", "https://example.com"),
    ("https://user:secret@example.com/", "https://example.com"),
    ("http://[::1]:9000/", "http://[::1]:9000"),
    ("https://example.com:443/", "https://example.com"),
    # Tabs and newlines are dropped from URLs, by browsers and by Python alike.
    ("https://ex\tample.com/", "https://example.com"),
])
def test_valid_start_urls(url, origin):
    assert validate_start_url(url) == origin


@pytest.mark.parametrize("url", [
    "", "   ", None, 42, "example.com", "file:///etc/hosts", "data:text/html,<h1>x</h1>",
    "javascript:alert(1)", "ftp://example.com/", "http://", "https://example.com:99999/",
    "http://[::1", "http://[zz]/", "https://exa mple.com/", "https://example.com:0/",
])
def test_invalid_start_urls_are_refused(url):
    with pytest.raises(SessionError, match="http or https"):
        validate_start_url(url)


@pytest.mark.parametrize("criterion", ["", "   ", None])
def test_a_success_criterion_is_required(tmp_path, criterion):
    with pytest.raises(SessionError, match="success_criterion is required"):
        _session(tmp_path, success_criterion=criterion)


# -- lifecycle -----------------------------------------------------------------

def test_start_opens_the_browser_and_exposes_the_explore_palette(tmp_path):
    s, browser = _session(tmp_path)
    message = s.start()
    assert "staying on https://example.com" in message
    assert CRITERION in message
    assert browser.entered == 1
    assert s.tool_names == EXPECTED_TOOLS  # no ask_human, no scaffold annotators
    assert stat.S_IMODE((tmp_path / "run").stat().st_mode) == 0o700


def test_a_redirect_at_start_sets_the_origin(tmp_path):
    s, _ = _session(tmp_path, browser=FakeBrowser(landed_url="https://www.example.com/home"))
    message = s.start()
    assert s.origin == "https://www.example.com"
    assert "redirected to https://www.example.com" in message
    assert "out_of_boundary" not in s.call("navigate", url="/orders")


def test_failed_browser_start_leaves_no_session(tmp_path):
    s, _ = _session(tmp_path, browser=FakeBrowser(fail_on_enter=True))
    with pytest.raises(RuntimeError, match="failed to start"):
        s.start()
    assert not s.started and not s.active


def test_calls_before_start_and_unknown_tools_are_refused(tmp_path):
    s, _ = _session(tmp_path)
    with pytest.raises(SessionError, match="no active session"):
        s.call("read_page")
    s.start()
    with pytest.raises(SessionError, match="unknown tool"):
        s.call("ask_human", question="?")


def test_each_call_is_a_turn_and_carries_a_status_line(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    out = s.call("click", target="Place order")
    assert out.startswith("clicked")
    assert re.search(r"\n\n\[theseus\] call 1 of 10, \d+ s of browser time left$", out), out
    s.call("note", text="looks fine")
    assert s.status()["turns_used"] == 2


def test_turn_limit_warns_on_the_last_call_and_refuses_the_next(tmp_path):
    s, _ = _session(tmp_path, caps=RunCaps.for_explore(max_turns=2))
    s.start()
    s.call("note", text="1")
    last = s.call("note", text="2")
    assert "limit reached, the next call will be refused" in last
    refused = s.call("note", text="3")
    assert refused.startswith("refused: the run has ended")
    assert s.status()["turns_used"] == 2  # the refused call isn't a turn
    assert s.status()["terminal_reason"] == "turn_budget_exhausted"


def test_status_and_end_notice_an_exhausted_limit(tmp_path):
    s, _ = _session(tmp_path, caps=RunCaps.for_explore(max_turns=1))
    s.start()
    s.call("note", text="only call")
    assert s.status()["terminal_reason"] == "turn_budget_exhausted"
    assert s.end()["abort_reason"] == "turn_budget_exhausted"


# -- grading -------------------------------------------------------------------

def test_assert_state_checks_the_fixed_criterion_not_client_text(tmp_path):
    page = make_page()
    s, browser = _session(tmp_path, browser=FakeBrowser(page=page), goal="Place an order")
    s.start()
    out = s.call("assert_state", description="Order desk")  # client text is ignored
    assert page.asserted == [CRITERION]
    assert out.startswith("matches")
    assert "run ended: success_asserted" in out
    assert s.call("read_page").startswith("refused: the run has ended")

    summary = s.end()
    assert summary["success"] is True
    assert summary["abort_reason"] == "success_asserted"
    assert summary["extra"]["success_criterion"] == CRITERION
    assert summary["extra"]["grade"] == "visible_text_match"
    written = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert written["success"] is True
    assert browser.exited == 1


def test_a_criterion_that_isnt_met_does_not_end_the_run(tmp_path):
    s, _ = _session(tmp_path, success_criterion="the page shows 'Refund issued'")
    s.start()
    assert s.call("assert_state").startswith("not_matched")
    assert s.active and s.status()["terminal_reason"] is None
    assert s.end()["extra"]["grade"] is None


def test_give_up_ends_the_run(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    s.call("give_up", reason="login wall")
    assert s.status()["terminal_reason"] == "give_up: login wall"


def test_ending_early_records_the_client_ended_it_and_is_idempotent(tmp_path):
    s, browser = _session(tmp_path)
    s.start()
    first = s.end()
    second = s.end()
    assert first == second
    assert first["success"] is False
    assert first["abort_reason"] == REASON_CLIENT_ENDED
    assert browser.exited == 1
    assert not s.active


def test_summary_counts_calls_even_when_a_tool_raises(tmp_path):
    page = make_page()
    page._theseus_click = lambda target: (_ for _ in ()).throw(RuntimeError("page crashed"))
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page))
    s.start()
    out = s.call("click", target="Place order")
    assert "page crashed" in out
    assert s.active and s.status()["terminal_reason"] is None
    s.call("note", text="after")
    assert s.end()["total_turns"] == 2


# -- origin --------------------------------------------------------------------

def test_navigation_is_limited_to_the_start_origin(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    assert "out_of_boundary" in s.call("navigate", url="https://elsewhere.example.org/")
    for same_origin in ("https://example.com/orders", "/orders", "?tab=2",
                        "HTTPS://EXAMPLE.COM/x", "https://example.com:443/y"):
        assert "out_of_boundary" not in s.call("navigate", url=same_origin), same_origin
    assert s.status()["boundary_violations"] == 1


def test_a_click_that_leaves_the_origin_is_sent_back_and_counted(tmp_path):
    page = make_page(click_goes_to={"Partner site": "https://partner.example.net/landing"})
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page))
    s.start()
    s.call("navigate", url="/orders")
    out = s.call("click", target="Partner site")
    assert "rejected: left_origin" in out
    assert "returned to https://example.com/start" in out  # a page Theseus chose, not where a click landed
    assert page.url == "https://example.com/start"
    assert s.status()["boundary_violations"] == 1


def test_the_guard_never_requests_a_page_a_click_landed_on_again(tmp_path):
    """A link that changes data with a plain GET isn't blocked; sending the page "back" to it would repeat it."""
    page = make_page(click_goes_to={"Cancel order": "https://example.com/cancel?id=7",
                                    "Partner site": "https://partner.example.net/landing"})
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page))
    s.start()
    s.call("click", target="Cancel order")
    assert page.url == "https://example.com/cancel?id=7"
    out = s.call("click", target="Partner site")
    assert "returned to https://example.com/start" in out
    asked = [call.args[0] for call in page.goto.call_args_list]
    assert "https://example.com/cancel?id=7" not in asked


def test_leaving_the_origin_three_times_ends_the_run(tmp_path):
    page = make_page(click_goes_to={"away": "https://elsewhere.example.org/"})
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page))
    s.start()
    for _ in range(3):
        out = s.call("click", target="away")
    assert "run ended: boundary_violations_exceeded" in out
    assert s.stopped_by_guard
    assert s.call("read_page").startswith("refused: the run has ended")


def test_a_page_that_moved_between_calls_is_caught_before_the_next_call(tmp_path):
    page = make_page()
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page))
    s.start()
    page.url = "https://tracker.example.org/"  # e.g. a delayed JS redirect
    out = s.call("read_page")
    assert out.splitlines()[0].startswith("the page went to https://tracker.example.org/ by itself")
    assert "returned to https://example.com/start" in out
    assert page.url == "https://example.com/start"
    assert s.status()["boundary_violations"] == 0  # no strike: nothing the agent did took it there


def test_a_redirect_after_navigate_is_the_sites_doing_not_a_strike(tmp_path):
    page = make_page(redirects={"https://example.com/orders": "https://idp.example.net/login"})
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page))
    s.start()
    out = s.call("navigate", url="/orders")
    assert "by itself" in out and "rejected: left_origin" not in out
    assert s.status()["boundary_violations"] == 0


def test_a_site_that_keeps_sending_the_page_to_a_sign_in_page_ends_the_run_as_login_required(tmp_path):
    page = make_page()
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page))
    s.start()
    for i in range(3):
        page.url = "https://idp.example.net/login"
        out = s.call("read_page")
    assert "run ended: login_required" in out
    assert "sent the page to https://idp.example.net 3 times" in out and "Sign in there in your Chrome" in out
    assert s.status()["terminal_reason"] == "login_required" and s.status()["boundary_violations"] == 0
    assert not s.stopped_by_guard  # a new session may start once the user has signed in


def test_a_late_sign_in_hop_is_followed_with_the_login_borrowed_there(tmp_path):
    page = make_page()
    followed = []

    def follow(url):
        followed.append(url)
        page.url = "https://example.com/home"  # the chain completed with the borrowed login
        return [{"host": "idp.example.net", "origin": "https://idp.example.net", "cookies": 2, "local_storage": "no_tab"}]

    s, _ = _session(tmp_path, browser=FakeBrowser(page=page, follow_sign_in=follow))
    s.start()
    page.url = "https://idp.example.net/login"  # a redirect that fired after the start settled
    out = s.call("read_page")
    assert followed == ["https://example.com/start"]
    assert "after borrowing your login for https://idp.example.net" in out
    assert s.status()["boundary_violations"] == 0 and not s.status()["ended"]


def test_a_tool_never_runs_on_a_page_that_could_not_be_brought_back(tmp_path):
    page = make_page()
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page))
    s.start()
    page.goto.side_effect = None  # the page ignores the request to go back
    page.url = "https://idp.example.net/login"
    out = s.call("read_page")
    assert "refused: the page is outside https://example.com and could not be brought back" in out
    assert s.status()["turns_used"] == 0 and out.rstrip().splitlines()[-1].startswith("[theseus]")


def test_forbidden_verbs_are_refused(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    assert "forbidden_text_match" in s.call("click", target="Delete everything")
    assert s.status()["forbidden_violations"] == 1


def test_read_only_simulates_clicks(tmp_path):
    s, _ = _session(tmp_path, read_only=True)
    s.start()
    assert s.call("click", target="Place order").startswith("simulated")


def test_screenshot_returns_an_absolute_path(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    out = s.call("screenshot")
    first = out.splitlines()[0]
    assert first.startswith(f"saved: {tmp_path / 'run' / 'screenshots'}/"), first


# -- time ----------------------------------------------------------------------

def test_time_waiting_between_calls_does_not_use_the_limit(tmp_path):
    clock = FakeClock()
    s, _ = _session(tmp_path, clock=clock, caps=RunCaps.for_explore(max_wall_clock_sec=60))
    s.start()
    s.call("note", text="1")
    clock.now += 3600  # the user took an hour to approve the next call
    assert s.call("note", text="2").startswith("noted")
    assert s.status()["terminal_reason"] is None
    assert s.idle_seconds() == 0


def test_active_clock_only_counts_running_calls():
    clock = FakeClock()
    active = ActiveClock(clock)
    active.start(); clock.now += 2; active.stop()
    clock.now += 100
    active.start(); clock.now += 3
    assert active() == 5
    active.stop()
    assert active() == 5


# -- limits from the environment -----------------------------------------------

def test_caps_from_env(monkeypatch):
    monkeypatch.setenv(MAX_TURNS_ENV, "5")
    monkeypatch.setenv(MAX_WALL_CLOCK_ENV, "120")
    caps = caps_from_env()
    assert (caps.max_turns, caps.max_wall_clock_sec) == (5, 120.0)


@pytest.mark.parametrize("value", ["", "abc", "0", "-3", "2.5", "²", "５"])
def test_caps_from_env_ignores_bad_values(monkeypatch, value):
    monkeypatch.setenv(MAX_TURNS_ENV, value)
    assert caps_from_env().max_turns == RunCaps.for_explore().max_turns


def test_idle_timeout_from_env(monkeypatch):
    assert idle_timeout_from_env() == 900
    monkeypatch.setenv(IDLE_TIMEOUT_ENV, "60")
    assert idle_timeout_from_env() == 60
    monkeypatch.setenv(IDLE_TIMEOUT_ENV, "soon")
    assert idle_timeout_from_env() == 900


# -- write block ------------------------------------------------------------------

def _writing_page(block, writes):
    """A page whose click sends ``writes`` [(method, url)] through the block."""
    page = make_page()

    def click(target):
        for method, url in writes:
            block._record({"method": method, "url": url, "resource_type": "fetch",
                           "navigation": False, "outcome": "blocked"})
        return {"success": True, "fallback_used": False, "selector": f"text={target}"}

    page._theseus_click = click
    return page


def test_blocked_writes_are_listed_in_the_result_and_the_summary(tmp_path):
    from theseus.explorer.write_block import WriteBlock

    block = WriteBlock()
    page = _writing_page(block, [("POST", "https://example.com/api/orders"),
                                 ("PUT", "https://cdn.example.net/x?y=1")])
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page, write_block=block))
    assert "Writes are blocked" in s.start()

    out = s.call("click", target="Place order")
    assert "blocked 2 write(s), not sent: POST /api/orders, PUT https://cdn.example.net/x?y=1" in out
    assert "nothing was saved" in out
    assert "blocked" not in s.call("note", text="no writes here")  # drained

    status = s.status()
    assert status["dry_run"] is True and status["blocked_writes"] == 2 and "writes_blocked" not in status
    extra = s.end()["extra"]
    assert extra["dry_run"] is True and extra["blocked_writes"] == 2


def test_repeated_writes_are_grouped_with_counts(tmp_path):
    from theseus.explorer.write_block import WriteBlock

    block = WriteBlock()
    writes = [("POST", "https://example.com/g/collect")] * 3 + [
        ("POST", f"https://example.com/api/{i}") for i in range(5)]
    s, _ = _session(tmp_path, browser=FakeBrowser(page=_writing_page(block, writes), write_block=block))
    s.start()
    out = s.call("click", target="Save all")
    assert "blocked 8 write(s)" in out
    assert "POST /g/collect (x3)" in out
    assert "and 2 more" in out


def test_writes_during_the_first_load_are_reported_at_start(tmp_path):
    from theseus.explorer.write_block import WriteBlock

    block = WriteBlock()
    block._record({"method": "POST", "url": "https://example.com/g/collect",
                   "resource_type": "ping", "navigation": False, "outcome": "blocked"})
    s, _ = _session(tmp_path, browser=FakeBrowser(write_block=block))
    started = s.start()
    assert "While the page loaded: blocked 1 write(s), not sent: POST /g/collect" in started
    assert "blocked" not in s.call("note", text="first call")


def test_allowed_writes_are_reported_and_counted_separately(tmp_path):
    from theseus.explorer.write_block import WriteBlock, WritePolicy

    block = WriteBlock(WritePolicy(allow_patterns=("**/graphql",)))
    page = make_page()

    def click(target):
        block._record({"method": "POST", "url": "https://example.com/graphql", "resource_type": "fetch",
                       "navigation": False, "allowed": True, "allowed_by": "**/graphql", "outcome": "sent"})
        return {"success": True}

    page._theseus_click = click
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page, write_block=block))
    assert "Requests matching THESEUS_WRITE_ALLOW (**/graphql) are sent" in s.start()
    out = s.call("click", target="Save")
    assert "sent 1 write(s) allowed by THESEUS_WRITE_ALLOW: POST /graphql" in out
    assert s.status()["allowed_writes"] == 1
    extra = s.end()["extra"]
    # Not a dry run any more: a write really went out.
    assert extra["dry_run"] is False and extra["allowed_writes"] == 1


def test_write_block_gaps_are_warned_about_and_summarised(tmp_path):
    from theseus.explorer.write_block import WriteBlock

    block = WriteBlock()
    s, _ = _session(tmp_path, browser=FakeBrowser(write_block=block))
    s.start()
    block._worker_started(MagicMock(url="https://example.com/worker.js"))
    out = s.call("note", text="x")
    assert "warning: a page started a Web Worker" in out
    block._worker_started(MagicMock())
    assert "warning" not in s.call("note", text="y")  # the same gap is reported once
    assert s.end()["extra"]["write_block_gaps"][0].endswith("(x2)")


def test_new_tabs_are_reported(tmp_path):
    browser = FakeBrowser()
    s, _ = _session(tmp_path, browser=browser)
    s.start()
    browser.context.open_tab("https://example.com/help")
    out = s.call("note", text="x")
    assert "opened a new tab (https://example.com/help)" in out
    assert "opened a new tab" not in s.call("note", text="y")


def test_allowed_writes_are_announced(tmp_path):
    from theseus.explorer.write_block import WriteBlock, WritePolicy

    block = WriteBlock(WritePolicy(block=False))
    s, _ = _session(tmp_path, browser=FakeBrowser(write_block=block))
    assert "Writes are ALLOWED" in s.start()
    extra = s.end()["extra"]
    assert extra["dry_run"] is False
    assert extra["blocked_writes"] is None and extra["allowed_writes"] is None  # not observed, not zero


def test_new_tab_note_does_not_claim_writes_are_blocked_when_they_are_not(tmp_path):
    from theseus.explorer.write_block import WriteBlock, WritePolicy

    browser = FakeBrowser(write_block=WriteBlock(WritePolicy(block=False)))
    s, _ = _session(tmp_path, browser=browser)
    s.start()
    browser.context.open_tab("https://example.com/help")
    out = s.call("note", text="x")
    assert "opened a new tab" in out and "blocked" not in out


@pytest.mark.parametrize("status, expected", [
    ("copied", "2 cookie(s) for https://example.com and its localStorage"),
    ("partial", "and part of its localStorage"),
    ("unreadable", "the page wouldn't give it up"),
    ("empty", "your open tab had no localStorage to copy"),
    ("busy", "your tab on it didn't answer, maybe a dialog is open"),
    ("no_tab", "no localStorage: no tab of yours was open on it"),
])
def test_the_start_message_says_what_login_was_borrowed(tmp_path, status, expected):
    info = {"host": "example.com", "origin": "https://example.com", "cookies": 2, "local_storage": status}
    from contextlib import contextmanager
    from types import SimpleNamespace

    def borrowed(config, run_dir):
        @contextmanager
        def cm():
            yield SimpleNamespace(page=make_page(), write_block=None, borrowed_login=[info], opened_tabs=[])
        return cm()

    s = McpSession(SessionConfig(start_url="https://example.com/", success_criterion=CRITERION),
                   run_dir=tmp_path / "run", session_cm_factory=borrowed)
    message = s.start()
    assert "Borrowed your login from your Chrome" in message and expected in message


def test_a_tab_opened_while_the_first_page_loaded_is_reported(tmp_path):
    browser = FakeBrowser()
    browser.context.open_tab("https://example.com/sso-helper")  # before start() reads the list
    s, _ = _session(tmp_path, browser=browser)
    assert "opened a new tab (https://example.com/sso-helper)" in s.start()


def test_a_blocked_navigation_never_becomes_the_return_target(tmp_path):
    from theseus.explorer.write_block import WriteBlock

    block = WriteBlock()
    page = make_page(click_goes_to={"Log out": "https://example.com/logout",
                                    "Partner": "https://partner.example.net/"})
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page, write_block=block))
    s.start()
    s.call("navigate", url="/orders")
    block._blocked_navigations.add("https://example.com/logout")  # the form post was faked
    page.title.return_value = "Blocked by Theseus"                # and its blocked page is showing
    s.call("click", target="Log out")
    out = s.call("click", target="Partner")
    assert "returned to https://example.com/start" in out
    assert page.url == "https://example.com/start"


class _RealishPage:
    """Stands in for a Playwright page: ``_sync`` defined on the class."""

    def __init__(self):
        self.synced = 0
        self.waited = 0

    def _sync(self, coro):
        import asyncio
        asyncio.run(coro)
        self.synced += 1

    def wait_for_timeout(self, ms):
        self.waited += 1


def test_pump_runs_playwright_loop_without_a_traced_action(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    page = _RealishPage()
    s.context.page = page
    s.pump()
    assert (page.synced, page.waited) == (1, 0)
    mock_page = s.context.page = make_page()  # no _sync on its class: fall back
    s.pump()
    mock_page.wait_for_timeout.assert_called_once()


def test_playwright_still_has_the_internal_hook_the_pump_uses():
    """The pump runs Playwright's loop through the private SyncBase._sync, which
    adds no trace actions. If an upgrade removes it, the pump falls back to
    wait_for_timeout (one trace action every 25 ms): fail here, loudly."""
    from playwright.sync_api import Page

    assert callable(getattr(Page, "_sync", None)), "Playwright no longer has Page._sync"


# -- starting: what can go wrong once the browser is open --------------------------

def test_a_failure_after_the_browser_opened_closes_it_instead_of_leaking_it(tmp_path):
    browser = FakeBrowser(borrowed_login=object())  # not iterable: _login_sentence raises after the browser opened
    s, _ = _session(tmp_path, browser=browser)
    with pytest.raises(TypeError):
        s.start()
    assert browser.entered == 1 and browser.exited == 1
    assert not s.started


def test_the_devtools_endpoint_is_refused_as_a_start_url(tmp_path, monkeypatch):
    monkeypatch.setenv("THESEUS_CDP_URL", "http://127.0.0.1:9222")
    with pytest.raises(SessionError, match="DevTools endpoint"):
        _session(tmp_path, start_url="http://localhost:9222/json/list")
    _session(tmp_path, start_url="http://localhost:3000/")  # another port on the same host is fine


def test_a_browser_session_that_finds_the_devtools_endpoint_gives_a_refusal(tmp_path):
    from theseus.explorer.borrow import devtools_endpoint_error

    def factory(config, run_dir):
        raise devtools_endpoint_error(config.start_url)

    s = McpSession(SessionConfig(start_url="https://short.example.com/", success_criterion=CRITERION),
                   run_dir=tmp_path / "run", session_cm_factory=factory)
    with pytest.raises(SessionError, match="DevTools endpoint"):
        s.start()


def test_the_start_message_says_plainly_when_nothing_was_borrowed(tmp_path):
    info = {"host": "example.com", "origin": "https://example.com", "cookies": 0, "local_storage": "no_tab"}
    s, _ = _session(tmp_path, browser=FakeBrowser(borrowed_login=[info]))
    message = s.start()
    assert "Nothing was borrowed from your Chrome (https://example.com: no cookies" in message
    assert "starts logged out" in message and "sessionStorage or IndexedDB" in message
    assert "Borrowed your login" not in message


@pytest.mark.parametrize("status, reason", [
    ("empty", "your open tab had no localStorage"),
    ("busy", "your tab on it didn't answer, maybe a dialog is open"),
    ("no_tab", "no tab of yours was open on it"),
    ("unreadable", "its page wouldn't give up its localStorage"),
])
def test_with_no_cookies_the_message_gives_the_real_reason_for_the_storage(tmp_path, status, reason):
    info = {"host": "example.com", "origin": "https://example.com", "cookies": 0, "local_storage": status}
    s, _ = _session(tmp_path, browser=FakeBrowser(borrowed_login=[info]))
    message = s.start()
    assert reason in message
    if status != "empty":
        assert "had no localStorage" not in message  # only an empty tab is described that way


def test_a_chain_of_origins_is_listed_in_the_start_message(tmp_path):
    infos = [{"host": "app.example.com", "origin": "https://app.example.com", "cookies": 2, "local_storage": "copied"},
             {"host": "idp.example.net", "origin": "https://idp.example.net", "cookies": 1, "local_storage": "no_tab"}]
    s, _ = _session(tmp_path, browser=FakeBrowser(borrowed_login=infos))
    message = s.start()
    assert "2 cookie(s) for https://app.example.com and its localStorage" in message
    assert "1 cookie(s) for https://idp.example.net" in message


def _stand_in_browser(landed, *, title="Blocked by Theseus"):
    from theseus.explorer.write_block import WriteBlock

    block = WriteBlock()
    block._blocked_navigations.add(landed.split("?")[0])
    page = make_page(click_goes_to={"Partner": "https://partner.example.net/"})
    page.title.return_value = title
    return FakeBrowser(page=page, write_block=block, landed_url=landed), page


def test_a_blocked_sign_in_handoff_at_start_is_explained_and_never_the_return_target(tmp_path):
    browser, page = _stand_in_browser("https://example.com/acs")
    s, _ = _session(tmp_path, browser=browser)
    started = s.start()
    assert "stand-in for a form post it blocked" in started and "THESEUS_WRITE_ALLOW" in started
    assert s._home_url == "https://example.com/start"  # the start URL, not the blocked document
    out = s.call("click", target="Partner")  # the page ends up on another site
    assert "returned to https://example.com/start" in out and page.url == "https://example.com/start"


def test_a_blocked_handoff_after_a_redirect_falls_back_to_the_origins_root(tmp_path):
    browser, _ = _stand_in_browser("https://login.example.net/acs")
    s, _ = _session(tmp_path, browser=browser)
    assert "redirected to https://login.example.net" in s.start()
    assert s._home_url == "https://login.example.net/"


def test_a_normal_start_page_is_remembered_and_not_explained(tmp_path):
    s, _ = _session(tmp_path)
    assert "stand-in" not in s.start()
    assert s._home_url == "https://example.com/start"


def test_text_on_the_blocked_stand_in_page_never_satisfies_the_criterion(tmp_path):
    browser, page = _stand_in_browser("https://example.com/save")
    s, _ = _session(tmp_path, browser=browser, success_criterion="the page shows 'submitted'")
    s.start()
    out = s.call("assert_state")
    assert out.startswith("not_matched") and "stand-in for a form post it blocked" in out
    assert page.asserted == []  # the page text was never consulted
    assert s.status()["success_asserted"] is False and not s.status()["ended"]
    out = s.call("wait_for", description="'submitted'")
    assert "stand-in for a form post" in out and "appeared" not in out


def test_a_waiting_for_the_page_to_settle_still_works_on_the_stand_in_page(tmp_path):
    browser, page = _stand_in_browser("https://example.com/save")
    page._theseus_wait_for = lambda description: {"success": True, "settled": True, "matched_at_ms": 5}
    s, _ = _session(tmp_path, browser=browser)
    s.start()
    assert "page settled" in s.call("wait_for", description="until the page is quiet")


def test_the_same_url_visited_normally_later_is_graded_normally(tmp_path):
    browser, page = _stand_in_browser("https://example.com/save")
    s, _ = _session(tmp_path, browser=browser)
    s.start()
    page.title.return_value = "Order placed"  # a normal GET of the same URL
    assert s.call("assert_state").startswith("matches")


# -- ending ----------------------------------------------------------------------------

def test_writes_released_while_the_browser_closes_are_in_the_summary(tmp_path):
    from theseus.explorer.write_block import WriteBlock

    block = WriteBlock()

    def pages_unload():  # sendBeacon and keepalive requests go out as the browser closes
        for _ in range(2):
            block._record({"method": "POST", "url": "https://example.com/beacon", "outcome": "blocked",
                           "resource_type": "ping", "navigation": False})

    s, _ = _session(tmp_path, browser=FakeBrowser(write_block=block, on_exit=pages_unload))
    s.start()
    summary = s.end()
    assert summary["extra"]["blocked_writes"] == 2
    assert json.loads((tmp_path / "run" / "summary.json").read_text())["extra"]["blocked_writes"] == 2
    assert s.status()["blocked_writes"] == 2  # session_status after the end agrees


def test_a_first_summary_is_on_disk_before_the_browser_closes(tmp_path):
    seen = []
    summary_path = tmp_path / "run" / "summary.json"
    s, _ = _session(tmp_path, browser=FakeBrowser(on_exit=lambda: seen.append(summary_path.exists())))
    s.start()
    s.end()
    assert seen == [True]  # if closing hung, the run would still have a summary


def test_a_browser_that_will_not_close_cleanly_does_not_lose_the_summary(tmp_path, capsys):
    s, _ = _session(tmp_path, browser=FakeBrowser(exit_error=RuntimeError("disk full")))
    s.start()
    summary = s.end()  # no exception
    assert summary["extra"]["cleanup_error"] == "RuntimeError: disk full"
    assert s.ended and (tmp_path / "run" / "summary.json").exists()
    assert "disk full" in capsys.readouterr().err
    assert s.end() == summary


# -- the memory guard ------------------------------------------------------------------

def test_a_session_measures_its_own_memory_growth_not_the_process_lifetime_peak(tmp_path, monkeypatch):
    from theseus.explorer import caps

    peak = {"mb": 5000}  # an earlier session left the process peak high
    monkeypatch.setattr(caps, "_default_rss_mb", lambda: peak["mb"])
    s, _ = _session(tmp_path)
    s.start()
    assert s.budget.sample_rss() == 0 and s.budget.exhausted_reason() is None
    peak["mb"] = 9000
    assert s.budget.sample_rss() == 4000
    assert s.budget.exhausted_reason() == "memory_budget_exhausted"


# -- the pump ----------------------------------------------------------------------------

def test_a_failing_pump_is_reported_in_the_result_and_the_status(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    s.pump_status = lambda: (3, "TargetClosedError: gone")
    out = s.call("note", text="x")
    assert "network pump failed 3 time(s) (last: TargetClosedError: gone)" in out
    assert out.rstrip().splitlines()[-1].startswith("[theseus]")  # the status line stays last
    assert "pump failed" not in s.call("note", text="y")  # reported once per new failure
    s.pump_status = lambda: (5, "TargetClosedError: gone")
    assert "failed 5 time(s)" in s.call("note", text="z")
    assert s.status()["pump_errors"] == 5 and s.status()["pump_last_error"] == "TargetClosedError: gone"


def test_a_bad_allow_pattern_stops_the_session_with_a_clear_message(tmp_path, monkeypatch):
    from theseus.mcp.session import default_session_cm

    monkeypatch.setenv("THESEUS_WRITE_ALLOW", "**/a/**/b/**/c")
    config = SessionConfig(start_url="https://example.com/", success_criterion=CRITERION)
    with pytest.raises(ValueError, match="THESEUS_WRITE_ALLOW pattern .* more than 2 wildcards before"):
        default_session_cm(config, tmp_path / "run")


def test_the_allowed_sentence_describes_the_setting_and_read_only(tmp_path):
    from theseus.explorer.write_block import WriteBlock, WritePolicy

    block = WriteBlock(WritePolicy(block=False))
    s, _ = _session(tmp_path, browser=FakeBrowser(write_block=block))
    assert "actions act with your real permissions" in s.start()
    s2, _ = _session(tmp_path / "ro", browser=FakeBrowser(write_block=WriteBlock(WritePolicy(block=False))),
                     read_only=True)
    started = s2.start()
    assert "read_only fakes every click, fill and submit, so nothing will change" in started
    assert "actions change real data" not in started


def test_what_the_page_chooses_is_capped_in_the_notes(tmp_path):
    from theseus.explorer.write_block import WriteBlock

    block = WriteBlock()
    page = make_page()

    def click(target):
        block._record({"method": "X" * 400, "url": "https://example.com/" + "a" * 900, "outcome": "blocked",
                       "resource_type": "fetch", "navigation": False})
        return {"success": True}

    page._theseus_click = click
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page, write_block=block))
    s.start()
    out = s.call("click", target="Go")
    assert "X" * 17 not in out and "a" * 121 not in out and "blocked 1 write(s)" in out


def test_cancelled_and_refused_requests_are_mentioned(tmp_path):
    from theseus.explorer.write_block import WriteBlock, WritePolicy

    block = WriteBlock(WritePolicy(allow_patterns=("**/search",)))
    page = make_page()

    def click(target):
        block._record({"method": "POST", "url": "https://example.com/search", "outcome": "aborted",
                       "resource_type": "fetch", "navigation": False, "allowed": False})
        block._record({"method": "GET", "url": "http://127.0.0.1:9222/json/list", "outcome": "refused",
                       "resource_type": "devtools", "navigation": False})
        return {"success": True}

    page._theseus_click = click
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page, write_block=block))
    s.start()
    out = s.call("click", target="Go")
    assert "1 request(s) allowed by THESEUS_WRITE_ALLOW were cancelled by the page before they were sent" in out
    assert "refused 1 request(s) to your Chrome's DevTools endpoint" in out


def test_many_new_tabs_are_summarised(tmp_path):
    browser = FakeBrowser()
    s, _ = _session(tmp_path, browser=browser)
    s.start()
    for i in range(7):
        browser.context.open_tab(f"https://example.com/t{i}")
    out = s.call("note", text="x")
    assert out.count("the page opened a new tab") == 3 and "the page opened 4 more tab(s)" in out


def test_the_sessions_own_tab_is_never_reported_as_new(tmp_path):
    s, browser = _session(tmp_path)
    assert "opened a new tab" not in s.start()
    assert browser.context.tabs and browser.context.tabs[0] is browser.page


def test_the_default_session_blocks_writes_and_writes_a_trace_only_when_asked(tmp_path, monkeypatch):
    from theseus.mcp import session as session_mod

    seen = {}
    monkeypatch.setattr(session_mod, "plain_session", lambda url, **kwargs: seen.update(kwargs) or "cm")
    config = SessionConfig(start_url="https://example.com/", success_criterion=CRITERION)
    session_mod.default_session_cm(config, tmp_path / "run")
    assert seen["write_block"].active is True  # blocking is what the server does unless the user says otherwise
    assert seen["write_block"].sink_path == tmp_path / "run" / "captured_writes.jsonl"
    assert seen["trace_dir"] is None
    monkeypatch.setenv("THESEUS_TRACE", "1")
    session_mod.default_session_cm(config, tmp_path / "run")
    assert seen["trace_dir"] == tmp_path / "run"
    monkeypatch.setenv("THESEUS_ALLOW_WRITES", "1")
    session_mod.default_session_cm(config, tmp_path / "run")
    assert seen["write_block"].active is False


def test_writes_are_blocked_by_default_in_the_policy():
    from theseus.explorer.write_block import WritePolicy

    assert WritePolicy.from_env().block is True and WritePolicy().block is True


def test_a_departure_that_comes_back_by_itself_is_left_alone(tmp_path):
    """The page is part-way through a sign-in chain (app -> provider -> app): reloading it would cut the chain."""
    page = make_page()
    reloaded = []

    def settle():  # the chain finishes while Theseus waits
        page.url = "https://example.com/home"

    s, _ = _session(tmp_path, browser=FakeBrowser(page=page, settle=settle,
                                                  follow_sign_in=lambda url: reloaded.append(url)))
    s.start()
    page.url = "https://idp.example.net/login"
    out = s.call("read_page")
    assert "came back by itself" in out and reloaded == []
    assert s.status()["boundary_violations"] == 0 and s._self_departures == {}
    assert page.url == "https://example.com/home"


def test_a_departure_that_stays_out_after_settling_is_counted_and_followed(tmp_path):
    page = make_page()
    settled, followed = [], []
    s, _ = _session(tmp_path, browser=FakeBrowser(
        page=page, settle=lambda: settled.append(1),
        follow_sign_in=lambda url: (followed.append(url), setattr(page, "url", url))[0]))
    s.start()
    page.url = "https://idp.example.net/login"  # still there after the wait
    out = s.call("read_page")
    assert settled and followed == ["https://example.com/start"]
    assert "by itself" in out and "came back" not in out and s._self_departures == {"https://idp.example.net": 1}


def test_a_form_that_posts_to_another_origin_is_blocked_and_not_a_violation(tmp_path):
    from theseus.explorer.write_block import WriteBlock

    block = WriteBlock()
    page = make_page(click_goes_to={"Send": "https://partner.example.net/receive"})
    s, _ = _session(tmp_path, browser=FakeBrowser(page=page, write_block=block))
    s.start()
    block._blocked_navigations.add("https://partner.example.net/receive")  # the post was faked...
    page.title.return_value = "Blocked by Theseus"                        # ...and its stand-in shows at that URL
    for _ in range(4):
        out = s.call("click", target="Send")
    assert "the form posts to https://partner.example.net, outside https://example.com" in out
    assert "the post was blocked, so nothing was sent there" in out and "returned to https://example.com/start" in out
    assert s.status()["boundary_violations"] == 0 and not s.status()["ended"]


def test_a_new_runs_directory_and_its_parents_are_private(tmp_path):
    from theseus.mcp.session import make_private_dirs

    run_dir = tmp_path / ".theseus" / "runs" / "20261025T000000Z_site"
    make_private_dirs(run_dir)
    for directory in (tmp_path / ".theseus", tmp_path / ".theseus" / "runs", run_dir):
        assert directory.is_dir() and (directory.stat().st_mode & 0o077) == 0, directory
    make_private_dirs(run_dir)  # again: nothing to do, no error


def test_a_session_start_leaves_its_runs_root_private(tmp_path):
    s, _ = _session(tmp_path / "home" / ".theseus" / "runs")
    s.start()
    assert (s.run_dir.parent.stat().st_mode & 0o077) == 0 and (s.run_dir.stat().st_mode & 0o077) == 0


def test_the_start_message_says_when_the_sign_in_was_longer_than_theseus_follows(tmp_path):
    s, _ = _session(tmp_path, browser=FakeBrowser(unfollowed=["https://s5.example.net"]))
    message = s.start()
    assert "stopped following the sign-in after 4 reloads" in message
    assert "the login of https://s5.example.net was not borrowed" in message


def test_a_normal_start_has_no_such_note(tmp_path):
    s, _ = _session(tmp_path)
    assert "stopped following" not in s.start()


def test_a_late_sign_in_that_hits_the_limit_says_so_in_the_return_note(tmp_path):
    page = make_page()
    left = ["https://s9.example.net"]

    def follow(url):
        page.url = "https://example.com/start"
        return []

    s, _ = _session(tmp_path, browser=FakeBrowser(page=page, follow_sign_in=follow, unfollowed=left))
    s.start()
    page.url = "https://idp.example.net/login"
    out = s.call("read_page")
    assert "by itself" in out and "the login of https://s9.example.net was not borrowed" in out


# -- site profiles ----------------------------------------------------------------------

SANDBOX = "https://staging.example.com"
SEARCH_URL = f"{SANDBOX}/api/v2/search/journeys"


def _profiles(tmp_path, monkeypatch, text=None, name="staging.yaml"):
    """A private profiles directory, optionally holding one profile; the server is pointed at it."""
    import textwrap

    d = tmp_path / "sites"
    d.mkdir(exist_ok=True)
    d.chmod(0o700)
    if text is not None:
        (d / name).write_text(textwrap.dedent(text))
    monkeypatch.setenv("THESEUS_PROFILES_DIR", str(d))
    return d / name


def _on_sandbox(tmp_path, **kwargs):
    return _session(tmp_path, start_url=f"{SANDBOX}/", **kwargs)


def test_a_profile_edit_reaches_the_next_sessions_write_block_without_a_restart(tmp_path, monkeypatch):
    from theseus.mcp import session as session_mod

    seen = {}
    monkeypatch.setattr(session_mod, "plain_session", lambda url, **kwargs: seen.update(kwargs) or "cm")
    path = _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\n")
    config = SessionConfig(start_url=f"{SANDBOX}/", success_criterion=CRITERION)

    first = McpSession(config, run_dir=tmp_path / "run1")
    first._session_cm_factory(config, tmp_path / "run1")
    assert seen["write_block"].active is True and seen["write_block"].policy.allow_patterns == ()

    path.write_text(f"origins: [{SANDBOX}]\nallow: ['{SEARCH_URL}']\n")  # the user edits; the server keeps running
    second = McpSession(config, run_dir=tmp_path / "run2")
    second._session_cm_factory(config, tmp_path / "run2")
    assert seen["write_block"].active is True
    assert seen["write_block"].policy.allow_patterns == (SEARCH_URL,)
    assert seen["write_block"].launch_args  # still blocking everything else


def test_the_profile_adds_to_the_environments_patterns(tmp_path, monkeypatch):
    from theseus.mcp import session as session_mod

    seen = {}
    monkeypatch.setattr(session_mod, "plain_session", lambda url, **kwargs: seen.update(kwargs) or "cm")
    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\nallow: ['{SEARCH_URL}']\n")
    monkeypatch.setenv("THESEUS_WRITE_ALLOW", "https://login.example.com/**")
    config = SessionConfig(start_url=f"{SANDBOX}/", success_criterion=CRITERION)
    s = McpSession(config, run_dir=tmp_path / "run")
    s._session_cm_factory(config, tmp_path / "run")
    assert seen["write_block"].policy.allow_patterns == ("https://login.example.com/**", SEARCH_URL)
    assert s._allow_source == "THESEUS_WRITE_ALLOW and the site profile"


def test_a_profile_for_another_site_changes_nothing(tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\nallow: ['{SEARCH_URL}']\nwrites: block\n")
    s, _ = _session(tmp_path)  # https://example.com/start
    started = s.start()
    assert s.profile is None and "Site profile" not in started and "Ignored" not in started
    assert s.write_policy.allow_patterns == ()
    assert s.end()["extra"]["profile"] is None
    assert "profiles_ignored" not in s.summary["extra"]


def test_the_start_message_names_the_profile_and_where_allowed_requests_come_from(tmp_path, monkeypatch):
    from theseus.explorer.write_block import WriteBlock, WritePolicy

    path = _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\nallow: ['{SEARCH_URL}']\nforbidden_text: [Send Invoice]\n")
    block = WriteBlock(WritePolicy(allow_patterns=(SEARCH_URL,)))
    s, _ = _on_sandbox(tmp_path, browser=FakeBrowser(write_block=block))
    started = s.start()
    assert f"Site profile 'staging' ({path}) applies: 1 request pattern(s) are let through; 1 forbidden verb(s) added." in started
    assert f"Requests matching the site profile ({SEARCH_URL}) are sent." in started
    block._record({"method": "POST", "url": SEARCH_URL, "resource_type": "fetch", "navigation": False,
                   "allowed": True, "allowed_by": SEARCH_URL, "outcome": "sent"})
    assert "sent 1 write(s) allowed by the site profile: POST /api/v2/search/journeys" in s.call("note", text="x")
    extra = s.end()["extra"]
    assert extra["profile"]["name"] == "staging" and extra["profile"]["allow"] == [SEARCH_URL]
    assert extra["profile"]["writes"] == "block" and extra["profile"]["authorized_by"] is None
    assert extra["dry_run"] is False and extra["allowed_writes"] == 1


def test_a_live_profile_says_who_authorized_it(tmp_path, monkeypatch):
    from theseus.explorer.write_block import WriteBlock, WritePolicy

    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\nwrites: live\nauthorized_by: ron@example.com\n")
    s, _ = _on_sandbox(tmp_path, browser=FakeBrowser(write_block=WriteBlock(WritePolicy(block=False))))
    started = s.start()
    assert ("Writes are ALLOWED (the site profile 'staging' says writes: live, authorized by ron@example.com): "
            "actions act with your real permissions.") in started
    assert "writes are live (authorized by ron@example.com)" in started
    extra = s.end()["extra"]
    assert extra["profile"]["writes"] == "live" and extra["profile"]["authorized_by"] == "ron@example.com"
    assert extra["dry_run"] is False


def test_a_live_profile_cant_overrule_block_writes(tmp_path, monkeypatch):
    from theseus.explorer.write_block import WriteBlock

    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\nwrites: live\nauthorized_by: ron@example.com\n")
    monkeypatch.setenv("THESEUS_BLOCK_WRITES", "1")
    s, _ = _on_sandbox(tmp_path)
    assert s.write_policy.block is True
    s, _ = _on_sandbox(tmp_path / "again", browser=FakeBrowser(write_block=WriteBlock(s.write_policy)))
    started = s.start()
    assert "Writes are blocked" in started
    assert "It says writes are live, but THESEUS_BLOCK_WRITES is set, so they stay blocked." in started


def test_a_profile_adds_forbidden_verbs_and_the_defaults_stay(tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\nforbidden_text: [Send Invoice, delete]\n")
    s, _ = _on_sandbox(tmp_path)
    s.start()
    assert "forbidden_text_match" in s.call("click", target="Send invoice to customer")
    assert "forbidden_text_match" in s.call("click", target="Delete everything")  # a default, and not doubled up
    assert s.forbidden_verbs.count("Delete") == 1 and "delete" not in s.forbidden_verbs
    assert "forbidden_text_match" not in s.call("click", target="Place order")


def test_without_a_profile_the_forbidden_verbs_are_the_defaults(tmp_path):
    from theseus.explorer.explorer_tools import DEFAULT_EXPLORE_FORBIDDEN

    s, _ = _session(tmp_path)
    assert s.forbidden_verbs == tuple(DEFAULT_EXPLORE_FORBIDDEN)


def test_a_profiles_limits_apply_and_the_environments_win(tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\ncaps: {{max_turns: 60, max_wall_clock_sec: 1200}}\n")
    config = SessionConfig(start_url=f"{SANDBOX}/", success_criterion=CRITERION)
    s = McpSession(config, run_dir=tmp_path / "run", session_cm_factory=FakeBrowser().factory)
    assert s.caps.max_turns == 60 and s.caps.max_wall_clock_sec == 1200.0
    monkeypatch.setenv("THESEUS_MAX_TURNS", "5")
    s = McpSession(config, run_dir=tmp_path / "run2", session_cm_factory=FakeBrowser().factory)
    assert s.caps.max_turns == 5 and s.caps.max_wall_clock_sec == 1200.0
    monkeypatch.setenv("THESEUS_MAX_WALL_CLOCK_SEC", "30")
    s = McpSession(config, run_dir=tmp_path / "run3", session_cm_factory=FakeBrowser().factory)
    assert s.caps.max_wall_clock_sec == 30.0


def test_the_start_message_states_the_limits_in_force(tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\ncaps: {{max_turns: 60}}\n")
    config = SessionConfig(start_url=f"{SANDBOX}/", success_criterion=CRITERION)
    s = McpSession(config, run_dir=tmp_path / "run", session_cm_factory=FakeBrowser().factory)  # the default caps
    assert "Limits: 60 tool calls" in s.start()


def test_a_broken_profile_is_ignored_and_the_start_message_and_summary_say_so(tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\nalow: ['{SEARCH_URL}']\n", name="bad.yaml")
    s, _ = _on_sandbox(tmp_path)
    started = s.start()
    assert s.profile is None and s.write_policy.allow_patterns == () and s.write_policy.block is True
    assert "Ignored site profile(s): bad.yaml: unknown setting(s) 'alow'" in started
    extra = s.end()["extra"]
    assert extra["profile"] is None and extra["profiles_ignored"][0].startswith("bad.yaml: unknown setting")


def test_two_profiles_for_one_origin_are_both_ignored_and_say_so(tmp_path, monkeypatch):
    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\nallow: ['{SEARCH_URL}']\n", name="a.yaml")
    _profiles(tmp_path, monkeypatch, f"origins: [{SANDBOX}]\nwrites: live\nauthorized_by: me\n", name="b.yaml")
    s, _ = _on_sandbox(tmp_path)
    started = s.start()
    assert s.profile is None and s.write_policy.block is True and s.write_policy.allow_patterns == ()
    assert "a.yaml, b.yaml all name https://staging.example.com, so none of them applies" in started


def test_the_session_never_takes_a_profile_name_from_the_caller():
    import inspect

    assert "profile" not in inspect.signature(McpSession.__init__).parameters
    assert "profile" not in {f for f in SessionConfig.__dataclass_fields__}


# -- page text is untrusted ---------------------------------------------------------

def test_read_page_and_inspect_error_end_with_a_reminder_that_the_page_text_is_untrusted(tmp_path):
    from theseus.mcp.session import UNTRUSTED_PAGE_TEXT_NOTE

    s, _ = _session(tmp_path)
    s.start()
    for tool in ("read_page", "inspect_error"):
        lines = s.call(tool).splitlines()
        assert lines.count(UNTRUSTED_PAGE_TEXT_NOTE) == 1, lines
        assert lines.index(UNTRUSTED_PAGE_TEXT_NOTE) < len(lines) - 1 and lines[-1].startswith("[theseus]")
    assert s.call("read_page").startswith("url: ")  # the page text still comes first


def test_other_results_carry_no_such_reminder(tmp_path):
    from theseus.mcp.session import UNTRUSTED_PAGE_TEXT_NOTE

    s, _ = _session(tmp_path)
    s.start()
    for tool, kwargs in (("click", {"target": "Place order"}), ("navigate", {"url": "/orders"}), ("note", {"text": "x"})):
        assert UNTRUSTED_PAGE_TEXT_NOTE not in s.call(tool, **kwargs)


def test_a_failed_read_page_has_no_page_text_and_no_reminder(tmp_path):
    from theseus.mcp.session import UNTRUSTED_PAGE_TEXT_NOTE

    s, _ = _session(tmp_path)
    s.start()

    def boom():
        raise RuntimeError("boom")

    s._functions["read_page"] = boom
    out = s.call("read_page")
    assert "read_page_failed: RuntimeError: boom" in out and UNTRUSTED_PAGE_TEXT_NOTE not in out


# -- the audit record: who, and when ------------------------------------------------

def test_the_summary_says_who_ran_the_session_and_when(tmp_path):
    import getpass
    from datetime import datetime, timedelta

    s, _ = _session(tmp_path)
    s.start()
    s.call("read_page")
    extra = s.end()["extra"]
    started, ended = (datetime.fromisoformat(extra[k]) for k in ("started_at", "ended_at"))
    assert extra["user"] == getpass.getuser()
    assert started.utcoffset() == timedelta(0) and ended.utcoffset() == timedelta(0) and started <= ended
    on_disk = json.loads((tmp_path / "run" / "summary.json").read_text())["extra"]
    assert on_disk["user"] == extra["user"] and on_disk["started_at"] == extra["started_at"]


def test_the_trace_lines_of_the_session_carry_their_own_time(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    s.call("read_page")
    s.end()
    lines = [json.loads(line) for line in (tmp_path / "run" / "trace.jsonl").read_text().splitlines()]
    assert lines and all("at" in line and line["at"].endswith("+00:00") for line in lines)


# -- limits on what a call may carry --------------------------------------------------

def test_an_oversized_argument_is_refused_without_using_a_turn_or_a_strike(tmp_path):
    from theseus.mcp.limits import MAX_FILL_VALUE_CHARS

    s, browser = _session(tmp_path)
    s.start()
    out = s.call("fill", label="Notes", value="x" * (MAX_FILL_VALUE_CHARS + 1))
    assert out.startswith("rejected: argument_invalid: value is 10001 characters; the limit is 10000. Shorten it.")
    assert "call 0 of 10" in out and s.budget.turns_used == 0 and s.context.forbidden_guard.violations == 0
    assert not (tmp_path / "run" / "trace.jsonl").exists() or "fill" not in (tmp_path / "run" / "trace.jsonl").read_text()
    after = s.call("fill", label="Notes", value="short")  # the next call reaches the tool and uses the first turn
    assert not after.startswith("rejected") and "call 1 of 10" in after


def test_a_refused_call_still_counts_as_activity_and_the_session_stays_open(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    assert s.call("navigate", url="https://example.com/" + "a" * 3000).startswith("rejected: argument_invalid")
    assert s.active and not s.context.terminal_reason


def test_an_index_out_of_range_is_refused(tmp_path):
    s, _ = _session(tmp_path)
    s.start()
    assert "index is -3; it must be from 0 to 100000" in s.call("click_index", index=-3)


def test_session_start_arguments_over_the_limit_are_refused_before_anything_opens(tmp_path):
    from theseus.mcp.limits import MAX_CRITERION_CHARS, MAX_GOAL_CHARS, MAX_URL_CHARS

    for kwargs, message in (
        ({"start_url": "https://example.com/" + "x" * MAX_URL_CHARS}, "start_url is"),
        ({"goal": "g" * (MAX_GOAL_CHARS + 1)}, "goal is"),
        ({"success_criterion": "c" * (MAX_CRITERION_CHARS + 1)}, "success_criterion is"),
    ):
        browser = FakeBrowser()
        with pytest.raises(SessionError, match=message):
            _session(tmp_path, browser=browser, **kwargs)
        assert browser.entered == 0
