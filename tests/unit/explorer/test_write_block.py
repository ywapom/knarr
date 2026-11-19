"""Unit tests for knarr/explorer/write_block.py (no browser; fake CDP events)."""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from knarr.explorer.write_block import (
    DISABLE_UNCOVERED_CHANNELS_SCRIPT,
    LAUNCH_ARGS,
    READ_ONLY_RESOURCE_TYPES,
    UNFILTERABLE_RESOURCE_TYPES,
    WRITE_RESOURCE_TYPES,
    AllowPattern,
    WriteBlock,
    WritePolicy,
    describe_body,
    describe_message,
    path_template,
    record_url,
)
from knarr.explorer import write_block as wb


class FakeCDP:
    def __init__(self, fail_on=()):
        self.sent = []
        self.fail_on = set(fail_on)

    def send(self, method, params=None):
        self.sent.append((method, params or {}))
        if method in self.fail_on:
            raise RuntimeError(f"{method}: No such request (it was cancelled)")
        return {}

    def last(self):
        return self.sent[-1]


def _block(fail_on=(), **policy):
    block = WriteBlock(WritePolicy(**policy))
    block._cdp = FakeCDP(fail_on)
    return block


def _event(method, url="https://example.com/api/orders", body=None, content_type=None,
           resource_type="Fetch", entries=None, **request_fields):
    request = {"method": method, "url": url, "headers": {}, **request_fields}
    if content_type:
        request["headers"]["Content-Type"] = content_type
    if body is not None:
        request["postData"] = body
    if entries is not None:
        request["postDataEntries"] = entries
    return {"requestId": "r1", "request": request, "resourceType": resource_type}


def _pause(block, *args, **kwargs):
    block.handle_paused(_event(*args, **kwargs))
    return block._cdp.last()


# -- routing -------------------------------------------------------------------

@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_safe_methods_continue(method):
    block = _block()
    assert _pause(block, method) == ("Fetch.continueRequest", {"requestId": "r1"})
    assert block.captured == []


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_writes_are_faked_and_recorded_without_values(method):
    block = _block()
    sent, params = _pause(block, method, url="https://example.com/api/orders?id=7&reset-token-abc",
                          body='{"qty": 2, "card": "4111"}', content_type="application/json")
    assert sent == "Fetch.fulfillRequest" and params["responseCode"] == 200
    assert base64.b64decode(params["body"]) == b"{}"
    [entry] = block.captured
    assert entry["method"] == method and entry["url"] == "https://example.com/api/orders"
    assert entry["query_keys"] == ["id"] and entry["query_unnamed"] == 1
    assert (entry["body_kind"], entry["body_fields"]) == ("application/json", ["qty", "card"])
    assert entry["outcome"] == "blocked" and "allowed" not in entry
    assert "4111" not in json.dumps(entry) and "reset-token-abc" not in json.dumps(entry)


def test_a_blocked_form_submission_shows_a_blocked_page_and_is_remembered():
    block = _block()
    _, params = _pause(block, "POST", url="https://example.com/logout?next=<b>",
                       body="a=1", content_type="application/x-www-form-urlencoded",
                       resource_type="Document")
    page = base64.b64decode(params["body"]).decode()
    assert "Blocked by Knarr" in page and "POST https://example.com/logout" in page
    assert "<b>" not in page
    assert block.captured[0]["navigation"] is True
    assert block.is_blocked_document("https://example.com/logout?x=1", lambda: "Blocked by Knarr")


def test_body_from_post_data_entries():
    block = _block()
    raw = b"user=bob&password=hunter2"
    _pause(block, "POST", content_type="application/x-www-form-urlencoded",
           entries=[{"bytes": base64.b64encode(raw).decode()}])
    entry = block.captured[0]
    assert entry["body_fields"] == ["user", "password"] and entry["body_bytes"] == len(raw)


def test_the_handler_never_raises_and_still_fakes_the_write():
    block = _block()
    deep = "[" * 200_000 + "]" * 200_000  # json.loads raises RecursionError
    sent, _ = _pause(block, "POST", body=deep, content_type="application/json")
    assert sent == "Fetch.fulfillRequest"
    assert block.captured[0]["method"] == "POST"


def test_a_failing_cdp_send_does_not_escape():
    block = _block()
    block._cdp.send = MagicMock(side_effect=RuntimeError("target closed"))
    block.handle_paused(_event("POST"))  # no exception


# -- what counts as a read ------------------------------------------------------

@pytest.mark.parametrize("fields", [
    {"postData": "x"},
    {"hasPostData": True},
    {"postDataEntries": [{"bytes": "eA=="}]},
])
@pytest.mark.parametrize("method", ["OPTIONS", "GET", "HEAD"])
def test_a_safe_method_that_carries_a_body_is_a_write(method, fields):
    block = _block()
    event = _event(method)
    event["request"].update(fields)
    block.handle_paused(event)
    assert block._cdp.last()[0] == "Fetch.fulfillRequest"
    assert block.blocked[0]["method"] == method


@pytest.mark.parametrize("method", ["get", "Get", "options", "OPTıONS", "OPTIONſ", "", None, 7])
def test_only_the_exact_safe_method_names_are_reads(method):
    block = _block()
    event = _event("GET")
    event["request"]["method"] = method
    block.handle_paused(event)
    assert block._cdp.last()[0] == "Fetch.fulfillRequest"
    assert len(block.blocked) == 1


def test_an_event_with_no_request_at_all_is_faked_not_continued():
    block = _block()
    block.handle_paused({"requestId": "r1"})
    assert block._cdp.last()[0] == "Fetch.fulfillRequest"
    block2 = _block()
    block2.handle_paused({"requestId": "r1", "request": {}})
    assert block2._cdp.last()[0] == "Fetch.fulfillRequest"


# -- counting what was sent -------------------------------------------------------

def test_an_allowed_request_is_counted_as_sent_only_after_the_browser_let_it_go():
    block = _block(allow_patterns=("**/search",))
    _pause(block, "POST", url="https://example.com/search")
    [entry] = block.captured
    assert entry["outcome"] == "sent" and entry["allowed"] is True
    assert block._cdp.sent[-1][0] == "Fetch.continueRequest"
    assert len(block.allowed) == 1 and block.blocked == []


def test_an_allowed_request_the_page_cancelled_first_was_not_sent_and_is_not_counted():
    block = _block(fail_on={"Fetch.continueRequest"}, allow_patterns=("**/search",))
    for _ in range(20):  # a typeahead that aborts each request when the next key arrives
        block.handle_paused(_event("POST", url="https://example.com/search"))
    assert len(block.captured) == 20
    assert {e["outcome"] for e in block.captured} == {"aborted"}
    assert block.allowed == [] and block.blocked == []
    assert block.summary()["allowed_writes"] == 0 and block.summary()["dry_run"] is True


