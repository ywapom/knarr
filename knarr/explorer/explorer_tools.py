"""Knarr tool palette: navigation, observation and action tools, the
guards around them, and 2 scaffold annotators.

The surface:

- ``navigate(url)``       — hard navigation, URL-boundary checked
- ``read_page()``         — structured page summary, capped at 10KB
- ``screenshot()``        — full-page PNG, saved to disk, returned to model
- ``note(text)``          — free-form trace annotation
- ``record_intent(...)``  — scaffold-only parameter-to-selector mapping
- ``record_return(...)``  — scaffold-only return-value annotation

Plus the safety machinery:

- ``URLBoundaryGuard``    — pattern-matches navigations against the
                            manifest's ``url_boundary``. Three out-of-
                            boundary strikes terminate the run.
- ``ForbiddenActionGuard`` — selector + text-match denylist. Same
                             three-strikes contract. The action
                             tools (click, fill, submit) consult it.
- ``ExplorerContext``     — DI container holding the page, the trace
                            writer, the guards, the counters, and the
                            terminal signal the runner polls.

Read-only mode: navigation/observation tools are read-class — they
don't write to the site. When ``ExplorerContext.read_only=True``, the
*write* tools (click, fill, submit; not in this module yet) become
no-ops returning ``{simulated: true, would_have: args}``. None of the
6 tools here change state at the tool layer; ``--read-only`` is a
no-op for them.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


from knarr.explorer.origin import normalize_url
from knarr.explorer.trace import TraceWriter


# ============================================================================
# URL-boundary matching
# ============================================================================

_BOUNDARY_DOUBLESTAR = "\x00DOUBLESTAR\x00"
_BOUNDARY_SINGLESTAR = "\x00SINGLESTAR\x00"


def _compile_boundary_pattern(pattern: str) -> re.Pattern[str]:
    """Convert a glob-style URL pattern to a regex.

    ``**`` matches any character including ``/``; ``*`` matches one path
    segment (no ``/``). All other regex metacharacters are escaped.
    Anchored at both ends so a pattern matches the entire URL, not a
    prefix — otherwise ``https://app.example.com/applications/*``
    would accept anything starting with that prefix and out-of-boundary
    sub-paths would slip through.
    """
    # Sentinel-swap the wildcards before escaping so re.escape doesn't
    # mangle our intent.
    placeholder = (
        pattern.replace("**", _BOUNDARY_DOUBLESTAR)
        .replace("*", _BOUNDARY_SINGLESTAR)
    )
    escaped = re.escape(placeholder)
    escaped = escaped.replace(re.escape(_BOUNDARY_DOUBLESTAR), ".*")
    escaped = escaped.replace(re.escape(_BOUNDARY_SINGLESTAR), "[^/]+")
    return re.compile(f"^{escaped}$")


# Public name for other modules (the write block's allow patterns).
compile_url_pattern = _compile_boundary_pattern


class URLBoundaryGuard:
    """Tracks URL-boundary patterns and out-of-boundary attempt counts.

    The guard is per-run; resetting it between runs is the runner's job.
    Three out-of-boundary attempts abort with reason
    ``"boundary_violations_exceeded"`` (the "no blind crawl" rule).
    """

    MAX_VIOLATIONS = 3

    def __init__(self, patterns: Iterable[str]) -> None:
        self._patterns: List[re.Pattern[str]] = [
            _compile_boundary_pattern(p) for p in patterns
        ]
        self.violations = 0
        self.violation_log: List[str] = []

    def allows(self, url: str) -> bool:
        """True iff ``url`` matches at least one boundary pattern."""
        return any(p.match(url) for p in self._patterns)

    def record_violation(self, url: str) -> bool:
        """Increment the counter. Returns True iff the limit was just
        crossed — the caller should then signal terminal abort on the
        ExplorerContext."""
        self.violations += 1
        self.violation_log.append(url)
        return self.violations >= self.MAX_VIOLATIONS


# ============================================================================
# Forbidden-action enforcement
# ============================================================================

@dataclass(frozen=True)
class ForbiddenEntry:
    """One denylist row, post-manifest-parse.

    ``kind`` is one of ``"selector"`` or ``"text_match"``. Selector
    entries match exact selectors only — sub-string is too aggressive.
    Text-match entries are case-insensitive substring checks against
    the target string passed to fuzzy click/submit tools.
    """

    kind: str
    value: str


class ForbiddenActionGuard:
    """Same shape as the URL guard — patterns + counter + log.

    Consulted by the action tools (click, click_index, fill, submit) and
    by ask_human, and tested in isolation.
    """

    MAX_VIOLATIONS = 3

    def __init__(self, entries: Iterable[ForbiddenEntry]) -> None:
        entries = list(entries)
        self._selectors: List[str] = [
            e.value for e in entries if e.kind == "selector"
        ]
        self._text_matches: List[str] = [
            e.value.lower() for e in entries if e.kind == "text_match"
        ]
        self.violations = 0
        self.violation_log: List[Tuple[str, str]] = []

    def check_selector(self, selector: str) -> bool:
        """Return True iff ``selector`` is allowed."""
        return selector not in self._selectors

    def check_text_match(self, target: str) -> bool:
        """Return True iff ``target`` doesn't match any text-denial."""
        if not target:
            return True
        haystack = target.lower()
        return not any(needle in haystack for needle in self._text_matches)

    def record_violation(self, kind: str, value: str) -> bool:
        """Increment counter; True iff the limit was just crossed."""
        self.violations += 1
        self.violation_log.append((kind, value))
        return self.violations >= self.MAX_VIOLATIONS


# Verbs no exploratory session should ever fire, applied when nothing more
# specific (a manifest, a site profile) adds to them. Matched case-insensitively
# as substrings of a click target by ForbiddenActionGuard, and also against the
# text of an ask_human question.
#
# Chosen by consequence, not by tidiness: each of these either destroys data,
# starts a workflow with a real downstream obligation, or promotes something
# to live. Purchases aren't listed: the write block already fakes the request
# that would place an order. A site profile's ``forbidden_text`` adds verbs for
# one site (a shop's "Buy now", say); nothing removes these.
DEFAULT_EXPLORE_FORBIDDEN: tuple = (
    # Destructive
    "Delete",
    "Archive",
    "Close account",
    "Cancel subscription",
    # Starts a real workflow with downstream obligations
    "Send Request",
    "Request Approval",
    # Promotes to live
    "Publish",
    "Activate",
    "Go Live",
    "Approve",
)


# ============================================================================
# Explorer context — DI container for the 6 tools.
# ============================================================================

@dataclass
class ExplorerContext:
    """Mutable per-run state shared across the 6 Knarr tools.

    Every tool closes over this object so invocations write back into
    one place. The runner reads ``terminal_reason`` after each tool
    call to decide whether to stop the per-turn loop; tools never
    raise to signal abort — they set the field and return.

    Construction-time fields (passed by the runner):
        page, run_dir, manifest_boundary_patterns,
        manifest_forbidden_entries, trace, run_logger, scaffold_mode.

    Run-time fields (mutated by tools):
        turn, terminal_reason, screenshots_taken, etc.
    """

    page: Any
    run_dir: Path
    trace: TraceWriter
    boundary_guard: URLBoundaryGuard
    forbidden_guard: ForbiddenActionGuard
    run_logger: Any = None
    scaffold_mode: bool = False
    read_only: bool = False

    # -- interactive mode (--interactive) -------------------------------
    # ``interactive`` gates whether ask_human is REGISTERED at all, not just
    # whether it works. Availability is the safety property: a CI or k8s run
    # cannot block waiting for an operator who isn't there if the tool was
    # never in the palette. ``consult`` and ``learned_writer`` are duck-typed
    # (Any) so this module doesn't import the runner's consult channel, which
    # itself imports human_demo.
    interactive: bool = False
    consult: Any = None
    learned_writer: Any = None
    feature: str = ""
    run_id: str = ""
    goal: str = ""
    max_human_asks: int = 0
    # When True, assert_state passes only if EVERY quoted phrase is on the
    # page (the MCP server grades against a fixed criterion). The runner
    # keeps the original any-phrase check.
    assert_all_phrases: bool = False
    # Tells whether the page now showing is Knarr's own "Blocked" stand-in
    # for a form post (the MCP session sets it). Text on that page is never
    # evidence that a goal was reached: the site never answered.
    stand_in_page: Optional[Callable[[], bool]] = None
    human_asks_used: int = 0
    human_wait_ms: int = 0
    # Questions already put to the operator this trial, for the near-duplicate
    # guard. A list rather than a set because matching is fuzzy, not exact.
    asked_questions: List[str] = field(default_factory=list)

    turn: int = 0
    start_ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    terminal_reason: Optional[str] = None
    screenshots_taken: int = 0
    success_asserted: bool = False
    # Index → visible-text map from the most recent read_page(). Lets
    # click_index() run the forbidden-action text check without a second
    # DOM round-trip. Refreshed every read_page; empty until the first one.
    last_elements: Dict[int, str] = field(default_factory=dict)

    def now_ms(self) -> int:
        return int(time.time() * 1000) - self.start_ts_ms

    def signal_terminal(self, reason: str) -> None:
        """Latch terminal_reason. The runner polls this after each tool
        call. First-write-wins so the original cause is preserved if a
        tool tries to overwrite later."""
        if self.terminal_reason is None:
            self.terminal_reason = reason

    def emit_log(self, msg: str, level: str = "info") -> None:
        if self.run_logger is None:
            return
        method = getattr(self.run_logger, f"log_{level}", None)
        if method is None:
            return
        try:
            method(msg)
        except Exception:
            pass

    @property
    def human_asks_remaining(self) -> int:
        return max(0, self.max_human_asks - self.human_asks_used)


# ============================================================================
# Page summary — what read_page() returns.
# ============================================================================

@dataclass(frozen=True)
class IndexedElement:
    """One interactive element with a stable, model-facing index.

    Ported from browser-use's core idea: instead of asking the model to
    guess a text/selector for every click, ``read_page`` enumerates the
    visible interactive elements, assigns each a 0-based ``index``, and
    stamps ``data-knarr-index=<index>`` onto the live DOM node. The
    model then calls ``click_index(3)`` and the tool resolves the exact
    same node via ``[data-knarr-index="3"]`` — zero selector guessing.

    ``kind`` is a coarse bucket (button / link / input / tab / menuitem /
    checkbox / radio / other) so the render reads naturally. ``text`` is
    the visible text; ``label`` is the resolved input label when ``kind``
    is ``input`` (empty otherwise).
    """

    index: int
    kind: str
    text: str = ""
    label: str = ""

    def describe(self) -> str:
        """One-line render for the read_page summary + the trace sample."""
        shown = self.text or self.label
        return f"[{self.index}] {self.kind} {shown!r}"


