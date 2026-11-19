"""Unit tests for knarr/explorer/caps.py.

The cap module is timing-sensitive — we use injected providers for
``time_provider`` and ``rss_provider`` so every test is deterministic.
"""

from __future__ import annotations

import pytest

from knarr.explorer.caps import (
    DEFAULT_MAX_RSS_MB,
    DEFAULT_MAX_TOKENS_EXPLORE,
    DEFAULT_MAX_TOKENS_SCAFFOLD,
    DEFAULT_MAX_TURNS,
    DEFAULT_MAX_WALL_CLOCK_SEC,
    REASON_RSS,
    REASON_TOKENS,
    REASON_TURNS,
    REASON_WALL_CLOCK,
    BudgetTracker,
    RunCaps,
)


# ============================================================================
# Tiny harness: a settable clock so wall-clock checks are deterministic.
# ============================================================================

class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _tracker(
    *,
    max_turns: int = 5,
    max_tokens: int = 1000,
    max_wall_clock_sec: float = 60.0,
    max_rss_mb: int = 1024,
    rss_value: int = 100,
):
    """Build a tracker with small caps + a FakeClock + a fixed RSS."""
    caps = RunCaps(
        max_turns=max_turns,
        max_tokens=max_tokens,
        max_wall_clock_sec=max_wall_clock_sec,
        max_rss_mb=max_rss_mb,
    )
    clock = FakeClock()
    tracker = BudgetTracker(
        caps,
        time_provider=clock,
        rss_provider=lambda: rss_value,
    )
    return tracker, clock


# ============================================================================
# RunCaps factories
# ============================================================================

def test_explore_factory_uses_explore_token_default():
    caps = RunCaps.for_explore()
    assert caps.max_tokens == DEFAULT_MAX_TOKENS_EXPLORE
    assert caps.max_turns == DEFAULT_MAX_TURNS
    assert caps.max_wall_clock_sec == DEFAULT_MAX_WALL_CLOCK_SEC
    assert caps.max_rss_mb == DEFAULT_MAX_RSS_MB


def test_scaffold_factory_uses_scaffold_token_default():
    caps = RunCaps.for_scaffold()
    assert caps.max_tokens == DEFAULT_MAX_TOKENS_SCAFFOLD
    assert caps.max_tokens > DEFAULT_MAX_TOKENS_EXPLORE


def test_caps_are_frozen():
    caps = RunCaps.for_explore()
    with pytest.raises(AttributeError):
        caps.max_turns = 1  # type: ignore[misc]


def test_overrides_propagate():
    caps = RunCaps.for_explore(
        max_turns=5, max_tokens=100, max_wall_clock_sec=10, max_rss_mb=128
    )
    assert (caps.max_turns, caps.max_tokens, caps.max_wall_clock_sec, caps.max_rss_mb) == (
        5,
        100,
        10,
        128,
    )


# ============================================================================
# BudgetTracker — happy path + exhaustion order
# ============================================================================

def test_record_turn_and_record_tokens_accumulate():
    t, _ = _tracker(max_turns=10, max_tokens=1000)
    t.record_turn()
    t.record_turn()
    t.record_tokens(150)
    t.record_tokens(50)
    assert t.turns_used == 2
    assert t.tokens_used == 200


def test_record_tokens_clamps_to_zero_on_negative_overreach():
    t, _ = _tracker()
    t.record_tokens(100)
    t.record_tokens(-200)  # over-reconcile
    assert t.tokens_used == 0


def test_not_exhausted_when_under_caps():
    t, _ = _tracker()
    t.record_turn()
    t.record_tokens(500)
    assert t.is_exhausted() is False
    assert t.exhausted_reason() is None


def test_turns_exhaustion():
    t, _ = _tracker(max_turns=3)
    for _ in range(3):
        t.record_turn()
    assert t.exhausted_reason() == REASON_TURNS


def test_tokens_exhaustion():
    t, _ = _tracker(max_tokens=1000)
    t.record_tokens(1000)
    assert t.exhausted_reason() == REASON_TOKENS


def test_wall_clock_exhaustion():
    t, clock = _tracker(max_wall_clock_sec=10.0)
    clock.now = 10.0
    assert t.exhausted_reason() == REASON_WALL_CLOCK


def test_rss_exhaustion_only_after_sample():
    t, _ = _tracker(max_rss_mb=512, rss_value=600)
    # Until sample_rss runs, the tracker has no RSS observation —
    # the RSS cap can't trip on its own. This is intentional: a runner
    # samples every N turns, not every microsecond, and a never-
    # sampled tracker must not falsely abort.
    assert t.exhausted_reason() is None
    t.sample_rss()
    assert t.exhausted_reason() == REASON_RSS


def test_exhaustion_order_is_turns_then_tokens_then_wall_clock_then_rss():
    """When multiple budgets are exhausted simultaneously, the operator
    gets a stable attribution rather than whichever check ran first.
    """
    t, clock = _tracker(
        max_turns=1, max_tokens=10, max_wall_clock_sec=1.0, max_rss_mb=10
    )
    t.record_turn()
    t.record_tokens(100)
    clock.now = 5.0
    t.sample_rss()  # rss_value default 100 > 10
    assert t.exhausted_reason() == REASON_TURNS


# ============================================================================
# Remaining accessors
# ============================================================================

def test_remaining_helpers():
    t, clock = _tracker(max_turns=10, max_tokens=1000, max_wall_clock_sec=60.0)
    t.record_turn()
    t.record_tokens(250)
    clock.now = 15.0
    assert t.remaining_turns() == 9
    assert t.remaining_tokens() == 750
    assert t.remaining_wall_clock_sec() == pytest.approx(45.0)


def test_remaining_does_not_go_negative():
    t, clock = _tracker(max_turns=2, max_tokens=100, max_wall_clock_sec=10.0)
    t.record_turn()
    t.record_turn()
    t.record_turn()
    t.record_tokens(500)
    clock.now = 50.0
    assert t.remaining_turns() == 0
    assert t.remaining_tokens() == 0
    assert t.remaining_wall_clock_sec() == 0.0


# ============================================================================
# Snapshot
# ============================================================================

def test_snapshot_round_trips_to_dict():
    t, clock = _tracker(max_turns=10, max_tokens=1000, max_wall_clock_sec=60.0)
    t.record_turn()
    t.record_tokens(123)
    clock.now = 2.5
    t.sample_rss()
    snap = t.snapshot()
    assert snap["turns_used"] == 1
    assert snap["tokens_used"] == 123
    assert snap["wall_clock_ms"] == 2500
    assert snap["rss_mb"] == 100
    assert snap["remaining_tokens"] == 877


# ============================================================================
# RSS pluggability
# ============================================================================

def test_sample_rss_uses_injected_provider():
    sampled = []
    caps = RunCaps.for_explore()
    tracker = BudgetTracker(
        caps,
        time_provider=FakeClock(),
        rss_provider=lambda: 1234,
    )
    sampled.append(tracker.sample_rss())
    assert sampled == [1234]
    assert tracker.last_rss_mb == 1234


def test_default_rss_provider_returns_nonneg_int():
    """The fall-back provider must at least not raise on the host."""
    from knarr.explorer.caps import _default_rss_mb

    value = _default_rss_mb()
    assert isinstance(value, int)
    assert value >= 0
