"""Unit tests for theseus/explorer/session.py — the default browser session.

Playwright is replaced by a fake, so no browser starts.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from theseus.explorer import session as session_mod
from theseus.explorer.borrow import DevToolsEndpointError
from theseus.explorer.write_block import LAUNCH_ARGS, WriteBlock, WritePolicy


@pytest.fixture(autouse=True)
def _no_real_chrome(monkeypatch):
    """A test that borrows a login says how; none may read the developer's own Chrome."""
    def refuse(*args, **kwargs):
        raise AssertionError("this test reached for a real Chrome: patch session_mod.borrow_login")

    monkeypatch.setattr(session_mod, "borrow_login", refuse)


def _fake_playwright(*, contexts=None):
    pw = MagicMock(name="playwright")
    launched = MagicMock(name="launched_browser")
    attached = MagicMock(name="attached_browser")
    attached.contexts = [MagicMock(name="user_context")] if contexts is None else contexts
    pw.chromium.launch.return_value = launched
    pw.chromium.connect_over_cdp.return_value = attached
    sync_pw = MagicMock(name="sync_playwright")
    sync_pw.return_value.__enter__.return_value = pw
    return sync_pw, pw, launched, attached


def _run(sync_pw, **kwargs):
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with session_mod.plain_session("https://example.com/start", **kwargs) as s:
            return s


def test_launch_is_headless_by_default_and_navigates():
    sync_pw, pw, launched, _ = _fake_playwright()
    s = _run(sync_pw)
    assert pw.chromium.launch.call_args.kwargs == {"headless": True, "chromium_sandbox": True}
    context = launched.new_context.return_value
    page = context.new_page.return_value
    assert s.page is page
    page.goto.assert_called_once_with("https://example.com/start", wait_until="domcontentloaded")
    page.set_default_timeout.assert_called_once_with(session_mod.ACTION_TIMEOUT_MS)
    context.close.assert_called_once()
    launched.close.assert_called_once()


def test_headed_launch():
    sync_pw, pw, _, _ = _fake_playwright()
    _run(sync_pw, headed=True)
    assert pw.chromium.launch.call_args.kwargs == {"headless": False, "chromium_sandbox": True}


def test_channel_selects_an_installed_browser():
    sync_pw, pw, _, _ = _fake_playwright()
    _run(sync_pw, channel="chrome")
    assert pw.chromium.launch.call_args.kwargs == {"headless": True, "chromium_sandbox": True, "channel": "chrome"}


def test_storage_state_is_passed_to_the_new_context():
    sync_pw, _, launched, _ = _fake_playwright()
    _run(sync_pw, storage_state="/tmp/state.json")
    options = launched.new_context.call_args.kwargs
    assert options["storage_state"] == "/tmp/state.json"


def test_attach_borrows_the_login_then_launches_theseus_own_browser(monkeypatch):
    sync_pw, pw, launched, _ = _fake_playwright()
    calls = []

    def borrow(cdp_url, url):
        calls.append((cdp_url, url))
        return ({"cookies": [{"name": "sid"}], "origins": []},
                {"host": "example.com", "origin": "https://example.com", "cookies": 1, "local_storage": "no_tab"})

    monkeypatch.setattr(session_mod, "borrow_login", borrow)
    launched.new_context.return_value.new_page.return_value.url = "https://example.com/start"
    s = _run(sync_pw, cdp_url="http://127.0.0.1:9222")
    assert calls == [("http://127.0.0.1:9222", "https://example.com/start")]
    pw.chromium.connect_over_cdp.assert_not_called()  # Playwright never attaches to the user's Chrome
    pw.chromium.launch.assert_called_once()
    assert launched.new_context.call_args.kwargs["storage_state"] == {"cookies": [{"name": "sid"}], "origins": []}
    assert s.borrowed_login[0]["local_storage"] == "no_tab"


def test_a_chrome_with_nothing_to_borrow_starts_empty(monkeypatch):
    sync_pw, _, launched, _ = _fake_playwright()
    monkeypatch.setattr(session_mod, "borrow_login", lambda cdp, url: (
        {"cookies": [], "origins": []},
        {"host": "example.com", "origin": "https://example.com", "cookies": 0, "local_storage": "no_tab"}))
    launched.new_context.return_value.new_page.return_value.url = "https://example.com/start"
    _run(sync_pw, cdp_url="http://127.0.0.1:9222")
    assert launched.new_context.call_args.kwargs["storage_state"] == {"cookies": [], "origins": []}


