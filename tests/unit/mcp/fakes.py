"""Shared fakes for the MCP tests: a page and a browser-session factory."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock


def make_page(*, url="https://example.com/start", threads=None, click_goes_to=None, redirects=None):
    """A page the explorer tools can drive through their ``_theseus_*`` hooks.

    ``threads`` collects the thread id of every hook call when given.
    ``click_goes_to`` maps a click target to the URL the click navigates to.
    ``goto`` updates ``page.url`` like a real navigation; ``redirects`` maps a
    URL it is asked for to the URL it ends up on (a site's redirect).
    """
    page = MagicMock(name="page")
    page.url = url
    page.title.return_value = "Example"
    page._theseus_visible_buttons = ["Place order"]
    page._theseus_visible_input_labels = []
    page._theseus_visible_headings = ["Order desk"]
    page._theseus_modal_text = None
    click_goes_to = dict(click_goes_to or {})

    def seen():
        if threads is not None:
            threads.add(threading.get_ident())

    def goto(target, **kwargs):
        seen()
        page.url = redirects.get(target, target) if redirects else target

    def click(target):
        seen()
        if target in click_goes_to:
            page.url = click_goes_to[target]
        return {"success": True, "fallback_used": False, "selector": f"text={target}"}

    def click_index(index):
        seen()
        return {"success": True, "selector": f'[data-theseus-index="{index}"]'}

    def assert_state(description):
        seen()
        page.asserted.append(description)
        return {"matches": "Order placed" in description, "evidence": "fake"}

    page.goto.side_effect = goto
    page.asserted = []
    page._theseus_click = click
    page._theseus_click_index = click_index
    page._theseus_assert_state = assert_state
    return page


class FakeBrowser:
    """Records how often the browser session was entered and exited."""

    def __init__(self, page=None, fail_on_enter=False, landed_url=None, write_block=None,
                 on_exit=None, exit_error=None, borrowed_login=None, follow_sign_in=None, settle=None, unfollowed=None):
        self.page = page or make_page()
        self.write_block = write_block
        self.fail_on_enter = fail_on_enter
        self.landed_url = landed_url  # simulate a redirect at start
        self.on_exit = on_exit  # called while the browser closes, like pages unloading
        self.exit_error = exit_error  # raised when the browser closes
        self.borrowed_login = borrowed_login
        self.follow_sign_in = follow_sign_in  # plain_session's: reload a URL through sign-in
        self.settle = settle  # plain_session's: wait for the page to stop navigating by itself
        self.unfollowed = unfollowed if unfollowed is not None else []
        self.entered = 0
        self.exited = 0
        self.context = FakeContext()
        self.context.tabs.append(self.page)  # plain_session's list holds the first tab too

    def factory(self, config, run_dir):
        @contextmanager
        def cm():
            if self.fail_on_enter:
                raise RuntimeError("browser failed to start")
            self.entered += 1
            self.page.url = self.landed_url or config.start_url
            try:
                yield SimpleNamespace(page=self.page, trace_path=None, write_block=self.write_block,
                                      context=self.context, opened_tabs=self.context.tabs,
                                      borrowed_login=self.borrowed_login,
                                      follow_sign_in=self.follow_sign_in, settle=self.settle,
                                      unfollowed=self.unfollowed)
            finally:
                if self.on_exit is not None:
                    self.on_exit()
                self.exited += 1
                if self.exit_error is not None:
                    raise self.exit_error

        return cm()


class FakeContext:
    """Collects tabs like plain_session's ``opened_tabs``, so a test can open one."""

    def __init__(self):
        self.tabs = []

    def open_tab(self, url):
        tab = MagicMock(name="tab")
        tab.url = url
        self.tabs.append(tab)
        return tab
