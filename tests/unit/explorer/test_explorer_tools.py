"""Unit tests for theseus/explorer/explorer_tools.py.

Every tool is tested against a MagicMock page so the suite runs
without Playwright. The URL-boundary and forbidden-action guards are
also unit-tested in isolation — they're the load-bearing safety
machinery the action tools plug into."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest.mock import MagicMock

import pytest

from theseus.explorer.explorer_tools import (
    ExplorerContext,
    ForbiddenActionGuard,
    ForbiddenEntry,
    PageSummary,
    URLBoundaryGuard,
    _compile_boundary_pattern,
)
from theseus.explorer.trace import TraceWriter

from tests.unit.explorer.palette import build_palette


# ============================================================================
# Fixture builder
# ============================================================================

def _make_page(
    *,
    url: str = "https://app.example.com/applications/10001",
    title: str = "Example App",
    buttons: List[str] = None,
    inputs: List[str] = None,
    headings: List[str] = None,
    modal_text: str = None,
):
    page = MagicMock()
    page.url = url
    page.title.return_value = title
    page._theseus_visible_buttons = list(buttons or [])
    page._theseus_visible_input_labels = list(inputs or [])
    page._theseus_visible_headings = list(headings or [])
    page._theseus_modal_text = modal_text
    page.goto.return_value = None
    page.screenshot.return_value = None
    return page


def _make_context(
    tmp_path: Path,
    *,
    boundary_patterns=None,
    forbidden_entries=None,
    scaffold_mode: bool = False,
    read_only: bool = False,
    page=None,
):
    if boundary_patterns is None:
        boundary_patterns = [
            "https://app.example.com/applications/*",
            "https://app.example.com/applications/*/offers",
            "https://app.example.com/applications/*/offers/**",
        ]
    if forbidden_entries is None:
        forbidden_entries = []
    trace = TraceWriter(tmp_path)
    return ExplorerContext(
        page=page or _make_page(),
        run_dir=tmp_path,
        trace=trace,
        boundary_guard=URLBoundaryGuard(boundary_patterns),
        forbidden_guard=ForbiddenActionGuard(forbidden_entries),
        scaffold_mode=scaffold_mode,
        read_only=read_only,
    )


def _tool(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not in {[t.name for t in tools]}")


# ============================================================================
# Tool palette — shape
# ============================================================================

def test_explore_palette_has_twelve_tools(tmp_path: Path):
    ctx = _make_context(tmp_path, scaffold_mode=False)
    tools = build_palette(ctx)
    names = sorted(t.name for t in tools)
    assert names == [
        "assert_state",
        "click",
        "click_index",
        "fill",
        "give_up",
        "inspect_error",
        "navigate",
        "note",
        "read_page",
        "screenshot",
        "submit",
        "wait_for",
    ]


def test_scaffold_palette_has_fourteen_tools(tmp_path: Path):
    ctx = _make_context(tmp_path, scaffold_mode=True)
    tools = build_palette(ctx)
    names = sorted(t.name for t in tools)
    assert names == [
        "assert_state",
        "click",
        "click_index",
        "fill",
        "give_up",
        "inspect_error",
        "navigate",
        "note",
        "read_page",
        "record_intent",
        "record_return",
        "screenshot",
        "submit",
        "wait_for",
    ]


# ============================================================================
# navigate
# ============================================================================

def test_navigate_within_boundary_calls_goto(tmp_path: Path):
    page = _make_page()
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "navigate").run(
        {"url": "https://app.example.com/applications/10001/offers"}
    )
    assert result.startswith("navigated:")
    page.goto.assert_called_once_with(
        "https://app.example.com/applications/10001/offers"
    )
    assert ctx.terminal_reason is None


def test_navigate_out_of_boundary_rejects_without_goto(tmp_path: Path):
    page = _make_page()
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "navigate").run({"url": "https://evil.example.com/"})
    assert result.startswith("rejected:")
    page.goto.assert_not_called()
    assert ctx.boundary_guard.violations == 1
    assert ctx.terminal_reason is None  # 1 strike, not 3


def test_navigate_three_strikes_signals_terminal(tmp_path: Path):
    ctx = _make_context(tmp_path)
    tools = build_palette(ctx)
    nav = _tool(tools, "navigate")
    nav.run({"url": "https://evil.example.com/"})
    nav.run({"url": "https://evil.example.com/x"})
    assert ctx.terminal_reason is None
    nav.run({"url": "https://evil.example.com/y"})
    assert ctx.terminal_reason == "boundary_violations_exceeded"


def test_navigate_records_trace_entry_on_success(tmp_path: Path):
    ctx = _make_context(tmp_path)
    tools = build_palette(ctx)
    _tool(tools, "navigate").run(
        {"url": "https://app.example.com/applications/10001/offers"}
    )
    raw = ctx.trace.path.read_text().strip()
    assert "navigate" in raw
    assert "offers" in raw


def test_navigate_records_trace_entry_on_rejection(tmp_path: Path):
    ctx = _make_context(tmp_path)
    tools = build_palette(ctx)
    _tool(tools, "navigate").run({"url": "https://evil.example.com/"})
    raw = ctx.trace.path.read_text().strip()
    assert "out_of_boundary" in raw


def test_navigate_handles_goto_exception(tmp_path: Path):
    page = _make_page()
    page.goto.side_effect = RuntimeError("page closed")
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "navigate").run(
        {"url": "https://app.example.com/applications/10001"}
    )
    assert result.startswith("navigation_failed:")
    assert "page closed" in result


# ============================================================================
# read_page
# ============================================================================

def test_read_page_returns_url_and_title(tmp_path: Path):
    page = _make_page(
        url="https://app.example.com/applications/10001/offers",
        title="Offers — Example App",
        buttons=["Create Offer", "Filter"],
        inputs=["Search offers"],
        headings=["Offers"],
    )
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    rendered = _tool(tools, "read_page").run({})
    assert "url: https://app.example.com/applications/10001/offers" in rendered
    assert "Offers — Example App" in rendered
    assert "Create Offer" in rendered
    assert "Search offers" in rendered


def test_read_page_truncates_to_budget(tmp_path: Path):
    # Pile up enough buttons to bust the 10KB budget if not truncated.
    page = _make_page(buttons=[f"btn-{i}-" + "x" * 200 for i in range(80)])
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    rendered = _tool(tools, "read_page").run({})
    # Either the cap kicks in (the render itself includes "truncated")
    # OR the natural rendering happens to fit (then nothing to assert).
    # We assert only the upper bound.
    assert len(rendered) <= 10_064  # budget + small slack


def test_read_page_records_trace_entry(tmp_path: Path):
    ctx = _make_context(tmp_path)
    tools = build_palette(ctx)
    _tool(tools, "read_page").run({})
    raw = ctx.trace.path.read_text().strip()
    assert "read_page" in raw


def test_read_page_trace_includes_extracted_state_samples(tmp_path: Path):
    """A diagnosis was once blocked by trace.jsonl showing only
    success + char_count for read_page — no visibility into what
    extraction actually produced. The trace record must now include
    small samples of buttons / links / headings / open_menus /
    field_errors + a modal_present flag, so post-hoc diagnosis doesn't
    require unzipping trace.zip and DOM-diving."""
    import json

    page = _make_page(
        buttons=["Save", "Cancel"],
        inputs=["Project Name"],
        headings=["Basic"],
    )
    page._theseus_visible_links = ["Offers"]
    page._theseus_open_menus = ["New Project", "Project from Template"]
    page._theseus_field_errors = [
        "Category cannot be empty",
        "Basic has validation errors",
    ]
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    _tool(tools, "read_page").run({})

    lines = ctx.trace.path.read_text().splitlines()
    read_page_records = [
        r for r in (json.loads(l) for l in lines) if r["tool"] == "read_page"
    ]
    assert len(read_page_records) == 1
    result = read_page_records[0]["result"]
    assert result["success"] is True
    assert isinstance(result["char_count"], int)
    assert result["buttons_sample"] == ["Save", "Cancel"]
    assert result["links_sample"] == ["Offers"]
    assert result["headings_sample"] == ["Basic"]
    assert result["open_menus_sample"] == ["New Project", "Project from Template"]
    assert result["field_errors_sample"] == [
        "Category cannot be empty",
        "Basic has validation errors",
    ]
    assert result["modal_present"] is False


def test_read_page_trace_caps_sample_lengths(tmp_path: Path):
    """Samples in the trace are capped at 8 items to keep trace.jsonl
    cheap while still visualizing what Claude saw. The full state is
    always in the returned string; the trace samples are a compact
    forensic aid."""
    import json

    many = [f"btn-{i}" for i in range(20)]
    page = _make_page(buttons=many)
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    _tool(tools, "read_page").run({})
    r = json.loads(ctx.trace.path.read_text().splitlines()[0])
    assert len(r["result"]["buttons_sample"]) == 8
    assert r["result"]["buttons_sample"][0] == "btn-0"
    assert r["result"]["buttons_sample"][-1] == "btn-7"


# ============================================================================
# screenshot
# ============================================================================

def test_screenshot_writes_to_run_dir_and_records_filename(tmp_path: Path):
    page = _make_page()
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "screenshot").run({})
    assert result.endswith(".png")
    # Filename must include the turn + counter zero-pad shape.
    assert result.startswith("000-001.png")
    # page.screenshot was called with the right kwargs.
    args = page.screenshot.call_args.kwargs
    assert args["path"].endswith(result)
    assert args["full_page"] is False  # the visible window: a whole page of a long site is too tall to read


def test_screenshot_increments_counter_within_turn(tmp_path: Path):
    page = _make_page()
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    sc = _tool(tools, "screenshot")
    first = sc.run({})
    second = sc.run({})
    assert first != second
    assert "001" in first
    assert "002" in second


def test_screenshot_handles_page_exception(tmp_path: Path):
    page = _make_page()
    page.screenshot.side_effect = RuntimeError("page gone")
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "screenshot").run({})
    assert result.startswith("screenshot_failed:")
    assert "page gone" in result


# ============================================================================
# note
# ============================================================================

def test_note_writes_to_trace_and_returns_noted(tmp_path: Path):
    ctx = _make_context(tmp_path)
    tools = build_palette(ctx)
    result = _tool(tools, "note").run({"text": "The app renders this in a portal"})
    assert result == "noted"
    raw = ctx.trace.path.read_text().strip()
    assert "portal" in raw


# ============================================================================
# scaffold-only annotators
# ============================================================================

def test_record_intent_writes_annotation_and_args(tmp_path: Path):
    ctx = _make_context(tmp_path, scaffold_mode=True)
    tools = build_palette(ctx)
    _tool(tools, "record_intent").run(
        {
            "method_param": "name",
            "value": "theseus-offer-1",
            "source": "input labeled 'Offer name'",
        }
    )
    raw = ctx.trace.path.read_text().strip()
    assert "record_intent" in raw
    assert "Offer name" in raw
    assert '"kind": "intent"' in raw


def test_record_return_writes_annotation_and_args(tmp_path: Path):
    ctx = _make_context(tmp_path, scaffold_mode=True)
    tools = build_palette(ctx)
    _tool(tools, "record_return").run(
        {
            "value_description": "list of offer IDs",
            "evidence": "DOM role=row, data-offer-id",
        }
    )
    raw = ctx.trace.path.read_text().strip()
    assert '"kind": "return"' in raw


def test_scaffold_annotators_unavailable_in_explore_mode(tmp_path: Path):
    ctx = _make_context(tmp_path, scaffold_mode=False)
    tools = build_palette(ctx)
    names = [t.name for t in tools]
    assert "record_intent" not in names
    assert "record_return" not in names


# ============================================================================
# URLBoundaryGuard
# ============================================================================

def test_boundary_singlestar_matches_one_segment():
    pat = _compile_boundary_pattern("https://app.example.com/applications/*")
    assert pat.match("https://app.example.com/applications/10001")
    # Single * does NOT cross a slash:
    assert pat.match("https://app.example.com/applications/10001/offers") is None


def test_boundary_doublestar_matches_multiple_segments():
    pat = _compile_boundary_pattern(
        "https://app.example.com/applications/*/offers/**"
    )
    assert pat.match("https://app.example.com/applications/10001/offers/123")
    assert pat.match(
        "https://app.example.com/applications/10001/offers/123/edit/v2"
    )


def test_boundary_does_not_match_different_host():
    pat = _compile_boundary_pattern("https://app.example.com/applications/*")
    assert pat.match("https://evil.example.com/applications/10001") is None


def test_boundary_guard_allows_returns_true_for_match():
    g = URLBoundaryGuard(
        ["https://app.example.com/applications/*/offers"]
    )
    assert g.allows("https://app.example.com/applications/10001/offers") is True


def test_boundary_guard_three_strikes():
    g = URLBoundaryGuard(["https://app.example.com/applications/*"])
    assert g.record_violation("a") is False
    assert g.record_violation("b") is False
    assert g.record_violation("c") is True  # crossed limit
    assert g.violations == 3
    assert g.violation_log == ["a", "b", "c"]


# ============================================================================
# ForbiddenActionGuard
# ============================================================================

def test_forbidden_selector_denies_exact_match():
    g = ForbiddenActionGuard(
        [ForbiddenEntry(kind="selector", value="[data-test=delete]")]
    )
    assert g.check_selector("[data-test=delete]") is False
    assert g.check_selector("[data-test=save]") is True


def test_forbidden_text_match_is_case_insensitive_substring():
    g = ForbiddenActionGuard(
        [ForbiddenEntry(kind="text_match", value="Delete")]
    )
    assert g.check_text_match("Delete offer") is False
    assert g.check_text_match("DELETE everything") is False
    assert g.check_text_match("Save") is True
    # Empty target is allowed (no risk of accidental match).
    assert g.check_text_match("") is True


def test_forbidden_three_strikes():
    g = ForbiddenActionGuard(
        [ForbiddenEntry(kind="text_match", value="bad")]
    )
    assert g.record_violation("text_match", "bad1") is False
    assert g.record_violation("text_match", "bad2") is False
    assert g.record_violation("text_match", "bad3") is True


# ============================================================================
# Context internals
# ============================================================================

def test_signal_terminal_is_first_write_wins(tmp_path: Path):
    ctx = _make_context(tmp_path)
    ctx.signal_terminal("boundary_violations_exceeded")
    ctx.signal_terminal("forbidden_action_violations_exceeded")
    assert ctx.terminal_reason == "boundary_violations_exceeded"


def test_now_ms_is_monotonic(tmp_path: Path):
    ctx = _make_context(tmp_path)
    first = ctx.now_ms()
    second = ctx.now_ms()
    assert second >= first
    assert first >= 0


# ============================================================================
# Action tools — click / fill / submit / wait_for / assert_state / give_up.
# Tests register ``_theseus_*`` methods on the MagicMock page so the tool
# bodies exercise their happy + sad paths without Playwright.
# ============================================================================

def _attach_clicker(page, *, success: bool = True, fallback_used: bool = False):
    """Install a deterministic _theseus_click on the test page."""
    def _click(target):
        return {"success": success, "fallback_used": fallback_used, "target": target}
    page._theseus_click = _click


def _attach_filler(page, *, success: bool = True):
    def _fill(label, value):
        return {"success": success, "label": label, "value": value}
    page._theseus_fill = _fill


def _attach_submitter(page, *, success: bool = True, verb: str = "Save"):
    def _submit():
        if success:
            return {"success": True, "verb": verb}
        return {"success": False, "error": "no submit button found"}
    page._theseus_submit = _submit


def _attach_waiter(page, *, success: bool = True, matched_at_ms: int = 250):
    def _wait(description):
        return {
            "success": success,
            "matched_at_ms": matched_at_ms if success else 0,
            "error": "" if success else f"timeout waiting for {description!r}",
        }
    page._theseus_wait_for = _wait


def _attach_asserter(page, *, matches: bool = True, evidence: str = "ok"):
    def _assert(description):
        return {"matches": matches, "evidence": evidence}
    page._theseus_assert_state = _assert


# ----------------------------------------------------------------------------
# click
# ----------------------------------------------------------------------------

def test_click_happy_path_records_trace_and_returns_clicked(tmp_path: Path):
    page = _make_page()
    _attach_clicker(page, success=True)
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "click").run({"target": "Save offer"})
    assert result.startswith("clicked")
    assert "Save offer" in ctx.trace.path.read_text()


def test_click_records_fallback_used_flag(tmp_path: Path):
    page = _make_page()
    _attach_clicker(page, success=True, fallback_used=True)
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    _tool(tools, "click").run({"target": "Save"})
    assert '"fallback_used": true' in ctx.trace.path.read_text()


def test_click_failure_returns_click_failed(tmp_path: Path):
    page = _make_page()
    def _broken(target):
        return {"success": False, "error": "element not found"}
    page._theseus_click = _broken
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "click").run({"target": "ghost"})
    assert result.startswith("click_failed:")
    assert "element not found" in result


def test_click_denial_by_text_match(tmp_path: Path):
    page = _make_page()
    _attach_clicker(page)
    ctx = _make_context(
        tmp_path,
        page=page,
        forbidden_entries=[ForbiddenEntry(kind="text_match", value="Delete")],
    )
    tools = build_palette(ctx)
    result = _tool(tools, "click").run({"target": "Delete offer"})
    assert result.startswith("rejected: forbidden_text_match")
    assert ctx.forbidden_guard.violations == 1
    # Make sure the page was never asked to click.
    raw = ctx.trace.path.read_text()
    assert "forbidden_text_match" in raw


def test_click_three_forbidden_strikes_signals_terminal(tmp_path: Path):
    page = _make_page()
    _attach_clicker(page)
    ctx = _make_context(
        tmp_path,
        page=page,
        forbidden_entries=[ForbiddenEntry(kind="text_match", value="bad")],
    )
    tools = build_palette(ctx)
    click = _tool(tools, "click")
    click.run({"target": "bad-1"})
    click.run({"target": "bad-2"})
    assert ctx.terminal_reason is None
    click.run({"target": "bad-3"})
    assert ctx.terminal_reason == "forbidden_action_violations_exceeded"


def test_click_read_only_simulates(tmp_path: Path):
    page = _make_page()
    real_click_called = []
    def _real(target):
        real_click_called.append(target)
        return {"success": True}
    page._theseus_click = _real
    ctx = _make_context(tmp_path, page=page, read_only=True)
    tools = build_palette(ctx)
    result = _tool(tools, "click").run({"target": "Save offer"})
    assert result.startswith("simulated:")
    assert real_click_called == [], "read-only must not invoke the real page"
    raw = ctx.trace.path.read_text()
    assert '"simulated": true' in raw
    assert "Save offer" in raw


# ----------------------------------------------------------------------------
# fill
# ----------------------------------------------------------------------------

def test_fill_happy_path(tmp_path: Path):
    page = _make_page()
    _attach_filler(page)
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "fill").run({"label": "Offer name", "value": "theseus-1"})
    assert result.startswith("filled: Offer name")
    raw = ctx.trace.path.read_text()
    assert "Offer name" in raw
    assert "theseus-1" not in raw  # what was typed stays out of the file (see the redaction tests below)


def test_fill_read_only_simulates(tmp_path: Path):
    page = _make_page()
    _attach_filler(page)
    ctx = _make_context(tmp_path, page=page, read_only=True)
    tools = build_palette(ctx)
    result = _tool(tools, "fill").run({"label": "Offer name", "value": "x"})
    assert result.startswith("simulated:")


def test_fill_denial_by_label_text_match(tmp_path: Path):
    page = _make_page()
    _attach_filler(page)
    ctx = _make_context(
        tmp_path,
        page=page,
        forbidden_entries=[ForbiddenEntry(kind="text_match", value="archive")],
    )
    tools = build_palette(ctx)
    result = _tool(tools, "fill").run(
        {"label": "Archive flag", "value": "yes"}
    )
    assert result.startswith("rejected: forbidden_text_match")


# What was typed into a field may be a password or personal data: the trace is a file on disk
# that outlives the session, so it records the label and the length, never the value.

SECRET = "hunter2-correct-horse"


def _fill_trace(ctx):
    import json

    return [json.loads(line) for line in ctx.trace.path.read_text().splitlines() if line]


def test_a_filled_value_is_not_written_to_the_trace_only_its_length(tmp_path: Path):
    page = _make_page()
    _attach_filler(page)
    ctx = _make_context(tmp_path, page=page)
    result = _tool(build_palette(ctx), "fill").run({"label": "Password", "value": SECRET})
    assert result.startswith("filled: Password")
    assert SECRET not in ctx.trace.path.read_text()
    (record,) = _fill_trace(ctx)
    assert record["args"] == {"label": "Password", "value_chars": len(SECRET)}


def test_a_read_only_fill_does_not_write_the_value_to_the_trace_either(tmp_path: Path):
    page = _make_page()
    _attach_filler(page)
    ctx = _make_context(tmp_path, page=page, read_only=True)
    result = _tool(build_palette(ctx), "fill").run({"label": "Password", "value": SECRET})
    assert result.startswith("simulated:")  # the model typed it, so its own result may repeat it
    assert SECRET not in ctx.trace.path.read_text()
    (record,) = _fill_trace(ctx)
    assert record["args"] == {"label": "Password", "value_chars": len(SECRET)}
    assert record["result"]["would_have"] == {"label": "Password", "value_chars": len(SECRET)}


def test_a_refused_fill_does_not_write_the_value_to_the_trace(tmp_path: Path):
    page = _make_page()
    _attach_filler(page)
    ctx = _make_context(tmp_path, page=page, forbidden_entries=[ForbiddenEntry(kind="text_match", value="archive")])
    result = _tool(build_palette(ctx), "fill").run({"label": "Archive flag", "value": SECRET})
    assert result.startswith("rejected: forbidden_text_match")
    assert SECRET not in ctx.trace.path.read_text()


def test_a_value_repeated_in_an_error_is_taken_out_of_the_trace(tmp_path: Path):
    page = _make_page()

    def _failing_fill(label, value):  # Playwright's call log repeats the value it was asked to type
        return {"success": False, "error": f'TimeoutError: locator.fill: Timeout 30000ms exceeded.\n  - fill("{value}")'}

    page._theseus_fill = _failing_fill
    ctx = _make_context(tmp_path, page=page)
    result = _tool(build_palette(ctx), "fill").run({"label": "Password", "value": SECRET})
    assert result.startswith("fill_failed")
    assert SECRET not in ctx.trace.path.read_text()
    (record,) = _fill_trace(ctx)
    assert 'fill("[value]")' in record["result"]["error"] and record["result"]["success"] is False


def test_a_very_short_value_is_not_cut_out_of_an_error(tmp_path: Path):
    page = _make_page()
    page._theseus_fill = lambda label, value: {"success": False, "error": "TimeoutError: element is not an input"}
    ctx = _make_context(tmp_path, page=page)
    _tool(build_palette(ctx), "fill").run({"label": "Code", "value": "in"})  # "in" would mangle "input"
    (record,) = _fill_trace(ctx)
    assert record["result"]["error"] == "TimeoutError: element is not an input"
    assert record["args"] == {"label": "Code", "value_chars": 2}


def test_other_tools_are_recorded_as_before(tmp_path: Path):
    page = _make_page()
    _attach_clicker(page)
    ctx = _make_context(tmp_path, page=page)
    _tool(build_palette(ctx), "click").run({"target": "Save"})
    (record,) = _fill_trace(ctx)
    assert record["args"] == {"target": "Save"}


# ----------------------------------------------------------------------------
# submit
# ----------------------------------------------------------------------------

def test_submit_records_verb_on_success(tmp_path: Path):
    page = _make_page()
    _attach_submitter(page, success=True, verb="Create")
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "submit").run({})
    assert result.startswith("submitted")
    assert "Create" in result
    assert '"verb": "Create"' in ctx.trace.path.read_text()


def test_submit_failure_returns_submit_failed(tmp_path: Path):
    page = _make_page()
    _attach_submitter(page, success=False)
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "submit").run({})
    assert result.startswith("submit_failed:")


def test_submit_read_only_simulates(tmp_path: Path):
    page = _make_page()
    _attach_submitter(page)
    ctx = _make_context(tmp_path, page=page, read_only=True)
    tools = build_palette(ctx)
    result = _tool(tools, "submit").run({})
    assert result.startswith("simulated:")
    raw = ctx.trace.path.read_text()
    assert "verbs_tried" in raw  # the simulator records the would-be verb list


# ----------------------------------------------------------------------------
# wait_for
# ----------------------------------------------------------------------------

def test_wait_for_success(tmp_path: Path):
    page = _make_page()
    _attach_waiter(page, success=True, matched_at_ms=300)
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "wait_for").run({"description": "offers list visible"})
    assert "matched" in result
    assert "300ms" in result


def test_wait_for_timeout_records_error(tmp_path: Path):
    page = _make_page()
    _attach_waiter(page, success=False)
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "wait_for").run({"description": "never_appears"})
    assert "timeout" in result
    raw = ctx.trace.path.read_text()
    assert "error" in raw


# ----------------------------------------------------------------------------
# assert_state
# ----------------------------------------------------------------------------

def test_assert_state_matches_signals_terminal_success(tmp_path: Path):
    page = _make_page()
    _attach_asserter(page, matches=True, evidence="found in /offers list")
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "assert_state").run(
        {"description": "offer named theseus-1 in list"}
    )
    assert result.startswith("matches:")
    assert ctx.success_asserted is True
    assert ctx.terminal_reason == "success_asserted"


def test_assert_state_not_matched_does_not_latch_terminal(tmp_path: Path):
    page = _make_page()
    _attach_asserter(page, matches=False, evidence="not visible on page")
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    result = _tool(tools, "assert_state").run({"description": "x"})
    assert result.startswith("not_matched:")
    assert ctx.success_asserted is False
    assert ctx.terminal_reason is None


# ----------------------------------------------------------------------------
# give_up
# ----------------------------------------------------------------------------

def test_give_up_latches_terminal_with_reason(tmp_path: Path):
    ctx = _make_context(tmp_path)
    tools = build_palette(ctx)
    result = _tool(tools, "give_up").run({"reason": "stuck in modal"})
    assert result.startswith("giving_up:")
    assert "stuck in modal" in ctx.terminal_reason
    raw = ctx.trace.path.read_text()
    assert "give_up" in raw


def test_give_up_with_empty_reason_still_latches(tmp_path: Path):
    ctx = _make_context(tmp_path)
    tools = build_palette(ctx)
    _tool(tools, "give_up").run({"reason": ""})
    assert ctx.terminal_reason is not None
    assert "no reason" in ctx.terminal_reason


# ============================================================================
# Production-path adapters.
# Tests register no _theseus_* methods so the click/wait/assert fallbacks
# run against duck-typed Playwright-shaped pages.
# ============================================================================

def _make_playwright_shaped_page(
    *,
    body_text: str = "",
    raise_on_text_locator: bool = False,
):
    """Build a page that mimics enough of Playwright's surface to
    exercise the production action adapters without ``_theseus_*``
    overrides — using ``spec_set`` so ``hasattr(page, '_theseus_click')``
    returns False (otherwise the production fallback never runs)."""

    class _PlaywrightLike:
        """Spec class — explicitly enumerates Playwright surface
        Theseus's adapters touch. Anything outside this list errors
        on access (so an accidental ``page._theseus_click`` raises
        ``AttributeError`` and the production path runs)."""

        url = ""

        def title(self): ...
        def locator(self, selector): ...
        def get_by_text(self, target, exact=False): ...
        def wait_for_load_state(self, state, timeout=None): ...
        def screenshot(self, path=None, full_page=False): ...
        def goto(self, url): ...
        def get_by_label(self, label, exact=False): ...
        def get_by_role(self, role, name=None): ...

    page = MagicMock(spec=_PlaywrightLike)
    page.url = "https://app.example.com/applications/10001"
    page.title.return_value = "Example App"

    body = MagicMock()
    body.inner_text.return_value = body_text

    def _locator(selector):
        if selector == "body":
            return body
        return MagicMock(name=f"locator({selector})")

    page.locator.side_effect = _locator
    return page


# ----------------------------------------------------------------------------
# _click_target — fuzzy chain
# ----------------------------------------------------------------------------

def test_click_target_uses_first_matching_selector(tmp_path: Path, monkeypatch):
    """The first selector in the chain wins → fallback_used=False."""
    from theseus.explorer import explorer_tools as et

    calls = {"args": None}
    def _fake_find(page, selectors, timeout=2500, run_logger=None):
        calls["args"] = (page, selectors, timeout)
        # First selector wins (the data-testid one).
        elem = MagicMock(name="elem")
        return elem, selectors[0]
    monkeypatch.setattr(
        "theseus.explorer.explorer_tools._find_first_visible",
        _fake_find,
    )
    page = _make_playwright_shaped_page()
    result = et._click_target(page, "Save offer")
    assert result["success"] is True
    assert result["fallback_used"] is False
    assert result["selector"] == calls["args"][1][0]


def test_click_target_records_fallback_when_chain_winner_isnt_first(
    tmp_path: Path, monkeypatch
):
    from theseus.explorer import explorer_tools as et

    def _fake_find(page, selectors, **_kwargs):
        # Second-from-last selector wins.
        winner = selectors[-2]
        return MagicMock(name="elem"), winner
    monkeypatch.setattr(
        "theseus.explorer.explorer_tools._find_first_visible",
        _fake_find,
    )
    page = _make_playwright_shaped_page()
    result = et._click_target(page, "Create Offer")
    assert result["fallback_used"] is True


def test_click_target_falls_through_to_get_by_text(tmp_path: Path, monkeypatch):
    """When no selector in the chain matches, the adapter falls
    through to Playwright's text engine and reports fallback_used=True."""
    from theseus.explorer import explorer_tools as et

    monkeypatch.setattr(
        "theseus.explorer.explorer_tools._find_first_visible",
        lambda page, selectors, **_: (None, None),
    )
    page = _make_playwright_shaped_page()
    page.get_by_text.return_value.first.click = MagicMock()
    result = et._click_target(page, "Some text target")
    assert result["success"] is True
    assert result["fallback_used"] is True
    assert "text~" in result["selector"]
    page.get_by_text.assert_called_once()


