"""Default browser session for Theseus: plain Playwright, no site login.

Theseus always runs its own browser: Playwright's bundled Chromium, or an
installed browser named by ``THESEUS_BROWSER_CHANNEL`` (e.g. ``chrome``), with
a fresh temporary profile and Chromium's sandbox on (``THESEUS_NO_SANDBOX=1``
turns it off, for a container that can't run it). Logging in is the user's
job; the session can start logged in in one of two ways:

- ``THESEUS_CDP_URL`` (e.g. ``http://127.0.0.1:9222``): borrow the login from a
  Chrome the user started with ``--remote-debugging-port`` and logged in to
  (``explorer.borrow``): the cookies Chrome would send to the start site and,
  from an open tab on that origin, its localStorage. If the page then moves to
  other origins by itself (an SSO redirect to a sign-in provider), each
  origin's login is borrowed in turn and the start URL is loaded again, up to
  a few times. Playwright never attaches to the user's Chrome.
- ``THESEUS_STORAGE_STATE``: a Playwright storage-state file.

Before it hands the page over, the session waits until the page has stopped
navigating by itself: until it has loaded (up to a limit), its requests have
gone quiet and no navigation has followed, and for a ``Refresh`` header or
meta refresh that points to another origin, until that fires. A redirect that
comes later than that (a timer, a slow script) can't be seen in advance; the
MCP session handles it when it happens (``McpSession._left_by_itself``).
Each hop of a sign-in chain loads the start URL again, so whatever the page
does on load happens once per hop.

Because the browser is Theseus's own, the write block
(``explorer.write_block``) intercepts every request at the browser level.

A Playwright trace (``trace.zip``) is written only with ``THESEUS_TRACE=1``:
traces record cookies and request headers, so they hold the session's login.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, ContextManager, Dict, Iterator, List, Optional, Set, Tuple
from urllib.parse import urljoin

from theseus.explorer.borrow import (
    BorrowError,
    borrow_login,
    devtools_endpoint_error,
    is_devtools_address,
    is_devtools_endpoint,
)
from theseus.explorer.origin import normalize_origin
from theseus.explorer.write_block import CAPTURED_WRITES_FILE, WriteBlock, WritePolicy, env_flag

TRACE_ENV = "THESEUS_TRACE"
CDP_URL_ENV = "THESEUS_CDP_URL"
STORAGE_STATE_ENV = "THESEUS_STORAGE_STATE"
BROWSER_CHANNEL_ENV = "THESEUS_BROWSER_CHANNEL"
NO_SANDBOX_ENV = "THESEUS_NO_SANDBOX"

DEFAULT_VIEWPORT = {"width": 1600, "height": 1000}

# Playwright's own default is 30 s, which turns every missed locator into a
# long stall for the agent.
ACTION_TIMEOUT_MS = 8000

# Waiting for the page to stop navigating by itself: up to this many rounds of
# (load event, network idle when a login is being borrowed, then a short quiet
# period with no new navigation). A redirect that fires later than that is
# handled when it happens (McpSession).
SETTLE_ROUNDS = 5
SETTLE_LOAD_TIMEOUT_MS = 4000
SETTLE_IDLE_TIMEOUT_MS = 2000
SETTLE_QUIET_MS = 400
# A Refresh header or meta refresh to another origin is waited for if it fires within this long.
SETTLE_MAX_REFRESH_SEC = 10

# Origins a sign-in chain may add to the borrowed login, one reload each.
MAX_LOGIN_HOPS = 4


def trace_from_env() -> bool:
    return env_flag(TRACE_ENV)


def cdp_url_from_env() -> Optional[str]:
    return os.environ.get(CDP_URL_ENV, "").strip() or None


def browser_channel_from_env() -> Optional[str]:
    return os.environ.get(BROWSER_CHANNEL_ENV, "").strip() or None


def storage_state_from_env() -> Optional[str]:
    """The configured storage-state file, if set and present."""
    raw = os.environ.get(STORAGE_STATE_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return str(path) if path.is_file() else None


# Sets a borrowed origin's localStorage before the page's own scripts run, in that
# origin's frames only: once per tab (a marker in sessionStorage), overwriting
# what the page wrote on its anonymous first load, and not again on later loads,
# so a key the site removes on purpose (a logout) stays removed.
_LOCAL_STORAGE_INIT = """(() => {{
  const [origin, items] = {payload};
  if (location.origin !== origin) return;
  try {{
    if (sessionStorage.getItem('__theseus_local_storage') === origin) return;
    sessionStorage.setItem('__theseus_local_storage', origin);
  }} catch (e) {{ return; }}
  for (const [k, v] of items) {{
    try {{ localStorage.setItem(k, v); }} catch (e) {{}}
  }}
}})();"""


def _apply_login(context: Any, state: Dict[str, Any]) -> bool:
    """Add a borrowed login to a running context; True if anything was added."""
    added = False
    if state.get("cookies"):
        context.add_cookies(state["cookies"])
        added = True
    for entry in state.get("origins", []):
        items = [[i["name"], i["value"]] for i in entry["localStorage"]]
        if items:
            context.add_init_script(_LOCAL_STORAGE_INIT.format(payload=json.dumps([entry["origin"], items])))
            added = True
    return added


_REFRESH = re.compile(r"""\s*([0-9]+(?:\.[0-9]*)?)\s*[;,]\s*(?:url\s*=\s*)?['"]?([^'"]*)['"]?\s*$""", re.I)


def _parse_refresh(value: Any, base_url: str) -> Optional[Tuple[float, str]]:
    """(seconds, absolute URL) of a ``Refresh`` header or a meta refresh's content,
    or None: no value, or a refresh with no URL, which only reloads the page."""
    if not isinstance(value, str):
        return None
    found = _REFRESH.match(value)
    if not found or not found.group(2).strip():
        return None
    return float(found.group(1)), urljoin(base_url, found.group(2).strip())


def _pending_refresh(page: Any, header: Optional[str]) -> Optional[float]:
    """Seconds until the page redirects itself to another origin by ``Refresh``
    header or meta refresh, if that is soon enough to wait for."""
    from theseus.explorer.origin import normalize_origin as origin_of

    try:
        meta = page.evaluate("() => { const m = document.querySelector('meta[http-equiv=\"refresh\" i]'); "
                             "return m ? m.getAttribute('content') : null; }")
        here = page.url
    except Exception:  # the page is changing under us: the navigation event tells
        return None
    for value in (header, meta):
        parsed = _parse_refresh(value, here)
        if parsed and parsed[0] <= SETTLE_MAX_REFRESH_SEC and origin_of(parsed[1]) not in (None, origin_of(here)):
            return parsed[0]
    return None


def _settle(page: Any, navigations: List[str], quiet_network: bool = False,
            refresh_header: Callable[[], Optional[str]] = lambda: None) -> None:
    """Wait until the page has stopped navigating by itself.

    Each round waits for the load event, then (with ``quiet_network``) for the
    page's requests to finish: a script that checks the session with a request
    and then redirects shows up as one. A round that saw a navigation, or in
    which the page was still loading when the wait ran out, is followed by
    another. Last, once, a ``Refresh`` header or meta refresh to another origin
    is waited for. At most SETTLE_ROUNDS rounds."""
    waited_for_refresh = False
    for _ in range(SETTLE_ROUNDS):
        seen = len(navigations)
        loaded = True
        try:
            page.wait_for_load_state("load", timeout=SETTLE_LOAD_TIMEOUT_MS)
        except Exception:
            loaded = False  # a slow page: its load handler may still redirect
        if quiet_network:
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_IDLE_TIMEOUT_MS)
            except Exception:
                pass
        page.wait_for_timeout(SETTLE_QUIET_MS)
        if len(navigations) != seen or not loaded:
            continue
        if not waited_for_refresh:
            delay = _pending_refresh(page, refresh_header())
            if delay is not None:
                waited_for_refresh = True
                page.wait_for_timeout(int((delay + 0.5) * 1000))
                continue
        return


def _already_closed(exc: BaseException) -> bool:
    return "has been closed" in str(exc) or type(exc).__name__ == "TargetClosedError"


def _with_sandbox_hint(exc: BaseException, sandbox_on: bool) -> BaseException:
    """The exception, with a way out when Chromium's own sandbox may be why the
    browser won't start. Nothing is added once the user has opted out."""
    text = str(exc).lower()
    if sandbox_on and ("sandbox" in text or "has been closed" in text or type(exc).__name__ == "TargetClosedError"):
        return RuntimeError(f"{exc}\nIf Chromium's sandbox can't start here (in a container, or under another "
                            f"sandbox), set {NO_SANDBOX_ENV}=1.")
    return exc


