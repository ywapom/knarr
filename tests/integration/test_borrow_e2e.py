"""End-to-end: borrowing a login from the user's Chrome, in real browsers.

Each test starts the real server over stdio against a throwaway Chrome (headless,
temporary profile, debug port) and a local site on two cookie jars: the app on
127.0.0.1 and its sign-in provider on localhost. Opt-in: THESEUS_BROWSER_TESTS=1.
"""

from __future__ import annotations

import asyncio
import json
import signal
import subprocess
from pathlib import Path

import pytest

from tests.integration.test_mcp_stdio import (  # noqa: F401  (fixture import)
    RawServer,
    _alive,
    _chrome_alive,
    _client,
    _page_targets,
    _server_env,
    _text,
    page_url,
    origins,
    pytestmark,
    throwaway_chrome,
)
from tests.integration.test_write_block import CRITERION, site  # noqa: F401  (fixture import)


def _user_cookies(endpoint, cookies):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        browser.contexts[0].add_cookies(cookies)


async def _open_and_look(env, tmp_path, url):
    async with _client(env, tmp_path) as client:
        started = _text(await client.call_tool("session_start", {"start_url": url, "success_criterion": CRITERION}))
        page = _text(await client.call_tool("read_page", {}))
        status = json.loads(_text(await client.call_tool("session_status", {})))
        after = _text(await client.call_tool("note", {"text": "still working"}))
        await client.call_tool("session_end", {})
        return started, page, status, after


# -- sign-in chains --------------------------------------------------------------------

def test_an_sso_redirect_chain_is_followed_with_the_users_login(site, tmp_path, throwaway_chrome):
    """app -> script redirect -> sign-in provider -> back to the app, signed in."""
    _user_cookies(throwaway_chrome, [{"name": "idp", "value": "1", "domain": "localhost", "path": "/"}])
    env = _server_env(tmp_path, THESEUS_CDP_URL=throwaway_chrome)

    started, page, status, _ = asyncio.run(_open_and_look(env, tmp_path, site.a + "/sso"))

    assert f"staying on {site.a}" in started and "redirected" not in started, started
    assert f"cookie(s) for {site.b}" in started, started  # the provider's login was borrowed on the way
    assert "Dashboard" in page and "Please sign in" not in page, page
    assert status["boundary_violations"] == 0 and status["origin"] == site.a
    assert not status["ended"]


def test_a_user_who_is_not_signed_in_lands_on_the_sign_in_page_without_a_lockout(site, tmp_path, throwaway_chrome):
    env = _server_env(tmp_path, THESEUS_CDP_URL=throwaway_chrome)

    started, page, status, after = asyncio.run(_open_and_look(env, tmp_path, site.a + "/sso"))

    assert f"redirected to {site.b}" in started and f"staying on {site.b}" in started, started
    assert "Nothing was borrowed from your Chrome" in started
    assert "Please sign in" in page, page
    assert status["boundary_violations"] == 0 and not status["ended"]
    assert after.startswith("noted"), after  # the session works; it is not disabled


def test_a_script_redirect_settles_before_the_origin_is_pinned_even_without_borrowing(site, tmp_path):
    started, page, status, after = asyncio.run(_open_and_look(_server_env(tmp_path), tmp_path, site.a + "/sso"))

    assert f"redirected to {site.b}" in started, started
    assert "Please sign in" in page and status["boundary_violations"] == 0
    assert after.startswith("noted")


def test_local_storage_items_named_like_storage_methods_are_all_copied(site, tmp_path, throwaway_chrome):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(throwaway_chrome)
        browser.contexts[0].new_page().goto(site.a + "/login")  # 8 items, among them "key", "length", "getItem"
    env = _server_env(tmp_path, THESEUS_CDP_URL=throwaway_chrome)

    started, page, _, _ = asyncio.run(_open_and_look(env, tmp_path, site.a + "/"))

    assert "and its localStorage" in started, started
    assert "ls items: 8" in page and "token: present" in page, page


