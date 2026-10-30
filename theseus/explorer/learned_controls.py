"""Durable record of controls a human taught Theseus.

Every answered ``ask_human`` pause produces a :class:`LearnedControl` — the
question, the operator's words, and the selector ladder observed from their
demonstration. Those records are written twice:

  - ``<run_dir>/learned_controls.jsonl`` — this run's own artifact, next to
    ``trace.jsonl``, for post-hoc review of a single session.
  - ``runs/explorer/learned/<feature>.jsonl`` — the **cumulative** per-feature
    file, which :func:`load_learned_controls` reads back at the start of every
    later run so the prompt can carry "controls previously taught to you".

That second file is what makes a teaching session worth having. Without it the
operator re-explains the locale selector on every run.

**Why promotion into shipped config is a separate, manual step.** These records
never auto-merge into a shared selector file. A selector here was inferred
from one demonstration, mediated by an LLM's description of what it needed —
good enough to try next run, not good enough to ship to every consumer of a
shared selector file. Promoting a record means adding it as an *additional
rung* under human review: most keys in a typical selector file have a single
rung, so a wrong rung 0 is exactly the failure mode worth not automating.

Fail-safe like ``TraceWriter``, and for the same reason: a disk problem during a
long attended run must cost a record, not the run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from theseus.explorer.human_demo import DemoEvent, SelectorRung, best_rung


# Cumulative store, relative to the ``runs/`` root the CLI already uses for
# per-run directories. One file per feature so a run only loads what it needs.
LEARNED_SUBDIR = "learned"
DEFAULT_FILENAME = "learned_controls.jsonl"

# How many prior records a prompt will carry. The block is rebuilt on every
# outer turn, so this is a per-turn token cost, not a one-off.
MAX_PROMPT_CONTROLS = 12


@dataclass(frozen=True)
class LearnedControl:
    """One control a human pointed Theseus at.

    ``intent`` is what the agent was trying to reach, taken from its own
    question — that is what a later run matches on, since the selector is
    exactly the thing it doesn't know yet.
    """

    feature: str
    intent: str
    question: str = ""
    human_text: str = ""
    url: str = ""
    selector_ladder: Tuple[SelectorRung, ...] = ()
    label: str = ""
    panel_path: Tuple[str, ...] = ()
    locale: str = ""
    storefront: str = ""
    action_kind: str = ""          # "click" | "change"
    source: str = "human_demo"     # or "human_text" when nothing was observed
    run_id: str = ""

    @property
    def selector(self) -> str:
        """The most durable rung, or empty when the record is text-only."""
        rung = best_rung(self.selector_ladder)
        return rung.selector if rung is not None else ""

    def describe(self) -> str:
        """One line for the prompt's "previously taught" block."""
        bits = [f"{self.intent!r}"]
        if self.selector:
            bits.append(f"-> {self.selector}")
        if self.label:
            bits.append(f"(label: {self.label})")
        if self.panel_path:
            bits.append(f"in {' / '.join(self.panel_path)}")
        if self.locale:
            bits.append(f"[locale {self.locale}]")
        if self.storefront:
            bits.append(f"[storefront {self.storefront}]")
        return " ".join(bits)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature": self.feature,
            "intent": self.intent,
            "question": self.question,
            "human_text": self.human_text,
            "url": self.url,
            "selector_ladder": [r.to_dict() for r in self.selector_ladder],
            "label": self.label,
            "panel_path": list(self.panel_path),
            "locale": self.locale,
            "storefront": self.storefront,
            "action_kind": self.action_kind,
            "source": self.source,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["LearnedControl"]:
        """Parse one JSONL line. Returns None for anything unusable.

        Tolerant on purpose: this file accumulates across runs and code
        versions, so one stale-shaped line should not blind a run to all the
        others.
        """
        if not isinstance(raw, dict):
            return None
        feature = str(raw.get("feature") or "").strip()
        intent = str(raw.get("intent") or "").strip()
        if not feature or not intent:
            return None
        ladder: List[SelectorRung] = []
        for item in raw.get("selector_ladder") or ():
            if isinstance(item, dict):
                kind = str(item.get("kind") or "").strip()
                selector = str(item.get("selector") or "").strip()
                if kind and selector:
                    ladder.append(SelectorRung(kind=kind, selector=selector))
        panel = raw.get("panel_path")
        return cls(
            feature=feature,
            intent=intent,
            question=str(raw.get("question") or ""),
            human_text=str(raw.get("human_text") or ""),
            url=str(raw.get("url") or ""),
            selector_ladder=tuple(ladder),
            label=str(raw.get("label") or ""),
            panel_path=tuple(str(p) for p in panel) if isinstance(panel, (list, tuple)) else (),
            locale=str(raw.get("locale") or ""),
            storefront=str(raw.get("storefront") or ""),
            action_kind=str(raw.get("action_kind") or ""),
            source=str(raw.get("source") or "human_demo"),
            run_id=str(raw.get("run_id") or ""),
        )