@pytest.mark.parametrize("landed", ["https://www.example.net/home", "https://example.com:8443/", "http://example.com/start"])
def test_attach_borrows_the_landed_origins_login_after_a_redirect(monkeypatch, landed):
    sync_pw, _, launched, _ = _fake_playwright()
    calls = []

    def borrow(cdp_url, url):
        calls.append(url)
        return ({"cookies": [{"name": url}], "origins": []},
                {"host": "h", "origin": url, "cookies": 1, "local_storage": "no_tab"})

    monkeypatch.setattr(session_mod, "borrow_login", borrow)
    page = launched.new_context.return_value.new_page.return_value
    page.url = landed
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with session_mod.plain_session("https://example.com/start", cdp_url="http://127.0.0.1:9222") as s:
            pass
    assert calls == ["https://example.com/start", landed]  # another origin: borrowed again
    launched.new_context.return_value.add_cookies.assert_called_once_with([{"name": landed}])
    # The start URL is loaded again, so the whole chain runs with the borrowed login.
    assert [c.args[0] for c in page.goto.call_args_list] == ["https://example.com/start"] * 2
    assert len(s.borrowed_login) == 2


def test_no_second_borrow_when_the_start_url_stays_on_its_origin(monkeypatch):
    sync_pw, _, launched, _ = _fake_playwright()
    calls = []
    monkeypatch.setattr(session_mod, "borrow_login",
                        lambda cdp, url: (calls.append(url) or ({"cookies": [], "origins": []},
                                          {"host": "example.com", "origin": "https://example.com",
                                           "cookies": 0, "local_storage": "no_tab"})))
    launched.new_context.return_value.new_page.return_value.url = "https://example.com/other"
    _run(sync_pw, cdp_url="http://127.0.0.1:9222")
    assert calls == ["https://example.com/start"]


def test_a_failed_start_still_saves_the_trace_and_closes_the_browser(tmp_path):
    sync_pw, _, launched, _ = _fake_playwright()
    context = launched.new_context.return_value
    context.new_page.return_value.goto.side_effect = TimeoutError("start page too slow")
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with pytest.raises(TimeoutError):
            with session_mod.plain_session("https://example.com/start", trace_dir=tmp_path):
                pass
    context.tracing.stop.assert_called_once_with(path=str(tmp_path / "trace.zip"))
    context.close.assert_called_once()
    launched.close.assert_called_once()


def test_trace_written_into_trace_dir_for_launched_browser(tmp_path: Path):
    sync_pw, _, launched, _ = _fake_playwright()
    s = _run(sync_pw, trace_dir=tmp_path)
    context = launched.new_context.return_value
    context.tracing.start.assert_called_once()
    context.tracing.stop.assert_called_once_with(path=str(tmp_path / "trace.zip"))
    assert s.trace_path == tmp_path / "trace.zip"


def test_cleanup_runs_when_the_body_raises():
    sync_pw, _, launched, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with pytest.raises(RuntimeError):
            with session_mod.plain_session("https://example.com/start"):
                raise RuntimeError("agent crashed")
    launched.new_context.return_value.close.assert_called_once()
    launched.close.assert_called_once()


@pytest.mark.parametrize("value, expected", [
    (None, None), ("", None), ("   ", None),
    ("http://127.0.0.1:9222", "http://127.0.0.1:9222"),
    ("  http://127.0.0.1:9222  ", "http://127.0.0.1:9222"),
])
def test_cdp_url_from_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(session_mod.CDP_URL_ENV, raising=False)
    else:
        monkeypatch.setenv(session_mod.CDP_URL_ENV, value)
    assert session_mod.cdp_url_from_env() == expected


def test_storage_state_from_env(monkeypatch, tmp_path: Path):
    state = tmp_path / "state.json"
    monkeypatch.setenv(session_mod.STORAGE_STATE_ENV, str(state))
    assert session_mod.storage_state_from_env() is None  # file missing
    state.write_text("{}")
    assert session_mod.storage_state_from_env() == str(state)
    monkeypatch.setenv(session_mod.STORAGE_STATE_ENV, "   ")
    assert session_mod.storage_state_from_env() is None
    monkeypatch.delenv(session_mod.STORAGE_STATE_ENV)
    assert session_mod.storage_state_from_env() is None