def test_click_target_returns_error_on_exception(tmp_path: Path, monkeypatch):
    from theseus.explorer import explorer_tools as et

    def _boom(page, selectors, **_kwargs):
        raise RuntimeError("connection lost")
    monkeypatch.setattr(
        "theseus.explorer.explorer_tools._find_first_visible",
        _boom,
    )
    page = _make_playwright_shaped_page()
    result = et._click_target(page, "anything")
    assert result["success"] is False
    assert "connection lost" in result["error"]


@pytest.mark.parametrize("target", ["", "   ", "\t\n", None])
def test_click_target_refuses_empty_target(target, monkeypatch):
    """An empty target would match every element via :has-text("") and
    get_by_text(""), so it must fail without touching the page."""
    from theseus.explorer import explorer_tools as et

    finder = MagicMock(side_effect=AssertionError("chain must not run"))
    monkeypatch.setattr(et, "_find_first_visible", finder)
    page = _make_playwright_shaped_page()
    result = et._click_target(page, target)
    assert result == {"success": False, "error": "empty click target"}
    finder.assert_not_called()
    page.get_by_text.assert_not_called()


# ----------------------------------------------------------------------------
# _find_first_visible — the selector chain walker
# ----------------------------------------------------------------------------

class _ChainPage:
    """Page whose locators report visibility from a per-selector script.

    ``visible`` maps selector -> list of booleans, one per poll (the last
    value repeats). ``broken`` selectors raise like a malformed selector.
    """

    def __init__(self, visible=None, broken=()):
        self.visible = dict(visible or {})
        self.broken = set(broken)
        self.polls = {}

    def locator(self, selector):
        if selector in self.broken:
            raise ValueError(f"malformed selector: {selector}")
        page = self

        class _First:
            def is_visible(self_inner):
                n = page.polls.get(selector, 0)
                page.polls[selector] = n + 1
                script = page.visible.get(selector, [False])
                return script[min(n, len(script) - 1)]

        loc = MagicMock()
        loc.first = _First()
        return loc