# -- the DevTools endpoint ---------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "localhost.", "2130706433", "[::1]"])
def test_the_devtools_endpoint_is_never_opened_whatever_its_spelling(tmp_path, throwaway_chrome, host):
    port = throwaway_chrome.rsplit(":", 1)[1]
    baseline = len(_page_targets(throwaway_chrome))
    env = _server_env(tmp_path, THESEUS_CDP_URL=throwaway_chrome)

    async def scenario():
        async with _client(env, tmp_path) as client:
            return _text(await client.call_tool("session_start", {
                "start_url": f"http://{host}:{port}/json/list", "success_criterion": CRITERION}))

    started = asyncio.run(scenario())
    assert started.startswith("refused:") and "DevTools endpoint" in started, started
    assert _chrome_alive(throwaway_chrome) and len(_page_targets(throwaway_chrome)) == baseline


# -- the connection and the browser --------------------------------------------------------

def test_borrowing_ignores_a_proxy_in_the_environment(page_url, tmp_path, throwaway_chrome):
    dead = "http://127.0.0.1:1"  # nothing listens there: any use of it fails
    proxies = {name: dead for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
    env = _server_env(tmp_path, THESEUS_CDP_URL=throwaway_chrome, **proxies)

    started, page, _, _ = asyncio.run(_open_and_look(env, tmp_path, page_url))

    assert started.startswith("Session started"), started
    assert "Place order" in page


def _descendants_with(server, pattern):
    pids = server.descendants()
    found = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split()
    return pids, pids & set(map(int, found))


@pytest.mark.parametrize("no_sandbox", [False, True], ids=["sandboxed", "no_sandbox_opt_out"])
def test_theseus_browser_runs_with_chromiums_sandbox_unless_told_not_to(page_url, tmp_path, no_sandbox):
    extra = {"THESEUS_NO_SANDBOX": "1"} if no_sandbox else {}
    server = RawServer(_server_env(tmp_path, **extra), tmp_path)
    try:
        assert server.call("session_start", start_url=page_url, success_criterion=CRITERION).startswith("Session started")
        browser_pids, unsandboxed = _descendants_with(server, "no-sandbox")
        assert browser_pids
        assert bool(unsandboxed) is no_sandbox, unsandboxed
    finally:
        server.proc.send_signal(signal.SIGTERM)
        server.proc.wait(timeout=30)
    assert not _alive(browser_pids)


# -- a redirect that fires after the start has settled -------------------------------------------------------------------------

def test_a_late_redirect_to_a_sign_in_provider_is_followed_with_the_users_login(site, tmp_path, throwaway_chrome):
    """setTimeout(redirect, 1.8 s): after the start's wait, so the first calls find the page on the app."""
    _user_cookies(throwaway_chrome, [{"name": "idp", "value": "1", "domain": "localhost", "path": "/"}])
    env = _server_env(tmp_path, THESEUS_CDP_URL=throwaway_chrome)

    async def scenario():
        async with _client(env, tmp_path) as client:
            started = _text(await client.call_tool(
                "session_start", {"start_url": site.a + "/late-sso", "success_criterion": CRITERION}))
            notes = []
            for _ in range(8):
                notes.append(_text(await client.call_tool("read_page", {})))
                await asyncio.sleep(0.6)
            status = json.loads(_text(await client.call_tool("session_status", {})))
            await client.call_tool("session_end", {})
            return started, notes, status

    started, notes, status = asyncio.run(scenario())
    assert f"staying on {site.a}" in started
    assert status["boundary_violations"] == 0 and status["terminal_reason"] is None, status
    assert "Dashboard" in notes[-1] and "Please sign in" not in notes[-1], notes[-1]
    assert any("by itself" in n for n in notes), "the late redirect should have been noticed and followed"


def test_a_site_that_keeps_sending_the_page_to_sign_in_ends_the_run_as_login_required(site, tmp_path, throwaway_chrome):
    env = _server_env(tmp_path, THESEUS_CDP_URL=throwaway_chrome)  # nobody is signed in anywhere

    async def scenario():
        async with _client(env, tmp_path) as client:
            await client.call_tool("session_start", {"start_url": site.a + "/late-sso", "success_criterion": CRITERION})
            text = ""
            for _ in range(14):
                await asyncio.sleep(2.2)  # long enough for the page to be sent away again each time
                text = _text(await client.call_tool("read_page", {}))
                if "login_required" in text:
                    break
            status = json.loads(_text(await client.call_tool("session_status", {})))
            await client.call_tool("session_end", {})
            second = _text(await client.call_tool(
                "session_start", {"start_url": site.a + "/", "success_criterion": CRITERION}))
            return text, status, second

    text, status, second = asyncio.run(scenario())
    assert "run ended: login_required" in text and "Sign in there in your Chrome" in text, text
    assert status["boundary_violations"] == 0 and status["terminal_reason"] == "login_required"
    assert second.startswith("Session started"), second  # not a guard stop: the user can sign in and go on


# -- Chrome's debug port is out of reach of every page ------------------------------------------------------------------------------

def test_no_page_in_theseus_browser_can_reach_the_users_debug_port(site, tmp_path, throwaway_chrome):
    import urllib.request

    port = throwaway_chrome.rsplit(":", 1)[1]

    def new_tab():
        request = urllib.request.Request(throwaway_chrome + "/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(request, timeout=5) as r:
            return json.load(r)["id"]

    def open_tabs():
        return {t["id"] for t in _page_targets(throwaway_chrome)}

    def attack(tab_id):
        return (f"<!doctype html><title>Attack</title><h1>Attack</h1>"
                f"<img src='http://localhost.:{port}/json/close/{tab_id}'>"
                f"<script>fetch('http://localhost.:{port}/json/close/{tab_id}', {{mode: 'no-cors'}}).catch(() => {{}});</script>")

    # Control: with no debug endpoint named, nothing protects the user's Chrome, and the page does close the tab.
    control = new_tab()
    site.extra_pages["/attack-control"] = attack(control)

    async def run(path, **extra):
        env = _server_env(tmp_path, **extra)
        async with _client(env, tmp_path) as client:
            started = _text(await client.call_tool(
                "session_start", {"start_url": site.a + path, "success_criterion": CRITERION}))
            await asyncio.sleep(1.5)
            note = _text(await client.call_tool("note", {"text": "after the page"}))
            summary = json.loads(_text(await client.call_tool("session_end", {})))
            return started + "\n" + note, summary

    asyncio.run(run("/attack-control", THESEUS_ALLOW_WRITES="1"))
    assert control not in open_tabs(), "the control should show that a page can close the tab"

    victim = new_tab()
    site.extra_pages["/attack"] = attack(victim)
    note, summary = asyncio.run(run("/attack", THESEUS_CDP_URL=throwaway_chrome))
    assert victim in open_tabs(), "a page in Theseus's browser closed a tab of the user's Chrome"
    assert "DevTools endpoint" in note, note
    assert any("DevTools endpoint" in gap for gap in summary["extra"]["write_block_gaps"])
    assert any(c["outcome"] == "refused" for c in _captured_in(Path(summary["run_dir"])))


def _captured_in(run_dir):
    path = run_dir / "captured_writes.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


# -- localStorage of a sign-in origin ----------------------------------------------------------------------------------------

def test_borrowed_local_storage_replaces_what_the_anonymous_load_wrote_and_is_applied_once(site, tmp_path, throwaway_chrome):
    """The site seeds a key anonymously on its first load (redux-persist style). The borrowed value must win
    on the load after borrowing, and a later logout (the app removing the key) must not be undone."""
    from playwright.sync_api import sync_playwright

    site.extra_pages["/sso-store"] = f"<script>location.replace('{site.b}/idp-store')</script>"
    site.extra_pages["/idp-store"] = (
        "<!doctype html><title>IdP</title><h1>IdP</h1><h2 id=r>loading</h2>"
        "<button onclick=\"localStorage.removeItem('persist:root'); document.getElementById('r').textContent = 'logged out'\">Log out</button>"
        "<a href='/idp-store'>Reload</a>"
        "<script>if (!localStorage.getItem('persist:root')) localStorage.setItem('persist:root', 'ANON');"
        "document.getElementById('r').textContent = 'idp:' + localStorage.getItem('persist:root');</script>")
    with sync_playwright() as pw:  # the user's tab on the provider holds the real value
        browser = pw.chromium.connect_over_cdp(throwaway_chrome)
        tab = browser.contexts[0].new_page()
        tab.goto(site.b + "/idp-store")
        tab.evaluate("localStorage.setItem('persist:root', 'SECRET')")
    env = _server_env(tmp_path, THESEUS_CDP_URL=throwaway_chrome)

    async def scenario():
        async with _client(env, tmp_path) as client:
            started = _text(await client.call_tool(
                "session_start", {"start_url": site.a + "/sso-store", "success_criterion": CRITERION}))
            first = _text(await client.call_tool("read_page", {}))
            await client.call_tool("click", {"target": "Log out"})
            await client.call_tool("click", {"target": "Reload"})
            await asyncio.sleep(0.5)
            after_logout = _text(await client.call_tool("read_page", {}))
            await client.call_tool("session_end", {})
            return started, first, after_logout

    started, first, after_logout = asyncio.run(scenario())
    assert f"and its localStorage" in started and site.b in started, started
    assert "idp:SECRET" in first, first               # the borrowed value won over the anonymous seed
    assert "idp:ANON" in after_logout, after_logout   # and a logout was not undone by re-applying it


# -- redirects that come late or after a delay the page announced ---------------------------------------------------------------

def _timing_pages(site):
    site.extra_headers["/refresh-sso"] = {"Refresh": f"1; url={site.b}/idp-login"}
    site.extra_pages["/refresh-sso"] = "<h1>Dashboard</h1>"
    site.extra_pages["/meta-sso"] = (f"<!doctype html><meta http-equiv='refresh' content='2;url={site.b}/idp-login'>"
                                     "<h1>Dashboard</h1>")
    site.extra_pages["/slow-sso"] = (
        "<!doctype html><h1>Dashboard</h1><img src='/slow.png'>"
        f"<script>window.onload = () => {{ if (!document.cookie.includes('app=ok')) location.replace('{site.b}/idp-login'); }};</script>")


@pytest.mark.parametrize("path", ["/refresh-sso", "/meta-sso", "/slow-sso"])
def test_a_redirect_announced_by_a_refresh_or_held_back_by_a_slow_load_is_waited_for(site, tmp_path, throwaway_chrome, path):
    """Each of these used to be missed at the start and counted as a boundary violation afterwards."""
    _timing_pages(site)
    _user_cookies(throwaway_chrome, [{"name": "idp", "value": "1", "domain": "localhost", "path": "/"}])
    env = _server_env(tmp_path, THESEUS_CDP_URL=throwaway_chrome)

    started, page, status, after = asyncio.run(_open_and_look(env, tmp_path, site.a + path))

    assert f"staying on {site.a}" in started and "redirected" not in started, started
    assert f"cookie(s) for {site.b}" in started, started          # the provider's login was borrowed at the start
    assert "Dashboard" in page and "Please sign in" not in page, page
    assert status["boundary_violations"] == 0 and status["origin"] == site.a and not status["ended"], status
    assert "by itself" not in page and after.startswith("noted")


def test_a_chrome_that_is_not_on_this_machine_is_refused_before_anything_is_asked_of_it(site, tmp_path):
    env = _server_env(tmp_path, THESEUS_CDP_URL="http://chrome-box.example.com:9222")

    async def run():
        async with _client(env, tmp_path) as client:
            return _text(await client.call_tool("session_start", {"start_url": site.a + "/", "success_criterion": CRITERION}))

    refused = asyncio.run(run())
    assert "THESEUS_CDP_URL must be on this machine" in refused, refused
    assert not refused.startswith("Session started")
