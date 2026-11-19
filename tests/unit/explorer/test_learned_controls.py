"""Unit tests for the learned-controls store.

This is the layer that makes a teaching session worth having: without durable,
*reloadable* records, the operator re-explains the same locale selector on every
run. So the tests here concentrate on two things — that a lesson survives to the
next run, and that a bad lesson never gets stored.

The second matters more than it looks. Most keys in a typical selector file
have a single rung, which means a wrong selector does not degrade, it just
times out opaquely. That is why ``controls_from_demo``
refuses css-only ladders and why promotion into shipped config is a separate
reviewed step rather than something this module does.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from knarr.explorer.human_demo import DemoEvent, SelectorRung
from knarr.explorer.learned_controls import (
    LearnedControl,
    LearnedControlWriter,
    control_from_text,
    controls_from_demo,
    load_learned_controls,
    render_prompt_block,
    _safe_feature,
)


# ============================================================================
# Helpers
# ============================================================================

def _event(
    *,
    kind: str = "click",
    ladder: List[SelectorRung] = None,
    label: str = "Locale",
    text: str = "English (US)",
    panel: tuple = ("messages-panel", "Messages"),
) -> DemoEvent:
    if ladder is None:
        ladder = [SelectorRung("testid", '[data-testid="locale-select"]')]
    return DemoEvent(
        kind=kind,
        selector_ladder=tuple(ladder),
        label=label,
        text=text,
        panel_path=panel,
    )


def _control(**kwargs: Any) -> LearnedControl:
    defaults: Dict[str, Any] = {
        "feature": "offers",
        "intent": "where is the locale selector?",
        "selector_ladder": (SelectorRung("testid", '[data-testid="locale-select"]'),),
    }
    defaults.update(kwargs)
    return LearnedControl(**defaults)


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# ============================================================================
# Deriving controls from a demonstration
# ============================================================================

def test_control_derived_from_a_demonstrated_click():
    controls = controls_from_demo(
        [_event()],
        feature="offers",
        intent="where is the locale selector?",
        human_text="top right",
        url="https://test.example.com/offer/1",
        run_id="run-1",
    )

    assert len(controls) == 1
    control = controls[0]
    assert control.selector == '[data-testid="locale-select"]'
    assert control.panel_path == ("messages-panel", "Messages")
    assert control.source == "human_demo"
    assert control.run_id == "run-1"


def test_css_only_ladder_is_refused():
    """A six-deep nth-of-type chain is likelier to mislead than to help.

    Storing it would teach the next run a positional path that breaks on any
    DOM reshuffle — and on the offers surface a broken single rung surfaces as
    an opaque timeout, not a named failure.
    """
    controls = controls_from_demo(
        [_event(ladder=[SelectorRung("css", "div > div > button:nth-of-type(3)")])],
        feature="offers",
        intent="the thing",
    )
    assert controls == []


def test_event_without_a_ladder_is_refused():
    controls = controls_from_demo(
        [_event(ladder=[])], feature="offers", intent="the thing"
    )
    assert controls == []


@pytest.mark.parametrize("kind", ["testid", "id", "name", "aria", "role"])
def test_any_stable_rung_is_accepted(kind: str):
    controls = controls_from_demo(
        [_event(ladder=[SelectorRung(kind, "sel"), SelectorRung("css", "div")])],
        feature="offers",
        intent="the thing",
    )
    assert len(controls) == 1
    assert controls[0].selector == "sel"


def test_locale_is_sniffed_from_the_selector():
    """Locale is the highest-value dimension for localized copy work, and it's
    usually right there in the field id."""
    controls = controls_from_demo(
        [
            _event(
                kind="change",
                ladder=[SelectorRung("id", "#title-input-es-mx")],
                label="Title",
                text="",
            )
        ],
        feature="offers",
        intent="spanish title field",
    )
    assert controls[0].locale == "es-mx"


def test_locale_absent_is_not_a_problem():
    controls = controls_from_demo(
        [_event(ladder=[SelectorRung("testid", '[data-testid="save"]')], text="Save")],
        feature="offers",
        intent="save button",
    )
    assert controls[0].locale == ""


def test_multiple_events_yield_multiple_controls():
    controls = controls_from_demo(
        [
            _event(ladder=[SelectorRung("testid", '[data-testid="a"]')]),
            _event(ladder=[SelectorRung("id", "#b")], kind="change"),
        ],
        feature="offers",
        intent="two things",
    )
    assert len(controls) == 2
    assert [c.action_kind for c in controls] == ["click", "change"]


def test_text_only_control_keeps_the_words():
    """Words without a selector are still worth keeping.

    Often the real answer — "that field only appears after you switch
    storefront" — is not a selector at all.
    """
    control = control_from_text(
        feature="offers",
        intent="why is the field missing?",
        human_text="it only appears after you pick a storefront",
    )
    assert control.source == "human_text"
    assert control.selector == ""
    assert "storefront" in control.human_text


# ============================================================================
# Writer
# ============================================================================

def test_writer_writes_run_and_cumulative_copies(tmp_path: Path):
    """Two destinations, two jobs: the run artifact reviews one session, the
    cumulative file is what the NEXT run reads."""
    run_dir = tmp_path / "run-1"
    learned_dir = tmp_path / "learned"
    writer = LearnedControlWriter(run_dir=run_dir, cumulative_dir=learned_dir)
    writer.record(_control())

    assert writer.record_count == 1
    assert len(_read_jsonl(run_dir / "learned_controls.jsonl")) == 1
    assert len(_read_jsonl(learned_dir / "offers.jsonl")) == 1


def test_writer_appends_across_runs(tmp_path: Path):
    learned_dir = tmp_path / "learned"
    for i in (1, 2):
        writer = LearnedControlWriter(
            run_dir=tmp_path / f"run-{i}", cumulative_dir=learned_dir
        )
        writer.record(_control(intent=f"thing {i}"))

    assert len(_read_jsonl(learned_dir / "offers.jsonl")) == 2


def test_writer_separates_features(tmp_path: Path):
    learned_dir = tmp_path / "learned"
    writer = LearnedControlWriter(run_dir=tmp_path / "r", cumulative_dir=learned_dir)
    writer.record(_control(feature="offers"))
    writer.record(_control(feature="audiences"))

    assert (learned_dir / "offers.jsonl").exists()
    assert (learned_dir / "audiences.jsonl").exists()
    assert len(_read_jsonl(learned_dir / "offers.jsonl")) == 1


def test_writer_swallows_a_serialization_error(tmp_path: Path):
    """Fail-safe like TraceWriter: a bad record costs the record, not the run."""

    class Unserializable(LearnedControl):
        def to_dict(self) -> Dict[str, Any]:
            return {"bad": object()}

    logger = _RecordingLogger()
    writer = LearnedControlWriter(
        run_dir=tmp_path / "r", cumulative_dir=tmp_path / "l", run_logger=logger
    )
    writer.record(Unserializable(feature="offers", intent="x"))

    assert writer.record_count == 0
    assert any("not JSON-serializable" in m for m in logger.warnings)


def test_writer_swallows_a_disk_error(tmp_path: Path):
    """A file where the run directory should be makes mkdir fail."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory")
    logger = _RecordingLogger()
    writer = LearnedControlWriter(
        run_dir=blocker / "run", cumulative_dir=blocker / "learned", run_logger=logger
    )
    writer.record(_control())

    assert writer.record_count == 0
    assert logger.warnings