def test_find_first_visible_prefers_earliest_selector_in_chain():
    from theseus.explorer.explorer_tools import _find_first_visible

    page = _ChainPage(visible={"b": [True], "c": [True]})
    _, selector = _find_first_visible(page, ["a", "b", "c"], timeout=200)
    assert selector == "b"


def test_find_first_visible_skips_broken_selectors():
    from theseus.explorer.explorer_tools import _find_first_visible

    page = _ChainPage(visible={"c": [True]}, broken={"a", "b"})
    _, selector = _find_first_visible(page, ["a", "b", "c"], timeout=200)
    assert selector == "c"


def test_find_first_visible_finds_element_that_appears_late():
    from theseus.explorer.explorer_tools import _find_first_visible

    page = _ChainPage(visible={"b": [False, False, True]})
    _, selector = _find_first_visible(page, ["a", "b"], timeout=2000)
    assert selector == "b"
    assert page.polls["b"] == 3


def test_find_first_visible_gives_up_after_timeout():
    import time as _time
    from theseus.explorer.explorer_tools import _find_first_visible

    page = _ChainPage()
    started = _time.monotonic()
    result = _find_first_visible(page, ["a", "b"], timeout=250)
    elapsed = _time.monotonic() - started
    assert result == (None, None)
    assert 0.2 <= elapsed < 1.0