def test_make_session_factory_reads_env_and_headed(monkeypatch, tmp_path):
    monkeypatch.setenv(session_mod.CDP_URL_ENV, "http://127.0.0.1:9222")
    for name in (session_mod.STORAGE_STATE_ENV, session_mod.TRACE_ENV, "THESEUS_ALLOW_WRITES",
                 "THESEUS_BLOCK_WRITES", "THESEUS_WRITE_ALLOW"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(session_mod.BROWSER_CHANNEL_ENV, " chrome ")
    captured = {}

    def _fake_plain_session(url, **kwargs):
        captured.update(kwargs, url=url)
        return "cm"

    monkeypatch.setattr(session_mod, "plain_session", _fake_plain_session)
    invocation = MagicMock(headed=True)
    assert session_mod.make_session_factory(trace_dir=tmp_path)("https://x", invocation) == "cm"
    block = captured.pop("write_block")
    # The runner doesn't block writes unless asked; the captured file is still set up.
    assert not block.active and block.sink_path == tmp_path / "captured_writes.jsonl"
    assert captured == {
        "url": "https://x",
        "headed": True,
        "trace_dir": None,  # no trace without THESEUS_TRACE=1
        "cdp_url": "http://127.0.0.1:9222",
        "storage_state": None,
        "channel": "chrome",
    }
    monkeypatch.setenv(session_mod.TRACE_ENV, "1")
    monkeypatch.setenv("THESEUS_BLOCK_WRITES", "1")
    session_mod.make_session_factory(trace_dir=tmp_path)("https://x", invocation)
    assert captured["trace_dir"] == tmp_path and captured["write_block"].active


# -- settling and the sign-in chain -----------------------------------------------

class ScriptedPage:
    """A page that navigates by itself after loading, the way an SSO redirect does.

    ``script(page, url, borrowed)`` is called on every ``goto`` and returns the
    URLs the page then moves to, one per quiet period (``wait_for_timeout``).
    """

    def __init__(self, script, borrowed):
        self.main_frame = SimpleNamespace(url="about:blank")
        self.url = "about:blank"
        self.gotos = []
        self._script, self._borrowed = script, borrowed
        self._pending = []
        self._on_navigated = None

    def on(self, event, handler):
        if event == "framenavigated":
            self._on_navigated = handler

    def set_default_timeout(self, ms):
        pass

    def _move(self, url):
        self.url = self.main_frame.url = url
        if self._on_navigated:
            self._on_navigated(self.main_frame)

    def goto(self, url, **kwargs):
        self.gotos.append(url)
        steps = list(self._script(self, url, self._borrowed))
        self._move(steps.pop(0))
        self._pending = steps
        return MagicMock(name="response", **{"server_addr.return_value": None})

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, ms):
        if self._pending:
            self._move(self._pending.pop(0))


def _scripted_session(script, *, cdp_url="http://127.0.0.1:9222", have_login=lambda url: True):
    sync_pw, _, launched, _ = _fake_playwright()
    borrowed = []
    context = launched.new_context.return_value
    page = ScriptedPage(script, borrowed)
    context.new_page.return_value = page
    calls = []

    def borrow(cdp, url):
        calls.append(url)
        cookies = [{"name": f"sid-{session_mod.normalize_origin(url)}"}] if have_login(url) else []
        return ({"cookies": cookies, "origins": []},
                {"host": "h", "origin": session_mod.normalize_origin(url), "cookies": len(cookies),
                 "local_storage": "no_tab"})

    def add_cookies(cookies):
        borrowed.extend(c["name"] for c in cookies)

    context.add_cookies.side_effect = add_cookies
    return sync_pw, page, calls, borrowed, borrow


def _sso(page, url, borrowed):
    """app -> (script) -> idp; with the idp's login borrowed, idp -> app home."""
    if url.startswith("https://app.example.com"):
        yield url
        yield "https://idp.example.net/login"  # a JavaScript redirect after the page loaded
        if "sid-https://idp.example.net" in borrowed:
            yield "https://app.example.com/home"
    else:
        yield url


def test_a_script_redirect_to_a_sign_in_provider_gets_that_providers_login(monkeypatch):
    sync_pw, page, calls, borrowed, borrow = _scripted_session(_sso)
    monkeypatch.setattr(session_mod, "borrow_login", borrow)
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with session_mod.plain_session("https://app.example.com/", cdp_url="http://127.0.0.1:9222") as s:
            assert s.page.url == "https://app.example.com/home"  # signed in: back on the app
    assert calls == ["https://app.example.com/", "https://idp.example.net/login"]
    assert page.gotos == ["https://app.example.com/"] * 2  # the chain ran again with both logins
    assert [i["origin"] for i in s.borrowed_login] == ["https://app.example.com", "https://idp.example.net"]