def test_cumulative_failure_is_warned_about_distinctly(tmp_path: Path):
    """The run artifact can succeed while durability silently fails.

    That's the case worth a loud warning: the operator would otherwise close
    the terminal believing the lesson was saved.
    """
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")
    logger = _RecordingLogger()
    writer = LearnedControlWriter(
        run_dir=tmp_path / "run", cumulative_dir=blocker / "learned", run_logger=logger
    )
    writer.record(_control())

    assert writer.record_count == 1          # run artifact did land
    assert any("NOT carry" in m for m in logger.warnings)


def test_record_all_returns_the_count(tmp_path: Path):
    writer = LearnedControlWriter(
        run_dir=tmp_path / "r", cumulative_dir=tmp_path / "l"
    )
    assert writer.record_all([_control(intent="a"), _control(intent="b")]) == 2


def test_writer_without_cumulative_dir_still_works(tmp_path: Path):
    writer = LearnedControlWriter(run_dir=tmp_path / "r", cumulative_dir=None)
    writer.record(_control())
    assert writer.record_count == 1


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: List[str] = []

    def log_warning(self, msg: str) -> None:
        self.warnings.append(msg)


# ============================================================================
# Reader
# ============================================================================

def test_round_trip_write_then_load(tmp_path: Path):
    """The actual point of the feature: a lesson survives to the next run."""
    learned_dir = tmp_path / "learned"
    writer = LearnedControlWriter(run_dir=tmp_path / "r", cumulative_dir=learned_dir)
    writer.record(_control(human_text="top right, shows English (US)"))

    loaded = load_learned_controls("offers", cumulative_dir=learned_dir)
    assert len(loaded) == 1
    assert loaded[0].selector == '[data-testid="locale-select"]'
    assert loaded[0].human_text == "top right, shows English (US)"