@dataclass(frozen=True)
class PageSummary:
    """Structured view of the current page, capped to a budget.

    Knarr consumes a lot of tokens; this summary intentionally drops
    the raw DOM and keeps only the facets Claude needs to navigate the
    UI: visible button/input/link labels, headings, modal text, any
    currently-open menus (Portal-rendered dropdowns), and any inline
    validation errors surfaced by the app.
    """

    url: str
    title: str
    visible_buttons: Tuple[str, ...]
    visible_input_labels: Tuple[str, ...]
    visible_headings: Tuple[str, ...]
    modal_text: Optional[str]
    truncated: bool
    visible_links: Tuple[str, ...] = ()
    open_menus: Tuple[str, ...] = ()
    field_errors: Tuple[str, ...] = ()
    # Indexed interactive elements (browser-use-style click-by-index). Added
    # last with a default so the many positional/kw constructions in the test
    # suite keep working; empty tuple renders no "interactive elements" block.
    elements: Tuple[IndexedElement, ...] = ()

    def to_text(self) -> str:
        """Render for inclusion in the Claude prompt.

        Stable layout so the model sees the same shape every turn —
        formatting drift is a real source of token waste.

        ``field_errors`` renders FIRST after url/title/headings because
        validation errors are usually the most action-relevant signal on
        the page — if the app just rejected a save with "Class
        cannot be empty," Claude needs that in front of the buttons list
        rather than buried after modal_text.

        ``open_menus`` renders next — an open dropdown is usually the
        next action the page wants.
        """
        lines = [f"url: {self.url}", f"title: {self.title}"]
        if self.visible_headings:
            lines.append("headings: " + " | ".join(self.visible_headings))
        if self.field_errors:
            lines.append(
                "field errors: " + " | ".join(self.field_errors[:20])
            )
        if self.modal_text:
            lines.append(f"modal: {self.modal_text}")
        if self.open_menus:
            lines.append(
                "open menu items: " + ", ".join(self.open_menus[:25])
            )
        # Indexed interactive elements render as the primary action surface:
        # the model is told to prefer click_index(<n>) over fuzzy click().
        # The flat buttons/links/inputs lists below stay for backward
        # compatibility and as a redundant textual cue.
        if self.elements:
            lines.append("interactive elements (prefer click_index):")
            for el in self.elements[:60]:
                lines.append("  " + el.describe())
        if self.visible_buttons:
            lines.append(
                "buttons: " + ", ".join(self.visible_buttons[:25])
            )
        if self.visible_links:
            lines.append(
                "links: " + ", ".join(self.visible_links[:25])
            )
        if self.visible_input_labels:
            lines.append(
                "inputs: " + ", ".join(self.visible_input_labels[:25])
            )
        if self.truncated:
            lines.append("(page summary truncated to fit budget)")
        return "\n".join(lines)


PAGE_SUMMARY_BUDGET_CHARS = 10_000


# ============================================================================
# Tools — 4 navigation/observation + 2 scaffold annotators.
# ============================================================================