def test_find_first_visible_returns_immediately_when_every_selector_is_broken():
    import time as _time
    from theseus.explorer.explorer_tools import _find_first_visible

    page = _ChainPage(broken={"a", "b"})
    started = _time.monotonic()
    assert _find_first_visible(page, ["a", "b"], timeout=5000) == (None, None)
    assert _time.monotonic() - started < 0.5


def test_find_first_visible_handles_empty_chain():
    from theseus.explorer.explorer_tools import _find_first_visible

    assert _find_first_visible(_ChainPage(), [], timeout=5000) == (None, None)


# ----------------------------------------------------------------------------
# Selector chain generation
# ----------------------------------------------------------------------------

def test_generate_click_selectors_orders_data_testid_first():
    from theseus.explorer.explorer_tools import _generate_click_selectors

    chain = _generate_click_selectors("Save offer")
    assert chain[0] == '[data-testid="save-offer"]'
    assert '[data-test="save-offer"]' in chain
    assert any('button:has-text("Save offer")' in s for s in chain)


def test_generate_click_selectors_handles_special_chars():
    from theseus.explorer.explorer_tools import _generate_click_selectors

    chain = _generate_click_selectors("Save / Cancel — now!")
    # Slug is letters + digits + collapsed dashes.
    assert any("save-cancel-now" in s for s in chain)


def test_generate_click_selectors_handles_empty_target():
    """Punctuation-only target produces an empty slug; chain falls
    back to has-text selectors only."""
    from theseus.explorer.explorer_tools import _generate_click_selectors

    chain = _generate_click_selectors("...")
    # No data-testid selectors when slug is empty.
    assert not any("[data-testid=" in s for s in chain)
    # has-text selectors still present.
    assert any('button:has-text' in s for s in chain)