def test_a_blocked_request_the_page_cancelled_is_still_held_back_and_counted():
    block = _block(fail_on={"Fetch.fulfillRequest"})
    block.handle_paused(_event("POST"))
    assert len(block.blocked) == 1  # nothing reached the server either way


def test_a_record_that_cannot_be_written_never_changes_how_the_request_is_resolved(tmp_path):
    block = WriteBlock(WritePolicy(allow_patterns=("**/search",)), sink_path=tmp_path)  # a directory: unwritable
    block._cdp = FakeCDP()
    block.handle_paused(_event("POST", url="https://example.com/search"))
    block.handle_paused(_event("POST", url="https://example.com/other"))
    assert [m for m, _ in block._cdp.sent] == ["Fetch.continueRequest", "Fetch.fulfillRequest"]
    assert [e["outcome"] for e in block.captured] == ["sent", "blocked"]


class _BrokenSink:
    def write(self, text):
        raise UnicodeEncodeError("ascii", text, 0, 1, "surrogates not allowed")

    def flush(self):
        pass

    def close(self):
        raise OSError("already gone")


def test_whatever_goes_wrong_with_the_sink_the_write_is_still_resolved_and_counted(tmp_path):
    block = WriteBlock(sink_path=tmp_path / "w.jsonl")
    block._cdp = FakeCDP()
    block._sink = _BrokenSink()
    block.handle_paused(_event("POST"))
    assert block._cdp.last()[0] == "Fetch.fulfillRequest" and len(block.blocked) == 1
    block.close()  # a sink that fails to close doesn't raise either


def test_text_that_cannot_be_encoded_still_gets_recorded_as_ascii_json(tmp_path):
    block = WriteBlock(WritePolicy(record_fields=("\ud800",)), sink_path=tmp_path / "w.jsonl")
    block._cdp = FakeCDP()
    lone = '{"\\ud800": "\\ud800", "ok": 2}'  # a lone surrogate, as JSON text
    block.handle_paused(_event("POST", body=lone, content_type="application/json"))
    block.close()
    [line] = (tmp_path / "w.jsonl").read_bytes().decode("ascii").splitlines()  # pure ASCII
    assert json.loads(line)["outcome"] == "blocked"


# -- allow patterns ------------------------------------------------------------

@pytest.mark.parametrize("pattern, url, allowed", [
    ("**/api/search", "https://x.example/api/search?q=shoes", True),
    ("**/api/search", "https://x.example/api/orders/delete?next=/api/search", False),
    ("**/api/search?**", "https://x.example/api/orders/delete?next=/api/search?q", False),
    ("**/api/search?q=*", "https://x.example/api/search?q=shoes", True),
    ("**/api/search?q=*", "https://x.example/api/search?q=shoes&admin=1", False),
    ("https://x.example/graphql", "https://evil.example/graphql", False),
])
def test_allow_patterns_match_path_and_query_separately(pattern, url, allowed):
    block = _block(allow_patterns=(pattern,))
    sent, _ = _pause(block, "POST", url=url)
    assert (sent == "Fetch.continueRequest") is allowed
    assert block.captured[0].get("allowed", False) is allowed


@pytest.mark.parametrize("pattern, url, allowed", [
    ("http://localhost:*/api/search", "http://localhost:3000/api/search", True),
    # Credentials in the URL never count as part of the host or port.
    ("http://localhost:*/api/search", "http://localhost:1@evil.localhost:3000/api/search", False),
    ("http://localhost:*/api/search", "http://user:pw@localhost:3000/api/search", True),
    ("https://*.example.com/api/*", "https://a:b@evil.net:99@x.example.com/api/save", True),
    ("https://*.example.com/api/*", "https://x.example.com@evil.net/api/save", False),
    ("http://localhost:3000/**", "http://[::1]:3000/x", False),
    ("http://[::1]:3000/**", "http://[::1]:3000/x", True),
    ("**/search", "http://[::1/search", False),  # not a URL at all
    ("**", "data:text/plain,hello", False),
])
def test_allow_patterns_see_scheme_host_port_and_path_only(pattern, url, allowed):
    assert AllowPattern(pattern).matches(url) is allowed


def test_a_url_with_credentials_is_recorded_without_them():
    block = _block()
    _pause(block, "POST", url="https://admin:hunter2@example.com:8443/api/orders?x=1")
    assert block.captured[0]["url"] == "https://example.com:8443/api/orders"
    assert "hunter2" not in json.dumps(block.captured) and "admin" not in json.dumps(block.captured)
    assert "hunter2" not in base64.b64decode(block._cdp.last()[1]["body"]).decode()


def test_an_unreadable_url_is_faked_and_recorded_without_its_text():
    block = _block(allow_patterns=("**",))
    _pause(block, "POST", url="http://[::1/secret-token-xyz")
    assert block._cdp.last()[0] == "Fetch.fulfillRequest"
    assert block.captured[0]["url"] == "(unreadable URL)" and "secret" not in json.dumps(block.captured)


@pytest.mark.parametrize("pattern, reason", [
    ("**/a/**/b/**/c", "more than 2 wildcards before the '?'"),
    ("https://api.example.com/*-*-*-*/save", "more than 2 wildcards before the '?'"),
    ("https://*.*.*.*.example.com/**", "more than 2 wildcards before the '?'"),
    ("https://**.example.com/save", "'**' in the host or port"),
    ("**/search?a=*&b=*&c=*", "more than 2 wildcards after the '?'"),
    ("https://app.example.com", "has no path; write https://app.example.com/** for the whole site"),
    ("http://localhost:3000", "has no path"),
    ("app.example.com/api/**", "can't match any URL: start it with a scheme"),
    ("/api/save", "can't match any URL"),
])
def test_patterns_that_cannot_work_or_could_take_seconds_are_refused_with_the_reason(pattern, reason):
    with pytest.raises(ValueError) as err:
        WriteBlock(WritePolicy(allow_patterns=(pattern,)))
    assert reason in str(err.value) and pattern in str(err.value)


@pytest.mark.parametrize("pattern", [
    "**/api/**", "https://api.example.com/*/items/*", "https://*.example.com/**", "**/graphql",
    "https://app.example.com/**", "wss://example.com/live", "**/search?q=*&page=*", "**/search?**",
])
def test_ordinary_patterns_are_accepted(pattern):
    AllowPattern(pattern)