def build_explorer_functions(
    context: ExplorerContext,
) -> List[Callable[..., str]]:
    """Build the Knarr tool palette as plain functions.

    Returns a fresh list per call; each function closes over ``context``
    so invocations route through one mutable bag of state. Callers wrap
    them for their transport: ``knarr.mcp`` for MCP, or a runner's own
    tool wrapper.

    When ``context.scaffold_mode=False`` the two ``record_*`` tools are
    omitted from the returned palette — explore mode doesn't use them.
    """

    def navigate(url: str) -> str:
        """Navigate to a URL within the run's URL boundary.

        @param url(str): Absolute or relative URL (resolved against the
            current page). Must fall inside the URL boundary.
            Out-of-boundary URLs return an error without performing the
            navigation. Three such attempts terminate the run.
        @return (str): "navigated: <url>" on success, "rejected: <reason>"
            otherwise.
        """
        current = _safe_attr(context.page, "url", "")
        url = normalize_url(url, current if isinstance(current, str) else "")
        if not context.boundary_guard.allows(url):
            crossed = context.boundary_guard.record_violation(url)
            result = {
                "success": False,
                "reason": "out_of_boundary",
                "url": url,
                "violations": context.boundary_guard.violations,
            }
            context.trace.record(
                turn=context.turn,
                tool="navigate",
                args={"url": url},
                result=result,
                ts_ms=context.now_ms(),
            )
            if crossed:
                context.signal_terminal("boundary_violations_exceeded")
            context.emit_log(
                f"navigate: rejected out-of-boundary url={url} "
                f"({context.boundary_guard.violations}/{URLBoundaryGuard.MAX_VIOLATIONS})",
                level="warning",
            )
            return (
                f"rejected: out_of_boundary url={url}. Stay within the "
                f"URL boundary."
            )

        try:
            _safe_call(context.page, "goto", url, default=None)
            success = True
            error: Optional[str] = None
        except Exception as exc:
            success = False
            error = f"{type(exc).__name__}: {exc}"

        result = {"success": success}
        if error is not None:
            result["error"] = error
        context.trace.record(
            turn=context.turn,
            tool="navigate",
            args={"url": url},
            result=result,
            ts_ms=context.now_ms(),
        )
        if not success:
            return f"navigation_failed: {error}"
        return f"navigated: {url}"

    def read_page() -> str:
        """Return a structured summary of the current page.

        Read-only. Used by Claude as the primary "look around" tool
        before deciding what to click. Output is capped at ~10KB so the
        model doesn't drown in a large accessibility tree.

        @return (str): A multi-line summary with URL, title, visible
            button labels, visible input labels, headings, modal text.
        """
        summary = _capture_page_summary(context.page)
        rendered = summary.to_text()
        if len(rendered) > PAGE_SUMMARY_BUDGET_CHARS:
            rendered = rendered[: PAGE_SUMMARY_BUDGET_CHARS - 64] + "\n(truncated)"
        # Refresh the index→text map so click_index() can resolve an
        # element's text for the forbidden-action check. Stale indexes from
        # a prior page are dropped wholesale by the reassignment.
        context.last_elements = {
            el.index: (el.text or el.label) for el in summary.elements
        }
        # Trace instrumentation: capture a compact snapshot of the
        # extracted state so post-hoc diagnosis ("did the extraction
        # actually surface field_errors?") doesn't require replaying
        # the run or unzipping trace.zip. Small caps keep the trace
        # JSONL cheap; the full state is always in the string return.
        context.trace.record(
            turn=context.turn,
            tool="read_page",
            args={},
            result={
                "success": True,
                "char_count": len(rendered),
                "url": summary.url,
                "buttons_sample": list(summary.visible_buttons[:8]),
                "links_sample": list(summary.visible_links[:8]),
                "headings_sample": list(summary.visible_headings[:8]),
                "open_menus_sample": list(summary.open_menus[:8]),
                "field_errors_sample": list(summary.field_errors[:8]),
                "element_count": len(summary.elements),
                "elements_sample": [el.describe() for el in summary.elements[:8]],
                "modal_present": summary.modal_text is not None,
            },
            ts_ms=context.now_ms(),
        )
        return rendered

    def inspect_error(target: str = "") -> str:
        """Click a validation-error indicator (red "!" icon/badge) to reveal
        the detail hidden behind it.

        Many apps' validation errors show only a roll-up icon inline — so
        read_page reports a container like "Details has validation errors"
        with no fillable field, because the authoritative message (often a
        JSON payload naming the EXACT field and locale that is wrong) is
        revealed only when you CLICK the icon. This tool finds the clickable
        error indicators on the current page, clicks the one whose label
        matches ``target`` (or the first if ``target`` is empty), and returns
        the revealed detail, flattening any JSON error payload into readable
        "field.path — message" lines.

        Reach for this whenever a "... has validation errors" container refuses
        to clear even though every field you can SEE is already filled — the
        real requirement is usually a field in a different locale, or a deeper
        nested field, that the revealed JSON names explicitly.

        @param target(str): Substring of the error indicator's label to click
            (e.g. "Details", "Settings"). Empty → the first clickable error
            indicator found.
        @return (str): The revealed error detail (per-field / per-locale
            messages when the panel carries JSON), plus the list of clickable
            error indicators seen on the page.
        """
        outcome = _reveal_error(context.page, target) or {}
        clicked = bool(outcome.get("clicked"))
        candidates = [str(c) for c in (outcome.get("candidates") or [])]
        revealed_text = str(outcome.get("revealed_text") or "")
        detail_lines = _flatten_json_errors(revealed_text)

        result: Dict[str, Any] = {
            "success": clicked,
            "clicked_label": outcome.get("clicked_label"),
            "candidates": candidates[:20],
            "detail_lines": detail_lines[:40],
            "revealed_char_count": len(revealed_text),
            "debug": (outcome.get("debug") or [])[:8],
        }
        if "error" in outcome:
            result["error"] = outcome["error"]
        context.trace.record(
            turn=context.turn,
            tool="inspect_error",
            args={"target": target},
            result=result,
            ts_ms=context.now_ms(),
        )

        if not clicked:
            seen = ", ".join(candidates) if candidates else "none"
            return (
                f"no_clickable_error: nothing matched target={target!r} "
                f"(clickable error indicators seen: {seen})"
            )
        lines = [f"revealed error detail (clicked {outcome.get('clicked_label')!r}):"]
        if detail_lines:
            lines.extend("  - " + d for d in detail_lines[:40])
        elif revealed_text:
            lines.append("  " + revealed_text[:800])
        else:
            lines.append("  (icon clicked but no detail panel text was captured)")
        if candidates:
            lines.append("clickable error indicators: " + ", ".join(candidates[:20]))
        return "\n".join(lines)

    def screenshot(full_page: bool = False) -> str:
        """Take a screenshot and save it to ``run_dir/screenshots/``.

        Of the visible window by default: a full page of a long site is
        too tall to read. Pass ``full_page=True`` for the whole page.

        @param full_page(bool): True for the whole page, scrolled; False for the visible window.
        @return (str): The filename (relative to ``screenshots/``). Claude
            sees this as plain text; the runner is responsible for
            attaching the PNG as image content on subsequent turns.
        """
        context.screenshots_taken += 1
        filename = f"{context.turn:03d}-{context.screenshots_taken:03d}.png"
        target_dir = context.run_dir / "screenshots"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / filename
        success = True
        error: Optional[str] = None
        try:
            _safe_call(
                context.page,
                "screenshot",
                {"path": str(target_path), "full_page": bool(full_page)},
                default=None,
            )
        except Exception as exc:
            success = False
            error = f"{type(exc).__name__}: {exc}"
        result: Dict[str, Any] = {"success": success}
        if not success:
            result["error"] = error
        context.trace.record(
            turn=context.turn,
            tool="screenshot",
            args={},
            result=result,
            ts_ms=context.now_ms(),
            screenshot=filename if success else None,
        )
        return filename if success else f"screenshot_failed: {error}"

    def note(text: str) -> str:
        """Annotate the trace with a free-form observation.

        Useful for "I noticed the app renders this in a portal" remarks
        the pattern miner consumes. No side effects on the page.

        @param text(str): The annotation.
        @return (str): "noted" on success.
        """
        context.trace.record(
            turn=context.turn,
            tool="note",
            args={"text": text},
            result={"success": True},
            ts_ms=context.now_ms(),
        )
        return "noted"

    def click(target: str) -> str:
        """Click a UI element by visible text, label, or selector.

        Fuzzy match — accepts a button's text content, an input's
        label, or a CSS selector. Routes through the page's selector
        helpers; the trace records whether the deterministic selector
        path missed and a fallback was used.

        @param target(str): What to click — e.g. "Save offer", "Offers".
        @return (str): "clicked: <target>" / "simulated: ..." /
            "rejected: ..." / "click_failed: ...".
        """
        return _execute_action(
            context,
            tool_name="click",
            args={"target": target},
            text_for_denial=target,
            simulator=lambda: {"would_have": {"target": target}},
            real=lambda: _click_target(context.page, target),
            success_prefix="clicked",
        )

    def click_index(index: int) -> str:
        """Click the interactive element with the given index.

        ``read_page`` enumerates the visible interactive elements, prints
        them as ``[3] button 'Add to Cart'``, and stamps each live DOM node
        with ``data-knarr-index``. Pass that integer here and the tool
        clicks the exact node — no text or selector guessing, which is the
        main failure mode of fuzzy ``click`` on selector-fragile sites.

        Call ``read_page`` first: indexes are only valid until the next
        navigation or DOM change, and go stale exactly like browser-use's.

        @param index(int): The element index from the latest ``read_page``.
        @return (str): "clicked: [i] <text>" / "simulated: ..." /
            "rejected: ..." / "click_index_failed: ...".
        """
        # Resolve the element's text from the last read_page so the
        # forbidden-action denylist still applies to click-by-index (an
        # attacker prompt could otherwise route a "Delete" click through
        # the index path to dodge the text_match guard).
        text_for_denial = context.last_elements.get(index, "")
        return _execute_action(
            context,
            tool_name="click_index",
            args={"index": index},
            text_for_denial=text_for_denial or None,
            simulator=lambda: {
                "would_have": {"index": index, "text": text_for_denial}
            },
            real=lambda: _click_index(context.page, index),
            success_prefix=f"clicked: [{index}] {text_for_denial}".rstrip(),
        )

    def fill(label: str, value: str) -> str:
        """Fill an input identified by its visible label.

        @param label(str): The input's visible label (e.g. "Offer name").
        @param value(str): The value to type.
        @return (str): "filled: <label>=<value>" / "simulated: ..." /
            "rejected: ..." / "fill_failed: ...".
        """
        return _execute_action(
            context,
            tool_name="fill",
            args={"label": label, "value": value},
            text_for_denial=label,
            simulator=lambda: {"would_have": {"label": label, "value_chars": len(value)}},
            real=lambda: _fill_field(context.page, label, value),
            success_prefix=f"filled: {label}",
        )

    def submit() -> str:
        """Submit the current form by clicking its submit button.

        Looks for a visible button named "Save", "Create", "Apply" or
        "Submit", in that order, preferring an exact name. The button
        found goes through the same forbidden-action check as ``click``,
        so "Save and Publish" is refused like any other forbidden verb.

        @return (str): "submitted: <button>" / "simulated: ..." /
            "rejected: ..." / "submit_failed: ...".
        """
        page = context.page
        if hasattr(page, "_knarr_submit"):
            return _execute_action(
                context,
                tool_name="submit",
                args={},
                text_for_denial=None,
                simulator=lambda: {"would_have": {"verbs_tried": list(SUBMIT_VERBS)}},
                real=lambda: dict(page._knarr_submit()),
                success_prefix="submitted",
            )
        found = _find_submit_button(page)
        if found is None:
            return _execute_action(
                context,
                tool_name="submit",
                args={},
                text_for_denial=None,
                simulator=lambda: {"would_have": {"verbs_tried": list(SUBMIT_VERBS)}},
                real=lambda: {
                    "success": False,
                    "error": f"no visible button named any of {SUBMIT_VERBS}",
                },
                success_prefix="submitted",
            )
        locator, name = found
        return _execute_action(
            context,
            tool_name="submit",
            args={"button": name},
            text_for_denial=name,
            simulator=lambda: {"would_have": {"button": name}},
            real=lambda: _click_submit_button(locator, name),
            success_prefix="submitted",
        )

    def wait_for(description: str) -> str:
        """Wait for quoted text to appear, or for the page to settle.

        With quoted text in the description (e.g. ``"'Order placed'"``),
        waits until any quoted phrase is visible. Without quotes, waits
        for no network activity. Capped at a few seconds either way.

        @param description(str): The condition, with the text to wait for in quotes.
        @return (str): "waited: matched / timeout".
        """
        if _quoted_phrases(description or "") and _on_stand_in_page(context):
            outcome = {"success": False, "matched_at_ms": 0, "error": STAND_IN_EVIDENCE}
        else:
            outcome = _wait_for(context.page, description, stand_in=context.stand_in_page)
        success = bool(outcome.get("success"))
        result = {
            "success": success,
            "matched_at_ms": int(outcome.get("matched_at_ms", 0)),
        }
        if not success and "error" in outcome:
            result["error"] = outcome["error"]
        context.trace.record(
            turn=context.turn,
            tool="wait_for",
            args={"description": description},
            result=result,
            ts_ms=context.now_ms(),
        )
        if success and outcome.get("matched"):
            return f"waited: {outcome['matched']!r} appeared ({result['matched_at_ms']}ms)"
        if success and outcome.get("settled"):
            return f"waited: page settled, no network activity ({result['matched_at_ms']}ms)"
        if success:
            return f"waited: matched ({result['matched_at_ms']}ms)"
        if outcome.get("error") == STAND_IN_EVIDENCE:
            return f"waited: not satisfied — {STAND_IN_EVIDENCE}"
        return f"waited: timeout — {result.get('error') or description!r}"

    def assert_state(description: str) -> str:
        """Check whether the current page satisfies the success criterion.

        In explore mode this is the success signal: when it
        returns matches=True the runner records the run as successful
        and terminates. Scaffold mode also calls it but
        defers the final verdict to the dotted-path validator.

        Implementation is duck-typed via ``_knarr_assert_state``; in
        production this is a deterministic check (DOM landmarks, URL
        pattern, content presence). Claude does *not* grade its own
        run — the verdict is computed by the page-side helper.

        @param description(str): The assertion to test.
        @return (str): "matches: <description>" or "not_matched: <description>".
        """
        if _on_stand_in_page(context):
            outcome = {"matches": False, "evidence": STAND_IN_EVIDENCE}
        else:
            outcome = _assert_state(context.page, description, require_all=context.assert_all_phrases,
                                    stand_in=context.stand_in_page)
        matches = bool(outcome.get("matches"))
        evidence = outcome.get("evidence", "")
        result = {"success": True, "matches": matches, "evidence": evidence}
        context.trace.record(
            turn=context.turn,
            tool="assert_state",
            args={"description": description},
            result=result,
            ts_ms=context.now_ms(),
        )
        if matches:
            context.success_asserted = True
            context.signal_terminal("success_asserted")
            return f"matches: {description} (evidence: {evidence})"
        return f"not_matched: {description} (evidence: {evidence})"

    def give_up(reason: str) -> str:
        """Terminate the session as failed.

        Latches ``context.terminal_reason`` so the runner exits the
        per-turn loop. The reason is captured in summary.json for
        post-run review.

        @param reason(str): Why the model is giving up.
        @return (str): "giving_up: <reason>".
        """
        context.trace.record(
            turn=context.turn,
            tool="give_up",
            args={"reason": reason},
            result={"success": True},
            ts_ms=context.now_ms(),
        )
        context.signal_terminal(f"give_up: {reason or 'no reason'}")
        return f"giving_up: {reason or 'no reason'}"

    def ask_human(question: str, tried: str = "") -> str:
        """Pause the run and ask the operator watching the browser for help.

        Interactive runs only. The browser is headed and a human is present:
        they can answer in words, or simply perform the interaction themselves,
        in which case their clicks and field edits are recorded and handed back
        to you as concrete selectors.

        Reach for this when you have genuinely tried and are stuck on *finding*
        something — a control that read_page doesn't list, a field that must
        exist because validation demands it but that you cannot locate, a menu
        that won't open however you click it. This is the tool for
        "I can't see how to do this", not for "I'd like confirmation".

        Do NOT call this:
          - before trying. Call read_page, try click_index, try inspect_error
            (for a "... has validation errors" container the real requirement is
            usually a field in another locale) — then ask.
          - twice about the same control. If the answer didn't unblock you, the
            operator has told you what they know; change approach or give_up.
          - to ask permission, or to ask whether you should continue. You have a
            goal and a success criterion; act on them.

        Asks are budgeted. When the budget is spent this tool stops working and
        you must finish on your own, so spend them on real dead ends.

        NOTE on the two params below: some tool wrappers truncate a @param description
        at its first newline, so both are written as single lines. The long-form
        guidance lives in the body above, which is not truncated.

        @param question(str): What you need to know, stated specifically — e.g. "Where is the locale selector for the Settings section?" rather than "please help".
        @param tried(str): What you already attempted, so the operator doesn't repeat it — e.g. "read_page (no locale control listed), clicked index 12, clicked text 'English (US)', inspect_error('Details')".
        @return (str): The operator's answer, plus a description of anything
            they did in the browser while you waited (with real selectors), plus
            what changed on the page.
        """
        # Budget check first — before any I/O, so an over-asking model gets a
        # cheap, consistent refusal. Deliberately NOT terminal: the model
        # should carry on and reach its own give_up, not be killed for asking.
        if context.human_asks_remaining <= 0:
            context.trace.record(
                turn=context.turn,
                tool="ask_human",
                args={"question": question, "tried": tried},
                result={"success": False, "reason": "ask_budget_exhausted"},
                ts_ms=context.now_ms(),
            )
            return (
                f"ask_budget_exhausted: you have used all "
                f"{context.max_human_asks} human ask(s). No one will answer "
                f"another. Stop asking and finish: either act on what you "
                f"already know, or call give_up(reason=...) now."
            )

        # The question itself goes through the forbidden-action guard.
        #
        # Without this, a run that can't click a forbidden button (or a
        # --read-only run whose clicks are simulated) can ask the operator to
        # do it instead, and an obliging operator will. Screening only the agent's own clicks makes read-only a
        # decorative guarantee, because the human is a fully-capable actor the
        # agent can recruit. The human is also the worst enforcement point:
        # they get a confident, plausible request from something that has been
        # right all session.
        #
        # Screened against the question AND the tried-list, since the ask for
        # help and the description of what was attempted are equally capable of
        # naming the forbidden verb as the thing to do next.
        probe = f"{question} {tried}"
        if not context.forbidden_guard.check_text_match(probe):
            crossed = context.forbidden_guard.record_violation(
                "ask_human_text_match", question[:200]
            )
            context.trace.record(
                turn=context.turn,
                tool="ask_human",
                args={"question": question, "tried": tried},
                result={
                    "success": False,
                    "reason": "forbidden_in_ask",
                    "violations": context.forbidden_guard.violations,
                },
                ts_ms=context.now_ms(),
            )
            if crossed:
                context.signal_terminal("forbidden_action_violations_exceeded")
            return (
                "rejected: your question asks the operator to perform a "
                "forbidden action. You may not route around a restriction by "
                "having a human do it for you — a read-only run is read-only "
                "for the run, not just for you. Ask how to REACH or READ "
                "something instead, or give_up(reason=...)."
            )

        # Near-duplicate guard. The operator's attention is the scarcest thing
        # in this loop, and a run can spend all its asks re-asking one
        # question in escalating forms. Refusing a repeat costs nothing and
        # keeps the remaining budget for a genuinely new dead end.
        if _is_near_duplicate(question, context.asked_questions):
            context.trace.record(
                turn=context.turn,
                tool="ask_human",
                args={"question": question, "tried": tried},
                result={"success": False, "reason": "duplicate_question"},
                ts_ms=context.now_ms(),
            )
            return (
                "rejected: you have already asked this. The operator answered "
                "as well as they could; asking again in different words will "
                "not produce a different answer and it does not cost you "
                "nothing — it costs their attention. Act on the answer you "
                "have, try a genuinely different approach, or give_up."
            )

        consult = context.consult
        if consult is None:
            context.trace.record(
                turn=context.turn,
                tool="ask_human",
                args={"question": question, "tried": tried},
                result={"success": False, "reason": "no_consult_provider"},
                ts_ms=context.now_ms(),
            )
            return (
                "ask_human_unavailable: no operator channel is attached to this "
                "run. Proceed on your own or give_up."
            )

        context.human_asks_used += 1
        context.asked_questions.append(question)
        context.emit_log(f"[HUMAN] pausing to ask: {question}", level="warning")

        try:
            reply = consult(
                question=question,
                tried=tried,
                page=context.page,
                goal=context.goal,
                asks_remaining=context.human_asks_remaining,
            )
        except Exception as exc:
            # A broken consult provider must not kill an attended run.
            context.trace.record(
                turn=context.turn,
                tool="ask_human",
                args={"question": question, "tried": tried},
                result={
                    "success": False,
                    "reason": "consult_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ts_ms=context.now_ms(),
            )
            return (
                f"ask_human_failed: {type(exc).__name__}. The operator channel "
                f"is broken; proceed on your own or give_up."
            )

        demo = getattr(reply, "demo", None)
        observed = list(getattr(demo, "events", ()) or ()) if demo is not None else []
        waited_ms = int(getattr(reply, "waited_ms", 0) or 0)
        context.human_wait_ms += waited_ms

        result: Dict[str, Any] = {
            "success": True,
            "action": getattr(reply, "action", "answered"),
            "waited_ms": waited_ms,
            "answer_chars": len(str(getattr(reply, "text", "") or "")),
            "observed_actions": len(observed),
            "asks_used": context.human_asks_used,
        }
        if demo is not None:
            result["recorder_installed"] = bool(
                getattr(demo, "recorder_installed", False)
            )
            result["recorder_survived"] = bool(
                getattr(demo, "recorder_survived", False)
            )
            result["state_diff"] = list(getattr(demo, "state_diff", ()) or ())[:8]
            result["observed_sample"] = [
                e.describe() for e in observed[:6] if hasattr(e, "describe")
            ]
        context.trace.record(
            turn=context.turn,
            tool="ask_human",
            args={"question": question, "tried": tried},
            result=result,
            ts_ms=context.now_ms(),
        )

        _persist_learned_controls(
            context, question=question, tried=tried, reply=reply, observed=observed
        )

        if getattr(reply, "is_abort", False):
            context.signal_terminal("human_abort")

        render = getattr(reply, "render_for_model", None)
        if callable(render):
            return render()
        return str(getattr(reply, "text", "") or "(no answer)")

    functions: List[Callable[..., str]] = [
        navigate,
        read_page,
        inspect_error,
        screenshot,
        note,
        click,
        click_index,
        fill,
        submit,
        wait_for,
        assert_state,
        give_up,
    ]

    if context.scaffold_mode:
        functions.extend(_scaffold_annotator_functions(context))

    # Registered ONLY in interactive mode. This is the safety property, not a
    # convenience: a headless CI or k8s run has nobody to answer, and a tool it
    # cannot call is a stronger guarantee than a tool that checks a flag and
    # returns an error string. Mirrors the scaffold-annotator gate above.
    if context.interactive:
        functions.append(ask_human)

    return functions


# Words too common to carry meaning when comparing two questions. Without this
# "the/is/a" overlap alone pushes unrelated questions over the threshold.
_ASK_STOPWORDS = frozenset(
    {
        "the", "a", "an", "is", "are", "was", "for", "of", "to", "in", "on", "at",
        "it", "its", "this", "that", "and", "or", "but", "with", "you", "your",
        "i", "my", "me", "can", "could", "would", "please", "how", "do", "does",
        "where", "what", "which", "so", "not", "no", "but", "if", "then", "be",
        "have", "has", "had", "am", "as", "by", "from", "into", "out", "up",
    }
)

# Jaccard similarity above which two questions count as the same ask. Tuned
# loose on purpose: a false "duplicate" costs the model one rephrase, while a
# false "new question" costs a human several minutes.
_ASK_DUPLICATE_THRESHOLD = 0.6


def _ask_tokens(text: str) -> set:
    """Content words of a question, lowercased, stopwords dropped."""
    cleaned = re.sub(r"[^a-z0-9\s]+", " ", (text or "").lower())
    return {w for w in cleaned.split() if len(w) > 2 and w not in _ASK_STOPWORDS}


def _is_near_duplicate(question: str, previous: List[str]) -> bool:
    """True when ``question`` restates something already asked.

    Jaccard overlap on content words. Deliberately simple: the alternative is an
    LLM call to judge similarity, which would spend tokens to save tokens and
    add a failure mode to a guard whose whole job is to be cheap and reliable.
    """
    if not previous:
        return False
    current = _ask_tokens(question)
    if len(current) < 3:
        # Too short to judge — let it through rather than block on noise.
        return False
    for earlier in previous:
        other = _ask_tokens(earlier)
        if not other:
            continue
        union = current | other
        if not union:
            continue
        if len(current & other) / len(union) >= _ASK_DUPLICATE_THRESHOLD:
            return True
    return False


def _persist_learned_controls(
    context: ExplorerContext,
    *,
    question: str,
    tried: str,
    reply: Any,
    observed: List[Any],
) -> None:
    """Write what the human taught into the learned-controls store.

    Skipped for abort and skip replies: neither carries a lesson, and recording
    "the operator gave up here" as a control would pollute the prompt block that
    later runs read.

    Import is local so ``explorer_tools`` keeps no module-level dependency on
    the interactive stack — non-interactive runs never load it.
    """
    writer = context.learned_writer
    if writer is None:
        return
    if getattr(reply, "is_abort", False) or getattr(reply, "is_skip", False):
        return

    try:
        from knarr.explorer import learned_controls as lc
    except Exception:
        return

    human_text = str(getattr(reply, "text", "") or "")
    url = ""
    try:
        raw_url = getattr(context.page, "url", "")
        url = str(raw_url() if callable(raw_url) else raw_url or "")
    except Exception:
        url = ""

    try:
        controls = lc.controls_from_demo(
            observed,
            feature=context.feature,
            intent=question,
            question=question,
            human_text=human_text,
            url=url,
            run_id=context.run_id,
        )
        # Nothing demonstrable was observed, but the operator typed something.
        # Words without a selector are still worth carrying forward — often the
        # answer is "that field only appears after you switch region",
        # which no selector would capture anyway.
        if not controls and human_text:
            controls = [
                lc.control_from_text(
                    feature=context.feature,
                    intent=question,
                    question=question,
                    human_text=human_text,
                    url=url,
                    run_id=context.run_id,
                )
            ]
        if controls:
            written = writer.record_all(controls)
            context.emit_log(
                f"[HUMAN] recorded {written} learned control(s) for "
                f"{context.feature!r}"
            )
    except Exception as exc:
        # Persistence is a side benefit; never let it break the run the human
        # just unblocked.
        context.emit_log(
            f"[HUMAN] could not persist learned controls: "
            f"{type(exc).__name__}: {exc}",
            level="warning",
        )


def _scaffold_annotator_functions(context: ExplorerContext) -> List[Callable[..., str]]:
    """The two scaffold-only annotation tools.

    These don't affect the page or the run's terminal state — they
    enrich the trace with semantic info a scaffold generator needs
    to template page-object code. Explore mode runs without them so
    Claude isn't tempted to over-annotate.
    """

    def record_intent(method_param: str, value: str, source: str) -> str:
        """Annotate which UI element fulfills which method parameter.

        @param method_param(str): The page-object method parameter
            (e.g. "name").
        @param value(str): The literal value used (e.g. "knarr-test-1").
        @param source(str): What UI element this came from (e.g.
            "input labeled 'Offer name'").
        @return (str): "recorded".
        """
        context.trace.record(
            turn=context.turn,
            tool="record_intent",
            args={
                "method_param": method_param,
                "value": value,
                "source": source,
            },
            result={"success": True},
            ts_ms=context.now_ms(),
            annotations=[
                {
                    "kind": "intent",
                    "method_param": method_param,
                    "value": value,
                    "source": source,
                }
            ],
        )
        return "recorded"

    def record_return(value_description: str, evidence: str) -> str:
        """Annotate the return value of a read-style operation.

        @param value_description(str): What the page-object method
            should return (e.g. "list of offer IDs from the
            offers list").
        @param evidence(str): What in the current page state supports
            that (e.g. "DOM elements with role=row, .id from
            data-offer-id attribute").
        @return (str): "recorded".
        """
        context.trace.record(
            turn=context.turn,
            tool="record_return",
            args={
                "value_description": value_description,
                "evidence": evidence,
            },
            result={"success": True},
            ts_ms=context.now_ms(),
            annotations=[
                {
                    "kind": "return",
                    "value_description": value_description,
                    "evidence": evidence,
                }
            ],
        )
        return "recorded"

    return [record_intent, record_return]


# ============================================================================
# Page snapshot helpers
# ============================================================================

# Cap on how many items of each kind we return from the page-state
# extraction. Keeps the JSON payload small on pages with a lot on them.
_PAGE_STATE_MAX_BUTTONS = 50
_PAGE_STATE_MAX_LINKS = 50
_PAGE_STATE_MAX_INPUTS = 50
_PAGE_STATE_MAX_HEADINGS = 30
_PAGE_STATE_MAX_OPEN_MENU_ITEMS = 30
_PAGE_STATE_MAX_FIELD_ERRORS = 20
_PAGE_STATE_MAX_TEXT_CHARS = 120
_PAGE_STATE_MAX_ELEMENTS = 100


# JavaScript run inside the page via Playwright's ``page.evaluate``.
# Returns a single dict so the round-trip is one IPC call. The whole
# function runs in the page context — it can see Portal-rendered nodes
# attached to ``document.body`` that wouldn't be visible from a Playwright
# locator scoped to the explorer's parent frame. The visibility check
# combines ``getComputedStyle`` + bounding-rect size + aria-hidden so
# off-screen / display:none / aria-hidden=true elements are skipped.
_PAGE_STATE_JS = """
() => {
  const MAX_BUTTONS = %(max_buttons)d;
  const MAX_LINKS = %(max_links)d;
  const MAX_INPUTS = %(max_inputs)d;
  const MAX_HEADINGS = %(max_headings)d;
  const MAX_OPEN_MENU_ITEMS = %(max_open_menu_items)d;
  const MAX_TEXT = %(max_text)d;
  const MAX_ELEMENTS = %(max_elements)d;

  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return false;
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    return true;
  };
  const textOf = (el) => {
    const t = ((el.innerText || el.textContent || '') + '').replace(/\\s+/g, ' ').trim();
    return t.length > MAX_TEXT ? t.slice(0, MAX_TEXT - 3) + '...' : t;
  };
  const dedupCap = (arr, cap) => {
    const seen = new Set();
    const out = [];
    for (const item of arr) {
      if (item && !seen.has(item)) {
        seen.add(item);
        out.push(item);
        if (out.length >= cap) break;
      }
    }
    return out;
  };
  const inputLabel = (el) => {
    const id = el.id;
    if (id) {
      try {
        const lbl = document.querySelector('label[for="' + CSS.escape(id) + '"]');
        if (lbl && isVisible(lbl)) {
          const t = textOf(lbl);
          if (t) return t;
        }
      } catch (e) { /* CSS.escape may not exist on ancient browsers */ }
    }
    return el.getAttribute('aria-label')
        || el.getAttribute('placeholder')
        || el.getAttribute('name')
        || '';
  };

  const buttons = dedupCap(
    Array.from(document.querySelectorAll('button, [role="button"]'))
      .filter(isVisible).map(textOf).filter(Boolean),
    MAX_BUTTONS,
  );
  const links = dedupCap(
    Array.from(document.querySelectorAll('a'))
      .filter(isVisible).map(textOf).filter(Boolean),
    MAX_LINKS,
  );
  const inputs = dedupCap(
    Array.from(document.querySelectorAll('input, textarea, select'))
      .filter(isVisible).map(inputLabel).filter(Boolean),
    MAX_INPUTS,
  );
  const headings = dedupCap(
    Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6'))
      .filter(isVisible).map(textOf).filter(Boolean),
    MAX_HEADINGS,
  );

  // ---- Indexed interactive elements (browser-use-style click-by-index) --
  // Number each visible interactive element in document order and stamp
  // data-knarr-index onto the live node so click_index(n) resolves the
  // SAME element via [data-knarr-index="n"] without selector guessing.
  // Indexes are only valid until the next navigation/DOM mutation — the
  // agent must read_page() again before clicking, same contract as
  // browser-use's selector map.
  const INTERACTIVE_SEL = [
    'button', '[role="button"]', 'a[href]', 'input', 'textarea', 'select',
    '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="checkbox"]', '[role="radio"]', '[contenteditable="true"]'
  ].join(', ');
  const elements = [];
  let elIndex = 0;
  for (const el of Array.from(document.querySelectorAll(INTERACTIVE_SEL))) {
    if (elIndex >= MAX_ELEMENTS) break;
    if (!isVisible(el)) continue;
    // Collapse nested interactive nodes (e.g. <a><button>…</button></a>)
    // to the outermost by skipping any element whose ancestor was already
    // indexed this pass. Cuts duplicate rows the model would have to
    // disambiguate.
    let anc = el.parentElement, nested = false;
    while (anc && anc !== document.body) {
      if (anc.hasAttribute && anc.hasAttribute('data-knarr-index')) { nested = true; break; }
      anc = anc.parentElement;
    }
    if (nested) continue;
    const tag = (el.tagName || '').toLowerCase();
    const role = el.getAttribute ? (el.getAttribute('role') || '') : '';
    let kind = 'other';
    if (tag === 'button' || role === 'button') kind = 'button';
    else if (tag === 'a' || role === 'link') kind = 'link';
    else if (tag === 'input' || tag === 'textarea' || tag === 'select') kind = 'input';
    else if (role === 'tab') kind = 'tab';
    else if (role === 'menuitem') kind = 'menuitem';
    else if (role === 'checkbox' || role === 'radio') kind = role;
    const label = (kind === 'input') ? inputLabel(el) : '';
    const text = textOf(el) || label;
    if (!text && !label) continue;   // anonymous / icon-only with no label
    el.setAttribute('data-knarr-index', String(elIndex));
    elements.push({ index: elIndex, kind: kind, tag: tag, text: text, label: label });
    elIndex++;
  }

  // Open menus: Portal-rendered dropdowns / popover menus. We collect
  // every [role="menuitem"] AND every <button> descendant text from each
  // visible non-hidden menu so a "Create" → dropdown UX surfaces its
  // options ("New Project", "New Project from Template", etc.) to Claude.
  const openMenuItems = [];
  for (const menu of Array.from(document.querySelectorAll('[role="menu"]'))) {
    if (menu.getAttribute('aria-hidden') === 'true') continue;
    if (!isVisible(menu)) continue;
    const items = Array.from(menu.querySelectorAll('[role="menuitem"], button'))
      .filter(isVisible).map(textOf).filter(Boolean);
    for (const t of items) openMenuItems.push(t);
  }
  const open_menus = dedupCap(openMenuItems, MAX_OPEN_MENU_ITEMS);

  // Modal text: pick the first visible dialog / aria-modal element.
  let modal_text = null;
  const modal = Array.from(
    document.querySelectorAll('[role="dialog"], [aria-modal="true"]')
  ).filter(isVisible)[0];
  if (modal) {
    const t = textOf(modal);
    modal_text = t || null;
  }

  // Field errors: validation feedback that isn't a button/link/heading
  // and isn't inside a modal.
  //
  // Some apps use styled-components with a `color="error"` theme prop
  // as their universal error marker. That shows up on TWO kinds of
  // element in the same UI:
  //   (a) Text messages — <p color="error">Class cannot be empty</p>
  //   (b) Icon-only tab/section indicators — <svg color="error">...</svg>
  //       inside a tab anchor whose label lives in a sibling <div>.
  //
  // One query + a small ancestor walk handles both. Text-carrying
  // color=error nodes emit their text verbatim; icon-only nodes
  // synthesize "<label> has validation errors" using the closest
  // labeled ancestor (a, button, or [role="tab"]).
  //
  // We keep the older ARIA + class + aria-invalid patterns as backstops
  // for non-styled-components UIs.
  const errorTexts = [];

  const colorErrorNodes = Array.from(
    document.querySelectorAll('[color="error"]')
  ).filter(isVisible);
  for (const el of colorErrorNodes) {
    const text = textOf(el);
    if (text) {
      errorTexts.push(text);
      continue;
    }
    // Icon-only error — walk up to a labeled ancestor.
    let ancestor = el.parentElement;
    let hops = 0;
    while (ancestor && ancestor !== document.body && hops < 6) {
      const tag = ancestor.tagName;
      const role = ancestor.getAttribute ? ancestor.getAttribute('role') : null;
      if (tag === 'A' || tag === 'BUTTON' || role === 'tab' || role === 'menuitem') {
        const label = textOf(ancestor);
        if (label) {
          errorTexts.push(label + ' has validation errors');
          break;
        }
      }
      ancestor = ancestor.parentElement;
      hops++;
    }
  }

  const alerts = Array.from(document.querySelectorAll(
    '[role="alert"], [aria-live="polite"], [aria-live="assertive"]'
  )).filter(isVisible).map(textOf).filter(Boolean);
  for (const t of alerts) errorTexts.push(t);

  // Class-name fallback for UIs that don't use ARIA. We only accept
  // elements whose class token literally contains "error" — a broad
  // "*error*" attribute selector would also match "no-error" etc.
  const errorClassNodes = Array.from(
    document.querySelectorAll('[class]')
  ).filter(el => {
    const cls = (el.getAttribute('class') || '').toLowerCase();
    // Match "error" as a token or substring, but skip "no-error" / "cleared"
    if (!/error|fielderror|errormessage/.test(cls)) return false;
    if (/no-?error|clearederror|erroricon/.test(cls)) return false;
    return isVisible(el);
  }).map(textOf).filter(Boolean);
  for (const t of errorClassNodes) errorTexts.push(t);

  // aria-invalid inputs → follow aria-describedby to fetch the message
  // (the accessibility-idiomatic way to bind an error to a field).
  const invalidInputs = Array.from(
    document.querySelectorAll('[aria-invalid="true"]')
  );
  for (const el of invalidInputs) {
    const describedBy = el.getAttribute('aria-describedby') || '';
    for (const id of describedBy.split(/\\s+/).filter(Boolean)) {
      const desc = document.getElementById(id);
      if (desc && isVisible(desc)) {
        const t = textOf(desc);
        if (t) errorTexts.push(t);
      }
    }
  }

  const field_errors = dedupCap(errorTexts, %(max_field_errors)d);

  return {
    url: location.href,
    title: document.title || '',
    buttons,
    links,
    inputs,
    headings,
    open_menus,
    field_errors,
    modal_text,
    elements,
  };
}
""" % {
    "max_buttons": _PAGE_STATE_MAX_BUTTONS,
    "max_links": _PAGE_STATE_MAX_LINKS,
    "max_inputs": _PAGE_STATE_MAX_INPUTS,
    "max_headings": _PAGE_STATE_MAX_HEADINGS,
    "max_open_menu_items": _PAGE_STATE_MAX_OPEN_MENU_ITEMS,
    "max_field_errors": _PAGE_STATE_MAX_FIELD_ERRORS,
    "max_text": _PAGE_STATE_MAX_TEXT_CHARS,
    "max_elements": _PAGE_STATE_MAX_ELEMENTS,
}


# JavaScript for inspect_error(): find every clickable validation-error
# indicator on the page and click the one matching ``target``. "Clickable"
# means the error node (a color="error" icon, an [aria-invalid] input, or a
# [class*=error] element) has itself or an ancestor within 6 hops that is an
# <a>/<button>, role=button/tab/menuitem, carries onclick/tabindex, or renders
# with cursor:pointer — the shapes apps use for a "click the red ! for
# detail" affordance. Returns the labels of ALL candidates (so the tool can
# report what it saw) plus which one it clicked. No detail is read here — the
# panel usually renders asynchronously, so capture happens in a second pass.
_REVEAL_CLICK_JS = """
(target) => {
  const MAX_TEXT = 160;
  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return false;
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    return true;
  };
  const textOf = (el) => {
    const t = ((el.innerText || el.textContent || '') + '').replace(/\\s+/g, ' ').trim();
    return t.length > MAX_TEXT ? t.slice(0, MAX_TEXT - 3) + '...' : t;
  };
  const labeled = (el) => {
    const tag = el.tagName;
    const role = el.getAttribute ? el.getAttribute('role') : null;
    return tag === 'A' || tag === 'BUTTON' ||
           role === 'button' || role === 'tab' || role === 'menuitem' || role === 'link' ||
           /^H[1-6]$/.test(tag || '');
  };
  const clickable = (el) => {
    const tag = el.tagName;
    const role = el.getAttribute ? el.getAttribute('role') : null;
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    return tag === 'A' || tag === 'BUTTON' ||
           role === 'button' || role === 'tab' || role === 'menuitem' || role === 'link' ||
           (el.hasAttribute && el.hasAttribute('onclick')) ||
           (el.getAttribute && el.getAttribute('tabindex') !== null) ||
           (style && style.cursor === 'pointer');
  };
  const climb = (el, pred, maxHops) => {
    let a = el, hops = 0;
    while (a && a !== document.body && hops < maxHops) {
      if (pred(a)) return a;
      a = a.parentElement; hops++;
    }
    return null;
  };
  // Robust click: some error indicators are SVG icons whose .click() is
  // absent in this engine — fall back to a bubbling synthetic MouseEvent so
  // React onClick handlers still fire.
  const doClick = (el) => {
    try { if (typeof el.click === 'function') { el.click(); return true; } } catch (e) {}
    try {
      el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
      return true;
    } catch (e) { return false; }
  };
  const nodes = Array.from(document.querySelectorAll(
    '[color="error"], [aria-invalid="true"], [class*="error" i]'
  )).filter(isVisible);
  const candidates = [];
  const debug = [];
  const seen = new Set();
  for (const el of nodes) {
    const clickTarget = clickable(el) ? el : climb(el, clickable, 6);
    if (!clickTarget) continue;
    // Label from the nearest TEXT-bearing labeled ancestor (the "Details"
    // tab, the "Settings" side item) rather than the icon wrapper itself,
    // so target-matching works and candidates don't collapse to "unlabeled".
    let labelEl = (labeled(clickTarget) && textOf(clickTarget)) ? clickTarget
                  : climb(el, (x) => labeled(x) && !!textOf(x), 8);
    let label = labelEl ? textOf(labelEl) : (textOf(clickTarget) || '');
    if (!label && clickTarget.getAttribute) label = clickTarget.getAttribute('aria-label') || '';
    if (!label) {
      const cls = ((clickTarget.getAttribute && clickTarget.getAttribute('class')) || '').slice(0, 40);
      label = '(error icon: ' + (clickTarget.tagName || '?').toLowerCase() + (cls ? '.' + cls : '') + ')';
    }
    if (seen.has(label)) continue;
    seen.add(label);
    candidates.push({ label, el: clickTarget });
    if (debug.length < 8) {
      debug.push({
        node_tag: el.tagName,
        node_cls: ((el.getAttribute && el.getAttribute('class')) || '').slice(0, 60),
        click_tag: clickTarget.tagName,
        click_role: (clickTarget.getAttribute && clickTarget.getAttribute('role')) || '',
        label: label,
      });
    }
  }
  let chosen = null;
  const t = (target || '').toLowerCase();
  if (t) chosen = candidates.find(c => c.label.toLowerCase().indexOf(t) !== -1) || null;
  if (!chosen) chosen = candidates[0] || null;
  let clicked = false, clickedLabel = null;
  if (chosen) {
    clicked = doClick(chosen.el);
    if (clicked) clickedLabel = chosen.label;
  }
  return { clicked, clickedLabel, candidates: candidates.map(c => c.label), debug };
}
"""


# JavaScript for inspect_error(): after the reveal click, read whatever detail
# panel appeared. Prefers a visible dialog/alert/tooltip; otherwise grabs the
# first visible element whose text looks like a JSON object. The raw text goes
# back to Python, which extracts and flattens any JSON error payload.
_REVEAL_CAPTURE_JS = """
() => {
  const MAX = 4000;
  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return false;
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    return true;
  };
  const textOf = (el) => ((el.innerText || el.textContent || '') + '').replace(/\\s+/g, ' ').trim();
  const containers = Array.from(document.querySelectorAll(
    '[role="dialog"], [role="alert"], [role="tooltip"], [aria-modal="true"]'
  )).filter(isVisible);
  let text = '';
  for (const c of containers) {
    const t = textOf(c);
    if (t) text += t + '\\n';
  }
  if (text.indexOf('{') === -1) {
    const all = Array.from(document.querySelectorAll('pre, code, div, span, p')).filter(isVisible);
    for (const el of all) {
      const t = textOf(el);
      if (t.length > 1 && t.length < MAX && t.charAt(0) === '{' && t.charAt(t.length - 1) === '}') {
        text += t + '\\n';
        break;
      }
    }
  }
  return { revealed_text: text.slice(0, MAX), dialog_present: containers.length > 0 };
}
"""


def _capture_via_evaluate(page: Any) -> Optional[Dict[str, Any]]:
    """Run ``_PAGE_STATE_JS`` in the page context and return the dict.

    Returns ``None`` when:
      - ``page.evaluate`` doesn't exist (duck-typed: test fakes that
        don't mock it),
      - ``page.evaluate`` raises (page closed, JS error, timeout),
      - The result isn't a dict (a MagicMock without spec returns
        another MagicMock — we filter that out so the fallback fires).

    A ``None`` return is the caller's signal to fall back to the
    ``_knarr_visible_*`` fixture-attribute path.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        result = evaluate(_PAGE_STATE_JS)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    return result


def _capture_via_fixture_attrs(page: Any) -> Dict[str, Any]:
    """Read ``_knarr_visible_*`` attributes off the page object.

    The test seam. Production runs never hit this path because the
    evaluate result is a real dict; test fixtures set these attributes
    on a MagicMock to control page state without faking ``evaluate``.
    """
    url = str(_safe_attr(page, "url", default="<unknown>"))
    title = ""
    try:
        title_fn = getattr(page, "title", None)
        if callable(title_fn):
            title = str(title_fn())
        elif isinstance(title_fn, str):
            title = title_fn
    except Exception:
        title = ""
    return {
        "url": url,
        "title": title,
        "buttons": list(_safe_list(page, "_knarr_visible_buttons", default=())),
        "links": list(_safe_list(page, "_knarr_visible_links", default=())),
        "inputs": list(_safe_list(page, "_knarr_visible_input_labels", default=())),
        "headings": list(_safe_list(page, "_knarr_visible_headings", default=())),
        "open_menus": list(_safe_list(page, "_knarr_open_menus", default=())),
        "field_errors": list(_safe_list(page, "_knarr_field_errors", default=())),
        "modal_text": _safe_attr(page, "_knarr_modal_text", default=None),
        "elements": list(_safe_list(page, "_knarr_elements", default=())),
    }


def _capture_page_summary(page: Any) -> PageSummary:
    """Best-effort structured snapshot.

    Two-layer dispatch:
      1. Try ``page.evaluate(_PAGE_STATE_JS)`` — production path. Runs
         inside the page context so it can see Portal-rendered nodes
         (open menus, modals) attached to ``document.body``.
      2. Fall back to ``_knarr_visible_*`` attributes — test seam.

    The shapes here line up with what Playwright pages expose, but we
    don't import Playwright — every call is duck-typed and falls back
    to a sane default. That lets unit tests pass a ``MagicMock`` page
    without the real browser.
    """
    state = _capture_via_evaluate(page)
    if state is None:
        state = _capture_via_fixture_attrs(page)

    buttons = tuple(str(b) for b in (state.get("buttons") or ()))
    links = tuple(str(l) for l in (state.get("links") or ()))
    inputs = tuple(str(i) for i in (state.get("inputs") or ()))
    headings = tuple(str(h) for h in (state.get("headings") or ()))
    open_menus = tuple(str(m) for m in (state.get("open_menus") or ()))
    field_errors = tuple(str(e) for e in (state.get("field_errors") or ()))
    modal_text = state.get("modal_text")
    elements = _coerce_indexed_elements(state.get("elements") or ())
    return PageSummary(
        url=str(state.get("url") or "<unknown>"),
        title=str(state.get("title") or ""),
        visible_buttons=buttons,
        visible_input_labels=inputs,
        visible_headings=headings,
        modal_text=str(modal_text) if modal_text else None,
        truncated=False,
        visible_links=links,
        open_menus=open_menus,
        field_errors=field_errors,
        elements=elements,
    )


def _coerce_indexed_elements(raw: Any) -> Tuple[IndexedElement, ...]:
    """Turn the JS/fixture element dicts into an ``IndexedElement`` tuple.

    Defensive against partial rows: an entry missing ``index`` falls back
    to its position, and non-dict rows are dropped. Keeps the extraction
    JS and the test fixtures on the same, forgiving contract.
    """
    out: List[IndexedElement] = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            continue
        try:
            index = int(e.get("index", i))
        except (TypeError, ValueError):
            index = i
        out.append(
            IndexedElement(
                index=index,
                kind=str(e.get("kind", "other") or "other"),
                text=str(e.get("text", "") or ""),
                label=str(e.get("label", "") or ""),
            )
        )
    return tuple(out)


# ============================================================================
# Safe duck-typed access.
# ============================================================================

def _safe_attr(obj: Any, name: str, default: Any) -> Any:
    try:
        value = getattr(obj, name, default)
        return value() if callable(value) else value
    except Exception:
        return default


def _safe_list(obj: Any, name: str, default: Iterable[Any]) -> Iterable[Any]:
    value = getattr(obj, name, default)
    if callable(value):
        try:
            value = value()
        except Exception:
            return list(default)
    try:
        return list(value)
    except TypeError:
        return list(default)


def _safe_call(obj: Any, name: str, *args: Any, default: Any) -> Any:
    fn = getattr(obj, name, None)
    if fn is None:
        return default
    # Support kwargs-as-dict for cleaner test mocking.
    if len(args) == 1 and isinstance(args[0], dict):
        return fn(**args[0])
    return fn(*args)


# ============================================================================
# Action-tool helpers — duck-typed page interactions.
#
# Tests register ``_knarr_*`` methods on the page object so the same
# tool body works without importing Playwright. Production code paths
# fall through to Playwright primitives (``page.get_by_text``, etc.) —
# wired up by the explorer runner when a real session is in play.
# ============================================================================

SUBMIT_VERBS = ("Save", "Create", "Apply", "Submit")
WAIT_FOR_TIMEOUT_MS = 8_000
ASSERT_STATE_TIMEOUT_MS = 2_500


MIN_SCRUBBED_VALUE_CHARS = 4  # a shorter value would mangle an error message and is rarely a secret


def _loggable_args(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """The arguments as the trace file records them.

    What was typed into a field stays out of the file, because it may be a
    password or personal data; its length is recorded instead.
    """
    if tool_name != "fill" or "value" not in args:
        return args
    shown = {key: val for key, val in args.items() if key != "value"}
    value = args["value"]
    shown["value_chars"] = len(value) if isinstance(value, str) else 0
    return shown


def _without_value(text: Any, value: Any) -> Any:
    """``text`` with the typed value taken out: Playwright's call log in an error repeats it."""
    if isinstance(text, str) and isinstance(value, str) and len(value) >= MIN_SCRUBBED_VALUE_CHARS:
        return text.replace(value, "[value]")
    return text


def _execute_action(
    context: ExplorerContext,
    *,
    tool_name: str,
    args: Dict[str, Any],
    text_for_denial: Optional[str],
    simulator: Callable[[], Dict[str, Any]],
    real: Callable[[], Dict[str, Any]],
    success_prefix: str,
) -> str:
    """Shared body for click / fill / submit.

    The shape is identical across them: forbidden-action check,
    read-only simulate, otherwise call the real adapter, record the
    outcome, return a human-readable string the model sees.
    """
    if text_for_denial is not None and not context.forbidden_guard.check_text_match(
        text_for_denial
    ):
        crossed = context.forbidden_guard.record_violation("text_match", text_for_denial)
        context.trace.record(
            turn=context.turn,
            tool=tool_name,
            args=_loggable_args(tool_name, args),
            result={
                "success": False,
                "reason": "forbidden_text_match",
                "target": text_for_denial,
                "violations": context.forbidden_guard.violations,
            },
            ts_ms=context.now_ms(),
        )
        if crossed:
            context.signal_terminal("forbidden_action_violations_exceeded")
        return (
            f"rejected: forbidden_text_match target={text_for_denial!r}. "
            f"That action is on the forbidden list."
        )

    if context.read_only:
        simulated = simulator()
        context.trace.record(
            turn=context.turn,
            tool=tool_name,
            args=_loggable_args(tool_name, args),
            result={"success": True, "simulated": True, **simulated},
            ts_ms=context.now_ms(),
        )
        return f"simulated: {tool_name}({args}) (read-only)"

    try:
        outcome = real() or {}
    except Exception as exc:
        outcome = {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    success = bool(outcome.get("success"))
    result: Dict[str, Any] = {"success": success}
    for key in ("fallback_used", "verb", "error", "note"):
        if key in outcome:
            result[key] = _without_value(outcome[key], args.get("value")) if tool_name == "fill" else outcome[key]
    context.trace.record(
        turn=context.turn,
        tool=tool_name,
        args=_loggable_args(tool_name, args),
        result=result,
        ts_ms=context.now_ms(),
    )
    if success:
        suffix = outcome.get("verb") or args.get("target") or args.get("label") or ""
        said = f"{success_prefix}: {suffix}" if suffix and not success_prefix.endswith(str(suffix)) else success_prefix
        return f"{said} ({outcome['note']})" if outcome.get("note") else said
    return f"{tool_name}_failed: {_short_error(outcome.get('error') or 'no detail')}"


CLICK_CHAIN_TIMEOUT_MS = 5000
CLICK_CHAIN_POLL_MS = 100


STAND_IN_EVIDENCE = (
    "this page is Knarr's stand-in for a form post it blocked, not the site's own response: "
    "the site never answered, so nothing on it shows the goal was reached"
)


def _quiet_true(check: Optional[Callable[[], bool]]) -> bool:
    """Whether ``check()`` is true; a missing check, or one that raises, is false."""
    if check is None:
        return False
    try:
        return bool(check())
    except Exception:
        return False


def _on_stand_in_page(context: "ExplorerContext") -> bool:
    return _quiet_true(context.stand_in_page)


def _pause(page: Any, seconds: float) -> None:
    """Wait ``seconds`` on the browser's event loop, not on the thread.

    Playwright's sync API only handles the browser's events while a call is
    in progress, and the write block answers paused requests from those
    events. A ``time.sleep`` would leave every blocked write, and every page
    request behind it, waiting for the next poll.
    """
    wait = getattr(page, "wait_for_timeout", None)
    if callable(wait):
        try:
            wait(max(1, int(seconds * 1000)))
            return
        except Exception:
            pass
    time.sleep(seconds)


def _find_first_visible(
    page: Any, selectors: List[str], timeout: int = CLICK_CHAIN_TIMEOUT_MS
) -> Tuple[Optional[Any], Optional[str]]:
    """Return ``(locator, selector)`` for the earliest visible match in ``selectors``.

    Polls the whole chain until ``timeout`` ms have passed, so an element
    that renders late is still found, and the earliest selector in chain
    order wins whenever several match on the same poll. A selector that
    raises (malformed — e.g. a target containing a double quote breaks
    ``:has-text("...")``) is dropped from the chain rather than ending the
    search. Returns ``(None, None)`` when nothing became visible in time.

    Plain Playwright locators only.
    """
    remaining = list(selectors)
    deadline = time.monotonic() + timeout / 1000
    while remaining:
        for selector in list(remaining):
            try:
                locator = page.locator(selector).first
                if locator.is_visible():
                    return locator, selector
            except Exception:
                remaining.remove(selector)
        if time.monotonic() >= deadline:
            break
        _pause(page, CLICK_CHAIN_POLL_MS / 1000)
    return None, None


def _click_target(page: Any, target: str) -> Dict[str, Any]:
    """Fuzzy click adapter.

    Test pages register ``_knarr_click(target) -> dict``. Production
    runs build a candidate-selector chain — data-testid, role-button,
    text-content, role-link — and walk it with ``_find_first_visible``
    so the first visible match wins. ``fallback_used`` is True iff the
    winning selector wasn't the first one in the chain — pattern miner
    uses that signal to flag selector-fragility sites for the eventual
    page object's ``selectors.json``.

    An empty or whitespace-only target is refused: ``:has-text("")`` and
    ``get_by_text("")`` match every element, so it would click whatever
    button happens to come first.
    """
    if hasattr(page, "_knarr_click"):
        return dict(page._knarr_click(target))
    if not str(target or "").strip():
        return {"success": False, "error": "empty click target"}
    selectors = _generate_click_selectors(target)
    try:
        element, used_selector = _find_first_visible(page, selectors)
        if element is not None:
            note = _click_recovering(element)
            fallback_used = bool(selectors) and used_selector != selectors[0]
            return {
                "success": True,
                "fallback_used": fallback_used,
                "selector": used_selector,
                **({"note": note} if note else {}),
            }
        # Last-resort: Playwright's text engine matches loosely on
        # visible text. Counts as fallback because every deterministic
        # selector missed.
        note = _click_recovering(page.get_by_text(target, exact=False).first)
        return {
            "success": True,
            "fallback_used": True,
            "selector": f"text~={target!r}",
            **({"note": note} if note else {}),
        }
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def _click_index(page: Any, index: int) -> Dict[str, Any]:
    """Click the element stamped with ``data-knarr-index=<index>``.

    Test pages register ``_knarr_click_index(index) -> dict``. Production
    resolves the attribute selector that the last ``read_page`` evaluate
    pass stamped onto the node and clicks the first match. A miss (stale
    index after a navigation, or a mutated DOM) surfaces as a failed
    outcome the model recovers from by calling ``read_page`` again.

    ``fallback_used`` is intentionally absent — click-by-index is an exact
    resolution, never a fuzzy fallback, so the pattern miner shouldn't
    count it as selector fragility.
    """
    if hasattr(page, "_knarr_click_index"):
        return dict(page._knarr_click_index(index))
    selector = f'[data-knarr-index="{index}"]'
    try:
        locator = page.locator(selector)
        note = _click_recovering(locator.first, timeout=5000)
        return {"success": True, "selector": selector, **({"note": note} if note else {})}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


def _generate_click_selectors(target: str) -> List[str]:
    """Build the ordered fallback chain for a fuzzy click target.

    Order matters: the chain is tried in sequence, the first visible
    match wins, and ``fallback_used`` is computed against the head.
    The slug variants come first because they hit an app's
    ``data-testid`` attribute when present — that's the most stable
    selector shape and we want to record clean ``fallback_used=False``
    when it works.
    """
    slug = _slug_for_selector(target)
    chain: List[str] = []
    if slug:
        chain.extend([
            f'[data-testid="{slug}"]',
            f'[data-test="{slug}"]',
        ])
    chain.extend([
        f'button:has-text("{target}")',
        f'[role="button"]:has-text("{target}")',
        f'a:has-text("{target}")',
        f'[role="link"]:has-text("{target}")',
        f'[role="tab"]:has-text("{target}")',
    ])
    if slug:
        chain.append(f'[data-testid*="{slug}"]')
    return chain


def _slug_for_selector(target: str) -> str:
    """Lowercased ASCII slug — letters, digits, dashes, underscores.

    Single-word targets become ``"save"``; multi-word collapse on
    dash. Empty / punctuation-only targets return an empty string so
    the caller can skip slug-shaped selectors entirely.
    """
    parts: List[str] = []
    for ch in target.lower():
        if ch.isalnum():
            parts.append(ch)
        elif ch in " -_":
            parts.append("-")
    slug = "".join(parts).strip("-")
    # Collapse runs of dashes.
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


def _reveal_error(page: Any, target: str) -> Dict[str, Any]:
    """Click an error indicator and capture the detail it reveals.

    Same duck-typed pattern as ``_click_target``. Test pages register
    ``_knarr_inspect_error(target) -> dict`` returning the full outcome
    (``clicked`` / ``clicked_label`` / ``candidates`` / ``revealed_text``).

    Production runs a two-pass evaluate: ``_REVEAL_CLICK_JS`` finds and
    clicks a clickable error indicator, then — after a short beat so the
    panel can render — ``_REVEAL_CAPTURE_JS`` reads whatever appeared. The
    click is treated as an *inspection* (like read_page), not a mutation:
    revealing an error panel has no durable side effect.
    """
    if hasattr(page, "_knarr_inspect_error"):
        return dict(page._knarr_inspect_error(target))

    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return {"clicked": False, "candidates": [], "error": "page.evaluate unavailable"}
    try:
        click_res = evaluate(_REVEAL_CLICK_JS, target or "")
    except Exception as exc:
        return {"clicked": False, "candidates": [], "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(click_res, dict):
        return {"clicked": False, "candidates": []}

    clicked = bool(click_res.get("clicked"))
    revealed_text = ""
    if clicked:
        # Let the revealed panel render before reading it. wait_for_timeout
        # is a no-op on test fakes (falls through _safe_call's default).
        _safe_call(page, "wait_for_timeout", 600, default=None)
        try:
            cap_res = evaluate(_REVEAL_CAPTURE_JS)
            if isinstance(cap_res, dict):
                revealed_text = str(cap_res.get("revealed_text") or "")
        except Exception:
            revealed_text = ""
    return {
        "clicked": clicked,
        "clicked_label": click_res.get("clickedLabel"),
        "candidates": list(click_res.get("candidates") or []),
        "revealed_text": revealed_text,
        "debug": list(click_res.get("debug") or []),
    }


def _flatten_json_errors(text: str) -> List[str]:
    """Extract the first balanced JSON object from ``text`` and flatten it
    into ``dotted.path — message`` lines.

    Some apps' "click the red !" panel renders the authoritative error as a
    JSON payload, e.g.::

        {"errors": {"l10n": {"fr-ca": {"details":
          {"headline": {"message": "Text is required ..."}}}}}}

    which flattens to::

        errors.l10n.fr-ca.details.headline.message — Text is required ...

    That names the exact locale + field the validation wants — far more
    actionable than the "Details has validation errors" roll-up. Returns
    an empty list when no parseable JSON object is present (the caller then
    falls back to the raw revealed text).
    """
    if not text:
        return []
    start = text.find("{")
    if start == -1:
        return []
    # Walk the braces to find the matching close, so trailing prose after
    # the JSON blob (or a leading sentence before it) doesn't break parsing.
    depth = 0
    end = -1
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return []
    try:
        data = json.loads(text[start:end])
    except (ValueError, TypeError):
        return []

    lines: List[str] = []

    def _walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                _walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                _walk(value, f"{path}[{idx}]")
        else:
            leaf = "" if node is None else str(node)
            lines.append(f"{path} — {leaf}" if path else leaf)

    _walk(data, "")
    return lines


def _fill_field(page: Any, label: str, value: str) -> Dict[str, Any]:
    """Fill-by-label adapter.

    Same duck-typed pattern as ``_click_target``. Tests register
    ``_knarr_fill(label, value) -> dict``; production tries
    Playwright's ``get_by_label`` then falls back.
    """
    if hasattr(page, "_knarr_fill"):
        return dict(page._knarr_fill(label, value))
    try:
        locator = page.get_by_label(label, exact=False)
        locator.first.fill(value)
        return {"success": True, "fallback_used": False}
    except Exception as exc:
        return {"success": False, "error": f"{type(exc).__name__}: {exc}"}


_BUTTON_NAME_JS = (
    "el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()"
)


def _find_submit_button(page: Any) -> Optional[Tuple[Any, str]]:
    """``(locator, visible name)`` of the first visible submit button, or None.

    Tries ``SUBMIT_VERBS`` in order; for each, an exact name match before a
    substring one, so a plain "Save" wins over "Save and Publish". Nothing
    is clicked here: the caller checks the name against the forbidden list
    first.
    """
    for verb in SUBMIT_VERBS:
        for exact in (True, False):
            try:
                locator = page.get_by_role("button", name=verb, exact=exact).first
                if not locator.is_visible():
                    continue
                name = locator.evaluate(_BUTTON_NAME_JS)
                return locator, (name.strip() if isinstance(name, str) and name.strip() else verb)
            except Exception:
                continue
    return None


_COVERED_BY = re.compile(r"-\s+(<[^\n]{0,400}?>)[^\n]*?intercepts pointer events")
MAX_ERROR_CHARS = 500


def _short_error(error: Any, limit: int = MAX_ERROR_CHARS) -> str:
    """An error for the agent: what went wrong in a line or two, not Playwright's whole retry log.

    A click blocked by another element names the element in the way; anything
    else is cut at ``limit`` characters. The trace keeps the full text."""
    text = str(error)
    covered = _COVERED_BY.search(text)
    if covered:
        first = text.split("\n", 1)[0].strip()
        return (f"{first} Another element covers the target: {covered.group(1)[:200]} "
                "Scroll, close what is in the way, or navigate to the link's address.")
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


COVERED_CLICK_NOTE = "another element covered it, so Knarr sent the click to it directly"
_COVERED_PROBE_MS = 2500


def _click_recovering(locator: Any, timeout: int = 5000) -> Optional[str]:
    """Click; if another element covers the target, send the click to the target itself.

    Playwright waits out the whole timeout for a header that never moves when
    two fixed elements overlap (a real run on a shop's site: a Buy button under
    the page's own header). The agent chose this element by its index, the
    forbidden-verb check has run on its text, and the write block still applies
    to anything the click does, so after a short wait the click is dispatched
    to the element. Returns a note when it had to be, else None. Any other
    failure gets the rest of the time and then raises as before."""
    probe = min(_COVERED_PROBE_MS, timeout)
    try:
        locator.click(timeout=probe)
        return None
    except Exception as exc:
        if "intercepts pointer events" in str(exc):
            locator.dispatch_event("click", timeout=2000)
            return COVERED_CLICK_NOTE
        if timeout <= probe:
            raise
    locator.click(timeout=timeout - probe)
    return None


def _click_submit_button(locator: Any, name: str) -> Dict[str, Any]:
    note = _click_recovering(locator, timeout=1500)
    return {"success": True, "verb": name, "fallback_used": False, **({"note": note} if note else {})}


def _wait_for(page: Any, description: str, stand_in: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Natural-language wait adapter.

    Test pages register ``_knarr_wait_for(description) -> dict``.
    Production waits for any quoted phrase in the description to become
    visible; with no quotes it waits for ``networkidle`` (no in-flight
    requests for ~500ms), the usual "has the page settled" signal after a
    click. The result says which happened, so a settle is never reported
    as a text match.
    """
    if hasattr(page, "_knarr_wait_for"):
        return dict(page._knarr_wait_for(description))
    start_ms = int(time.time() * 1000)
    phrases = _quoted_phrases(description or "")
    if phrases:
        deadline = time.monotonic() + WAIT_FOR_TIMEOUT_MS / 1000
        while True:
            for phrase in phrases:
                try:
                    if page.get_by_text(phrase, exact=False).first.is_visible():
                        elapsed = int(time.time() * 1000) - start_ms
                        if _quiet_true(stand_in):  # the words are Knarr's own: the site never answered
                            return {"success": False, "matched_at_ms": elapsed, "error": STAND_IN_EVIDENCE}
                        return {"success": True, "matched": phrase, "matched_at_ms": elapsed}
                except Exception:
                    pass
            if time.monotonic() >= deadline:
                elapsed = int(time.time() * 1000) - start_ms
                return {
                    "success": False,
                    "matched_at_ms": elapsed,
                    "error": f"none of {phrases} appeared within {WAIT_FOR_TIMEOUT_MS // 1000}s",
                }
            _pause(page, 0.2)
    try:
        page.wait_for_load_state("networkidle", timeout=WAIT_FOR_TIMEOUT_MS)
        elapsed = int(time.time() * 1000) - start_ms
        return {"success": True, "settled": True, "matched_at_ms": elapsed}
    except Exception as exc:
        elapsed = int(time.time() * 1000) - start_ms
        return {
            "success": False,
            "matched_at_ms": elapsed,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _assert_state(page: Any, description: str, require_all: bool = False,
                  stand_in: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """The state assertion, never satisfied by text on Knarr's own stand-in page.

    ``stand_in`` is asked after the page text was read, so it speaks for the
    page that produced the match."""
    result = _assert_state_text(page, description, require_all)
    if result.get("matches") and _quiet_true(stand_in):
        return {"matches": False, "evidence": STAND_IN_EVIDENCE}
    return result


def _assert_state_text(page: Any, description: str, require_all: bool = False) -> Dict[str, Any]:
    """State-assertion adapter.

    Test pages register ``_knarr_assert_state(description) -> dict``
    with ``{matches: bool, evidence: str}``. Production paths run a
    deterministic check against the page's visible text: extract
    quoted phrases from the description (or fall back to the whole
    description), see whether any appear in ``body.inner_text``.

    The heuristic is intentionally crude — its job is "not Claude
    grading its own work, but also not requiring per-feature
    infrastructure before any run can succeed." Higher-fidelity
    checks belong in a validator that runs after the session and checks
    the result independently of the agent.
    """
    if hasattr(page, "_knarr_assert_state"):
        return dict(page._knarr_assert_state(description))
    phrases = _extract_assert_phrases(description)
    try:
        body = page.locator("body").inner_text(timeout=ASSERT_STATE_TIMEOUT_MS)
    except Exception as exc:
        return {
            "matches": False,
            "evidence": f"could not read page body: {type(exc).__name__}: {exc}",
        }
    normalized = body.lower() if isinstance(body, str) else ""
    if require_all:
        missing = [p for p in phrases if p.lower() not in normalized]
        if phrases and not missing:
            return {
                "matches": True,
                "evidence": f"all of {phrases} found in visible page text",
            }
        return {
            "matches": False,
            "evidence": f"not found in visible page text: {missing or phrases}",
        }
    for phrase in phrases:
        if phrase.lower() in normalized:
            return {
                "matches": True,
                "evidence": f"phrase {phrase!r} found in visible page text",
            }
    return {
        "matches": False,
        "evidence": (
            f"none of the {len(phrases)} candidate phrase(s) found in "
            f"visible page text (phrases={phrases})"
        ),
    }


def _extract_assert_phrases(description: str) -> List[str]:
    """Pull quoted phrases out of an assert_state description.

    A criterion like ``Offer named 'knarr-1' appears in the offers
    list`` returns ``['knarr-1']`` — the quoted bit is the
    operator's signal of "this is the specific thing to check for."
    When the description carries no quotes, the entire description
    is the only candidate phrase; that's the conservative fallback.
    """
    quoted = _quoted_phrases(description)
    if quoted:
        return quoted
    stripped = description.strip()
    return [stripped] if stripped else []


def _quoted_phrases(description: str) -> List[str]:
    """The phrases in double or single quotes, in that order."""
    quoted = re.findall(r'"([^"]+)"', description) + re.findall(
        r"'([^']+)'", description
    )
    return [q.strip() for q in quoted if q.strip()]