def test_slug_for_selector_collapses_runs():
    from theseus.explorer.explorer_tools import _slug_for_selector

    assert _slug_for_selector("a---b   c") == "a-b-c"
    assert _slug_for_selector("Foo Bar Baz") == "foo-bar-baz"
    assert _slug_for_selector("") == ""
    assert _slug_for_selector("...!@#") == ""


# ----------------------------------------------------------------------------
# _wait_for production path (Playwright wait_for_load_state)
# ----------------------------------------------------------------------------

def test_wait_for_production_returns_success_when_networkidle_resolves(tmp_path: Path):
    from theseus.explorer import explorer_tools as et

    page = _make_playwright_shaped_page()
    page.wait_for_load_state.return_value = None
    result = et._wait_for(page, "page settled")
    assert result["success"] is True
    page.wait_for_load_state.assert_called_with(
        "networkidle", timeout=et.WAIT_FOR_TIMEOUT_MS
    )


def test_wait_for_production_returns_failure_on_timeout(tmp_path: Path):
    from theseus.explorer import explorer_tools as et

    page = _make_playwright_shaped_page()
    page.wait_for_load_state.side_effect = RuntimeError("Timeout 8000ms exceeded")
    result = et._wait_for(page, "ghost page")
    assert result["success"] is False
    assert "Timeout" in result["error"]


# ----------------------------------------------------------------------------
# _assert_state production path (DOM-text heuristic)
# ----------------------------------------------------------------------------

def test_assert_state_finds_quoted_phrase_in_body(tmp_path: Path):
    from theseus.explorer import explorer_tools as et

    page = _make_playwright_shaped_page(
        body_text="Offers list\nName: theseus-1\nStatus: active",
    )
    result = et._assert_state(page, "Offer named 'theseus-1' appears in the offers list")
    assert result["matches"] is True
    assert "theseus-1" in result["evidence"]


def test_assert_state_returns_not_matched_when_phrase_absent(tmp_path: Path):
    from theseus.explorer import explorer_tools as et

    page = _make_playwright_shaped_page(body_text="Empty offers list")
    result = et._assert_state(page, "Offer named 'never-existed' appears")
    assert result["matches"] is False
    assert "never-existed" in result["evidence"]


def test_assert_state_falls_back_to_full_description_when_no_quotes(tmp_path: Path):
    """No quotes → whole description is the only candidate phrase."""
    from theseus.explorer import explorer_tools as et

    page = _make_playwright_shaped_page(body_text="user is on the offers page now")
    result = et._assert_state(page, "user is on the offers page now")
    assert result["matches"] is True


def test_assert_state_records_body_read_failure(tmp_path: Path):
    from theseus.explorer import explorer_tools as et

    page = _make_playwright_shaped_page()
    page.locator.side_effect = lambda sel: (_ for _ in ()).throw(
        RuntimeError("page detached")
    )
    result = et._assert_state(page, "anything")
    assert result["matches"] is False
    assert "could not read page body" in result["evidence"]


def test_extract_assert_phrases_double_and_single_quotes():
    from theseus.explorer.explorer_tools import _extract_assert_phrases

    assert _extract_assert_phrases('Offer named "theseus-1" in list') == ["theseus-1"]
    assert _extract_assert_phrases("Offer named 'theseus-2'") == ["theseus-2"]
    # Multiple quotes get accumulated.
    pulled = _extract_assert_phrases('"foo" and "bar"')
    assert set(pulled) == {"foo", "bar"}


def test_extract_assert_phrases_empty_description():
    from theseus.explorer.explorer_tools import _extract_assert_phrases

    assert _extract_assert_phrases("") == []
    assert _extract_assert_phrases("   ") == []


# ============================================================================
# Production page-state extraction via page.evaluate
# (an earlier bug: read_page was returning empty summaries because
# nothing in production set _theseus_visible_* — only test fixtures did)
# ============================================================================

def test_capture_via_evaluate_returns_dict_from_page_evaluate():
    """Production path: page.evaluate returns a dict, dispatcher uses it
    directly without falling back to fixture attributes."""
    from theseus.explorer.explorer_tools import (
        _capture_via_evaluate,
        _PAGE_STATE_JS,
    )

    captured_script = {}
    fake_state = {
        "url": "https://staging.example.com/applications/10001",
        "title": "Example App — App 10001",
        "buttons": ["Offers", "Reports", "Save"],
        "links": ["Home", "Settings"],
        "inputs": ["Search"],
        "headings": ["Applications"],
        "open_menus": [],
        "modal_text": None,
    }

    page = SimpleNamespace(evaluate=lambda script: (
        captured_script.setdefault("script", script), fake_state
    )[1])

    result = _capture_via_evaluate(page)
    assert result == fake_state
    # Confirm the script we send is the canonical one — guards against
    # someone refactoring _PAGE_STATE_JS into something the production
    # path stops using.
    assert captured_script["script"] is _PAGE_STATE_JS


def test_capture_via_evaluate_returns_none_when_evaluate_raises():
    """A live page can fail evaluate for many reasons — page closed mid-
    navigation, JS error from a custom app extension, etc. The
    dispatcher must fall back, not crash the tool call."""
    from theseus.explorer.explorer_tools import _capture_via_evaluate

    def _raises(_script):
        raise RuntimeError("page closed")

    page = SimpleNamespace(evaluate=_raises)
    assert _capture_via_evaluate(page) is None


def test_capture_via_evaluate_returns_none_when_result_not_dict():
    """A MagicMock without spec returns another MagicMock from
    .evaluate(...), which is callable but isn't a dict. The dispatcher
    must reject that result so the fixture-attribute fallback fires —
    otherwise existing 286-test suite would break."""
    from theseus.explorer.explorer_tools import _capture_via_evaluate

    page_mock = MagicMock()
    # MagicMock.evaluate(...) returns a MagicMock (not a dict).
    assert _capture_via_evaluate(page_mock) is None


def test_capture_via_evaluate_returns_none_when_evaluate_absent():
    """Some test pages don't even have an evaluate method. The dispatcher
    must duck-type gracefully."""
    from theseus.explorer.explorer_tools import _capture_via_evaluate

    page = SimpleNamespace(url="https://x")
    assert _capture_via_evaluate(page) is None


def test_capture_page_summary_prefers_evaluate_path_over_fixture():
    """If page.evaluate returns a real dict, that wins — fixture
    attributes set on the same page are ignored. Production gets the
    real DOM scan; tests that want fixture control must avoid setting
    evaluate to a dict-returning callable."""
    from theseus.explorer.explorer_tools import _capture_page_summary

    fake_state = {
        "url": "https://x/y",
        "title": "real",
        "buttons": ["RealButton"],
        "links": [],
        "inputs": [],
        "headings": [],
        "open_menus": [],
        "modal_text": None,
    }
    page = SimpleNamespace(
        evaluate=lambda script: fake_state,
        url="https://wrong",  # would-be fixture
        title=lambda: "wrong",
        _theseus_visible_buttons=["FixtureButton"],  # would-be fixture
    )
    summary = _capture_page_summary(page)
    assert summary.url == "https://x/y"
    assert summary.title == "real"
    assert summary.visible_buttons == ("RealButton",)


def test_capture_page_summary_falls_back_to_fixture_attrs(tmp_path):
    """MagicMock pages (the standard test seam) have evaluate-returns-
    MagicMock semantics. The dispatcher must fall back to fixture
    attributes so existing tests keep working unchanged."""
    from theseus.explorer.explorer_tools import _capture_page_summary

    page = _make_page(
        url="https://sandbox.example/",
        title="t",
        buttons=["A", "B"],
        inputs=["i"],
        headings=["h"],
    )
    summary = _capture_page_summary(page)
    assert summary.url == "https://sandbox.example/"
    assert summary.title == "t"
    assert summary.visible_buttons == ("A", "B")
    assert summary.visible_input_labels == ("i",)
    assert summary.visible_headings == ("h",)


def test_capture_page_summary_surfaces_open_menus_from_evaluate():
    """The Portal-dropdown finding: clicking 'New' opens a [role=menu]
    that's not a descendant of the click target. The JS extraction
    queries the whole document so menu items show up in open_menus."""
    from theseus.explorer.explorer_tools import _capture_page_summary

    fake_state = {
        "url": "https://x",
        "title": "t",
        "buttons": ["New"],
        "links": [],
        "inputs": [],
        "headings": [],
        "open_menus": ["New Project", "Project from Template", "Import Project"],
        "modal_text": None,
    }
    page = SimpleNamespace(evaluate=lambda script: fake_state)
    summary = _capture_page_summary(page)
    assert summary.open_menus == (
        "New Project", "Project from Template", "Import Project"
    )