def test_matching_cost_is_bounded_by_the_url_length():
    pattern = AllowPattern("**/a/**/b")
    long_url = "https://x.example/" + "a/" * 100_000  # would backtrack for seconds
    started = time.monotonic()
    assert pattern.matches(long_url) is False
    assert time.monotonic() - started < 0.1
    assert AllowPattern("**").matches("https://x.example/" + "a" * 3000) is False  # too long never matches
    assert AllowPattern("**").matches("https://x.example/" + "a" * 100) is True


@pytest.mark.parametrize("pattern", ["https://api.example.com/search?q=*", "https://api.example.com/search",
                                     "https://api.example.com/search?**", "https://api.example.com/**"])
def test_the_length_cap_counts_the_query_too(pattern):
    short = "https://api.example.com/search?q=" + "x" * 50
    long = "https://api.example.com/search?q=" + "x" * 5000
    assert len(long) > wb.MAX_MATCH_CHARS
    assert AllowPattern(pattern).matches(long) is False
    if "search?" in pattern or pattern.endswith("**"):
        assert AllowPattern(pattern).matches(short) is True


def test_worst_case_patterns_finish_quickly_on_the_longest_url_that_can_match():
    url = "https://sub.example.com/" + "-".join(["a"] * 600) + "?q=" + "&".join(["k=v"] * 200)
    assert len(url) <= wb.MAX_MATCH_CHARS
    started = time.monotonic()
    for text in ("https://*.example.com/*-*", "**/*-*/z", "https://sub.example.com/**/z?**", "**?**&**"):
        try:
            pattern = AllowPattern(text)
        except ValueError:
            continue
        pattern.matches(url)
    assert time.monotonic() - started < 1.0


def test_a_run_of_stars_in_a_query_pattern_is_one_star():
    assert AllowPattern("**/search?**").matches("https://x.example/search?" + "a" * 100)
    assert AllowPattern("**/search?**").matches("https://x.example/search?a=b&c=d") is False  # * stops at &
    assert AllowPattern("**/search?q=**").matches("https://x.example/search?q=1&r=2") is False


def test_the_site_root_matches_an_allow_pattern_for_the_root():
    assert AllowPattern("https://app.example.com/").matches("https://app.example.com")
    assert AllowPattern("https://app.example.com/**").matches("https://app.example.com/")


# -- describing bodies -----------------------------------------------------------

@pytest.mark.parametrize("body, content_type, kind, fields", [
    (b'{"a": 1, "token": "t"}', "application/json; charset=utf-8", "application/json", ["a", "token"]),
    (b'{"a": 1}', "application/vnd.api+json", "application/vnd.api+json", ["a"]),
    (b"[1, 2]", "application/json", "application/json", None),
    (b"user=bob&password=hunter2", "application/x-www-form-urlencoded", "application/x-www-form-urlencoded", ["user", "password"]),
    (b"password=hunter2\r\n", "text/plain", "text/plain", None),
    (b"<login><password>hunter2</password></login>", "application/xml", "application/xml", None),
    (b"User alice failed card 4111111111111111", None, "unknown", None),
    (b"{not json", "application/json", "application/json", None),
])
def test_describe_body_lists_names_only_from_a_real_parser(body, content_type, kind, fields):
    info = describe_body(body, content_type)
    assert info["body_kind"] == kind and info["body_bytes"] == len(body)
    assert info.get("body_fields") == fields
    assert "hunter2" not in json.dumps(info) and "4111" not in json.dumps(info)


def test_multipart_names_come_from_the_part_headers_only():
    body = (b'--b\r\nContent-Disposition: form-data; name="doc"; filename="passport_john.pdf"\r\n'
            b'Content-Type: application/pdf\r\n\r\n<a name="secret-anchor">x</a>\r\n'
            b'--b\r\nContent-Disposition: form-data; name="note"\r\n\r\nhi\r\n--b--\r\n')
    info = describe_body(body, "multipart/form-data; boundary=b")
    assert info["body_fields"] == ["doc", "note"]
    assert "passport" not in json.dumps(info) and "secret-anchor" not in json.dumps(info)


def test_describe_message():
    assert describe_message('{"op": "save", "token": "t"}')["body_fields"] == ["op", "token"]
    assert describe_message('42["msg","secret"]') == {"body_kind": "text", "body_bytes": 18}
    assert describe_message(b"\x00\x01") == {"body_kind": "binary", "body_bytes": 2}


# -- names: a name is shown only if it looks like a field name -------------------------

def test_json_posted_as_a_form_is_not_taken_for_field_names():
    body = b'{"email":"jane@example.com","password":"hunter2","note":"a=b"}'
    info = describe_body(body, "application/x-www-form-urlencoded")
    assert "body_fields" not in info and info["unnamed_fields"] == 1
    assert "jane" not in json.dumps(info) and "hunter2" not in json.dumps(info)


@pytest.mark.parametrize("key", [
    "jane@example.com", "4111111111111111", "c2VjcmV0LXRva2VuMTIzNDU2Nzg5MA", "x" * 41,
    "a b", "naïve", "", "-lead", "1st", 'q"uote', "<script>",
])
def test_keys_that_could_be_data_are_counted_not_shown(key):
    body = json.dumps({key: 1, "title": "t"}).encode()
    info = describe_body(body, "application/json")
    assert info["body_fields"] == ["title"] and info["unnamed_fields"] == 1


@pytest.mark.parametrize("name", ["title", "user[name]", "address.line1", "__RequestVerificationToken", "x" * 40])
def test_ordinary_field_names_are_shown(name):
    assert describe_body(json.dumps({name: 1}).encode(), "application/json")["body_fields"] == [name]


def test_names_in_a_websocket_message_get_the_same_gate():
    info = describe_message(json.dumps({"op": "save", "jane@example.com": 1}))
    assert info["body_fields"] == ["op"] and info["unnamed_fields"] == 1


def test_query_names_get_the_same_gate_and_rejected_ones_are_counted():
    block = _block()
    _pause(block, "POST", url="https://example.com/a?q=shoes&c2VjcmV0LXRva2VuMTIzNDU2Nzg5MA==&jane@example.com=1&x")
    [entry] = block.captured
    assert entry["query_keys"] == ["q"] and entry["query_unnamed"] == 3
    assert "c2Vj" not in json.dumps(entry) and "jane" not in json.dumps(entry)


def test_repeated_names_are_listed_once():
    assert describe_body(b"tag=a&tag=b&tag=c", "application/x-www-form-urlencoded")["body_fields"] == ["tag"]