# ============================================================================
# Deriving records from a demonstration
# ============================================================================

# Locale codes as they appear in copy field ids — "es-mx", "en-US".
# Matched on selector text and label because the locale is the single most
# valuable dimension to capture for localized copy work, and it is usually right
# there in the field id (e.g. "#title-input-en-us").
_LOCALE_RE = re.compile(r"\b([a-z]{2}-[a-zA-Z]{2})\b")

# Longest intent kept on a record. The model's question is often several
# paragraphs — on live runs Claude often asks in ~450 characters — and
# ``intent`` is what gets rendered into every later run's prompt AND into
# whatever promotes a record into a shared selector file. The full text is preserved on
# ``question``; ``intent`` is the short handle.
MAX_INTENT_CHARS = 90


def shorten_intent(text: str) -> str:
    """Reduce a model's question to a short, matchable handle.

    Takes the first sentence (or the first clause up to the character cap),
    strips a leading "I need "/"Where is " style preamble, and drops the
    trailing question mark. Purely cosmetic in effect but load-bearing in
    practice: an unshortened intent makes the prompt block unreadable and
    crowds out the other lessons competing for the same slots.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return "unnamed control"
    # First sentence, if there is a natural break early enough to be useful.
    for stop in (". ", "? ", "! ", " — ", " - "):
        idx = cleaned.find(stop)
        if 0 < idx <= MAX_INTENT_CHARS:
            cleaned = cleaned[:idx]
            break
    cleaned = cleaned.rstrip(" ?.!")
    if len(cleaned) > MAX_INTENT_CHARS:
        cleaned = cleaned[:MAX_INTENT_CHARS].rsplit(" ", 1)[0] + "…"
    return cleaned or "unnamed control"


def controls_from_demo(
    events: Sequence[DemoEvent],
    *,
    feature: str,
    intent: str,
    question: str = "",
    human_text: str = "",
    url: str = "",
    run_id: str = "",
) -> List[LearnedControl]:
    """Turn observed events into learnable records.

    One record per event that carries a usable selector ladder. Events without
    one (a click on a bare ``<div>`` with no attributes and no stable path) are
    dropped — a record whose only selector is a six-deep ``nth-of-type`` chain
    is more likely to mislead a later run than to help it.

    When nothing at all was observed but the operator did type guidance, the
    caller still gets a single text-only record via :func:`control_from_text`,
    because the words are worth keeping even without a selector.
    """
    out: List[LearnedControl] = []
    for event in events:
        rung = best_rung(event.selector_ladder)
        if rung is None or rung.kind == "css":
            # css-only means no stable attribute existed on the node. Keep the
            # ladder out of the store rather than teach a positional path.
            continue
        out.append(
            LearnedControl(
                feature=feature,
                intent=shorten_intent(intent),
                question=question,
                human_text=human_text,
                url=url,
                selector_ladder=event.selector_ladder,
                label=event.label or event.text,
                panel_path=event.panel_path,
                locale=_sniff_locale(rung.selector, event.label, event.text),
                action_kind=event.kind,
                source="human_demo",
                run_id=run_id,
            )
        )
    return out


def control_from_text(
    *,
    feature: str,
    intent: str,
    question: str = "",
    human_text: str = "",
    url: str = "",
    run_id: str = "",
) -> LearnedControl:
    """A record carrying only the operator's words — no selector observed."""
    return LearnedControl(
        feature=feature,
        intent=shorten_intent(intent),
        question=question,
        human_text=human_text,
        url=url,
        source="human_text",
        run_id=run_id,
    )


def _sniff_locale(*candidates: str) -> str:
    """Best-effort locale extraction from a selector or label.

    Returns "" when nothing matches, which is the common case and not a
    problem — locale is an enrichment for a path->UI map, never a
    correctness requirement.
    """
    for text in candidates:
        if not text:
            continue
        match = _LOCALE_RE.search(text)
        if match:
            return match.group(1)
    return ""


# ============================================================================
# Writer
# ============================================================================