def test_a_provider_the_user_is_not_signed_in_to_leaves_the_page_there_without_a_reload(monkeypatch):
    sync_pw, page, calls, _, borrow = _scripted_session(_sso, have_login=lambda url: "idp" not in url)
    monkeypatch.setattr(session_mod, "borrow_login", borrow)
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with session_mod.plain_session("https://app.example.com/", cdp_url="http://127.0.0.1:9222") as s:
            assert s.page.url == "https://idp.example.net/login"  # where the page settled: login needed
    assert page.gotos == ["https://app.example.com/"]  # nothing new to try, so no second load
    assert s.borrowed_login[1]["cookies"] == 0


def test_a_chain_of_providers_is_followed_but_only_so_far(monkeypatch):
    def endless(page, url, borrowed):
        n = int(url.rsplit("hop", 1)[1]) if "hop" in url else 0
        yield url
        yield f"https://site{n + 1}.example.com/hop{n + 1}"

    sync_pw, page, calls, _, borrow = _scripted_session(endless)
    monkeypatch.setattr(session_mod, "borrow_login", borrow)
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with session_mod.plain_session("https://site0.example.com/", cdp_url="http://127.0.0.1:9222"):
            pass
    assert len(calls) <= session_mod.MAX_LOGIN_HOPS + 1
    assert len(page.gotos) <= session_mod.MAX_LOGIN_HOPS + 1


def test_without_a_borrowed_login_the_session_still_waits_for_redirects_to_finish():
    def redirects(page, url, borrowed):
        yield url
        yield "https://login.example.net/"  # a meta refresh or script, after the load event

    sync_pw, page, _, _, _ = _scripted_session(redirects, cdp_url=None)
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with session_mod.plain_session("https://app.example.com/") as s:
            assert s.page.url == "https://login.example.net/"
    assert s.borrowed_login is None and page.gotos == ["https://app.example.com/"]


def test_local_storage_of_a_later_origin_is_set_before_its_own_scripts_run():
    context = MagicMock()
    added = session_mod._apply_login(context, {
        "cookies": [],
        "origins": [{"origin": "https://idp.example.net", "localStorage": [{"name": "tok", "value": 'a"b</script>'}]}],
    })
    assert added
    script = context.add_init_script.call_args.args[0]
    assert 'if (location.origin !== origin) return;' in script  # only that origin's frames
    assert '["https://idp.example.net", [["tok", "a\\"b</script>"]]]' in script
    # Once per tab, overwriting what the anonymous first load wrote; never again on later loads.
    assert "sessionStorage.getItem('__theseus_local_storage') === origin) return;" in script
    assert "localStorage.setItem(k, v)" in script and "getItem(k) === null" not in script
    context.add_cookies.assert_not_called()


def test_a_login_with_nothing_in_it_adds_nothing():
    context = MagicMock()
    assert session_mod._apply_login(context, {"cookies": [], "origins": []}) is False
    context.add_cookies.assert_not_called()
    context.add_init_script.assert_not_called()


def test_the_session_can_follow_a_sign_in_chain_again_later():
    """A redirect that fires after the start settled is handled by loading the home page again,
    this time borrowing the login of the origin the page went to."""
    def script(page, url, borrowed):
        yield url
        if url.startswith("https://app.example.com") and "sid-https://idp.example.net" not in borrowed:
            yield "https://idp.example.net/login"
        elif url.startswith("https://app.example.com"):
            yield "https://app.example.com/home"

    sync_pw, page, calls, borrowed, borrow = _scripted_session(script)
    # The page doesn't redirect at the start; make the first load plain.
    plain = {"first": True}
    original = page._script

    def script_once(p, url, b):
        if plain.pop("first", False):
            yield url
        else:
            yield from original(p, url, b)

    page._script = script_once
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        import theseus.explorer.session as mod

        mod_borrow = mod.borrow_login
        mod.borrow_login = borrow
        try:
            with mod.plain_session("https://app.example.com/", cdp_url="http://127.0.0.1:9222") as s:
                assert calls == ["https://app.example.com/"] and s.page.url == "https://app.example.com/"
                page._move("https://idp.example.net/login")  # a redirect that fired after the start settled
                got = s.follow_sign_in("https://app.example.com/")
                assert [i["origin"] for i in got] == ["https://idp.example.net"]
                assert s.page.url == "https://app.example.com/home"
        finally:
            mod.borrow_login = mod_borrow
    assert calls == ["https://app.example.com/", "https://idp.example.net/login"]