# -- values the user named, paths, CORS ----------------------------------------------

def test_only_values_of_fields_the_user_named_are_recorded():
    block = _block(record_fields=("title", "q"))
    _pause(block, "POST", url="https://example.com/api/orders?q=shoes&token=t",
           body='{"title": "Mug", "password": "hunter2", "qty": 2}', content_type="application/json")
    entry = block.captured[0]
    assert entry["values"] == {"title": "Mug"}
    assert entry["query_values"] == {"q": "shoes"}
    assert "hunter2" not in json.dumps(entry) and '"t"' not in json.dumps(entry)


def test_named_values_from_forms_and_multipart_text_parts_only():
    form = describe_body(b"title=Mug&password=x", "application/x-www-form-urlencoded", ("title",))
    assert form["values"] == {"title": "Mug"}
    multi = describe_body(b'--b\r\nContent-Disposition: form-data; name="doc"; filename="a.pdf"\r\n\r\nPDF\r\n'
                          b'--b\r\nContent-Disposition: form-data; name="title"\r\n\r\nMug\r\n--b--\r\n',
                          "multipart/form-data; boundary=b", ("doc", "title"))
    assert multi["values"] == {"title": "Mug"}  # never a file's content


def test_multipart_text_is_utf8_unless_the_part_names_a_charset():
    title = "東京タワー ✓ café"
    body = (b'--b\r\nContent-Disposition: form-data; name="title"\r\n\r\n' + title.encode() +
            b'\r\n--b\r\nContent-Disposition: form-data; name="note"\r\nContent-Type: text/plain; charset=latin-1\r\n\r\n'
            + "café".encode("latin-1") + b'\r\n--b--\r\n')
    info = describe_body(body, "multipart/form-data; boundary=b", ("title", "note"))
    assert info["values"] == {"title": title, "note": "café"}


def test_a_part_with_an_unknown_charset_does_not_lose_the_other_names():
    body = (b'--b\r\nContent-Disposition: form-data; name="first"\r\n\r\none\r\n'
            b'--b\r\nContent-Disposition: form-data; name="odd"\r\nContent-Type: text/plain; charset=bogus\r\n\r\ntwo\r\n'
            b'--b\r\nContent-Disposition: form-data; name="last"\r\n\r\nthree\r\n--b--\r\n')
    info = describe_body(body, "multipart/form-data; boundary=b", ("odd",))
    assert info["body_fields"] == ["first", "odd", "last"] and info["values"] == {"odd": "two"}


@pytest.mark.parametrize("path, template", [
    ("/api/orders", "/api/orders"),
    ("/invitations/9f2c4b7e1a/accept", "/invitations/:param/accept"),
    ("/api/users/jane.doe@example.com/role", "/api/users/:param/role"),
    ("/orders/123", "/orders/:param"),
    ("/p/" + "a" * 40, "/p/:param"),
    ("/files/a%20b", "/files/:param"),
    ("/", "/"),
    # Shapes the earlier digit rule let through: letters-only tokens and codes.
    ("/invite/xKcdQfGh/accept", "/invite/:param/accept"),
    ("/reset/ABCDEFGHIJKLMNOP", "/reset/:param"),
    ("/coupons/WELCOMEBACK/apply", "/coupons/:param/apply"),
    ("/u/用户/save", "/u/:param/save"),
    # Route words stay.
    ("/api/v2/getUser", "/api/v2/getUser"),
    ("/UserService/UpdateUser", "/UserService/UpdateUser"),
    ("/sign-in", "/sign-in"),
    ("/reset_password", "/reset_password"),
    ("/save.json", "/save.json"),
    ("/oauth2/token", "/oauth2/token"),
    ("/v1beta1/things", "/v1beta1/things"),
    ("/graphql", "/graphql"),
])
def test_path_segments_that_are_not_route_words_are_templated(path, template):
    assert path_template(path) == template


def test_a_lowercase_slug_has_the_shape_of_a_route_word_and_is_kept():
    # The known limit of recording paths: a slug such as a username can't be told from a route word.
    assert path_template("/users/jane-doe/role") == "/users/jane-doe/role"


def test_recorded_urls_use_the_template_and_drop_credentials_and_query():
    block = _block()
    _pause(block, "POST", url="https://example.com/invitations/9f2c4b7e1a/accept?x=1")
    assert block.captured[0]["url"] == "https://example.com/invitations/:param/accept"
    assert record_url("https://u:p@Example.COM:8443/a") == "https://example.com:8443/a"


def test_a_cross_origin_fake_allows_its_origin_with_credentials():
    block = _block()
    event = _event("POST", url="https://api.example.net/save")
    event["request"]["headers"]["Origin"] = "https://app.example.com"
    block.handle_paused(event)
    headers = {h["name"]: h["value"] for h in block._cdp.last()[1]["responseHeaders"]}
    assert headers["Access-Control-Allow-Origin"] == "https://app.example.com"
    assert headers["Access-Control-Allow-Credentials"] == "true"
    assert headers["Vary"] == "Origin"


@pytest.mark.parametrize("resource_type", ["Fetch", "XHR", "Document"])
def test_a_request_from_an_opaque_origin_gets_the_null_origin_allowed(resource_type):
    block = _block()
    event = _event("POST", url="https://api.example.net/save", resource_type=resource_type)
    event["request"]["headers"]["Origin"] = "null"  # sandboxed iframe, data: URL
    block.handle_paused(event)
    headers = {h["name"]: h["value"] for h in block._cdp.last()[1]["responseHeaders"]}
    assert headers["Access-Control-Allow-Origin"] == "null"
    assert headers["Access-Control-Allow-Credentials"] == "true"


def test_a_same_origin_fake_has_no_cors_headers():
    block = _block()
    _, params = _pause(block, "POST")
    assert [h["name"] for h in params["responseHeaders"]] == ["Content-Type"]


def test_only_the_blocked_document_is_excluded_from_return_targets():
    block = _block()
    _pause(block, "POST", url="https://example.com/settings", resource_type="Document")
    assert block.is_blocked_document("https://example.com/settings", lambda: "Blocked by Knarr")
    assert not block.is_blocked_document("https://example.com/settings", lambda: "Settings")  # later GET
    called = []
    assert not block.is_blocked_document("https://example.com/other", lambda: called.append(1))
    assert called == []  # the title is only read for a URL that had a blocked post
    assert not block.is_blocked_document("about:blank", lambda: "Blocked by Knarr")