def test_load_returns_empty_for_a_missing_file(tmp_path: Path):
    assert load_learned_controls("never-taught", cumulative_dir=tmp_path) == []


def test_load_returns_empty_without_a_directory():
    assert load_learned_controls("offers", cumulative_dir=None) == []


def test_load_skips_corrupt_lines(tmp_path: Path):
    """A corrupt store must not blind a run to the records that ARE fine."""
    path = tmp_path / "offers.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_control(intent="good one").to_dict()),
                "{not json at all",
                json.dumps({"feature": "offers"}),      # no intent
                json.dumps({"intent": "no feature"}),   # no feature
                "",
                json.dumps(_control(intent="another good one").to_dict()),
            ]
        )
    )
    loaded = load_learned_controls("offers", cumulative_dir=tmp_path)
    assert {c.intent for c in loaded} == {"good one", "another good one"}


def test_load_prefers_the_newest_teaching(tmp_path: Path):
    """A control taught twice was probably taught differently the second time —
    the site changed, or the first answer was wrong. The later record wins."""
    path = tmp_path / "offers.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    _control(
                        intent="locale selector",
                        selector_ladder=(SelectorRung("id", "#old"),),
                    ).to_dict()
                ),
                json.dumps(
                    _control(
                        intent="locale selector",
                        selector_ladder=(SelectorRung("id", "#new"),),
                    ).to_dict()
                ),
            ]
        )
    )
    loaded = load_learned_controls("offers", cumulative_dir=tmp_path)
    assert loaded[0].selector == "#new"


def test_load_deduplicates_repeat_teachings(tmp_path: Path):
    path = tmp_path / "offers.jsonl"
    same = json.dumps(_control(intent="locale selector").to_dict())
    path.write_text("\n".join([same] * 5))

    assert len(load_learned_controls("offers", cumulative_dir=tmp_path)) == 1


def test_load_respects_the_limit(tmp_path: Path):
    path = tmp_path / "offers.jsonl"
    path.write_text(
        "\n".join(
            json.dumps(_control(intent=f"thing {i}").to_dict()) for i in range(30)
        )
    )
    assert len(load_learned_controls("offers", cumulative_dir=tmp_path, limit=4)) == 4


def test_load_tolerates_an_unreadable_file(tmp_path: Path):
    """A directory where a file is expected — read_text raises OSError."""
    (tmp_path / "offers.jsonl").mkdir()
    assert load_learned_controls("offers", cumulative_dir=tmp_path) == []


# ============================================================================
# Prompt rendering
# ============================================================================

def test_prompt_block_is_empty_without_controls():
    assert render_prompt_block([]) == ""


def test_prompt_block_carries_selector_and_note():
    block = render_prompt_block(
        [_control(human_text="top right", label="Locale", locale="es-mx")]
    )
    assert '[data-testid="locale-select"]' in block
    assert "top right" in block
    assert "es-mx" in block


def test_prompt_block_warns_that_lessons_may_be_stale():
    """The model must treat a missing taught control as "look around", not
    "give up" — sites change between runs."""
    block = render_prompt_block([_control()])
    assert "may have changed" in block
    assert "read_page" in block


