"""Unit tests for knarr/mcp/limits.py — what a tool call may carry."""

from __future__ import annotations

import pytest

from knarr.mcp.limits import (
    MAX_CRITERION_CHARS,
    MAX_FILL_VALUE_CHARS,
    MAX_GOAL_CHARS,
    MAX_INDEX,
    MAX_LABEL_CHARS,
    MAX_TEXT_CHARS,
    MAX_URL_CHARS,
    TEXT_LIMITS,
    check_session_start,
    check_tool_arguments,
)

CASES = [(tool, name, limit) for tool, names in TEXT_LIMITS.items() for name, limit in names.items()]


@pytest.mark.parametrize("tool, name, limit", CASES)
def test_text_at_the_limit_is_accepted_and_one_more_is_refused(tool, name, limit):
    assert check_tool_arguments(tool, {name: "a" * limit}) is None
    refused = check_tool_arguments(tool, {name: "a" * (limit + 1)})
    assert refused == f"{name} is {limit + 1} characters; the limit is {limit}. Shorten it."


@pytest.mark.parametrize("tool, name, limit", CASES)
def test_a_nul_character_is_refused(tool, name, limit):
    assert check_tool_arguments(tool, {name: "ab\x00cd"}) == f"{name} contains a NUL character, which is not allowed."


def test_every_tool_that_takes_text_has_a_limit():
    assert set(TEXT_LIMITS) == {"click", "fill", "navigate", "wait_for", "inspect_error", "note", "give_up"}
    assert TEXT_LIMITS["fill"]["value"] == MAX_FILL_VALUE_CHARS > MAX_TEXT_CHARS > MAX_LABEL_CHARS
    assert MAX_URL_CHARS == 2048  # the same cap as an allow pattern


@pytest.mark.parametrize("index, ok", [(0, True), (7, True), (MAX_INDEX, True), (-1, False), (MAX_INDEX + 1, False),
                                       (10**12, False)])
def test_an_index_must_be_in_range(index, ok):
    refused = check_tool_arguments("click_index", {"index": index})
    assert (refused is None) is ok
    if not ok:
        assert f"index is {index}" in refused


@pytest.mark.parametrize("arguments", [{}, {"fill": None}, {"value": 12345}, {"value": ["a" * 99999]}])
def test_a_missing_or_wrongly_typed_argument_is_not_this_modules_concern(arguments):
    assert check_tool_arguments("fill", arguments) is None


def test_tools_without_text_arguments_and_unknown_tools_pass():
    for tool in ("read_page", "submit", "screenshot", "assert_state", "no_such_tool"):
        assert check_tool_arguments(tool, {"anything": "x" * 10**6}) is None


def test_session_start_arguments_have_limits():
    assert check_session_start("https://a.example/", "goal", "the page shows 'x'") is None
    assert check_session_start("https://a.example/" + "x" * MAX_URL_CHARS, "g", "c").startswith("start_url is")
    assert check_session_start("https://a.example/", "g" * (MAX_GOAL_CHARS + 1), "c").startswith("goal is")
    assert check_session_start("https://a.example/", "g", "c" * (MAX_CRITERION_CHARS + 1)).startswith("success_criterion is")
    assert check_session_start("https://a.example/", "g", "c\x00").startswith("success_criterion contains a NUL")
    assert check_session_start(None, None, None) is None  # the missing criterion is reported by the session