def test_a_very_large_body_is_noted_as_a_limit():
    block = _block()
    _pause(block, "POST", body="x" * 6_000_000, content_type="text/plain")
    [note] = block.drain_gaps()
    assert "5 MB" in note and "memory" in note
    block2 = _block()
    _pause(block2, "POST", body="x" * 1000, content_type="text/plain")
    assert block2.drain_gaps() == []


# -- policy, summary, gaps, installation ------------------------------------------

@pytest.mark.parametrize("env, default, blocks", [
    ({}, True, True), ({}, False, False),
    ({"KNARR_BLOCK_WRITES": "1"}, False, True),
    ({"KNARR_ALLOW_WRITES": "1"}, True, False),
    ({"KNARR_ALLOW_WRITES": "1", "KNARR_BLOCK_WRITES": "1"}, False, True),  # block wins
])
def test_policy(monkeypatch, env, default, blocks):
    for name in ("KNARR_ALLOW_WRITES", "KNARR_BLOCK_WRITES", "KNARR_RECORD_FIELDS", "KNARR_WRITE_ALLOW"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert WritePolicy.from_env(default_block=default).block is blocks


def test_write_allow_env_is_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv("KNARR_WRITE_ALLOW", " **/graphql , ,https://x.example.com/search ")
    assert WritePolicy.from_env().allow_patterns == ("**/graphql", "https://x.example.com/search")


def test_summary_is_a_dry_run_until_a_write_goes_out():
    block = _block(allow_patterns=("**/search",))
    _pause(block, "POST", url="https://example.com/api/orders")
    assert block.summary() == {"dry_run": True, "blocked_writes": 1, "allowed_writes": 0,
                               "write_block_gaps": []}
    _pause(block, "POST", url="https://example.com/search")  # a write really sent
    assert block.summary() == {"dry_run": False, "blocked_writes": 1, "allowed_writes": 1,
                               "write_block_gaps": []}


def test_summary_without_interception_does_not_claim_zero():
    summary = WriteBlock(WritePolicy(block=False)).summary()
    assert summary == {"dry_run": False, "blocked_writes": None, "allowed_writes": None,
                       "write_block_gaps": []}


def test_the_summary_has_one_key_for_the_policy():
    assert "writes_blocked" not in _block().summary()


def test_repeated_gaps_are_reported_once_and_counted():
    block = _block()
    for _ in range(3):
        block._worker_started(MagicMock())
    assert len(block.drain_gaps()) == 1 and block.drain_gaps() == []
    assert block.gaps[0].endswith("(x3)")


def test_records_go_to_the_sink_file_and_drain_returns_only_new(tmp_path):
    block = WriteBlock(sink_path=tmp_path / "run" / "captured_writes.jsonl")
    block._cdp = FakeCDP()
    _pause(block, "POST", url="https://example.com/a")
    assert [w["url"] for w in block.drain()] == ["https://example.com/a"]
    assert block.drain() == []
    _pause(block, "DELETE", url="https://example.com/b")
    assert [w["method"] for w in block.drain()] == ["DELETE"]
    lines = (tmp_path / "run" / "captured_writes.jsonl").read_text().splitlines()
    assert [json.loads(line)["method"] for line in lines] == ["POST", "DELETE"]


def test_install_intercepts_the_request_types_that_can_carry_a_write():
    block, browser, context = WriteBlock(), MagicMock(), MagicMock()
    cdp = browser.new_browser_cdp_session.return_value
    block.install(browser, context)
    cdp.on.assert_called_once_with("Fetch.requestPaused", block.handle_paused)
    [call] = cdp.send.call_args_list
    assert call.args[0] == "Fetch.enable"
    patterns = call.args[1]["patterns"]
    assert [p["resourceType"] for p in patterns] == list(WRITE_RESOURCE_TYPES)
    assert all(p["urlPattern"] == "*" and p["requestStage"] == "Request" for p in patterns)
    context.add_init_script.assert_called_once_with(DISABLE_UNCOVERED_CHANNELS_SCRIPT)
    context.route_web_socket.assert_called_once_with("**", block.handle_websocket)
    context.route.assert_not_called()  # no per-page routing


def test_a_browser_that_rejects_the_type_filter_gets_every_request_paused():
    block, browser, context = WriteBlock(), MagicMock(), MagicMock()
    cdp = browser.new_browser_cdp_session.return_value
    cdp.send.side_effect = [RuntimeError("Unknown resource type in fetch filter"), None]
    block.install(browser, context)
    assert cdp.send.call_args_list[1].args == (
        "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})


def test_an_allowing_policy_installs_nothing():
    block, browser, context = WriteBlock(WritePolicy(block=False)), MagicMock(), MagicMock()
    block.install(browser, context)
    browser.new_browser_cdp_session.assert_not_called()
    context.add_init_script.assert_not_called()


def test_every_request_type_chrome_has_is_classified():
    """A Playwright upgrade that brings a new request type fails here until someone decides
    whether it can carry a write; unlisted types are not paused."""
    import playwright

    path = Path(playwright.__file__).parent / "driver" / "package" / "types" / "protocol.d.ts"
    if not path.exists():
        pytest.skip("this Playwright has no protocol.d.ts")
    match = re.search(r'export type ResourceType = ([^;]+);', path.read_text())
    types = set(re.findall(r'"(\w+)"', match.group(1)))
    assert types, "could not read Network.ResourceType"
    classified = set(WRITE_RESOURCE_TYPES) | READ_ONLY_RESOURCE_TYPES | UNFILTERABLE_RESOURCE_TYPES
    assert types <= classified, f"unclassified request types: {sorted(types - classified)}"
    assert not set(WRITE_RESOURCE_TYPES) & (READ_ONLY_RESOURCE_TYPES | UNFILTERABLE_RESOURCE_TYPES)
    assert not READ_ONLY_RESOURCE_TYPES & UNFILTERABLE_RESOURCE_TYPES


def test_the_launch_flags_switch_off_shared_workers_and_websocket_streams_everywhere():
    assert LAUNCH_ARGS == ("--disable-blink-features=SharedWorker,WebSocketStream",)
    assert WriteBlock().launch_args == LAUNCH_ARGS
    assert WriteBlock(WritePolicy(block=False)).launch_args == ()


def test_the_page_script_covers_every_channel_the_flags_do_not():
    for name in ("SharedWorker", "WebSocketStream", "WebTransport", "RTCPeerConnection", "webkitRTCPeerConnection"):
        assert f"'{name}'" in DISABLE_UNCOVERED_CHANNELS_SCRIPT


def test_outgoing_websocket_messages_are_recorded_and_dropped():
    block, ws = _block(), MagicMock()
    ws.url = "wss://example.com/live?key=k"
    block.handle_websocket(ws)
    (on_page_message,), _ = ws.on_message.call_args
    on_page_message('{"op": "save", "token": "t-secret"}')
    entry = block.captured[0]
    assert entry["method"] == "WEBSOCKET_SEND" and entry["url"] == "wss://example.com/live"
    assert entry["body_fields"] == ["op", "token"] and "t-secret" not in json.dumps(entry)
    assert entry["outcome"] == "blocked"
    ws.send.assert_not_called()


def test_an_allowed_websocket_forwards_and_counts_its_messages():
    block, ws = _block(allow_patterns=("wss://example.com/live",)), MagicMock()
    ws.url = "wss://example.com/live"
    block.handle_websocket(ws)
    server = ws.connect_to_server.return_value
    (on_page_message,), _ = ws.on_message.call_args
    on_page_message("subscribe")
    on_page_message("ping")
    assert [c.args[0] for c in server.send.call_args_list] == ["subscribe", "ping"]
    assert len(block.allowed) == 2 and block.blocked == []


def test_an_allowed_websocket_message_that_could_not_be_sent_is_not_counted():
    block, ws = _block(allow_patterns=("wss://example.com/live",)), MagicMock()
    ws.url = "wss://example.com/live"
    ws.connect_to_server.return_value.send.side_effect = RuntimeError("socket closed")
    block.handle_websocket(ws)
    (on_page_message,), _ = ws.on_message.call_args
    on_page_message("ping")
    assert block.allowed == [] and block.captured[0]["outcome"] == "aborted"


def test_records_file_is_opened_once_and_closed(tmp_path):
    block = WriteBlock(sink_path=tmp_path / "w.jsonl")
    block._cdp = FakeCDP()
    _pause(block, "POST")
    handle = block._sink
    _pause(block, "POST")
    assert block._sink is handle
    block.close()
    assert block._sink is None and len((tmp_path / "w.jsonl").read_text().splitlines()) == 2


# -- frames and workers the page script can't cover ---------------------------------------

def _frame(sandbox=None, srcdoc=None, error=None):
    frame = MagicMock(name="frame")
    if error:
        frame.frame_element.side_effect = error
    attributes = {"sandbox": sandbox, "srcdoc": srcdoc}
    frame.frame_element.return_value.get_attribute.side_effect = attributes.get
    return frame


def test_the_block_listens_for_workers_and_frames_on_every_page():
    block, browser, context = WriteBlock(), MagicMock(), MagicMock()
    block.install(browser, context)
    assert [c.args[0] for c in context.on.call_args_list] == ["page", "response"]
    assert context.on.call_args_list[0].args[1] == block._page_opened
    page = MagicMock()
    block._page_opened(page)
    assert {c.args[0] for c in page.on.call_args_list} == {"worker", "frameattached"}


@pytest.mark.parametrize("sandbox, srcdoc, gap", [
    ("allow-scripts", "<p>x</p>", True),
    ("", "<p>x</p>", True),                                  # sandbox with no tokens: opaque origin
    ("allow-scripts allow-forms", "<p>x</p>", True),
    ("allow-scripts allow-same-origin", "<p>x</p>", False),  # keeps its origin: gets the script
    (None, "<p>x</p>", False),                               # not sandboxed
    ("allow-scripts", None, False),                          # a sandboxed frame loaded from a URL
])
def test_a_sandboxed_srcdoc_frame_is_noted_as_a_gap(sandbox, srcdoc, gap):
    block = _block()
    block._frame_attached(_frame(sandbox, srcdoc))
    notes = block.drain_gaps()
    assert bool(notes) is gap
    if gap:
        assert "sandboxed frame" in notes[0] and "WebSocket" in notes[0]


def test_a_frame_that_cannot_be_inspected_is_ignored():
    block = _block()
    block._frame_attached(_frame(error=RuntimeError("Frame was detached")))
    assert block.drain_gaps() == []


def test_a_worker_gap_names_the_channels_it_cannot_block():
    block = _block()
    block._worker_started(MagicMock())
    assert "WebSocket and WebTransport" in block.drain_gaps()[0]


@pytest.mark.parametrize("event", [
    {"requestId": "r1", "request": ["POST"]},
    {"requestId": "r1", "request": {"method": ["POST"], "url": None, "headers": ["x"]}},
    {"requestId": "r1", "request": {"method": {"a": 1}, "url": 7, "headers": None}},
    {"requestId": "r1", "request": "POST /x"},
    {},
])
def test_a_malformed_event_never_raises_and_is_not_let_through(event):
    block = _block()
    block.handle_paused(event)  # no exception
    assert all(method != "Fetch.continueRequest" for method, _ in block._cdp.sent)


# -- names and values the user listed ----------------------------------------------------------------------------------

def test_a_name_the_user_listed_is_shown_whatever_it_looks_like():
    block = _block(record_fields=("$filter",))
    _pause(block, "POST", url="https://example.com/odata?$filter=name%20eq%20x&$select=a")
    [entry] = block.captured
    assert entry["query_keys"] == ["$filter"] and entry["query_values"] == {"$filter": "name eq x"}
    assert entry["query_unnamed"] == 1  # $select was not listed and does not look like a name


def test_query_values_do_not_depend_on_other_names_passing_the_gate():
    block = _block(record_fields=("q",))
    _pause(block, "POST", url="https://example.com/a?jane@example.com=1&q=shoes")
    assert block.captured[0]["query_values"] == {"q": "shoes"} and block.captured[0]["query_unnamed"] == 1


@pytest.mark.parametrize("name", [
    "a5f4dcc3b5aa765d61d8327deb882cf9",                 # md5, hex
    "123e4567-e89b-12d3-a456-426614174000",             # uuid
    "abcdef01-2345-6789-abcd-ef0123456789",             # uuid starting with a letter
    "c2VjcmV0LXRva2Vu",                                  # base64 with digits and both cases
    "title\n",                                          # a name plus a newline is not a name
])
def test_token_shaped_names_are_not_shown(name):
    info = describe_body(json.dumps({name: 1, "ok": 2}).encode(), "application/json")
    assert info["body_fields"] == ["ok"] and info["unnamed_fields"] == 1


def test_names_beyond_the_cap_leave_a_marker():
    body = json.dumps({f"field{i}": i for i in range(45)}).encode()
    info = describe_body(body, "application/json")
    assert len(info["body_fields"]) == 30 and info["body_fields_more"] == 15


def test_large_bodies_are_not_parsed():
    body = b"a=1&" * (wb.MAX_PARSE_BYTES // 4 + 10)
    info = describe_body(body, "application/x-www-form-urlencoded")
    assert info == {"body_kind": "application/x-www-form-urlencoded", "body_bytes": len(body)}


@pytest.mark.parametrize("path, template", [
    ("/users/jane.doe/role", "/users/:param/role"),
    ("/user-bucket/uploads/passport_john.pdf", "/user-bucket/uploads/:param"),
    ("/api/save.json", "/api/save.json"),
    ("/feed.xml", "/feed.xml"),
    ("/login.php", "/login.php"),
])
def test_only_known_file_extensions_stay_in_a_path(path, template):
    assert path_template(path) == template


# -- the DevTools endpoint ------------------------------------------------------------------------------------------------

def _devtools_block(**policy):
    block = _block(**policy)
    block.devtools_url = "http://127.0.0.1:9222"
    return block


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9222/json/list", "http://localhost:9222/json/close/ABC", "http://localhost.:9222/json/list",
    "http://[::1]:9222/devtools/inspector.html", "http://0.0.0.0:9222/json",
])
@pytest.mark.parametrize("resource_type", ["Document", "Fetch", "Image", "Script"])
def test_any_request_to_the_devtools_endpoint_is_refused_whatever_its_type_and_method(url, resource_type):
    block = _devtools_block()
    block.handle_paused(_event("GET", url=url, resource_type=resource_type))
    method, params = block._cdp.last()
    assert method == "Fetch.fulfillRequest" and params["responseCode"] == 403
    assert base64.b64decode(params["body"]).startswith(b"Blocked by Knarr: this is your Chrome's DevTools")
    [entry] = block.captured
    assert entry["outcome"] == "refused" and entry["resource_type"] == "devtools"
    assert block.summary()["blocked_writes"] == 0  # a refused read isn't a blocked write
    assert any("DevTools endpoint" in gap for gap in block.gaps)


def test_other_ports_on_localhost_are_not_the_devtools_endpoint():
    block = _devtools_block()
    block.handle_paused(_event("GET", url="http://localhost:3000/api"))
    assert block._cdp.last()[0] == "Fetch.continueRequest" and block.captured == []


def test_the_devtools_patterns_cover_the_usual_spellings_for_every_type():
    block, browser, context = WriteBlock(), MagicMock(), MagicMock()
    block.devtools_url = "ws://127.0.0.1:9333/devtools/browser/abc"
    block.install(browser, context)
    patterns = browser.new_browser_cdp_session.return_value.send.call_args.args[1]["patterns"]
    untyped = {p["urlPattern"] for p in patterns if "resourceType" not in p}
    assert {"http://127.0.0.1:9333/*", "http://localhost:9333/*", "http://localhost.:9333/*",
            "http://*.localhost:9333/*", "http://*.localhost.:9333/*",
            "http://[::1]:9333/*", "ws://localhost:9333/*"} <= untyped
    assert [p["resourceType"] for p in patterns if "resourceType" in p] == list(WRITE_RESOURCE_TYPES)


def test_with_writes_allowed_only_the_devtools_endpoint_is_intercepted():
    block = WriteBlock(WritePolicy(block=False))
    block.devtools_url = "http://127.0.0.1:9222"
    browser, context = MagicMock(), MagicMock()
    block.install(browser, context)
    cdp = browser.new_browser_cdp_session.return_value
    patterns = cdp.send.call_args.args[1]["patterns"]
    assert all("resourceType" not in p and ":9222" in p["urlPattern"] for p in patterns)
    context.add_init_script.assert_not_called()
    context.route_web_socket.assert_not_called()
    block._cdp = FakeCDP()
    block.handle_paused(_event("POST", url="https://example.com/save"))  # not paused in a real browser; if it were:
    assert block._cdp.last()[0] == "Fetch.continueRequest" and block.captured == []
    block.handle_paused(_event("GET", url="http://127.0.0.1:9222/json/list"))
    assert block._cdp.last()[0] == "Fetch.fulfillRequest" and block.captured[0]["outcome"] == "refused"


# -- WebSockets -----------------------------------------------------------------------------------------------------------

def test_a_blocked_socket_closes_the_servers_side_with_a_neutral_close_not_the_pages():
    block, ws = _block(), MagicMock()
    ws.url = "wss://example.com/chat"
    block.handle_websocket(ws)
    (on_close,), _ = ws.on_close.call_args
    on_close(4001, "page-chosen-reason")
    server = ws.connect_to_server.return_value
    server.close.assert_called_once_with()


def test_an_allowed_socket_passes_the_pages_close_through():
    block, ws = _block(allow_patterns=("wss://example.com/live",)), MagicMock()
    ws.url = "wss://example.com/live"
    block.handle_websocket(ws)
    (on_close,), _ = ws.on_close.call_args
    on_close(4001, "bye")
    ws.connect_to_server.return_value.close.assert_called_once_with(code=4001, reason="bye")


def test_a_failing_close_does_not_escape():
    block, ws = _block(), MagicMock()
    ws.url = "wss://example.com/chat"
    ws.connect_to_server.return_value.close.side_effect = RuntimeError("already closed")
    block.handle_websocket(ws)
    (on_close,), _ = ws.on_close.call_args
    on_close(1000, "")  # no exception


# -- reports the browser sends itself ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("header", ["report-to", "reporting-endpoints", "nel"])
def test_a_site_that_asks_chrome_for_reports_is_noted_as_a_gap(header):
    block = _block()
    block._response_seen(MagicMock(headers={header: "x", "content-type": "text/html"}))
    [note] = block.drain_gaps()
    assert "send it reports" in note and "can't hold them back" in note


def test_ordinary_responses_and_unreadable_ones_are_not_gaps():
    block = _block()
    block._response_seen(MagicMock(headers={"content-type": "text/html"}))
    bad = MagicMock()
    type(bad).headers = property(lambda self: (_ for _ in ()).throw(RuntimeError("gone")))
    block._response_seen(bad)
    assert block.drain_gaps() == []


# -- a flood of writes ---------------------------------------------------------------------------------------------------------------

def test_a_flood_is_counted_exactly_but_only_the_first_records_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(wb, "MAX_RECORDS", 50)
    block = WriteBlock(WritePolicy(allow_patterns=("**/ok",)), sink_path=tmp_path / "w.jsonl")
    block._cdp = FakeCDP()
    for i in range(80):
        block.handle_paused(_event("POST", url=f"https://example.com/n{'x' * (i % 3)}"))
    for _ in range(5):
        block.handle_paused(_event("POST", url="https://example.com/ok"))
    block.close()
    assert len(block.captured) == 50 and len((tmp_path / "w.jsonl").read_text().splitlines()) == 50
    assert block.summary()["blocked_writes"] == 80 and block.summary()["allowed_writes"] == 5
    assert block.summary()["dry_run"] is False  # a write went out, though it isn't listed
    assert sum("more than 50 writes" in gap for gap in block.gaps) == 1


# -- what the init script does to a polyfill -------------------------------------------------------------------------------------------

def test_the_page_script_lets_a_polyfill_assign_without_throwing():
    assert "get: () => blocked, set: () => {}" in DISABLE_UNCOVERED_CHANNELS_SCRIPT
    assert "writable: false" not in DISABLE_UNCOVERED_CHANNELS_SCRIPT


# -- the multipart reader ------------------------------------------------------------------------------------------------------------

def _part(name, value, filename=None, ctype=None, newline=b"\r\n"):
    head = b'Content-Disposition: form-data; name="' + name.encode() + b'"'
    if filename:
        head += b'; filename="' + filename.encode() + b'"'
    if ctype:
        head += newline + b"Content-Type: " + ctype.encode()
    return b"--B" + newline + head + newline + newline + value + newline


def test_multipart_with_bare_newlines_and_odd_headers():
    body = (_part("a", b"1", newline=b"\n") + _part("b", b"2", newline=b"\n") + b"--B--\n")
    info = describe_body(body, "multipart/form-data; boundary=B", ("a", "b"))
    assert info["body_fields"] == ["a", "b"] and info["values"] == {"a": "1", "b": "2"}


def test_multipart_without_a_boundary_or_with_a_quoted_one():
    assert "body_fields" not in describe_body(b"--B\r\n", "multipart/form-data")
    quoted = _part("a", b"1") + b"--B--\r\n"
    assert describe_body(quoted, 'multipart/form-data; boundary="B"')["body_fields"] == ["a"]


def test_multipart_files_and_other_types_never_give_up_content():
    body = (_part("doc", b"PDFDATA", filename="passport.pdf", ctype="application/pdf")
            + _part("raw", b"\x00\x01", ctype="application/octet-stream")
            + _part("note", b"hello") + b"--B--\r\n")
    info = describe_body(body, "multipart/form-data; boundary=B", ("doc", "raw", "note"))
    assert info["body_fields"] == ["doc", "raw", "note"] and info["values"] == {"note": "hello"}
    assert "passport" not in json.dumps(info)


def test_a_part_without_a_usable_name_is_counted_not_shown():
    body = (b"--B\r\nContent-Type: text/plain\r\n\r\nx\r\n"
            b"--B\r\nContent-Disposition: form-data; name*=UTF-8''caf%C3%A9\r\n\r\ny\r\n"
            + _part("ok", b"1") + b"--B--\r\n")
    info = describe_body(body, "multipart/form-data; boundary=B")
    assert info["body_fields"] == ["ok"] and info["unnamed_fields"] == 2


@pytest.mark.parametrize("header", [
    b'Content-Disposition: form-data; ' + b'name="x"; ' * 4000,
    b'Content-Disposition: form-data; name="' + b'a' * 60000,
    b'Content-Disposition: ' + b'\\"' * 30000 + b'; name="x"',
    b'Content-Type: text/plain; ' + b'charset=' * 8000,
])
def test_hostile_part_headers_are_read_in_linear_time(header):
    body = b"--B\r\n" + header + b"\r\n\r\nvalue\r\n--B--\r\n"
    started = time.monotonic()
    describe_body(body, "multipart/form-data; boundary=B", ("x",))
    assert time.monotonic() - started < 0.5


# -- a blocked form post to another origin ---------------------------------------------------------------------------------------------

def test_the_stand_in_for_a_cross_origin_post_is_recognised_at_the_posts_url():
    block = _block()
    _pause(block, "POST", url="https://partner.example.net/receive", resource_type="Document")
    assert block.is_blocked_document("https://partner.example.net/receive", lambda: "Blocked by Knarr")


def test_the_blocked_page_escapes_what_it_shows(monkeypatch):
    block = _block()
    monkeypatch.setattr(wb, "record_url", lambda url: "https://x.example/<script>alert(1)</script>")
    _, params = _pause(block, "POST", resource_type="Document")
    page = base64.b64decode(params["body"]).decode()
    assert "<script>" not in page and "&lt;script&gt;" in page


# -- switches ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, parsed", [
    ("1", True), ("true", True), ("YES", True), ("on", True), ("y", True), ("t", True), ("enabled", True), (" On ", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("n", False), ("f", False), ("disabled", False),
    ("", False), (None, False), ("  ", False),
    ("2", None), ("enable", None), ("maybe", None), ("onn", None),
])
def test_parse_switch(raw, parsed):
    assert wb.parse_switch(raw) is parsed