def test_page_summary_to_text_includes_open_menus_section():
    """The Claude-facing render must surface open menus distinctly from
    plain buttons. A Portal dropdown is usually the next action the page
    wants — labeling it differently helps Claude pick from the right list."""
    summary = PageSummary(
        url="https://x",
        title="t",
        visible_buttons=("New",),
        visible_input_labels=(),
        visible_headings=(),
        modal_text=None,
        truncated=False,
        visible_links=(),
        open_menus=("New Project", "Project from Template"),
    )
    text = summary.to_text()
    assert "open menu items: New Project, Project from Template" in text
    # And the plain "buttons:" line is still present too — Claude sees
    # both lists, with the menu items emphasized.
    assert "buttons: New" in text


def test_page_summary_to_text_includes_visible_links():
    """An app's navbar uses <a> for tabs — that has to be visible to
    Claude or it can't 'click Offers' from the application home."""
    summary = PageSummary(
        url="https://x",
        title="t",
        visible_buttons=(),
        visible_input_labels=(),
        visible_headings=(),
        modal_text=None,
        truncated=False,
        visible_links=("Offers", "Reports"),
    )
    text = summary.to_text()
    assert "links: Offers, Reports" in text


def test_page_summary_to_text_omits_links_section_when_empty():
    """Symmetry guard: empty link list shouldn't render as 'links: '."""
    summary = PageSummary(
        url="https://x",
        title="t",
        visible_buttons=("A",),
        visible_input_labels=(),
        visible_headings=(),
        modal_text=None,
        truncated=False,
    )
    assert "links:" not in summary.to_text()


def test_capture_via_fixture_attrs_reads_new_fields():
    """The fixture path also needs to know about open_menus and links
    so tests can drive the dispatcher's fallback branch end-to-end."""
    from theseus.explorer.explorer_tools import _capture_via_fixture_attrs

    page = MagicMock()
    page.url = "https://x"
    page.title.return_value = "t"
    page._theseus_visible_buttons = ["b"]
    page._theseus_visible_links = ["L"]
    page._theseus_visible_input_labels = []
    page._theseus_visible_headings = []
    page._theseus_open_menus = ["m1"]
    page._theseus_modal_text = None

    state = _capture_via_fixture_attrs(page)
    assert state["buttons"] == ["b"]
    assert state["links"] == ["L"]
    assert state["open_menus"] == ["m1"]


# ============================================================================
# field_errors extraction — inline validation error text
# (an earlier bug: Claude clicked Edit → Save without filling a
# required "Category" field, the app rendered "Category cannot
# be empty" as red inline text, but the extraction missed it and Claude
# thought Save had succeeded)
# ============================================================================

def test_capture_page_summary_surfaces_field_errors_from_evaluate():
    """When page.evaluate returns field_errors, they land in PageSummary
    verbatim so Claude can act on them next turn."""
    from theseus.explorer.explorer_tools import _capture_page_summary

    fake_state = {
        "url": "https://x/offer/new",
        "title": "New Offer",
        "buttons": ["Save", "Cancel"],
        "links": [],
        "inputs": ["Project Name", "Category"],
        "headings": ["Basic"],
        "open_menus": [],
        "field_errors": [
            "Category cannot be empty",
            "Details tab has 1 error",
        ],
        "modal_text": None,
    }
    page = SimpleNamespace(evaluate=lambda script: fake_state)
    summary = _capture_page_summary(page)
    assert summary.field_errors == (
        "Category cannot be empty",
        "Details tab has 1 error",
    )


def test_page_summary_to_text_puts_field_errors_before_buttons():
    """Validation errors are usually the most action-relevant signal on
    a page — they must render BEFORE the buttons list so Claude reads
    them first and doesn't just re-click Save."""
    summary = PageSummary(
        url="https://x",
        title="Draft Offer",
        visible_buttons=("Save", "Cancel"),
        visible_input_labels=(),
        visible_headings=(),
        modal_text=None,
        truncated=False,
        field_errors=("Category cannot be empty",),
    )
    text = summary.to_text()
    err_idx = text.find("field errors:")
    btn_idx = text.find("buttons:")
    assert err_idx >= 0, "field_errors must render"
    assert btn_idx >= 0, "buttons must render"
    assert err_idx < btn_idx, (
        "field_errors must render BEFORE buttons — otherwise Claude just "
        "re-clicks the button and never sees why the last click failed"
    )
    assert "Category cannot be empty" in text


def test_page_summary_to_text_omits_field_errors_section_when_empty():
    """Symmetry with visible_links: no errors → no 'field errors:' line
    (avoid rendering as 'field errors: ')."""
    summary = PageSummary(
        url="https://x",
        title="t",
        visible_buttons=("Save",),
        visible_input_labels=(),
        visible_headings=(),
        modal_text=None,
        truncated=False,
    )
    assert "field errors:" not in summary.to_text()


def test_capture_via_fixture_attrs_reads_theseus_field_errors():
    """The fixture-attribute test seam includes _theseus_field_errors
    so tests can drive the fallback branch end-to-end without mocking
    page.evaluate."""
    from theseus.explorer.explorer_tools import _capture_via_fixture_attrs

    page = MagicMock()
    page.url = "https://x"
    page.title.return_value = "t"
    page._theseus_visible_buttons = []
    page._theseus_visible_links = []
    page._theseus_visible_input_labels = []
    page._theseus_visible_headings = []
    page._theseus_open_menus = []
    page._theseus_field_errors = [
        "Category cannot be empty",
        "Owner is required",
    ]
    page._theseus_modal_text = None

    state = _capture_via_fixture_attrs(page)
    assert state["field_errors"] == [
        "Category cannot be empty",
        "Owner is required",
    ]


def test_page_summary_to_text_caps_field_errors_render():
    """Deep failure states can have 30+ error messages. The rendered
    prompt only needs the first 20 to convey what's wrong — token
    budget matters."""
    errors = tuple(f"Field {i} cannot be empty" for i in range(25))
    summary = PageSummary(
        url="https://x",
        title="t",
        visible_buttons=(),
        visible_input_labels=(),
        visible_headings=(),
        modal_text=None,
        truncated=False,
        field_errors=errors,
    )
    text = summary.to_text()
    assert "Field 0 cannot be empty" in text
    assert "Field 19 cannot be empty" in text
    assert "Field 20 cannot be empty" not in text


def test_field_errors_missing_from_evaluate_result_defaults_empty():
    """A page.evaluate result predating this JS change (or one that
    doesn't include field_errors for some reason) must not crash —
    the summary defaults to empty tuple, no error line renders."""
    from theseus.explorer.explorer_tools import _capture_page_summary

    fake_state = {
        "url": "https://x",
        "title": "t",
        "buttons": ["Save"],
        "links": [],
        "inputs": [],
        "headings": [],
        "open_menus": [],
        # field_errors intentionally omitted
        "modal_text": None,
    }
    page = SimpleNamespace(evaluate=lambda script: fake_state)
    summary = _capture_page_summary(page)
    assert summary.field_errors == ()
    assert "field errors:" not in summary.to_text()


# ============================================================================
# Indexed interactive elements + click_index (browser-use-style port)
# ============================================================================

def _elem(index, kind, text="", label=""):
    """Shape one element dict the way the extraction JS / fixtures emit it."""
    return {"index": index, "kind": kind, "text": text, "label": label}


def test_read_page_renders_indexed_elements_and_populates_map(tmp_path: Path):
    """read_page must surface the numbered element list AND cache the
    index->text map so click_index can run the forbidden-action check."""
    page = _make_page()
    page._theseus_elements = [
        _elem(0, "button", text="Add to Cart"),
        _elem(1, "link", text="Cart"),
        _elem(2, "input", label="Search Amazon"),
    ]
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    rendered = _tool(tools, "read_page").run({})
    assert "interactive elements (prefer click_index):" in rendered
    assert "[0] button 'Add to Cart'" in rendered
    assert "[2] input 'Search Amazon'" in rendered
    # The map is what click_index reads for its denial check.
    assert ctx.last_elements == {0: "Add to Cart", 1: "Cart", 2: "Search Amazon"}


def test_read_page_trace_records_element_sample(tmp_path: Path):
    import json

    page = _make_page()
    page._theseus_elements = [_elem(0, "button", text="Go")]
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    _tool(tools, "read_page").run({})
    records = [
        json.loads(l)
        for l in (tmp_path / "trace.jsonl").read_text().strip().split("\n")
    ]
    result = next(r for r in records if r["tool"] == "read_page")["result"]
    assert result["element_count"] == 1
    assert result["elements_sample"] == ["[0] button 'Go'"]


def test_click_index_resolves_and_clicks(tmp_path: Path):
    page = _make_page()
    clicked = {}

    def _record(index):
        clicked["index"] = index
        return {"success": True}

    page._theseus_click_index = _record
    ctx = _make_context(tmp_path, page=page)
    tools = build_palette(ctx)
    # Prime the map as read_page would.
    ctx.last_elements = {3: "Add to Cart"}
    result = _tool(tools, "click_index").run({"index": 3})
    assert clicked["index"] == 3
    assert result == "clicked: [3] Add to Cart"