def test_while_borrowing_the_session_also_waits_for_the_network_to_go_quiet(monkeypatch):
    sync_pw, _, launched, _ = _fake_playwright()
    monkeypatch.setattr(session_mod, "borrow_login", lambda cdp, url: (
        {"cookies": [], "origins": []},
        {"host": "h", "origin": "https://example.com", "cookies": 0, "local_storage": "no_tab"}))
    page = launched.new_context.return_value.new_page.return_value
    page.url = "https://example.com/start"
    _run(sync_pw, cdp_url="http://127.0.0.1:9222")
    waited = [c.args[0] for c in page.wait_for_load_state.call_args_list]
    assert "load" in waited and "networkidle" in waited

    sync_pw, _, launched, _ = _fake_playwright()
    page = launched.new_context.return_value.new_page.return_value
    _run(sync_pw)  # not borrowing: a cheaper wait
    assert "networkidle" not in [c.args[0] for c in page.wait_for_load_state.call_args_list]


def test_the_write_block_is_told_where_the_users_devtools_endpoint_is(monkeypatch):
    sync_pw, _, launched, _ = _fake_playwright()
    monkeypatch.setattr(session_mod, "borrow_login", lambda cdp, url: (
        {"cookies": [], "origins": []},
        {"host": "h", "origin": "https://example.com", "cookies": 0, "local_storage": "no_tab"}))
    launched.new_context.return_value.new_page.return_value.url = "https://example.com/start"
    seen = {}
    block = WriteBlock()
    block.install = lambda browser, context: seen.update(devtools_url=block.devtools_url)
    _run(sync_pw, cdp_url="http://127.0.0.1:9222", write_block=block)
    assert seen == {"devtools_url": "http://127.0.0.1:9222"}


def test_the_write_block_is_installed_before_the_first_page_loads():
    sync_pw, _, launched, _ = _fake_playwright()
    order = []
    block = WriteBlock()
    block.install = lambda browser, context: order.append("install")
    launched.new_context.return_value.new_page.return_value.goto.side_effect = lambda *a, **k: order.append("goto")
    _run(sync_pw, write_block=block)
    assert order[:2] == ["install", "goto"]


def test_a_first_page_that_cannot_open_gets_the_sandbox_hint_unless_the_user_opted_out(monkeypatch):
    for opted_out in (False, True):
        sync_pw, _, launched, _ = _fake_playwright()
        launched.new_context.return_value.new_page.side_effect = Exception(
            "BrowserContext.new_page: Target page, context or browser has been closed")
        if opted_out:
            monkeypatch.setenv(session_mod.NO_SANDBOX_ENV, "1")
        with patch("playwright.sync_api.sync_playwright", sync_pw):
            with pytest.raises(Exception) as err:
                with session_mod.plain_session("https://example.com/start"):
                    pass
        assert ("THESEUS_NO_SANDBOX=1" in str(err.value)) is (not opted_out)
        launched.close.assert_called_once()


def test_a_launch_failure_that_mentions_the_sandbox_is_not_dressed_up_after_opting_out(monkeypatch):
    monkeypatch.setenv(session_mod.NO_SANDBOX_ENV, "1")
    sync_pw, pw, _, _ = _fake_playwright()
    pw.chromium.launch.side_effect = RuntimeError("Executable doesn't exist (args: --no-sandbox)")
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with pytest.raises(RuntimeError) as err:
            with session_mod.plain_session("https://example.com/start"):
                pass
    assert "set THESEUS_NO_SANDBOX=1" not in str(err.value)


# -- the DevTools endpoint ------------------------------------------------------------

def test_the_devtools_endpoint_is_refused_before_a_browser_starts(monkeypatch):
    sync_pw, pw, _, _ = _fake_playwright()
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with pytest.raises(DevToolsEndpointError, match="DevTools endpoint"):
            with session_mod.plain_session("http://localhost:9222/json/list", cdp_url="http://127.0.0.1:9222"):
                pass
    pw.chromium.launch.assert_not_called()


def test_a_start_url_that_lands_on_the_devtools_endpoint_is_refused_and_cleaned_up(monkeypatch):
    sync_pw, _, launched, _ = _fake_playwright()
    monkeypatch.setattr(session_mod, "borrow_login", lambda cdp, url: (
        {"cookies": [], "origins": []},
        {"host": "h", "origin": "https://short.example.com", "cookies": 0, "local_storage": "no_tab"}))
    launched.new_context.return_value.new_page.return_value.url = "http://127.0.0.1:9222/json/list"
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with pytest.raises(DevToolsEndpointError):
            with session_mod.plain_session("https://short.example.com/x", cdp_url="http://127.0.0.1:9222"):
                pass
    launched.new_context.return_value.close.assert_called_once()
    launched.close.assert_called_once()