@pytest.mark.parametrize("value", ["1", "true", "on", "y", "t", "enabled", "2", "enable", "maybe"])
def test_block_writes_is_on_for_every_value_but_an_explicit_off(monkeypatch, value):
    """The runner defaults to not blocking; a typo must not leave it unblocked."""
    monkeypatch.setenv("KNARR_BLOCK_WRITES", value)
    assert WritePolicy.from_env(default_block=False).block is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "", "disabled"])
def test_block_writes_explicitly_off_leaves_the_default(monkeypatch, value):
    monkeypatch.setenv("KNARR_BLOCK_WRITES", value)
    assert WritePolicy.from_env(default_block=False).block is False
    assert WritePolicy.from_env(default_block=True).block is True


@pytest.mark.parametrize("value, allows", [("1", True), ("on", True), ("y", True), ("2", False), ("enable", False), ("0", False)])
def test_allow_writes_needs_a_value_that_clearly_says_on(monkeypatch, value, allows):
    monkeypatch.setenv("KNARR_ALLOW_WRITES", value)
    assert WritePolicy.from_env().block is (not allows)


def test_a_block_setting_beats_an_allow_setting_whatever_the_words(monkeypatch):
    monkeypatch.setenv("KNARR_ALLOW_WRITES", "on")
    monkeypatch.setenv("KNARR_BLOCK_WRITES", "maybe")
    assert WritePolicy.from_env(default_block=False).block is True


def test_the_page_script_stub_is_configurable_so_a_global_declaration_cannot_fail():
    assert "configurable: true" in DISABLE_UNCOVERED_CHANNELS_SCRIPT
    assert "configurable: false" not in DISABLE_UNCOVERED_CHANNELS_SCRIPT