def test_click_index_honors_forbidden_text_via_cached_map(tmp_path: Path):
    """The index path must not be a hole in the forbidden-action guard:
    a denied element's cached text still trips the denylist."""
    page = _make_page()
    page._theseus_click_index = lambda index: {"success": True}
    ctx = _make_context(
        tmp_path,
        page=page,
        forbidden_entries=[ForbiddenEntry(kind="text_match", value="delete")],
    )
    tools = build_palette(ctx)
    ctx.last_elements = {5: "Delete audience"}
    result = _tool(tools, "click_index").run({"index": 5})
    assert result.startswith("rejected: forbidden_text_match")
    assert ctx.forbidden_guard.violations == 1


def test_click_index_read_only_simulates(tmp_path: Path):
    page = _make_page()
    fired = {"called": False}

    def _boom(index):
        fired["called"] = True
        return {"success": True}

    page._theseus_click_index = _boom
    ctx = _make_context(tmp_path, page=page, read_only=True)
    tools = build_palette(ctx)
    ctx.last_elements = {0: "Save"}
    result = _tool(tools, "click_index").run({"index": 0})
    assert "simulated" in result
    assert fired["called"] is False  # write tool is a no-op in read-only


def test_click_index_unknown_index_still_clicks_without_denial(tmp_path: Path):
    """An index absent from the cached map (model raced ahead of a
    read_page) resolves with empty denial text — the real adapter decides
    whether the node exists."""
    page = _make_page()
    page._theseus_click_index = lambda index: {"success": True}
    ctx = _make_context(tmp_path, page=page)
    result = _tool(tools := build_palette(ctx), "click_index").run({"index": 9})
    assert result.startswith("clicked: [9]")


def test_capture_indexed_elements_from_evaluate(tmp_path: Path):
    """End-to-end through the production evaluate path: a JS result with
    an elements array becomes IndexedElement rows on the summary."""
    from theseus.explorer.explorer_tools import _capture_page_summary

    fake_state = {
        "url": "https://amazon.com",
        "title": "Amazon",
        "buttons": ["Add to Cart"],
        "links": [],
        "inputs": [],
        "headings": [],
        "open_menus": [],
        "field_errors": [],
        "modal_text": None,
        "elements": [_elem(0, "button", text="Add to Cart")],
    }
    page = SimpleNamespace(evaluate=lambda script: fake_state)
    summary = _capture_page_summary(page)
    assert len(summary.elements) == 1
    assert summary.elements[0].index == 0
    assert summary.elements[0].kind == "button"
    assert summary.elements[0].text == "Add to Cart"


def test_coerce_indexed_elements_drops_junk_rows():
    from theseus.explorer.explorer_tools import _coerce_indexed_elements

    coerced = _coerce_indexed_elements(
        [_elem(0, "button", text="Ok"), "not-a-dict", {"kind": "link"}]
    )
    # "not-a-dict" dropped; the index-less dict falls back to its position.
    assert [(e.index, e.kind) for e in coerced] == [(0, "button"), (2, "link")]


# ============================================================================
# Review fixes: submit's forbidden check, wait_for, all-phrase asserts,
# relative navigation.
# ============================================================================

class _Buttons:
    """A page with named buttons, for the production submit path."""

    def __init__(self, names, url="https://example.com/form"):
        self.names = list(names)
        self.url = url
        self.clicked = []

    def get_by_role(self, role, name, exact=False):
        assert role == "button"
        found = [n for n in self.names if (n == name if exact else name.lower() in n.lower())]
        page = self

        class _Locator:
            first = None

            def is_visible(self):
                return bool(found)

            def evaluate(self, script):
                return found[0]

            def click(self, timeout=None):
                page.clicked.append(found[0])

        loc = _Locator()
        loc.first = loc
        return loc


def _context_with(page, tmp_path, *, read_only=False, forbidden=("Publish", "Delete")):
    return _make_context(
        tmp_path, page=page, read_only=read_only,
        forbidden_entries=[ForbiddenEntry(kind="text_match", value=v) for v in forbidden],
    )


def test_submit_refuses_a_button_whose_name_is_forbidden(tmp_path):
    page = _Buttons(["Save and Publish"])
    ctx = _context_with(page, tmp_path)
    out = _tool(build_palette(ctx), "submit").run({})
    assert out.startswith("rejected: forbidden_text_match")
    assert page.clicked == []
    assert ctx.forbidden_guard.violations == 1


def test_submit_prefers_an_exact_name(tmp_path):
    page = _Buttons(["Save and Publish", "Save"])
    out = _tool(build_palette(_context_with(page, tmp_path)), "submit").run({})
    assert out == "submitted: Save"
    assert page.clicked == ["Save"]


def test_submit_reports_the_button_it_clicked(tmp_path):
    page = _Buttons(["Create offer"])
    out = _tool(build_palette(_context_with(page, tmp_path)), "submit").run({})
    assert out == "submitted: Create offer"


def test_submit_without_a_button_fails_cleanly(tmp_path):
    page = _Buttons(["Cancel"])
    out = _tool(build_palette(_context_with(page, tmp_path)), "submit").run({})
    assert out.startswith("submit_failed: no visible button")
    assert page.clicked == []


def test_submit_in_read_only_mode_clicks_nothing(tmp_path):
    page = _Buttons(["Save"])
    out = _tool(build_palette(_context_with(page, tmp_path, read_only=True)), "submit").run({})
    assert out.startswith("simulated")
    assert page.clicked == []


class _TextPage:
    """``get_by_text(phrase).first.is_visible()`` turns True after ``appears_after`` polls."""

    def __init__(self, appears_after=None, settles=True):
        self.appears_after = appears_after
        self.settles = settles
        self.polls = 0

    def get_by_text(self, phrase, exact=False):
        page = self

        class _Locator:
            def is_visible(self):
                page.polls += 1
                return page.appears_after is not None and page.polls > page.appears_after

        loc = MagicMock()
        loc.first = _Locator()
        return loc

    def wait_for_load_state(self, state, timeout=None):
        if not self.settles:
            raise TimeoutError("still busy")


def test_wait_for_waits_for_quoted_text(tmp_path):
    ctx = _make_context(tmp_path, page=_TextPage(appears_after=2))
    out = _tool(build_palette(ctx), "wait_for").run({"description": "until 'Order placed' shows"})
    assert out.startswith("waited: 'Order placed' appeared")


def test_wait_for_times_out_when_quoted_text_never_appears(tmp_path, monkeypatch):
    from theseus.explorer import explorer_tools as et

    monkeypatch.setattr(et, "WAIT_FOR_TIMEOUT_MS", 300)
    ctx = _make_context(tmp_path, page=_TextPage(appears_after=None))
    out = _tool(build_palette(ctx), "wait_for").run({"description": "'Order confirmed'"})
    assert out.startswith("waited: timeout")
    assert "Order confirmed" in out


def test_wait_for_without_quotes_reports_a_settle_not_a_match(tmp_path):
    ctx = _make_context(tmp_path, page=_TextPage())
    out = _tool(build_palette(ctx), "wait_for").run({"description": "the list loads"})
    assert out.startswith("waited: page settled")


def _body_page(text):
    page = MagicMock(spec=["locator"])
    page.locator.return_value.inner_text.return_value = text
    return page


@pytest.mark.parametrize("body, any_match, all_match", [
    ("Order placed. Total: $5", True, True),
    ("Order placed.", True, False),
    ("Nothing yet", False, False),
])
def test_assert_state_any_versus_all_phrases(body, any_match, all_match):
    from theseus.explorer.explorer_tools import _assert_state

    criterion = "shows 'Order placed' and 'Total: $5'"
    assert _assert_state(_body_page(body), criterion)["matches"] is any_match
    assert _assert_state(_body_page(body), criterion, require_all=True)["matches"] is all_match


def test_navigate_resolves_relative_urls_against_the_current_page(tmp_path):
    page = _make_page()
    page.url = "https://app.example.com/applications/1"
    ctx = _make_context(tmp_path, page=page, boundary_patterns=["https://app.example.com/**"])
    out = _tool(build_palette(ctx), "navigate").run({"url": "/offers?tab=2"})
    assert out == "navigated: https://app.example.com/offers?tab=2"
    page.goto.assert_called_once_with("https://app.example.com/offers?tab=2")


# ----------------------------------------------------------------------------
# Polling waits on the browser's event loop, not on the thread
# ----------------------------------------------------------------------------

