"""Unit tests for theseus/explorer/borrow.py (no Chrome; DevTools calls faked)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from theseus.explorer import borrow


def _storage_reply(items):
    """A Runtime.evaluate reply for the localStorage expression."""
    return {"id": 1, "result": {"result": {"value": json.dumps({"length": len(items), "items": items})}}}


def _fake_chrome(monkeypatch, cookies, tabs, tab_replies):
    """tabs: [(url, ws)]; tab_replies: {ws: localStorage items, None for no answer}."""
    def get_json(base, path, timeout):
        if path == "/json/version":
            return {"webSocketDebuggerUrl": "ws://browser"}
        return [{"type": "page", "url": url, "webSocketDebuggerUrl": ws} for url, ws in tabs]

    asked = []

    def call(ws_url, method, params, timeout):
        asked.append(ws_url)
        if ws_url == "ws://browser":
            assert method == "Storage.getCookies"
            return {"id": 1, "result": {"cookies": cookies}}
        items = tab_replies.get(ws_url)
        if items is None:
            return None
        return _storage_reply(items)

    monkeypatch.setattr(borrow, "_get_json", get_json)
    monkeypatch.setattr(borrow, "_call", call)
    return asked


def test_only_the_cookies_chrome_would_send_are_borrowed(monkeypatch):
    cookies = [{"name": n, "value": "v", "domain": d, "path": "/"} for n, d in [
        ("host", "app.example.com"), ("parent", ".example.com"), ("domain-self", ".app.example.com"),
        ("sub-host-only", "api.app.example.com"), ("sub-domain", ".api.app.example.com"),
        ("sibling", "mail.example.com"), ("other", "bank.example.org"), ("suffix", ".notexample.com")]]
    _fake_chrome(monkeypatch, cookies, [], {})
    state, info = borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/start")
    assert [c["name"] for c in state["cookies"]] == ["host", "parent", "domain-self"]
    assert info == {"host": "app.example.com", "origin": "https://app.example.com", "cookies": 3,
                    "local_storage": "no_tab", "local_storage_items": 0}


@pytest.mark.parametrize("url,cookie_domain", [
    ("http://[::1]:8000/", "[::1]"),
    ("http://[0:0:0:0:0:0:0:1]:8000/", "[::1]"),  # Chrome writes the compressed form
    ("http://[2001:DB8::1]/", "[2001:db8::1]"),
    ("http://münchen.de/", "xn--mnchen-3ya.de"),  # Chrome writes an IDN as punycode
    ("http://MÜNCHEN.de/", ".xn--mnchen-3ya.de"),
    ("http://127.0.0.1:3000/", "127.0.0.1"),
])
def test_cookie_domains_match_the_host_in_the_form_chrome_writes_them(monkeypatch, url, cookie_domain):
    _fake_chrome(monkeypatch, [{"name": "sid", "value": "v", "domain": cookie_domain, "path": "/"}], [], {})
    state, info = borrow.borrow_login("http://127.0.0.1:9222", url)
    assert [c["name"] for c in state["cookies"]] == ["sid"]
    assert state["cookies"][0]["domain"] == cookie_domain  # handed to Playwright as Chrome wrote it


def test_a_cookie_of_another_ip_literal_is_not_borrowed(monkeypatch):
    _fake_chrome(monkeypatch, [{"name": "sid", "value": "v", "domain": "[::2]", "path": "/"}], [], {})
    state, _ = borrow.borrow_login("http://127.0.0.1:9222", "http://[::1]:8000/")
    assert state["cookies"] == []


def test_cookies_are_converted_for_playwright(monkeypatch):
    _fake_chrome(monkeypatch, [{"name": "sid", "value": "s", "domain": "app.example.com", "path": "/",
                                "expires": 123.0, "httpOnly": True, "secure": True, "session": False},
                               {"name": "tmp", "value": "t", "domain": "app.example.com", "path": "/",
                                "session": True, "sameSite": "Strict"}], [], {})
    state, _ = borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")
    assert state["cookies"][0] == {"name": "sid", "value": "s", "domain": "app.example.com", "path": "/",
                                   "expires": 123.0, "httpOnly": True, "secure": True, "sameSite": "Lax"}
    assert state["cookies"][1]["expires"] == -1 and state["cookies"][1]["sameSite"] == "Strict"


def test_local_storage_comes_from_an_open_tab_on_the_exact_origin(monkeypatch):
    tabs = [("https://bank.example.org/", "ws://bank"), ("http://app.example.com/", "ws://http"),
            ("https://app.example.com/inbox", "ws://app")]
    asked = _fake_chrome(monkeypatch, [], tabs, {"ws://app": [["authToken", "abc"]]})
    state, info = borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")
    assert state["origins"] == [{"origin": "https://app.example.com",
                                 "localStorage": [{"name": "authToken", "value": "abc"}]}]
    assert info["local_storage"] == "copied" and info["local_storage_items"] == 1
    assert "ws://bank" not in asked and "ws://http" not in asked  # other origins are never read


def test_every_local_storage_item_is_copied_whatever_its_key_is_called(monkeypatch):
    names = ["key", "length", "getItem", "setItem", "removeItem", "clear", "constructor", "toString"]
    _fake_chrome(monkeypatch, [], [("https://app.example.com/", "ws://app")],
                 {"ws://app": [[n, f"v-{n}"] for n in names]})
    state, info = borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")
    assert [i["name"] for i in state["origins"][0]["localStorage"]] == names
    assert info["local_storage"] == "copied" and info["local_storage_items"] == len(names)


def test_the_localstorage_expression_reads_through_the_storage_api():
    js = borrow._LOCAL_STORAGE_JS
    # Called on the prototype with the stored object as `this`: an item named "key" can't shadow it.
    assert "P.key.call(s, i)" in js and "P.getItem.call(s, k)" in js
    assert "Object.getOwnPropertyDescriptor(P, 'length').get.call(s)" in js
    assert "Object.entries" not in js


def test_a_copy_shorter_than_the_storage_is_reported_as_partial():
    reply = {"id": 1, "result": {"result": {"value": json.dumps({"length": 3, "items": [["a", "1"]]})}}}
    items, status = borrow._read_local_storage(reply)
    assert items == [{"name": "a", "value": "1"}] and status == "partial"


def test_a_page_that_will_not_give_up_its_storage_is_reported_as_unreadable():
    reply = {"id": 1, "result": {"exceptionDetails": {"text": "SecurityError"}, "result": {"type": "object"}}}
    assert borrow._read_local_storage(reply) == ([], "unreadable")


def test_a_tab_that_does_not_answer_is_skipped_as_busy(monkeypatch):
    _fake_chrome(monkeypatch, [], [("https://app.example.com/", "ws://app")], {"ws://app": None})
    state, info = borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")
    assert state["origins"] == [] and info["local_storage"] == "busy"


def test_an_open_tab_with_empty_local_storage(monkeypatch):
    _fake_chrome(monkeypatch, [], [("https://app.example.com/", "ws://app")], {"ws://app": []})
    _, info = borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")
    assert info["local_storage"] == "empty"


def test_an_unreachable_chrome_is_a_clear_error(monkeypatch):
    def unreachable(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(borrow._DIRECT, "open", unreachable)
    with pytest.raises(borrow.BorrowError, match="could not reach Chrome"):
        borrow.borrow_login("http://127.0.0.1:1", "https://app.example.com/")


def test_a_websocket_failure_reading_cookies_is_a_borrow_error(monkeypatch):
    monkeypatch.setattr(borrow, "_get_json", lambda base, path, timeout: {"webSocketDebuggerUrl": "ws://browser"})

    def refused(*a, **k):
        raise ConnectionRefusedError("[Errno 61] Connection refused")

    monkeypatch.setattr(borrow, "_call", refused)
    with pytest.raises(borrow.BorrowError, match="could not read cookies"):
        borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")


def test_a_websocket_failure_on_a_tab_is_reported_as_busy(monkeypatch):
    _fake_chrome(monkeypatch, [], [("https://app.example.com/", "ws://app")], {})
    real_call = borrow._call

    def call(ws_url, *a, **k):
        if ws_url == "ws://app":
            raise ConnectionResetError("tab went away")
        return real_call(ws_url, *a, **k)

    monkeypatch.setattr(borrow, "_call", call)
    _, info = borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")
    assert info["local_storage"] == "busy"


# -- the connection to Chrome ----------------------------------------------------------

@pytest.mark.parametrize("given,base", [
    ("http://127.0.0.1:9222", "http://127.0.0.1:9222"),
    ("http://localhost:9222/", "http://localhost:9222"),
    ("ws://127.0.0.1:9222/devtools/browser/abc-123", "http://127.0.0.1:9222"),
    ("wss://localhost:9222/devtools/browser/x", "https://localhost:9222"),
    ("http://[::1]:9222", "http://[::1]:9222"),
])
def test_the_devtools_url_may_be_http_or_websocket(given, base):
    assert borrow._http_base(given) == base


@pytest.mark.parametrize("bad", ["", "9222", "ftp://127.0.0.1:9222", "http://"])
def test_a_devtools_url_that_is_not_http_or_websocket_is_rejected(bad):
    with pytest.raises(borrow.BorrowError, match="THESEUS_CDP_URL"):
        borrow._http_base(bad)


def test_devtools_requests_ignore_a_proxy_in_the_environment(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"webSocketDebuggerUrl": "ws://direct"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:1")  # nothing listens there
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        assert borrow._get_json(f"http://127.0.0.1:{server.server_port}", "/json/version", 5) == {
            "webSocketDebuggerUrl": "ws://direct"}
    finally:
        server.shutdown()


def test_the_websocket_connection_is_direct_where_the_library_can_proxy(monkeypatch):
    import websockets.sync.client as ws_client

    seen = {}

    def connect(uri, *, proxy="env", max_size=1, open_timeout=1):  # websockets 15's shape
        seen.update(proxy=proxy, max_size=max_size, open_timeout=open_timeout)
        return "connection"

    monkeypatch.setattr(ws_client, "connect", connect)
    assert borrow._connect("ws://127.0.0.1:9222/x", 5) == "connection"
    assert seen == {"proxy": None, "max_size": None, "open_timeout": 5}


def test_the_websocket_connection_works_where_the_library_has_no_proxy_option(monkeypatch):
    import websockets.sync.client as ws_client

    seen = {}

    def connect(uri, *, max_size=1, open_timeout=1):  # websockets 12-14
        seen.update(max_size=max_size, open_timeout=open_timeout)
        return "connection"

    monkeypatch.setattr(ws_client, "connect", connect)
    assert borrow._connect("ws://127.0.0.1:9222/x", 5) == "connection"
    assert seen == {"max_size": None, "open_timeout": 5}


# -- the DevTools endpoint is never a place to browse ----------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9222/json/list",
    "http://localhost:9222/json/close/ABC",
    "http://LOCALHOST:9222/devtools/inspector.html",
    "http://foo.localhost:9222/",
    "http://127.1:9222/json",
    "http://2130706433:9222/json",
    "http://0x7f.0.0.1:9222/json",
    "http://0.0.0.0:9222/json",
    "http://[::1]:9222/json",
    "http://[::ffff:127.0.0.1]:9222/json",
    "http://127.0.0.2:9222/json",
    "ws://127.0.0.1:9222/devtools/page/1",
])
def test_every_loopback_spelling_of_the_devtools_endpoint_is_recognised(url):
    assert borrow.is_devtools_endpoint(url, "http://127.0.0.1:9222") is True


@pytest.mark.parametrize("url", [
    "http://localhost:3000/",           # another port on the same host
    "http://localhost/",                # port 80
    "https://app.example.com:9222/",    # the same port on another host
    "http://127.0.0.1:9223/json",
    "not a url",
    "",
])
def test_other_urls_are_not_the_devtools_endpoint(url):
    assert borrow.is_devtools_endpoint(url, "http://127.0.0.1:9222") is False


def test_an_address_that_is_not_on_this_machine_is_no_devtools_endpoint_because_none_can_be_used():
    cdp = "http://chrome-box.example.com:9222"  # refused by _http_base, so no session ever has it
    assert borrow.is_devtools_endpoint("http://chrome-box.example.com:9222/json", cdp) is False


def test_the_devtools_endpoint_may_be_given_as_a_websocket_url():
    cdp = "ws://localhost:9222/devtools/browser/abc"
    assert borrow.is_devtools_endpoint("http://127.0.0.1:9222/json/list", cdp) is True


def test_a_page_served_from_the_devtools_address_is_recognised_by_its_ip():
    cdp = "http://localhost:9222"
    assert borrow.is_devtools_address({"ipAddress": "127.0.0.1", "port": 9222}, cdp) is True
    assert borrow.is_devtools_address({"ipAddress": "::1", "port": 9222}, cdp) is True
    assert borrow.is_devtools_address({"ipAddress": "127.0.0.1", "port": 3000}, cdp) is False
    assert borrow.is_devtools_address({"ipAddress": "93.184.216.34", "port": 9222}, cdp) is False
    assert borrow.is_devtools_address(None, cdp) is False
    assert borrow.is_devtools_address({}, cdp) is False


# -- more spellings of the DevTools endpoint ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost.:9222/json/list",        # a trailing dot is still localhost
    "http://LOCALHOST.:9222/",
    "http://foo.localhost.:9222/",
    "http://%6Cocalhost:9222/json",            # percent-encoded
    "http://127.0.0.1.:9222/",
    "http://[::1].:9222/",
    "http://localhost\u3002:9222/",            # an ideographic full stop
])
def test_trailing_dots_percent_escapes_and_unicode_dots_do_not_hide_the_endpoint(url):
    assert borrow.is_devtools_endpoint(url, "http://127.0.0.1:9222") is True


def test_a_trailing_dot_does_not_make_another_host_the_endpoint():
    assert borrow.is_devtools_endpoint("http://example.com.:9222/", "http://127.0.0.1:9222") is False
    assert borrow.is_devtools_endpoint("http://localhost.:9223/", "http://127.0.0.1:9222") is False


# -- partitioned cookies ------------------------------------------------------------------------------------------------

def _cookie(name, partition=None, domain="app.example.com"):
    c = {"name": name, "value": name, "domain": domain, "path": "/"}
    if partition is not None:
        c["partitionKey"] = partition
    return c


def test_a_cookie_set_by_an_embedded_frame_does_not_replace_the_first_party_cookie(monkeypatch):
    cookies = [
        _cookie("sid"),                                                    # the login
        _cookie("sid-embedded", {"topLevelSite": "https://partner.example.org", "hasCrossSiteAncestor": True}),
        _cookie("chips-first-party", {"topLevelSite": "https://example.com", "hasCrossSiteAncestor": False}),
        _cookie("chips-cross-ancestor", {"topLevelSite": "https://example.com", "hasCrossSiteAncestor": True}),
        _cookie("chips-other-site", {"topLevelSite": "https://other.test", "hasCrossSiteAncestor": False}),
        _cookie("chips-old-shape", "https://example.com"),                 # older Chrome: just a string
    ]
    _fake_chrome(monkeypatch, cookies, [], {})
    state, info = borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")
    assert [c["name"] for c in state["cookies"]] == ["sid", "chips-first-party", "chips-old-shape"]
    assert info["cookies"] == 3


# -- the sentence about what was borrowed --------------------------------------------------------------------------------------

def test_login_sentence_is_empty_when_nothing_was_asked_for():
    assert borrow.login_sentence(None) == "" and borrow.login_sentence([]) == ""


@pytest.mark.parametrize("status, reason", [
    ("empty", "your open tab had no localStorage"),
    ("busy", "your tab on it didn't answer, maybe a dialog is open"),
    ("no_tab", "no tab of yours was open on it, and localStorage can only be read from an open tab"),
    ("unreadable", "its page wouldn't give up its localStorage"),
])
def test_nothing_borrowed_says_why_per_origin(status, reason):
    sentence = borrow.login_sentence([
        {"host": "a", "origin": "https://a.example", "cookies": 0, "local_storage": status},
        {"host": "b", "origin": "https://b.example", "cookies": 0, "local_storage": "no_tab"}])
    assert f"https://a.example: no cookies, and {reason}" in sentence
    assert "https://b.example: no cookies, and no tab of yours" in sentence
    assert "starts logged out" in sentence and "sessionStorage or IndexedDB" in sentence


def test_one_origin_with_cookies_switches_to_the_per_origin_notes():
    sentence = borrow.login_sentence([
        {"host": "a", "origin": "https://a.example", "cookies": 2, "local_storage": "copied"},
        {"host": "b", "origin": "https://b.example", "cookies": 0, "local_storage": "busy"}])
    assert sentence.startswith(" Borrowed your login from your Chrome: 2 cookie(s) for https://a.example and its localStorage")
    assert "0 cookie(s) for https://b.example (no localStorage: your tab on it didn't answer" in sentence


# -- hosts the way Chrome writes them ---------------------------------------------------------------------------------

@pytest.mark.parametrize("typed, chrome", [
    ("straße.test", "xn--strae-oqa.test"),            # not strasse: the IDNA 2003 codec gets this wrong
    ("MÜNCHEN.de.", "xn--mnchen-3ya.de"),
    ("日本語.jp", "xn--wgv71a119e.jp"),
    ("ｅｘａｍｐｌｅ.com", "example.com"),                # fullwidth letters
    ("ẞ.de", "xn--zca.de"),
    ("localhost\u3002", "localhost"),                    # ideographic full stop
    ("xn--strae-oqa.test", "xn--strae-oqa.test"),     # already punycode
    ("App.Example.COM", "app.example.com"),
    ("2130706433", "127.0.0.1"), ("127.1", "127.0.0.1"), ("0x7f.0.0.1", "127.0.0.1"), ("127.0.0.1", "127.0.0.1"),
    ("[0:0:0:0:0:0:0:1]", "[::1]"), ("::1", "[::1]"),
    ("%6Cocalhost", "localhost"),
])
def test_canonical_host_is_the_form_chrome_writes(typed, chrome):
    assert borrow.canonical_host(typed) == chrome


def test_a_typed_unicode_host_finds_the_cookie_chrome_stored_under_punycode(monkeypatch):
    _fake_chrome(monkeypatch, [{"name": "sid", "value": "v", "domain": "xn--strae-oqa.test", "path": "/"}], [], {})
    state, _ = borrow.borrow_login("http://127.0.0.1:9222", "https://straße.test/")
    assert [c["name"] for c in state["cookies"]] == ["sid"]


def test_a_numeric_ipv4_start_url_finds_the_cookie_stored_for_the_dotted_address(monkeypatch):
    _fake_chrome(monkeypatch, [{"name": "sid", "value": "v", "domain": "127.0.0.1", "path": "/"}], [], {})
    state, _ = borrow.borrow_login("http://127.0.0.1:9222", "http://2130706433:3000/")
    assert [c["name"] for c in state["cookies"]] == ["sid"]


# -- which cookies a host gets -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("domain, host, sent", [
    # a domain cookie: the domain itself and its subdomains, on a dot boundary
    (".example.com", "example.com", True), (".example.com", "app.example.com", True), (".example.com", "a.b.example.com", True),
    (".example.com", "badexample.com", False), (".example.com", "notexample.com", False),
    (".example.com", "example.com.evil.net", False), (".example.com", "xexample.com", False),
    (".example.com", "example.org", False), (".app.example.com", "example.com", False),
    # a host-only cookie: that exact host, nothing under or around it
    ("example.com", "example.com", True), ("example.com", "EXAMPLE.com", True), ("example.com", "example.com.", True),
    ("example.com", "app.example.com", False), ("example.com", "badexample.com", False),
    ("app.example.com", "example.com", False), ("app.example.com", "xapp.example.com", False),
    # IP literals only match themselves
    ("127.0.0.1", "127.0.0.1", True), ("127.0.0.1", "127.0.0.2", False), ("127.0.0.1", "1127.0.0.1", False),
    ("[::1]", "[::1]", True), ("[::1]", "[::2]", False),
])
def test_a_cookie_goes_to_exactly_the_hosts_chrome_would_send_it_to(domain, host, sent):
    assert borrow._sent_to({"domain": domain}, host) is sent


# -- the DevTools endpoint must be on this machine ---------------------------------------

@pytest.mark.parametrize("host", [
    "localhost", "LOCALHOST", "localhost.", "127.0.0.1", "127.1", "127.255.255.254", "2130706433", "0x7f.0.0.1",
    "[::1]", "::1", "[0:0:0:0:0:0:0:1]", "::ffff:127.0.0.1",
])
def test_these_hosts_are_this_machine(host):
    assert borrow._is_this_machine(host) is True


@pytest.mark.parametrize("host", [
    "", "0.0.0.0", "::", "[::]", "a.localhost", "localhost.example.com", "127.0.0.1.example.com", "example.com",
    "chrome-box", "10.0.0.5", "192.168.1.20", "169.254.169.254", "8.8.8.8", "fe80::1", "::ffff:10.0.0.1", "1", "0x7f.evil",
    "127.0.0.1。evil.com",
])
def test_these_hosts_are_not(host):
    assert borrow._is_this_machine(host) is False


@pytest.mark.parametrize("given", [
    "http://chrome-box.example.com:9222", "https://chrome.example.com:9222/", "wss://chrome.example.com:9222/devtools/browser/x",
    "ws://192.168.1.20:9222/devtools/browser/x", "http://10.0.0.5:9222", "http://0.0.0.0:9222", "http://a.localhost:9222",
    "http://[::2]:9222",
])
def test_a_devtools_url_that_is_not_on_this_machine_is_refused(given):
    with pytest.raises(borrow.BorrowError, match="THESEUS_CDP_URL must be on this machine"):
        borrow._http_base(given)


def test_the_host_is_what_follows_the_at_sign():
    with pytest.raises(borrow.BorrowError, match="must be on this machine"):
        borrow._require_local("http://127.0.0.1@evil.example:9222/", "X")
    borrow._require_local("http://evil.example@127.0.0.1:9222/", "X")  # a user name there is _http_base's to refuse


@pytest.mark.parametrize("given", ["http://user:pw@127.0.0.1:9222", "http://evil.example@127.0.0.1:9222", "ws://:pw@localhost:9222/x",
                                   "http://127.0.0.1@evil.example:9222"])
def test_a_devtools_url_with_a_user_name_or_password_is_refused(given):
    with pytest.raises(borrow.BorrowError, match="user name or password"):
        borrow._http_base(given)


@pytest.mark.parametrize("given", ["http://[::1", "http://[zz]:9222", "http://127.0.0.1:99999999"])
def test_a_malformed_devtools_url_is_a_borrow_error_not_a_crash(given):
    try:
        borrow._http_base(given)
    except borrow.BorrowError:
        pass


def test_a_long_host_is_cut_in_the_message():
    with pytest.raises(borrow.BorrowError) as excinfo:
        borrow._http_base("http://" + "a" * 500 + ".example:9222")
    assert len(str(excinfo.value)) < 300 and "..." in str(excinfo.value)


def test_borrowing_from_a_chrome_that_is_not_on_this_machine_asks_nothing_of_it(monkeypatch):
    monkeypatch.setattr(borrow, "_get_json", lambda *a, **k: pytest.fail("a request was made"))
    monkeypatch.setattr(borrow, "_call", lambda *a, **k: pytest.fail("a DevTools call was made"))
    with pytest.raises(borrow.BorrowError, match="must be on this machine"):
        borrow.borrow_login("http://chrome-box.example.com:9222", "https://app.example.com/")


def test_an_http_request_to_a_devtools_address_not_on_this_machine_is_not_made(monkeypatch):
    monkeypatch.setattr(borrow._DIRECT, "open", lambda *a, **k: pytest.fail("a request was made"))
    with pytest.raises(borrow.BorrowError, match="The DevTools address must be on this machine"):
        borrow._get_json("http://chrome-box.example.com:9222", "/json/version", 5)


def test_a_websocket_to_an_address_not_on_this_machine_is_not_opened(monkeypatch):
    import websockets.sync.client as ws_client

    monkeypatch.setattr(ws_client, "connect", lambda *a, **k: pytest.fail("a connection was opened"))
    for url in ("ws://chrome-box.example.com:9222/devtools/browser/x", "wss://10.0.0.5:9222/x", "ws://0.0.0.0:9222/x"):
        with pytest.raises(borrow.BorrowError, match="A DevTools WebSocket address must be on this machine"):
            borrow._connect(url, 1)


class _FakeSocket:
    """A DevTools WebSocket: remembers what it was asked and answers from a table."""

    def __init__(self, url, answers):
        self.url, self.answers, self.sent = url, answers, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self, timeout=None):
        return json.dumps({"id": 1, **self.answers[self.url]})


def _fake_websockets(monkeypatch, answers):
    import websockets.sync.client as ws_client

    opened = []

    def connect(url, **kwargs):
        opened.append(url)
        return _FakeSocket(url, answers)

    monkeypatch.setattr(ws_client, "connect", connect)
    return opened


def test_a_reply_that_names_another_host_is_not_followed_for_the_cookies(monkeypatch):
    monkeypatch.setattr(borrow, "_get_json", lambda base, path, timeout: {
        "webSocketDebuggerUrl": "ws://attacker.example:9222/devtools/browser/x"})
    opened = _fake_websockets(monkeypatch, {})
    with pytest.raises(borrow.BorrowError, match="must be on this machine"):
        borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")
    assert opened == []


def test_a_tab_that_names_another_host_is_not_connected_to_and_the_cookies_are_still_borrowed(monkeypatch):
    local, elsewhere = "ws://127.0.0.1:9222/devtools/page/1", "ws://attacker.example:9222/devtools/page/2"

    def get_json(base, path, timeout):
        if path == "/json/version":
            return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}
        return [{"type": "page", "url": "https://app.example.com/a", "webSocketDebuggerUrl": elsewhere},
                {"type": "page", "url": "https://app.example.com/b", "webSocketDebuggerUrl": local}]

    cookie = {"name": "sid", "value": "v", "domain": "app.example.com", "path": "/"}
    opened = _fake_websockets(monkeypatch, {
        "ws://127.0.0.1:9222/devtools/browser/x": {"result": {"cookies": [cookie]}},
        local: _storage_reply([["k", "v"]]),
    })
    monkeypatch.setattr(borrow, "_get_json", get_json)
    state, info = borrow.borrow_login("http://127.0.0.1:9222", "https://app.example.com/")
    assert elsewhere not in opened and local in opened
    assert [c["name"] for c in state["cookies"]] == ["sid"] and info["local_storage"] == "copied"


def test_a_redirect_from_the_devtools_address_is_not_followed():
    seen = []

    class Target(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"webSocketDebuggerUrl": "ws://127.0.0.1:1"}')

        def log_message(self, *args):
            pass

    target = HTTPServer(("127.0.0.1", 0), Target)

    class Redirector(Target):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/stolen")
            self.end_headers()

    redirecting = HTTPServer(("127.0.0.1", 0), Redirector)
    for server in (target, redirecting):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(borrow.BorrowError, match="could not reach Chrome"):
            borrow._get_json(f"http://127.0.0.1:{redirecting.server_port}", "/json/version", 5)
        assert seen == []
    finally:
        for server in (target, redirecting):
            server.shutdown()
