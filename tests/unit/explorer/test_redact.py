"""Unit tests for knarr/explorer/redact.py — credentials in URLs stay out of the files a run leaves."""

from __future__ import annotations

import time

import pytest

from knarr.explorer.redact import MAX_STRING_CHARS, REDACTED, redact, redact_url, redact_urls_in_text

STAGING_URL = (
    "https://staging.example.com/projects/10000/items?kind=WEEKLY"
    "&tags=anchor%2Cbridge%2Ccharlie%2Cdefault%2Ceagle%2Cfoxtrot%2Cglide%2Cmeadow%2Cisland"
    "&sortBy=item.updatedAt&sortOrder=desc"
)


@pytest.mark.parametrize("url", [
    STAGING_URL,
    "https://example.com/",
    "https://example.com/a/b?page=2&sort=name&q=thisisalongsearchtermwithoutnumbers",
    "https://example.com/a#section-2",
    "https://example.com/a?code=",  # an empty value has nothing to hide
    "http://127.0.0.1:8080/x?y=1",
])
def test_an_ordinary_url_is_left_exactly_as_it_is(url):
    assert redact_url(url) == url


@pytest.mark.parametrize("name", [
    "code", "access_token", "id_token", "refresh_token", "token", "SAMLResponse", "sessionid", "JSESSIONID",
    "password", "passwd", "client_secret", "api_key", "apikey", "X-Amz-Security-Token", "X-Amz-Signature",
    "sig", "key", "sid", "jwt", "Authorization", "csrf_token", "assertion", "otp",
])
def test_a_parameter_named_like_a_credential_loses_its_value_but_keeps_its_name(name):
    assert redact_url(f"https://idp.example/cb?{name}=s3cr3t&ok=1") == f"https://idp.example/cb?{name}={REDACTED}&ok=1"


def test_a_value_that_looks_like_a_random_token_is_redacted_whatever_its_name():
    url = "https://example.com/x?next=eyJhbGciOiJIUzI1NiJ9abc123&n=1&hex=9f86d081884c7d659a2feaa0c55ad015"
    assert redact_url(url) == f"https://example.com/x?next={REDACTED}&n=1&hex={REDACTED}"


def test_a_value_of_percent_encoded_words_is_not_taken_for_a_token():
    assert redact_url(STAGING_URL) == STAGING_URL  # the tag list is 79 characters with eight %2C in it


def test_a_bare_token_in_the_query_is_redacted():
    assert redact_url("https://example.com/x?eyJhbGciOiJIUzI1NiJ9abc123") == f"https://example.com/x?{REDACTED}"


def test_credentials_in_the_fragment_are_redacted_as_well():
    assert redact_url("https://app.example/cb#access_token=abc&token_type=bearer&expires_in=3600") == (
        f"https://app.example/cb#access_token={REDACTED}&token_type={REDACTED}&expires_in=3600")
    assert redact_url("https://app.example/#/dashboard?token=abc") == f"https://app.example/#/dashboard?token={REDACTED}"
    assert redact_url("https://app.example/#eyJhbGciOiJIUzI1NiJ9abc123") == f"https://app.example/#{REDACTED}"
    assert redact_url("https://app.example/page#section-2") == "https://app.example/page#section-2"


@pytest.mark.parametrize("url, expected", [
    ("https://user:pass@host.example/path", "https://[userinfo]@host.example/path"),
    ("https://user@host.example:8443/", "https://[userinfo]@host.example:8443/"),
    ("https://a:b@c@host.example/", "https://[userinfo]@host.example/"),
    ("https://user:pass@host.example?code=abc", f"https://[userinfo]@host.example?code={REDACTED}"),
])
def test_user_and_password_in_a_url_are_removed(url, expected):
    assert redact_url(url) == expected


def test_a_url_inside_a_sentence_keeps_the_text_around_it():
    text = "navigated: https://idp.example/cb?code=abc123xyz&state=ok. Then (https://b.example/?token=t) failed, https://c.example/."
    assert redact_urls_in_text(text) == (
        f"navigated: https://idp.example/cb?code={REDACTED}&state=ok. Then (https://b.example/?token={REDACTED}) failed, https://c.example/.")


def test_text_without_a_web_url_is_returned_unchanged():
    for text in ("", "plain words", "code=abc123 is not a url", "ftp://host/x?token=abc", "a://b", None, 42):
        assert redact_urls_in_text(text) == text


def test_every_string_in_a_structure_is_redacted_and_the_rest_is_kept():
    value = {"url": "https://x.example/?code=abc", "n": 3, "ok": True, "none": None,
             "list": ["https://y.example/#access_token=t", 7], "deep": {"t": ("https://z.example/?sid=1",)}}
    assert redact(value) == {"url": f"https://x.example/?code={REDACTED}", "n": 3, "ok": True, "none": None,
                             "list": [f"https://y.example/#access_token={REDACTED}", 7],
                             "deep": {"t": [f"https://z.example/?sid={REDACTED}"]}}
    assert redact({"https://k.example/?code=abc": 1}) == {"https://k.example/?code=abc": 1}  # keys are left alone


def test_redacting_twice_changes_nothing():
    text = "https://u:p@x.example/a?code=abc&b=1#access_token=t and https://y.example/?next=eyJhbGciOiJIUzI1NiJ9abc123"
    once = redact_urls_in_text(text)
    assert redact_urls_in_text(once) == once


def test_a_self_referencing_value_does_not_recurse_forever():
    loop = {}
    loop["me"] = loop
    redact(loop)  # returns; the trace writer then drops the record when it can't serialise it


def test_a_huge_string_is_handled_quickly_and_one_beyond_the_limit_is_left_alone():
    started = time.monotonic()
    redact_urls_in_text("http://" * 20_000 + "?code=abc")
    redact_urls_in_text("a=b&" * 40_000)
    redact_urls_in_text(("https://x.example/?q=" + "a" * 100 + " ") * 1500)
    assert time.monotonic() - started < 2
    huge = "https://x.example/?code=abc " + "y" * MAX_STRING_CHARS
    assert redact_urls_in_text(huge) == huge