class _LoopPage(_ChainPage):
    """A chain page that also has Playwright's wait_for_timeout."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.waits = []

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


@pytest.fixture
def no_thread_sleep(monkeypatch):
    """A polling loop that sleeps on its thread would starve blocked requests: fail the test."""
    def fail(seconds):
        raise AssertionError(f"time.sleep({seconds}) on the browser thread")

    monkeypatch.setattr("theseus.explorer.explorer_tools.time.sleep", fail)


def test_finding_a_control_polls_through_the_browsers_event_loop(no_thread_sleep):
    from theseus.explorer.explorer_tools import CLICK_CHAIN_POLL_MS, _find_first_visible

    page = _LoopPage(visible={"b": [False, False, True]})
    _, selector = _find_first_visible(page, ["a", "b"], timeout=2000)
    assert selector == "b" and page.waits == [CLICK_CHAIN_POLL_MS] * 2


def test_waiting_for_text_polls_through_the_browsers_event_loop(no_thread_sleep):
    from theseus.explorer import explorer_tools as et

    class Page:  # no _theseus_* hooks: the production path
        def __init__(self):
            self.polls, self.waits = 0, []

        def get_by_text(self, phrase, exact=False):
            page = self

            class First:
                def is_visible(self_inner):
                    page.polls += 1
                    return page.polls >= 3

            return SimpleNamespace(first=First())

        def wait_for_timeout(self, ms):
            self.waits.append(ms)

    page = Page()
    result = et._wait_for(page, "wait until 'Saved' shows")
    assert result["success"] is True and result["matched"] == "Saved"
    assert page.waits == [200, 200]


def test_pause_falls_back_to_a_sleep_when_the_page_cannot_wait(monkeypatch):
    from theseus.explorer import explorer_tools as et

    slept = []
    monkeypatch.setattr(et.time, "sleep", slept.append)
    et._pause(object(), 0.1)  # a page without wait_for_timeout
    dead = MagicMock()
    dead.wait_for_timeout.side_effect = RuntimeError("Target closed")
    et._pause(dead, 0.2)  # a page that can't wait any more must not end the poll early
    assert slept == [0.1, 0.2]


def test_pause_waits_at_least_a_millisecond():
    from theseus.explorer import explorer_tools as et

    page = MagicMock()
    et._pause(page, 0.0001)
    page.wait_for_timeout.assert_called_once_with(1)


# ----------------------------------------------------------------------------
# Text on Theseus's own stand-in page is never evidence, for the page that produced the match
# ----------------------------------------------------------------------------

def test_a_phrase_that_appears_after_the_page_became_the_stand_in_does_not_count(no_thread_sleep):
    from theseus.explorer import explorer_tools as et

    class Page:  # the form submits itself on the 3rd poll; the stand-in then shows the phrase
        def __init__(self):
            self.polls, self.on_stand_in = 0, False

        def get_by_text(self, phrase, exact=False):
            page = self

            class First:
                def is_visible(self_inner):
                    page.polls += 1
                    if page.polls == 3:
                        page.on_stand_in = True
                    return page.on_stand_in  # 'submitted' is visible only on the stand-in

            return SimpleNamespace(first=First())

        def wait_for_timeout(self, ms):
            pass

    page = Page()
    result = et._wait_for(page, "wait until 'submitted' shows", stand_in=lambda: page.on_stand_in)
    assert result["success"] is False and result["error"] == et.STAND_IN_EVIDENCE


def test_assert_state_asks_about_the_page_after_reading_its_text():
    from theseus.explorer import explorer_tools as et

    class Page:
        on_stand_in = False

        def locator(self, selector):
            page = self

            class Body:
                def inner_text(self_inner, timeout=None):
                    page.on_stand_in = True  # the form submitted between the check before and the read
                    return "This page submitted data"

            return Body()

    page = Page()
    result = et._assert_state(page, "the page shows 'submitted'", require_all=True,
                              stand_in=lambda: page.on_stand_in)
    assert result == {"matches": False, "evidence": et.STAND_IN_EVIDENCE}
    assert et._assert_state(page, "the page shows 'submitted'", require_all=True)["matches"] is True  # no check, no veto


def test_a_stand_in_check_that_raises_is_not_a_veto():
    from theseus.explorer import explorer_tools as et

    assert et._quiet_true(lambda: 1 / 0) is False and et._quiet_true(None) is False


def test_screenshot_can_take_the_whole_page_when_asked(tmp_path: Path):
    page = _make_page()
    tools = build_palette(_make_context(tmp_path, page=page))
    _tool(tools, "screenshot").run({"full_page": True})
    assert page.screenshot.call_args.kwargs["full_page"] is True


# ----------------------------------------------------------------------------
# Clicks: centred first, and failures kept short
# ----------------------------------------------------------------------------

SHOP_LOG = """TimeoutError: Locator.click: Timeout 5000ms exceeded.
Call log:
waiting for locator("[data-theseus-index=\"17\"]").first
  -   locator resolved to <a class="ac-ln-button" data-theseus-index="17" href="/us/shop/goto/buy_watch/watch_series_12">…</a>
  - attempting click action
  -   <a role="button" aria-expanded="false" data-theseus-index="12" href="/us/shop/goto/bag" aria-label="Shopping Bag" id="globalnav-menubutton-link-bag">…</a> from <div id="globalheader">…</div> subtree intercepts pointer events
""" + "  - retrying click action, attempt #1\n  -   waiting 500ms\n" * 40


def test_a_click_blocked_by_another_element_names_it_in_a_few_lines():
    from theseus.explorer import explorer_tools as et

    short = et._short_error(SHOP_LOG)
    assert len(short) < 700 and len(SHOP_LOG) > 2500
    assert short.startswith("TimeoutError: Locator.click: Timeout 5000ms exceeded.")
    assert 'aria-label="Shopping Bag"' in short and "Another element covers the target" in short
    assert "navigate to the link's address" in short and "retrying click action" not in short


def test_any_other_error_is_cut_at_a_limit_and_a_short_one_is_left_alone():
    from theseus.explorer import explorer_tools as et

    assert et._short_error("TimeoutError: waiting for locator") == "TimeoutError: waiting for locator"
    long = "x" * 5000
    out = et._short_error(long)
    assert len(out) == et.MAX_ERROR_CHARS + 3 and out.endswith("...")


def test_the_returned_failure_is_short_but_the_trace_keeps_the_whole_error(tmp_path: Path):
    page = _make_page()
    del page._theseus_click_index  # the production path
    long_log = SHOP_LOG.replace("intercepts pointer events", "is not stable")
    page.locator.return_value.first.click.side_effect = RuntimeError(long_log)
    tools = build_palette(_make_context(tmp_path, page=page))
    result = _tool(tools, "click_index").run({"index": 17})
    assert result.startswith("click_index_failed: RuntimeError: TimeoutError") and len(result) < 800
    import json

    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert "retrying click action, attempt #1" in records[-1]["result"]["error"]


class FakeLocator:
    def __init__(self, click_errors=()):
        self.errors = list(click_errors)
        self.calls = []

    def click(self, timeout=None):
        self.calls.append(("click", timeout))
        if self.errors:
            raise RuntimeError(self.errors.pop(0))

    def dispatch_event(self, name, timeout=None):
        self.calls.append(("dispatch", name))


def test_a_click_that_works_is_just_a_click():
    from theseus.explorer import explorer_tools as et

    locator = FakeLocator()
    assert et._click_recovering(locator, timeout=5000) is None
    assert locator.calls == [("click", 2500)]


def test_a_target_covered_by_another_element_gets_the_click_directly_and_says_so():
    from theseus.explorer import explorer_tools as et

    locator = FakeLocator(["Locator.click: Timeout\n  - <div id=header> from <div> subtree intercepts pointer events"])
    assert et._click_recovering(locator) == et.COVERED_CLICK_NOTE
    assert locator.calls == [("click", 2500), ("dispatch", "click")]  # no waiting out the whole timeout


def test_any_other_click_failure_gets_the_rest_of_the_time_and_then_raises():
    from theseus.explorer import explorer_tools as et

    locator = FakeLocator(["not visible", "still not visible"])
    with pytest.raises(RuntimeError, match="still not visible"):
        et._click_recovering(locator, timeout=5000)
    assert locator.calls == [("click", 2500), ("click", 2500)]
    short = FakeLocator(["not stable"])
    with pytest.raises(RuntimeError, match="not stable"):
        et._click_recovering(short, timeout=1500)         # a short budget isn't split
    assert short.calls == [("click", 1500)]
    recovered = FakeLocator(["a slow animation"])
    assert et._click_recovering(recovered, timeout=5000) is None and len(recovered.calls) == 2


def test_the_note_reaches_the_agent_and_the_trace(tmp_path: Path):
    from theseus.explorer import explorer_tools as et

    page = _make_page()
    page._theseus_click_index = lambda index: {"success": True, "selector": "x", "note": et.COVERED_CLICK_NOTE}
    tools = build_palette(_make_context(tmp_path, page=page))
    result = _tool(tools, "click_index").run({"index": 3})
    assert result.endswith(f"({et.COVERED_CLICK_NOTE})") and result.startswith("clicked")
    import json

    records = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert records[-1]["result"]["note"] == et.COVERED_CLICK_NOTE