def _close(target: Any) -> None:
    """Close a browser or context; one that is already gone is fine."""
    try:
        target.close()
    except Exception as exc:
        if not _already_closed(exc):
            raise


@contextmanager
def plain_session(
    starting_url: str,
    *,
    headed: bool = False,
    trace_dir: Optional[Path] = None,
    cdp_url: Optional[str] = None,
    storage_state: Optional[str] = None,
    channel: Optional[str] = None,
    write_block: Optional[WriteBlock] = None,
) -> Iterator[SimpleNamespace]:
    """Launch Theseus's own browser and open ``starting_url``.

    Yields ``SimpleNamespace(page, context, write_block, trace_path,
    borrowed_login, opened_tabs, follow_sign_in)``. ``borrowed_login`` lists what
    was copied from the user's Chrome, origin by origin; ``opened_tabs`` collects
    tabs the site opens, from the first page load on; ``follow_sign_in(url)``
    loads a URL again the way the start URL was loaded, borrowing the login of
    any new sign-in origin the page reaches, for a page that moves to one later;
    ``settle()`` waits for the page to stop navigating by itself; ``unfollowed`` lists
    origins the last ``follow_sign_in`` reached but didn't borrow a login for, because
    the chain was longer than the limit. ``trace_dir`` writes a
    Playwright ``trace.zip`` there; callers pass it only when the user asked
    for tracing. The trace is saved and the browser closed however the
    session ends, a failed start included.
    """
    from playwright.sync_api import sync_playwright

    if cdp_url and is_devtools_endpoint(starting_url, cdp_url):
        raise devtools_endpoint_error(starting_url)

    borrowed: List[Dict[str, Any]] = []
    tried: Set[str] = set()
    options: dict = {"viewport": dict(DEFAULT_VIEWPORT)}
    if cdp_url:
        state, info = borrow_login(cdp_url, starting_url)
        options["storage_state"] = state
        borrowed.append(info)
        tried.add(info["origin"] or "")
    elif storage_state:
        options["storage_state"] = storage_state
    if write_block is not None and write_block.active:
        # A service worker could send requests the block can't attribute.
        options["service_workers"] = "block"

    with sync_playwright() as pw:
        sandbox_on = not env_flag(NO_SANDBOX_ENV)
        launch: dict = {"headless": not headed, "chromium_sandbox": sandbox_on}
        if channel:
            launch["channel"] = channel
        if write_block is not None and write_block.launch_args:
            launch["args"] = list(write_block.launch_args)
        try:
            browser = pw.chromium.launch(**launch)
        except Exception as exc:
            hinted = _with_sandbox_hint(exc, sandbox_on) if "sandbox" in str(exc).lower() else exc
            if hinted is exc:
                raise
            raise hinted from exc
        context: Any = None
        trace_path: Optional[Path] = None
        try:
            context = browser.new_context(**options)
            options.pop("storage_state", None)  # the borrowed login lives in the browser now
            state = None
            if write_block is not None:
                write_block.devtools_url = cdp_url  # no request of Theseus's browser reaches your Chrome's debug port
                write_block.install(browser, context)
            opened: list = []
            context.on("page", lambda tab: opened.append(tab))
            if trace_dir is not None:
                try:
                    context.tracing.start(screenshots=True, snapshots=True)
                    trace_path = Path(trace_dir) / "trace.zip"
                except Exception:
                    trace_path = None  # diagnostics, never a reason to abort a run

            try:
                page = context.new_page()
            except Exception as exc:
                hinted = _with_sandbox_hint(exc, sandbox_on)
                if hinted is exc:
                    raise
                raise hinted from exc
            page.set_default_timeout(ACTION_TIMEOUT_MS)
            navigations: List[str] = []
            page.on("framenavigated",
                    lambda frame: navigations.append(frame.url) if frame == page.main_frame else None)
            refresh: Dict[str, Optional[str]] = {"header": None}

            def on_response(response: Any) -> None:
                try:
                    if response.request.is_navigation_request() and response.frame == page.main_frame:
                        refresh["header"] = response.headers.get("refresh")
                except Exception:
                    pass

            page.on("response", on_response)
            # Origins the page reached but whose login was not borrowed because the hop limit was hit.
            unfollowed: List[str] = []

            def settle() -> None:
                _settle(page, navigations, quiet_network=bool(cdp_url), refresh_header=lambda: refresh["header"])

            def load(url: str) -> None:
                response = page.goto(url, wait_until="domcontentloaded")
                if cdp_url and response is not None and is_devtools_address(response.server_addr(), cdp_url):
                    raise devtools_endpoint_error(url)

            def follow_sign_in(target_url: str) -> List[Dict[str, Any]]:
                """Load ``target_url`` and follow the sign-in chain: wait until the page
                stops navigating by itself, borrow the login of each origin it passed
                through (at most MAX_LOGIN_HOPS times), and load again so the whole
                chain runs with it. Returns what was borrowed on the way."""
                got: List[Dict[str, Any]] = []
                del unfollowed[:]
                load(target_url)
                for hop in range(MAX_LOGIN_HOPS + 1):
                    settle()
                    if cdp_url and is_devtools_endpoint(page.url, cdp_url):
                        raise devtools_endpoint_error(page.url)
                    if not cdp_url:
                        break
                    fresh: Dict[str, str] = {}
                    for url in [*navigations, page.url]:
                        origin = normalize_origin(url)
                        if origin and origin not in tried and origin not in fresh:
                            fresh[origin] = url
                    if hop == MAX_LOGIN_HOPS:
                        unfollowed.extend(fresh)  # the chain is longer than the limit
                        break
                    added = False
                    for origin, url in fresh.items():
                        tried.add(origin)
                        try:
                            login, info = borrow_login(cdp_url, url)
                        except BorrowError:
                            if not borrowed:  # nothing could be borrowed at all: say why, at the start
                                raise
                            return got  # Chrome went away since the start: the page is where it is
                        borrowed.append(info)
                        got.append(info)
                        added = _apply_login(context, login) or added
                        login = None  # the borrowed login lives in the browser now
                    if not added:
                        break
                    del navigations[:]
                    load(target_url)
                return got

            follow_sign_in(starting_url)

            yield SimpleNamespace(
                page=page,
                context=context,
                write_block=write_block,
                trace_manager=None,
                trace_path=trace_path,
                borrowed_login=borrowed if cdp_url else None,
                opened_tabs=opened,
                follow_sign_in=follow_sign_in,
                settle=settle,
                unfollowed=unfollowed,
            )
        finally:
            if trace_path is not None:
                try:
                    trace_path.parent.mkdir(parents=True, exist_ok=True)
                    context.tracing.stop(path=str(trace_path))
                except Exception:
                    pass
            try:
                if context is not None:
                    _close(context)
            finally:
                try:
                    _close(browser)
                finally:
                    if write_block is not None:
                        write_block.close()


def make_session_factory(
    trace_dir: Optional[Path] = None,
) -> Callable[[str, Any], ContextManager[Any]]:
    """A runner ``SessionFactory`` configured from the environment.

    Honors ``invocation.headed``. ``trace_dir`` holds the trace (only with
    ``THESEUS_TRACE=1``) and the captured writes. The runner doesn't block
    writes unless ``THESEUS_BLOCK_WRITES=1``: blocking pauses the page's
    network while the model thinks, and a runner can have its own write gates
    (a read-only mode, say).
    """

    def factory(starting_url: str, invocation: Any) -> ContextManager[Any]:
        return plain_session(
            starting_url,
            headed=bool(getattr(invocation, "headed", False)),
            trace_dir=trace_dir if trace_from_env() else None,
            cdp_url=cdp_url_from_env(),
            storage_state=storage_state_from_env(),
            channel=browser_channel_from_env(),
            write_block=WriteBlock(
                WritePolicy.from_env(default_block=False),
                sink_path=Path(trace_dir) / CAPTURED_WRITES_FILE if trace_dir else None,
            ),
        )

    return factory