def test_describe_includes_the_panel_path():
    control = _control(panel_path=("messages-panel", "Messages"))
    assert "messages-panel / Messages" in control.describe()


# ============================================================================
# Filename safety
# ============================================================================

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("offers", "offers"),
        ("my feature", "my_feature"),
        ("", "unknown"),
        ("...", "unknown"),
    ],
)
def test_safe_feature_normalises(raw: str, expected: str):
    assert _safe_feature(raw) == expected


def test_safe_feature_blocks_path_traversal(tmp_path: Path):
    """Feature names are allowlist-reviewed, but the value does reach a
    filesystem path, so traversal is defended in depth."""
    cleaned = _safe_feature("../../etc/passwd")
    assert "/" not in cleaned
    assert ".." not in cleaned


def test_writer_cumulative_path_stays_inside_the_dir(tmp_path: Path):
    writer = LearnedControlWriter(run_dir=tmp_path, cumulative_dir=tmp_path / "learned")
    path = writer.cumulative_path("../escape")

    assert (tmp_path / "learned") in path.parents


# ============================================================================
# Intent shortening
#
# Live runs show Claude asking in ~450 characters,
# across several sentences. Storing that verbatim as ``intent`` made the prompt
# block unreadable and crowded out the other lessons competing for the same
# slots, so intents are shortened on the way in. The full text stays on
# ``question``.
# ============================================================================

from knarr.explorer.learned_controls import (  # noqa: E402
    MAX_INTENT_CHARS,
    shorten_intent,
)

_LONG_QUESTION = (
    "I need the exact selector (id or data-testid) of the Headline input for "
    "locale fr-ca (French, Canada) in the Details tab. The visible Headline "
    "inputs only cover the default language and English for a few regions, so "
    "fr-ca is not in the current DOM. It appears to live behind the \"More "
    "languages\" panel, which won't open in this read-only session at all."
)


def test_long_question_is_shortened():
    short = shorten_intent(_LONG_QUESTION)
    assert len(short) <= MAX_INTENT_CHARS + 1        # +1 for the ellipsis
    assert "selector" in short


def test_short_intent_is_left_alone():
    assert shorten_intent("Where is the locale selector") == "Where is the locale selector"


def test_trailing_punctuation_is_dropped():
    assert shorten_intent("Where is the locale selector?") == "Where is the locale selector"


def test_first_sentence_wins_when_it_is_short():
    assert shorten_intent("Find the locale picker. It is hidden somewhere.") == (
        "Find the locale picker"
    )


def test_shortening_does_not_split_a_word():
    short = shorten_intent("x" * 40 + " " + "y" * 200)
    assert "…" in short
    assert "yyy" not in short


def test_empty_intent_gets_a_placeholder():
    assert shorten_intent("") == "unnamed control"
    assert shorten_intent("   ") == "unnamed control"
    assert shorten_intent(None) == "unnamed control"


def test_whitespace_is_collapsed():
    assert shorten_intent("find\n  the\tselector") == "find the selector"


def test_controls_from_demo_shortens_the_intent():
    controls = controls_from_demo(
        [_event()], feature="offers", intent=_LONG_QUESTION,
        question=_LONG_QUESTION,
    )
    assert len(controls[0].intent) <= MAX_INTENT_CHARS + 1
    # The full text is preserved where it belongs.
    assert controls[0].question == _LONG_QUESTION


def test_control_from_text_shortens_the_intent():
    control = control_from_text(
        feature="offers", intent=_LONG_QUESTION, question=_LONG_QUESTION
    )
    assert len(control.intent) <= MAX_INTENT_CHARS + 1
    assert control.question == _LONG_QUESTION


def test_prompt_block_stays_readable_with_real_questions():
    """Regression guard on the symptom: one lesson must not dominate the block."""
    controls = controls_from_demo(
        [_event()], feature="offers", intent=_LONG_QUESTION
    )
    block = render_prompt_block(controls)
    longest = max(len(line) for line in block.splitlines())
    assert longest < 200, "a single lesson line is dominating the prompt block"
