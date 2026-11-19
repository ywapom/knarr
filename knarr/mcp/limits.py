"""Limits on what a tool call may carry.

The arguments come from the model, and through it from whatever a web page
managed to put in front of it. A length cap keeps one call from being a
denial of service (a megabyte typed into a field, a URL no browser accepts) and
a NUL character is refused because nothing downstream expects one. The numbers
are generous: they are well above anything a real call needs, and a refusal
names the limit so the model can shorten the text. A refused call does not use
up a turn, and it is not a guard violation.

The message that reaches the server over stdio is read whole by the MCP SDK
before any of this runs, so these limits cap what is used, not what is read.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

MAX_URL_CHARS = 2048
MAX_GOAL_CHARS = 2000
MAX_CRITERION_CHARS = 1000
MAX_LABEL_CHARS = 500  # a control's name, a click target, a phrase to wait for
MAX_FILL_VALUE_CHARS = 10_000
MAX_TEXT_CHARS = 2000  # a note, the reason for giving up
MAX_INDEX = 100_000

# tool -> argument -> the most characters it may have
TEXT_LIMITS: Dict[str, Dict[str, int]] = {
    "click": {"target": MAX_LABEL_CHARS},
    "fill": {"label": MAX_LABEL_CHARS, "value": MAX_FILL_VALUE_CHARS},
    "navigate": {"url": MAX_URL_CHARS},
    "wait_for": {"description": MAX_LABEL_CHARS},
    "inspect_error": {"target": MAX_LABEL_CHARS},
    "note": {"text": MAX_TEXT_CHARS},
    "give_up": {"reason": MAX_TEXT_CHARS},
}
SESSION_START_LIMITS = {
    "start_url": MAX_URL_CHARS,
    "goal": MAX_GOAL_CHARS,
    "success_criterion": MAX_CRITERION_CHARS,
}


def _problem(name: str, value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None  # a wrong type is the caller's error and is reported where it is used
    if len(value) > limit:
        return f"{name} is {len(value)} characters; the limit is {limit}. Shorten it."
    if "\x00" in value:
        return f"{name} contains a NUL character, which is not allowed."
    return None


def check_tool_arguments(tool: str, arguments: Mapping[str, Any]) -> Optional[str]:
    """Why a browser tool's arguments are refused, or None when they are fine."""
    for name, limit in TEXT_LIMITS.get(tool, {}).items():
        problem = _problem(name, arguments.get(name), limit)
        if problem:
            return problem
    if tool == "click_index":
        index = arguments.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and not 0 <= index <= MAX_INDEX:
            return f"index is {index}; it must be from 0 to {MAX_INDEX}."
    return None


def check_session_start(start_url: Any, goal: Any, success_criterion: Any) -> Optional[str]:
    """Why ``session_start``'s arguments are refused, or None when they are fine."""
    given = {"start_url": start_url, "goal": goal, "success_criterion": success_criterion}
    for name, limit in SESSION_START_LIMITS.items():
        problem = _problem(name, given[name], limit)
        if problem:
            return problem
    return None