class LearnedControlWriter:
    """Append-only JSONL writer, fail-safe, dual-destination.

    Modelled directly on ``trace.TraceWriter``: lazy file creation, silent drop
    on a serialization or disk error, optional ``run_logger`` for a warn line.
    Single-process only — two Python processes appending to the same
    per-feature file would interleave partial lines.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        cumulative_dir: Optional[Path] = None,
        run_logger: Any = None,
        filename: str = DEFAULT_FILENAME,
    ) -> None:
        self._run_dir = Path(run_dir)
        self._path = self._run_dir / filename
        self._cumulative_dir = Path(cumulative_dir) if cumulative_dir else None
        self._run_logger = run_logger
        self._record_count = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def record_count(self) -> int:
        return self._record_count

    def cumulative_path(self, feature: str) -> Optional[Path]:
        if self._cumulative_dir is None:
            return None
        return self._cumulative_dir / f"{_safe_feature(feature)}.jsonl"

    def record(self, control: LearnedControl) -> None:
        """Persist one control to both destinations. Never raises."""
        try:
            serialized = json.dumps(control.to_dict(), ensure_ascii=False)
        except (TypeError, ValueError):
            self._log_warn(
                f"LearnedControlWriter: dropped record intent="
                f"{control.intent!r} (payload not JSON-serializable)"
            )
            return

        wrote_any = False
        if self._append(self._path, serialized):
            wrote_any = True
        cumulative = self.cumulative_path(control.feature)
        if cumulative is not None:
            # A failure here is worth a distinct warning: the run artifact may
            # be fine while the thing that makes the lesson durable silently
            # isn't, and that is the failure an operator would want to know
            # about before they close the terminal.
            if not self._append(cumulative, serialized):
                self._log_warn(
                    f"LearnedControlWriter: run artifact written but cumulative "
                    f"store {cumulative} was not — this lesson will NOT carry "
                    f"into the next run."
                )
        if wrote_any:
            self._record_count += 1

    def record_all(self, controls: Sequence[LearnedControl]) -> int:
        before = self._record_count
        for control in controls:
            self.record(control)
        return self._record_count - before

    def _append(self, path: Path, serialized: str) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(serialized)
                fh.write("\n")
            return True
        except OSError as exc:
            self._log_warn(f"LearnedControlWriter: append to {path} failed ({exc})")
            return False

    def _log_warn(self, msg: str) -> None:
        if self._run_logger is not None and hasattr(self._run_logger, "log_warning"):
            try:
                self._run_logger.log_warning(msg)
            except Exception:
                pass


# ============================================================================
# Reader
# ============================================================================

def load_learned_controls(
    feature: str,
    *,
    cumulative_dir: Optional[Path] = None,
    limit: int = MAX_PROMPT_CONTROLS,
) -> List[LearnedControl]:
    """Read prior lessons for one feature, newest first.

    Newest first because a control taught twice was probably taught differently
    the second time — the site changed, or the first answer was wrong — and the
    later record should win the prompt slot. Deduplicated by
    ``(intent, selector)`` so repeat teachings of the same thing don't crowd
    out other lessons.

    Returns ``[]`` for a missing file, an unreadable file, or a feature never
    taught. Never raises: a corrupt store must not stop a run from starting.
    """
    if cumulative_dir is None:
        return []
    path = Path(cumulative_dir) / f"{_safe_feature(feature)}.jsonl"
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    controls: List[LearnedControl] = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        control = LearnedControl.from_dict(parsed)
        if control is not None:
            controls.append(control)

    seen: set = set()
    out: List[LearnedControl] = []
    for control in reversed(controls):
        key = (control.intent.lower(), control.selector)
        if key in seen:
            continue
        seen.add(key)
        out.append(control)
        if len(out) >= max(0, limit):
            break
    return out


def render_prompt_block(controls: Sequence[LearnedControl]) -> str:
    """Render prior lessons for a runner's prompt. Empty string when none.

    The wording matters: it tells the model these came from a human and are
    worth trying *first*, but also that they may be stale, so a failure to find
    one is a reason to look around rather than to give up.
    """
    if not controls:
        return ""
    lines = [
        "CONTROLS A HUMAN PREVIOUSLY TAUGHT YOU (for this feature):",
        "  Try these before hunting. They came from an operator demonstrating",
        "  on a real page, but the site may have changed since — if one doesn't",
        "  resolve, fall back to read_page rather than giving up.",
    ]
    for control in controls:
        lines.append(f"  - {control.describe()}")
        if control.human_text:
            lines.append(f"      operator note: {control.human_text}")
    return "\n".join(lines)


def _safe_feature(feature: str) -> str:
    """Sanitize a feature name for use as a filename.

    Feature names come from the runner's own configuration, so this
    is defence in depth rather than untrusted-input handling — but the value
    does reach a filesystem path, and a traversal there would write outside the
    runs directory.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", (feature or "unknown").strip())
    cleaned = cleaned.strip("._") or "unknown"
    return cleaned[:64]