def test_a_name_that_resolves_to_the_devtools_port_is_caught_by_the_address_served_from(monkeypatch):
    sync_pw, _, launched, _ = _fake_playwright()
    monkeypatch.setattr(session_mod, "borrow_login", lambda cdp, url: (
        {"cookies": [], "origins": []},
        {"host": "h", "origin": "http://alias.example.test", "cookies": 0, "local_storage": "no_tab"}))
    page = launched.new_context.return_value.new_page.return_value
    page.url = "http://alias.example.test:9222/json"  # a DNS alias for loopback
    page.goto.return_value.server_addr.return_value = {"ipAddress": "127.0.0.1", "port": 9222}
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with pytest.raises(DevToolsEndpointError):
            with session_mod.plain_session("http://alias.example.test:9222/json", cdp_url="http://localhost:9222"):
                pass
    launched.close.assert_called_once()


# -- launching ------------------------------------------------------------------------

def test_the_chromium_sandbox_can_be_turned_off_for_a_container(monkeypatch):
    monkeypatch.setenv(session_mod.NO_SANDBOX_ENV, "1")
    sync_pw, pw, _, _ = _fake_playwright()
    _run(sync_pw)
    assert pw.chromium.launch.call_args.kwargs["chromium_sandbox"] is False


def test_a_launch_that_fails_for_the_sandbox_says_how_to_turn_it_off():
    sync_pw, pw, _, _ = _fake_playwright()
    pw.chromium.launch.side_effect = RuntimeError("FATAL: No usable sandbox!")
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with pytest.raises(RuntimeError, match="THESEUS_NO_SANDBOX=1"):
            with session_mod.plain_session("https://example.com/start"):
                pass


def test_an_unrelated_launch_failure_is_not_dressed_up():
    sync_pw, pw, _, _ = _fake_playwright()
    pw.chromium.launch.side_effect = RuntimeError("Executable doesn't exist")
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with pytest.raises(RuntimeError) as err:
            with session_mod.plain_session("https://example.com/start"):
                pass
    assert "THESEUS_NO_SANDBOX" not in str(err.value)


def test_the_write_block_adds_its_browser_flags_to_the_launch():
    sync_pw, pw, _, _ = _fake_playwright()
    block = WriteBlock()
    block.install = MagicMock()
    _run(sync_pw, write_block=block)
    assert pw.chromium.launch.call_args.kwargs["args"] == list(LAUNCH_ARGS)


def test_no_flags_without_an_active_write_block():
    sync_pw, pw, _, _ = _fake_playwright()
    _run(sync_pw, write_block=WriteBlock(WritePolicy(block=False)))
    assert "args" not in pw.chromium.launch.call_args.kwargs


# -- closing ----------------------------------------------------------------------------

def test_a_browser_that_is_already_gone_does_not_make_closing_fail():
    from theseus.explorer.write_block import WriteBlock

    sync_pw, _, launched, _ = _fake_playwright()
    launched.new_context.return_value.close.side_effect = Exception(
        "BrowserContext.close: Target page, context or browser has been closed")
    block = WriteBlock(WritePolicy(block=False))
    block.close = MagicMock()
    _run(sync_pw, write_block=block)  # no exception
    launched.close.assert_called_once()
    block.close.assert_called_once()


def test_a_real_close_failure_still_closes_the_browser_and_the_write_block():
    sync_pw, _, launched, _ = _fake_playwright()
    launched.new_context.return_value.close.side_effect = RuntimeError("disk full")
    block = WriteBlock(WritePolicy(block=False))
    block.close = MagicMock()
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with pytest.raises(RuntimeError, match="disk full"):
            with session_mod.plain_session("https://example.com/start", write_block=block):
                pass
    launched.close.assert_called_once()
    block.close.assert_called_once()


# -- write block --------------------------------------------------------------

def test_the_write_block_is_installed_on_theseus_browser():
    from theseus.explorer.write_block import WriteBlock

    sync_pw, _, launched, _ = _fake_playwright()
    block = WriteBlock()
    block.install = MagicMock()
    s = _run(sync_pw, write_block=block)
    assert launched.new_context.call_args.kwargs["service_workers"] == "block"
    block.install.assert_called_once_with(launched, launched.new_context.return_value)
    assert s.write_block is block


def test_writes_allowed_leaves_service_workers_alone():
    from theseus.explorer.write_block import WriteBlock, WritePolicy

    sync_pw, _, launched, _ = _fake_playwright()
    _run(sync_pw, write_block=WriteBlock(WritePolicy(block=False)))
    assert "service_workers" not in launched.new_context.call_args.kwargs
    launched.new_context.return_value.route.assert_not_called()


