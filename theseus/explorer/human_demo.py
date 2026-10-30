"""Observation of a human demonstration during an ``ask_human`` pause.

When Theseus pauses to ask for help, the operator has two ways to answer:
type an explanation, or just *do the thing* in the live browser. This module
reads back the second kind.

How it works, and why this shape:

  1. Before blocking, ``install_recorder`` injects capture-phase ``click`` and
     ``change`` listeners that push a descriptor onto ``window.__theseusHumanLog``.
  2. Python blocks on ``input()``. Chromium is a separate OS process, so the page
     stays fully interactive while the operator works — this is the same property
     a hold-open browser session relies on.
  3. On resume, ``collect_demo`` reads the log back, removes the listeners, and
     diffs a before/after page snapshot.

Three deliberate choices worth knowing:

**Injected JS + polled read, not ``expose_function``.** Nothing in this repo
uses ``expose_function`` or ``add_init_script``; the established idiom is a JS
blob run through ``page.evaluate`` (see ``explorer_tools._REVEAL_CLICK_JS``).
Following it keeps the test seam identical — every ``evaluate`` call here is
duck-typed the way ``_capture_via_evaluate`` does it, so fake pages that don't
mock ``evaluate`` degrade instead of raising.

**A selector ladder per event, not one selector.** The convenient selector and
the durable one are rarely the same node attribute. Recording ``data-testid``,
``id``, ``name``, ``aria-label``, role+name AND a CSS path — in that order —
lets promotion into a shared selector file pick the stable rung later
instead of being stuck with whatever was easiest to compute now. ``cssPath`` is
last precisely because it is the brittle one.

**Events are attributed to the CONTROL, not the node the mouse hit.** Apps
wrap their controls, so ``event.target`` for a click on a button is routinely a
``<p>``, an ``<svg>`` or a nested ``<span>`` carrying no attributes of its own.
Recording that node yields a css-only ladder, which ``learned_controls`` rightly
refuses to store — so the recorder climbs ≤6 hops to the nearest interactive
ancestor first. Measured against a real React app: without the
climb 1 of 4 demonstrated clicks produced a storable ladder; with it, 5 of 5.

**Values are opt-in.** A demonstration of a copy edit is exactly where real
content — or worse, real user data — lands in a run artifact.
``capture_values=False`` (the default) records the field's label and the
*length* of what was typed, which is enough to prove the field was filled.

**Known limitation: navigation wipes the recorder.** ``window`` is destroyed on
navigation, so if the operator navigates during the pause the event log is lost.
That is reported honestly (``recorder_survived=False``) rather than silently
returning an empty demonstration, and the before/after state diff still shows
the URL change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


# Caps. The event log is bounded because a human clicking around for ten
# minutes could otherwise produce thousands of entries, all of which would
# end up in a prompt.
MAX_EVENTS = 60
MAX_TEXT_CHARS = 120
MAX_PANEL_DEPTH = 3
MAX_CSS_PATH_DEPTH = 6


# ============================================================================
# Records
# ============================================================================

@dataclass(frozen=True)
class SelectorRung:
    """One candidate selector for a demonstrated element.

    ``kind`` is the provenance (``testid`` / ``id`` / ``name`` / ``aria`` /
    ``role`` / ``css``), which is what makes the ladder rankable. ``selector``
    is a CSS selector for every kind except ``role``, where it is a
    human-readable ``role=button name="Save"`` rendering — Playwright can
    consume that shape but CSS cannot, so it must not be pasted into a
    selectors.json rung as-is.
    """

    kind: str
    selector: str

    def to_dict(self) -> Dict[str, str]:
        return {"kind": self.kind, "selector": self.selector}


# Preference order for promoting a ladder to a single durable selector.
# testid first (an app's own stable hook), css last (positional, breaks on
# any DOM reshuffle). ``role`` sits mid-pack: semantically durable but not a
# CSS selector, so it needs translation before it can be used as a rung.
RUNG_PREFERENCE: Tuple[str, ...] = ("testid", "id", "name", "aria", "role", "css")


def best_rung(ladder: Sequence[SelectorRung]) -> Optional[SelectorRung]:
    """Return the most durable rung in a ladder, or None if it's empty.

    Used when promoting a recorded selector into a shared selector file, to
    decide which rung is worth adding. Unknown kinds sort last rather
    than raising, so a future recorder addition degrades to low priority
    instead of breaking promotion.
    """
    if not ladder:
        return None
    return min(
        ladder,
        key=lambda r: (
            RUNG_PREFERENCE.index(r.kind) if r.kind in RUNG_PREFERENCE
            else len(RUNG_PREFERENCE)
        ),
    )


@dataclass(frozen=True)
class DemoEvent:
    """One interaction the operator performed while Theseus waited."""

    kind: str                                   # "click" | "change"
    tag: str = ""
    input_type: str = ""
    text: str = ""
    label: str = ""
    selector_ladder: Tuple[SelectorRung, ...] = ()
    # Nearest labelled ancestors, outermost last — the raw material for the
    # path->UI map (tab -> side-menu section -> field).
    panel_path: Tuple[str, ...] = ()
    value: Optional[str] = None                 # None unless capture_values
    value_len: int = 0

    def describe(self) -> str:
        """One-line rendering for the string handed back to the model."""
        rung = best_rung(self.selector_ladder)
        where = rung.selector if rung is not None else self.tag or "<element>"
        shown = self.text or self.label
        parts = [f"{self.kind} {where}"]
        if shown:
            parts.append(f"{shown!r}")
        if self.kind == "change":
            if self.value is not None:
                parts.append(f"= {self.value!r}")
            elif self.value_len:
                parts.append(f"= <{self.value_len} chars>")
        return " ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "kind": self.kind,
            "tag": self.tag,
            "input_type": self.input_type,
            "text": self.text,
            "label": self.label,
            "selector_ladder": [r.to_dict() for r in self.selector_ladder],
            "panel_path": list(self.panel_path),
            "value_len": self.value_len,
        }
        if self.value is not None:
            out["value"] = self.value
        return out


@dataclass(frozen=True)
class HumanDemo:
    """Everything observed during one pause.

    ``recorder_installed`` False means the JS never went in (no ``evaluate``,
    CSP, closed page). ``recorder_survived`` False means it went in but was
    gone at collect time — almost always a navigation. Both cases still carry
    a usable ``state_diff``, which is why they degrade rather than fail.
    """

    events: Tuple[DemoEvent, ...] = ()
    recorder_installed: bool = False
    recorder_survived: bool = False
    state_diff: Tuple[str, ...] = ()
    url_before: str = ""
    url_after: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.events and not self.state_diff

    def describe(self) -> str:
        """Render for the ``ask_human`` tool's return string.

        This is the whole feedback channel — every Theseus tool returns
        human-readable text that the runner hands back as the tool result, so
        the demonstration reaches Claude with no new plumbing.
        """
        lines: List[str] = []
        if self.events:
            lines.append("while you waited, the operator:")
            lines.extend(f"  - {e.describe()}" for e in self.events)
        elif self.recorder_installed and self.recorder_survived:
            lines.append(
                "while you waited, the operator did not interact with the page "
                "(no clicks or field changes recorded)."
            )
        elif self.recorder_installed and not self.recorder_survived:
            lines.append(
                "the action recorder was lost during the pause (the page "
                "navigated), so individual clicks were not captured."
            )
        else:
            lines.append(
                "the action recorder could not be installed, so individual "
                "clicks were not captured."
            )
        if self.state_diff:
            lines.append("page changed while you waited:")
            lines.extend(f"  - {d}" for d in self.state_diff)
        elif self.events:
            lines.append(
                "page state looks unchanged despite those actions — if you "
                "expected new fields, they may not have appeared."
            )
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "events": [e.to_dict() for e in self.events],
            "recorder_installed": self.recorder_installed,
            "recorder_survived": self.recorder_survived,
            "state_diff": list(self.state_diff),
            "url_before": self.url_before,
            "url_after": self.url_after,
        }


@dataclass(frozen=True)
class StateSnapshot:
    """Coarse page facets, captured before and after the pause.

    Deliberately not ``explorer_tools.PageSummary``: importing that would make
    ``explorer_tools -> (a runner's consult channel) -> human_demo -> explorer_tools``
    a cycle. These few counts and error strings are all the diff needs, and
    keeping them local keeps this module standalone-testable.
    """

    url: str = ""
    title: str = ""
    input_count: int = 0
    button_count: int = 0
    field_errors: Tuple[str, ...] = ()
    open_menus: Tuple[str, ...] = ()
    modal_present: bool = False

    @classmethod
    def from_dict(cls, raw: Any) -> "StateSnapshot":
        if not isinstance(raw, dict):
            return cls()
        return cls(
            url=str(raw.get("url") or ""),
            title=str(raw.get("title") or ""),
            input_count=_as_int(raw.get("input_count")),
            button_count=_as_int(raw.get("button_count")),
            field_errors=_as_str_tuple(raw.get("field_errors")),
            open_menus=_as_str_tuple(raw.get("open_menus")),
            modal_present=bool(raw.get("modal_present")),
        )


def diff_snapshots(before: StateSnapshot, after: StateSnapshot) -> Tuple[str, ...]:
    """Describe what changed between two snapshots, in plain sentences.

    Ordered most-diagnostic first: navigation, then cleared errors (the usual
    proof that a demonstration worked), then new errors, then new controls.
    """
    out: List[str] = []
    if before.url and after.url and before.url != after.url:
        out.append(f"navigated: {before.url} -> {after.url}")

    cleared = [e for e in before.field_errors if e not in after.field_errors]
    appeared = [e for e in after.field_errors if e not in before.field_errors]
    for err in cleared[:5]:
        out.append(f"validation error cleared: {err!r}")
    for err in appeared[:5]:
        out.append(f"new validation error: {err!r}")

    d_inputs = after.input_count - before.input_count
    if d_inputs > 0:
        out.append(f"{d_inputs} new input field(s) now visible")
    elif d_inputs < 0:
        out.append(f"{-d_inputs} input field(s) no longer visible")

    d_buttons = after.button_count - before.button_count
    if d_buttons:
        verb = "appeared" if d_buttons > 0 else "disappeared"
        out.append(f"{abs(d_buttons)} button(s) {verb}")

    new_menus = [m for m in after.open_menus if m not in before.open_menus]
    if new_menus:
        out.append(f"menu opened: {', '.join(new_menus[:5])}")

    if after.modal_present and not before.modal_present:
        out.append("a modal is now open")
    elif before.modal_present and not after.modal_present:
        out.append("the modal closed")

    return tuple(out)


# ============================================================================
# Injected JS
# ============================================================================

# Shared helper source, textually included in both blobs below. Kept as one
# string so the selector-ladder logic can't drift between install and snapshot.
_JS_HELPERS = """
  const MAX_TEXT = %(max_text)d;
  const MAX_PANEL = %(max_panel)d;
  const MAX_CSS = %(max_css)d;
  const norm = (s) => ((s || '') + '').replace(/\\s+/g, ' ').trim().slice(0, MAX_TEXT);
  const isVisible = (el) => {
    if (!el || !el.getBoundingClientRect) return false;
    if (el.getAttribute && el.getAttribute('aria-hidden') === 'true') return false;
    const st = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (st && (st.display === 'none' || st.visibility === 'hidden')) return false;
    const r = el.getBoundingClientRect();
    return !(r.width === 0 && r.height === 0);
  };
  const esc = (v) => ((v || '') + '').replace(/"/g, '\\\\"');
  const labelOf = (el) => {
    if (!el) return '';
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return norm(aria);
    if (el.labels && el.labels.length) return norm(el.labels[0].innerText);
    const ph = el.getAttribute && el.getAttribute('placeholder');
    if (ph) return norm(ph);
    const wrap = el.closest ? el.closest('label') : null;
    if (wrap) return norm(wrap.innerText);
    const id = el.getAttribute && el.getAttribute('id');
    if (id && document.querySelector) {
      try {
        const lbl = document.querySelector('label[for="' + esc(id) + '"]');
        if (lbl) return norm(lbl.innerText);
      } catch (e) { /* invalid id for a selector — skip */ }
    }
    return '';
  };
  const cssPath = (el) => {
    const parts = [];
    let cur = el;
    let depth = 0;
    while (cur && cur.nodeType === 1 && depth < MAX_CSS) {
      let seg = cur.tagName.toLowerCase();
      if (cur.parentNode) {
        const sibs = Array.prototype.filter.call(
          cur.parentNode.children, (c) => c.tagName === cur.tagName);
        if (sibs.length > 1) {
          seg += ':nth-of-type(' + (sibs.indexOf(cur) + 1) + ')';
        }
      }
      parts.unshift(seg);
      if (cur.id) { parts.unshift('#' + cur.id); break; }
      cur = cur.parentElement;
      depth += 1;
    }
    return parts.join(' > ');
  };
  const ladderOf = (el) => {
    const out = [];
    if (!el || !el.getAttribute) return out;
    const testid = el.getAttribute('data-testid');
    if (testid) out.push({kind: 'testid', selector: '[data-testid="' + esc(testid) + '"]'});
    const id = el.getAttribute('id');
    if (id) out.push({kind: 'id', selector: '#' + id});
    const name = el.getAttribute('name');
    if (name) out.push({kind: 'name', selector: '[name="' + esc(name) + '"]'});
    const aria = el.getAttribute('aria-label');
    if (aria) out.push({kind: 'aria', selector: '[aria-label="' + esc(aria) + '"]'});
    const role = el.getAttribute('role')
      || (el.tagName === 'BUTTON' ? 'button' : (el.tagName === 'A' ? 'link' : ''));
    if (role) {
      const nm = norm(el.innerText) || labelOf(el);
      out.push({kind: 'role', selector: 'role=' + role + (nm ? ' name="' + esc(nm) + '"' : '')});
    }
    out.push({kind: 'css', selector: cssPath(el)});
    return out;
  };
  // event.target is the innermost node the mouse actually hit — apps wrap
  // their controls, so a click on a button commonly reports a <p>, <svg> or
  // nested <span> with no attributes of its own. Recording THAT node yields a
  // css-only ladder, which is worthless (positional) and which
  // controls_from_demo rightly refuses to store. So climb to the control.
  //
  // Verified against a real React app: without this climb, 3 of 4
  // demonstrated clicks produced css-only ladders. Same ≤6-hop approach as
  // explorer_tools._REVEAL_CLICK_JS, for the same reason.
  const INTERACTIVE_TAGS = {BUTTON: 1, A: 1, INPUT: 1, SELECT: 1, TEXTAREA: 1, LABEL: 1};
  const INTERACTIVE_ROLES = {
    button: 1, link: 1, tab: 1, menuitem: 1, option: 1, checkbox: 1,
    radio: 1, combobox: 1, switch: 1, textbox: 1,
  };
  const isInteractive = (el) => {
    if (!el || el.nodeType !== 1 || !el.getAttribute) return false;
    if (INTERACTIVE_TAGS[el.tagName]) return true;
    const role = el.getAttribute('role');
    if (role && INTERACTIVE_ROLES[role]) return true;
    if (el.hasAttribute('data-testid')) return true;
    if (el.hasAttribute('onclick') || el.hasAttribute('tabindex')) return true;
    return false;
  };
  const controlFor = (el) => {
    let cur = el;
    let hops = 0;
    while (cur && cur.nodeType === 1 && hops < 6) {
      if (isInteractive(cur)) return cur;
      cur = cur.parentElement;
      hops += 1;
    }
    // Nothing interactive within reach — fall back to the original node so the
    // event is still reported rather than silently dropped.
    return el;
  };
  const panelOf = (el) => {
    // Walk up collecting the nearest identifying labels: a testid, a dialog
    // aria-label, or a section heading. This is what turns a bare selector
    // into "which panel am I in", which a path->UI map needs.
    const out = [];
    let cur = el ? el.parentElement : null;
    let hops = 0;
    while (cur && out.length < MAX_PANEL && hops < 40) {
      const tid = cur.getAttribute && cur.getAttribute('data-testid');
      const rl = cur.getAttribute && cur.getAttribute('role');
      if (tid) {
        out.push(tid);
      } else if (rl === 'dialog' || rl === 'tabpanel') {
        const nm = cur.getAttribute('aria-label');
        out.push(rl + (nm ? ':' + norm(nm) : ''));
      } else if (cur.querySelector) {
        const h = cur.querySelector(':scope > h1, :scope > h2, :scope > h3');
        if (h) { const t = norm(h.innerText); if (t) out.push(t); }
      }
      cur = cur.parentElement;
      hops += 1;
    }
    return out;
  };
  const snapshot = () => {
    const errSel = '[aria-invalid="true"], [class*="error-message"], [class*="errorText"]';
    const errs = [];
    Array.prototype.forEach.call(document.querySelectorAll(errSel), (e) => {
      if (errs.length < 10 && isVisible(e)) { const t = norm(e.innerText); if (t) errs.push(t); }
    });
    const menus = [];
    Array.prototype.forEach.call(
      document.querySelectorAll('[role="menu"], [role="listbox"]'), (m) => {
        if (menus.length < 10 && isVisible(m)) { const t = norm(m.innerText); if (t) menus.push(t); }
      });
    const vis = (sel) => Array.prototype.filter.call(
      document.querySelectorAll(sel), isVisible).length;
    const modal = document.querySelector('[role="dialog"], [role="alertdialog"]');
    return {
      url: location.href,
      title: document.title || '',
      input_count: vis('input, textarea, select'),
      button_count: vis('button, [role="button"]'),
      field_errors: errs,
      open_menus: menus,
      modal_present: !!(modal && isVisible(modal)),
    };
  };
""" % {
    "max_text": MAX_TEXT_CHARS,
    "max_panel": MAX_PANEL_DEPTH,
    "max_css": MAX_CSS_PATH_DEPTH,
}


# Install: attach capture-phase listeners and return the "before" snapshot in
# the same round trip. Capture phase (third arg true) matters — an app's React
# handlers frequently stopPropagation, which would starve a bubble-phase
# listener. Listeners are passive observers; they never preventDefault, so the
# operator's interaction behaves exactly as it would without us watching.
_RECORDER_INSTALL_JS = """
(opts) => {
%(helpers)s
  const MAX_EVENTS = %(max_events)d;
  const captureValues = !!(opts && opts.captureValues);
  if (window.__theseusRecorder) {
    return {installed: true, already: true, snapshot: snapshot()};
  }
  const log = [];
  const push = (kind, raw) => {
    if (!raw || raw.nodeType !== 1 || log.length >= MAX_EVENTS) return;
    // Attribute the event to the CONTROL, not the inner node the mouse hit.
    const el = controlFor(raw);
    let value = null;
    let valueLen = 0;
    const isField = 'value' in el && typeof el.value === 'string';
    if (isField) {
      valueLen = el.value.length;
      if (captureValues) value = el.value.slice(0, MAX_TEXT);
    }
    // Prefer the control's own text; fall back to the clicked node's, which is
    // often where the visible label actually lives (a <p> inside a <button>).
    const text = norm(el.innerText) || norm(raw.innerText);
    const rec = {
      kind: kind,
      tag: el.tagName ? el.tagName.toLowerCase() : '',
      input_type: (el.getAttribute && el.getAttribute('type')) || '',
      text: text,
      label: labelOf(el) || labelOf(raw),
      selector_ladder: ladderOf(el),
      panel_path: panelOf(el),
      value_len: valueLen,
    };
    if (value !== null) rec.value = value;
    log.push(rec);
  };
  const onClick = (ev) => { try { push('click', ev.target); } catch (e) {} };
  const onChange = (ev) => { try { push('change', ev.target); } catch (e) {} };
  document.addEventListener('click', onClick, true);
  document.addEventListener('change', onChange, true);
  window.__theseusRecorder = {log: log, onClick: onClick, onChange: onChange};
  return {installed: true, already: false, snapshot: snapshot()};
}
""" % {"helpers": _JS_HELPERS, "max_events": MAX_EVENTS}


# Collect: detach, hand back the log, and take the "after" snapshot. A missing
# window.__theseusRecorder here means navigation wiped it — reported as
# survived:false rather than an empty success.
_RECORDER_COLLECT_JS = """
() => {
%(helpers)s
  const rec = window.__theseusRecorder;
  if (!rec) return {survived: false, events: [], snapshot: snapshot()};
  try {
    document.removeEventListener('click', rec.onClick, true);
    document.removeEventListener('change', rec.onChange, true);
  } catch (e) { /* detaching a dead listener is harmless */ }
  try { delete window.__theseusRecorder; } catch (e) { window.__theseusRecorder = null; }
  return {survived: true, events: rec.log, snapshot: snapshot()};
}
""" % {"helpers": _JS_HELPERS}


# ============================================================================
# Python-side driver
# ============================================================================

def install_recorder(page: Any, *, capture_values: bool = False) -> Tuple[bool, StateSnapshot]:
    """Inject the recorder and capture the pre-pause snapshot.

    Returns ``(installed, before_snapshot)``. ``installed`` False means the
    caller should expect no event log — the pause still works, and the state
    diff still gets computed from whatever the fallback snapshot yields.

    Never raises: an un-installable recorder must not be able to abort a run
    that a human is standing by to help with.
    """
    raw = _evaluate(page, _RECORDER_INSTALL_JS, {"captureValues": bool(capture_values)})
    if not isinstance(raw, dict):
        return False, _fallback_snapshot(page)
    return bool(raw.get("installed")), StateSnapshot.from_dict(raw.get("snapshot"))


def collect_demo(
    page: Any,
    *,
    installed: bool,
    before: StateSnapshot,
) -> HumanDemo:
    """Read the event log back, detach the recorder, and diff page state."""
    # Test seam, matching explorer_tools' ``_theseus_*`` fixture convention:
    # a fake page can set ``_theseus_human_log`` to a list of event dicts and
    # skip the JS entirely.
    fixture_log = getattr(page, "_theseus_human_log", None)
    if isinstance(fixture_log, (list, tuple)):
        after = _fallback_snapshot(page)
        return HumanDemo(
            events=_parse_events(fixture_log),
            recorder_installed=True,
            recorder_survived=True,
            state_diff=diff_snapshots(before, after),
            url_before=before.url,
            url_after=after.url,
        )

    raw = _evaluate(page, _RECORDER_COLLECT_JS)
    if not isinstance(raw, dict):
        after = _fallback_snapshot(page)
        return HumanDemo(
            events=(),
            recorder_installed=installed,
            recorder_survived=False,
            state_diff=diff_snapshots(before, after),
            url_before=before.url,
            url_after=after.url,
        )

    after = StateSnapshot.from_dict(raw.get("snapshot"))
    return HumanDemo(
        events=_parse_events(raw.get("events")),
        recorder_installed=installed,
        recorder_survived=bool(raw.get("survived")),
        state_diff=diff_snapshots(before, after),
        url_before=before.url,
        url_after=after.url,
    )


# ============================================================================
# Internals
# ============================================================================

def _evaluate(page: Any, script: str, arg: Any = None) -> Any:
    """Duck-typed ``page.evaluate``, mirroring ``_capture_via_evaluate``.

    Returns None when evaluate is absent (fake pages), raises inside the page
    (JS error, closed page, CSP), or is otherwise unusable. Callers treat None
    as "degrade", never as an error.
    """
    evaluate = getattr(page, "evaluate", None)
    if not callable(evaluate):
        return None
    try:
        return evaluate(script) if arg is None else evaluate(script, arg)
    except Exception:
        return None


def _fallback_snapshot(page: Any) -> StateSnapshot:
    """Snapshot without JS — URL only, plus the test-fixture attributes.

    Enough for the diff to still report a navigation, which is the single most
    useful thing to know when the recorder didn't survive.
    """
    url = ""
    try:
        raw_url = getattr(page, "url", "")
        url = str(raw_url() if callable(raw_url) else raw_url or "")
    except Exception:
        url = ""
    errors = getattr(page, "_theseus_field_errors", ())
    menus = getattr(page, "_theseus_open_menus", ())
    return StateSnapshot(
        url=url,
        field_errors=_as_str_tuple(errors),
        open_menus=_as_str_tuple(menus),
    )


def _parse_events(raw: Any) -> Tuple[DemoEvent, ...]:
    """Convert the JS log into DemoEvents, dropping anything malformed.

    Defensive by intent: this data crossed a JS/Python boundary and lands in
    both a prompt and a persisted artifact, so a single odd entry should cost
    that entry and nothing more.
    """
    if not isinstance(raw, (list, tuple)):
        return ()
    events: List[DemoEvent] = []
    for item in raw[:MAX_EVENTS]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in ("click", "change"):
            continue
        value = item.get("value")
        events.append(
            DemoEvent(
                kind=kind,
                tag=str(item.get("tag") or ""),
                input_type=str(item.get("input_type") or ""),
                text=str(item.get("text") or "")[:MAX_TEXT_CHARS],
                label=str(item.get("label") or "")[:MAX_TEXT_CHARS],
                selector_ladder=_parse_ladder(item.get("selector_ladder")),
                panel_path=_as_str_tuple(item.get("panel_path"))[:MAX_PANEL_DEPTH],
                value=None if value is None else str(value)[:MAX_TEXT_CHARS],
                value_len=_as_int(item.get("value_len")),
            )
        )
    return tuple(events)


def _parse_ladder(raw: Any) -> Tuple[SelectorRung, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    rungs: List[SelectorRung] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        selector = str(item.get("selector") or "").strip()
        if kind and selector:
            rungs.append(SelectorRung(kind=kind, selector=selector))
    return tuple(rungs)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_str_tuple(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v) for v in value if v is not None)