def test_tabs_opened_from_the_first_load_on_are_collected():
    sync_pw, _, launched, _ = _fake_playwright()
    context = launched.new_context.return_value
    listeners = []
    context.on.side_effect = lambda event, handler: listeners.append(handler) if event == "page" else None
    popup = MagicMock(name="popup")
    context.new_page.return_value.goto.side_effect = lambda *a, **k: [h(popup) for h in listeners]
    s = _run(sync_pw)
    assert popup in s.opened_tabs  # opened while the first page loaded



@pytest.mark.parametrize("value, on", [(None, False), ("", False), ("0", False), ("1", True), ("yes", True)])
def test_trace_from_env(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv(session_mod.TRACE_ENV, raising=False)
    else:
        monkeypatch.setenv(session_mod.TRACE_ENV, value)
    assert session_mod.trace_from_env() is on


def test_a_chrome_that_went_away_does_not_fail_a_later_sign_in_follow():
    from theseus.explorer.borrow import BorrowError

    def script(page, url, borrowed):
        yield url
        yield "https://idp.example.net/login"

    sync_pw, page, calls, borrowed, borrow = _scripted_session(script)
    state = {"ok": True}

    def flaky_borrow(cdp, url):
        if not state["ok"]:
            raise BorrowError("could not reach Chrome")
        return borrow(cdp, url)

    with patch("playwright.sync_api.sync_playwright", sync_pw):
        import theseus.explorer.session as mod

        original = mod.borrow_login
        mod.borrow_login = flaky_borrow
        try:
            with mod.plain_session("https://app.example.com/", cdp_url="http://127.0.0.1:9222") as s:
                state["ok"] = False  # the user closed their Chrome
                assert s.follow_sign_in("https://app.example.com/") == []  # no error: the page stays where it is
        finally:
            mod.borrow_login = original


# -- settling: slow loads and refreshes -----------------------------------------------------------------------------

class SettlePage:
    """Just enough page for _settle: load results in order, a meta refresh, a url."""

    def __init__(self, loads=(), meta=None, url="https://app.example.com/", navigate_on_wait=None):
        self.loads = list(loads)       # True = loaded, False = the load wait timed out
        self.meta, self.url = meta, url
        self.waits, self.load_calls = [], 0
        self.navigate_on_wait = navigate_on_wait  # (wait_ms, callback): a redirect that fires during that wait
        self.navigations = None

    def wait_for_load_state(self, state, timeout=None):
        if state == "load":
            self.load_calls += 1
            if self.loads and not self.loads.pop(0):
                raise TimeoutError("Timeout 4000ms exceeded.")

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        if self.navigate_on_wait and ms == self.navigate_on_wait[0]:
            self.navigate_on_wait[1](self)

    def evaluate(self, script):
        return self.meta


def settle(page, header=None):
    navigations = []
    page.navigations = navigations
    session_mod._settle(page, navigations, refresh_header=lambda: header)
    return navigations


@pytest.mark.parametrize("value, parsed", [
    ("1;url=https://idp.example.net/login", (1.0, "https://idp.example.net/login")),
    ("2; URL='/next'", (2.0, "https://app.example.com/next")),
    ("0, https://idp.example.net/", (0.0, "https://idp.example.net/")),
    ("1.5;url=\"https://idp.example.net/\"", (1.5, "https://idp.example.net/")),
    ("5", None),                 # no URL: it reloads the same page
    ("5;", None), ("", None), (None, None), (7, None), ("soon;url=x", None),
])
def test_refresh_values(value, parsed):
    assert session_mod._parse_refresh(value, "https://app.example.com/start") == parsed


def test_a_page_that_is_still_loading_is_not_settled():
    page = SettlePage(loads=[False, False, True])  # two load waits time out, then it loads
    settle(page)
    assert page.load_calls == 3 and page.waits.count(session_mod.SETTLE_QUIET_MS) == 3


def test_a_page_that_never_finishes_loading_is_waited_for_only_so_long():
    page = SettlePage(loads=[False] * 50)
    settle(page)
    assert page.load_calls == session_mod.SETTLE_ROUNDS


def test_a_redirect_to_another_origin_by_meta_refresh_is_waited_for():
    page = SettlePage(meta="1;url=https://idp.example.net/login",
                      navigate_on_wait=(1500, lambda p: p.navigations.append("https://idp.example.net/login")))
    navigations = settle(page)
    assert page.waits.count(1500) == 1           # (1 s + half a second), once
    assert navigations == ["https://idp.example.net/login"]
    assert page.load_calls == 2                  # and the page it went to was settled too


def test_a_refresh_header_is_waited_for_too():
    page = SettlePage()
    settle(page, header="2; url=https://idp.example.net/")
    assert 2500 in page.waits


@pytest.mark.parametrize("meta", [
    "5",                                          # reloads the same page
    "1;url=/home",                                # stays on the origin
    "30;url=https://idp.example.net/",            # too late to wait for
    "nonsense", None,
])
def test_refreshes_that_do_not_leave_the_origin_or_come_too_late_are_not_waited_for(meta):
    page = SettlePage(meta=meta)
    settle(page)
    assert page.waits == [session_mod.SETTLE_QUIET_MS]


def test_a_page_that_cannot_be_asked_about_its_refresh_is_just_settled():
    page = SettlePage()
    page.evaluate = lambda script: (_ for _ in ()).throw(RuntimeError("Execution context was destroyed"))
    settle(page)
    assert page.waits == [session_mod.SETTLE_QUIET_MS]


def test_the_main_documents_refresh_header_is_remembered_by_the_session(monkeypatch):
    """The session's own settle() reads the header of the main document's response, and no other."""
    sync_pw, _, launched, _ = _fake_playwright()
    page = launched.new_context.return_value.new_page.return_value
    page.url = "https://app.example.com/"
    page.evaluate.return_value = None          # no meta refresh
    handlers, waits = {}, []
    page.on.side_effect = lambda event, handler: handlers.__setitem__(event, handler)
    page.wait_for_timeout.side_effect = waits.append
    kept = {}
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with session_mod.plain_session("https://app.example.com/") as s:
            document = MagicMock()
            document.request.is_navigation_request.return_value = True
            document.frame = page.main_frame
            document.headers = {"refresh": "1; url=https://idp.example.net/"}
            handlers["response"](document)
            asset = MagicMock()
            asset.request.is_navigation_request.return_value = False
            asset.headers = {"refresh": "9; url=https://ads.example.net/"}
            handlers["response"](asset)
            del waits[:]
            s.settle()
            kept["waits"] = list(waits)
    assert 1500 in kept["waits"] and 9500 not in kept["waits"]


# -- the hop limit -------------------------------------------------------------------------------------------------------------

def _endless_chain(page, url, borrowed):
    """Every login borrowed unlocks one more origin in the chain."""
    yield url
    depth = 1 + sum(1 for name in borrowed if name.startswith("sid-https://site"))
    for k in range(1, depth + 1):
        yield f"https://site{k}.example.com/"


def test_origins_left_unborrowed_by_the_hop_limit_are_listed(monkeypatch):
    sync_pw, page, calls, _, borrow = _scripted_session(_endless_chain)
    monkeypatch.setattr(session_mod, "borrow_login", borrow)
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with session_mod.plain_session("https://site0.example.com/", cdp_url="http://127.0.0.1:9222") as s:
            assert len(s.unfollowed) == 1 and s.unfollowed[0].startswith("https://site")
            assert s.unfollowed[0] not in [i["origin"] for i in s.borrowed_login]


def test_a_chain_within_the_limit_leaves_nothing_unfollowed(monkeypatch):
    sync_pw, page, calls, borrowed, borrow = _scripted_session(_sso)
    monkeypatch.setattr(session_mod, "borrow_login", borrow)
    with patch("playwright.sync_api.sync_playwright", sync_pw):
        with session_mod.plain_session("https://app.example.com/", cdp_url="http://127.0.0.1:9222") as s:
            assert s.unfollowed == []


@pytest.mark.parametrize("value, on", [("on", True), ("y", True), ("enabled", True), ("off", False), ("2", False), ("", False)])
def test_the_trace_and_sandbox_switches_read_the_same_words(monkeypatch, value, on):
    monkeypatch.setenv(session_mod.TRACE_ENV, value)
    assert session_mod.trace_from_env() is on
    sync_pw, pw, _, _ = _fake_playwright()
    monkeypatch.setenv(session_mod.NO_SANDBOX_ENV, value)
    _run(sync_pw)
    assert pw.chromium.launch.call_args.kwargs["chromium_sandbox"] is (not on)  # unreadable: the sandbox stays on


def test_a_devtools_address_not_on_this_machine_stops_the_session_before_any_browser_starts(monkeypatch):
    from theseus.explorer import borrow

    monkeypatch.setattr(session_mod, "borrow_login", borrow.borrow_login)  # the real one: it must refuse
    monkeypatch.setattr(borrow, "_get_json", lambda *a, **k: pytest.fail("Chrome was asked"))
    sync_pw, pw, _, _ = _fake_playwright()
    with pytest.raises(borrow.BorrowError, match="THESEUS_CDP_URL must be on this machine"):
        _run(sync_pw, cdp_url="http://chrome-box.example.com:9222")
    pw.chromium.launch.assert_not_called()
