"""Per-run cap enforcement for Knarr.

Four caps gate every exploration run, each chosen to fail closed
rather than burn budget invisibly:

  - ``max_turns``         hard-stops the per-turn loop after N
  - ``max_tokens``        running token estimate exceeds N
  - ``max_wall_clock_sec`` watchdog kills the run after N seconds
  - ``max_rss_mb``        sampled RSS crosses N (memory-footprint guard
                           — Knarr is the run mode most likely to leak
                           memory because Chromium screenshots accumulate)

Two design choices worth knowing:

1. **RSS sampling is pluggable.** The default ``BudgetTracker`` uses
   ``resource.getrusage`` for portability across macOS/Linux without
   adding ``psutil`` as a dep. Tests pass a ``rss_provider`` callable
   to simulate any RSS without touching the actual process. A
   runner samples every N turns, not every turn, since reading RSS is
   nontrivial overhead.

2. **Defaults are intentionally generous.** Lenient defaults avoid
   spurious mid-run aborts on slow or busy sites. Tighten them
   (``KNARR_MAX_TURNS``, ``KNARR_MAX_WALL_CLOCK_SEC``, a site
   profile's ``caps``) once you know the real distribution.

3. **Interactive runs get their own, much larger caps — and give up
   the wall-clock runaway guard to get them.** ``for_interactive``
   exists because a human answering an ``ask_human`` pause spends
   real minutes inside the same monotonic window the agent does, and
   the tracker cannot tell those apart: a run where the agent looped
   uselessly for 40 minutes and a run where the operator thought for
   35 look identical from here. Rather than thread pause/resume
   accounting through ``BudgetTracker`` (which would make every
   caller reason about two clocks), interactive mode simply raises
   the ceiling and accepts the weaker guard. That trade is sound in
   this one mode specifically: interactive runs are headed and
   attended, so the operator *is* the runaway guard, and a watchdog
   matters least exactly when somebody is watching.

   ``record_human_wait`` still accumulates the time spent blocked on
   a human so ``snapshot()`` reports it separately. Nothing enforces
   against it — it's the measurement that would justify real pause
   accounting later, kept honest so the decision can be revisited
   with data instead of a guess.

The tracker is stateful and not thread-safe — Knarr runs are
single-process, single-thread, so the simplicity is correct here.
"""

from __future__ import annotations

import sys

try:  # POSIX only; Windows has no resource module, and then RSS reads as 0
    import resource
except ImportError:  # pragma: no cover - exercised on Windows
    resource = None  # type: ignore[assignment]
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional


# ============================================================================
# Defaults — generous for the bootstrap phase, per the design note above.
# ============================================================================

DEFAULT_MAX_TURNS = 30
DEFAULT_MAX_WALL_CLOCK_SEC = 900           # 15 min
DEFAULT_MAX_RSS_MB = 3072                  # 3 GB

# Token budgets differ by mode — scaffold runs are 5×–10× larger by
# design (operations × variants × trials all multiply).
DEFAULT_MAX_TOKENS_EXPLORE = 500_000
DEFAULT_MAX_TOKENS_SCAFFOLD = 10_000_000

# Interactive (``--interactive``) caps. Every value is deliberately larger
# than its explore counterpart, for reasons that differ per axis:
#   turns       — an ask_human round trip can push the outer loop past 30
#   tokens      — the human's answer + observed demonstration re-enter the
#                 prompt, and a teaching session is longer than a solo one
#   wall clock  — human think-time is billed to this clock (see docstring
#                 note 3); 1h is a plausible teaching session
#   rss         — headed Chromium plus an hour of accumulating screenshots
DEFAULT_MAX_TURNS_INTERACTIVE = 100
DEFAULT_MAX_TOKENS_INTERACTIVE = 1_000_000
DEFAULT_MAX_WALL_CLOCK_SEC_INTERACTIVE = 3600
DEFAULT_MAX_RSS_MB_INTERACTIVE = 4096

# Termination reasons emitted into the trace's summary.json. Stable
# strings so dashboards can group by reason.
REASON_TURNS = "turn_budget_exhausted"
REASON_TOKENS = "token_budget_exhausted"
REASON_WALL_CLOCK = "wall_clock_budget_exhausted"
REASON_RSS = "memory_budget_exhausted"


# ============================================================================
# RunCaps — the configuration object the CLI builds.
# ============================================================================

@dataclass(frozen=True)
class RunCaps:
    """All four cap values for one run. Immutable after construction so
    a buggy tool can't loosen its own budget mid-run.

    Use :func:`for_explore` / :func:`for_scaffold` factories for the
    common cases; override individually when the CLI passes
    ``--max-tokens 750000``.
    """

    max_turns: int
    max_tokens: int
    max_wall_clock_sec: float
    max_rss_mb: int

    @classmethod
    def for_explore(
        cls,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_tokens: int = DEFAULT_MAX_TOKENS_EXPLORE,
        max_wall_clock_sec: float = DEFAULT_MAX_WALL_CLOCK_SEC,
        max_rss_mb: int = DEFAULT_MAX_RSS_MB,
    ) -> "RunCaps":
        return cls(
            max_turns=max_turns,
            max_tokens=max_tokens,
            max_wall_clock_sec=max_wall_clock_sec,
            max_rss_mb=max_rss_mb,
        )

    @classmethod
    def for_scaffold(
        cls,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_tokens: int = DEFAULT_MAX_TOKENS_SCAFFOLD,
        max_wall_clock_sec: float = DEFAULT_MAX_WALL_CLOCK_SEC,
        max_rss_mb: int = DEFAULT_MAX_RSS_MB,
    ) -> "RunCaps":
        return cls(
            max_turns=max_turns,
            max_tokens=max_tokens,
            max_wall_clock_sec=max_wall_clock_sec,
            max_rss_mb=max_rss_mb,
        )

    @classmethod
    def for_interactive(
        cls,
        *,
        max_turns: int = DEFAULT_MAX_TURNS_INTERACTIVE,
        max_tokens: int = DEFAULT_MAX_TOKENS_INTERACTIVE,
        max_wall_clock_sec: float = DEFAULT_MAX_WALL_CLOCK_SEC_INTERACTIVE,
        max_rss_mb: int = DEFAULT_MAX_RSS_MB_INTERACTIVE,
    ) -> "RunCaps":
        """Caps for ``--interactive`` runs, where a human answers pauses.

        Signature is identical to the sibling factories so the CLI's
        per-flag override path (``--max-wall-clock`` etc.) applies
        unchanged — an operator who wants the tighter guard back just
        passes the flag. See the module docstring's note 3 for why the
        wall-clock value is this generous.
        """
        return cls(
            max_turns=max_turns,
            max_tokens=max_tokens,
            max_wall_clock_sec=max_wall_clock_sec,
            max_rss_mb=max_rss_mb,
        )


# ============================================================================
# BudgetTracker — stateful usage recorder + exhaustion detector.
# ============================================================================

@dataclass
class _Counters:
    turns: int = 0
    tokens: int = 0
    started_at: float = 0.0
    last_rss_mb: int = 0
    # Cumulative milliseconds spent blocked on a human (ask_human pauses).
    # Reported, never enforced against — see the module docstring note 3.
    human_wait_ms: int = 0
    human_asks: int = 0


class BudgetTracker:
    """Records per-run usage and reports exhaustion against the caps.

    Construction starts the wall-clock; call :meth:`record_turn`,
    :meth:`record_tokens`, and (every few turns) :meth:`sample_rss`
    from the runner. :meth:`exhausted_reason` returns one of the
    ``REASON_*`` strings if any cap has been crossed, else ``None``.

    Time + RSS providers are injectable so tests can drive
    deterministic scenarios without ``time.sleep`` or real memory
    pressure.
    """

    def __init__(
        self,
        caps: RunCaps,
        *,
        time_provider: Callable[[], float] = time.monotonic,
        rss_provider: Optional[Callable[[], int]] = None,
    ) -> None:
        self._caps = caps
        self._time = time_provider
        self._rss = rss_provider or _default_rss_mb
        self._counters = _Counters(started_at=self._time())

    # -- recorders ------------------------------------------------------

    def record_turn(self) -> None:
        self._counters.turns += 1

    def record_tokens(self, n: int) -> None:
        # n can be negative if the runner reconciles an over-estimate;
        # never let the displayed total go below zero.
        self._counters.tokens = max(0, self._counters.tokens + int(n))

    def sample_rss(self) -> int:
        """Capture current RSS in MB and update last_rss_mb. Returns
        the sampled value. Called periodically by the runner.
        """
        value = int(self._rss())
        self._counters.last_rss_mb = value
        return value

    def record_human_wait(self, waited_ms: int, asks: int = 1) -> None:
        """Record time spent blocked on a human answering an ask_human pause.

        Purely observational: this time is ALSO inside ``wall_clock_sec``
        (the tracker has one monotonic clock, and interactive mode chose a
        bigger ceiling over two clocks — module docstring note 3). Recording
        it separately is what lets a reader of summary.json tell a slow
        operator from a looping agent after the fact, and is the evidence
        that would justify real pause accounting later. Negative values are
        clamped so a bad time provider can't walk the total backwards.

        ``asks`` lets the runner reconcile a whole trial in one call rather
        than per pause, which is what it actually has on hand once the
        per-turn loop exits.
        """
        self._counters.human_wait_ms += max(0, int(waited_ms))
        self._counters.human_asks += max(0, int(asks))

    # -- accessors ------------------------------------------------------

    @property
    def turns_used(self) -> int:
        return self._counters.turns

    @property
    def tokens_used(self) -> int:
        return self._counters.tokens

    @property
    def wall_clock_sec(self) -> float:
        return self._time() - self._counters.started_at

    @property
    def last_rss_mb(self) -> int:
        return self._counters.last_rss_mb

    @property
    def human_wait_ms(self) -> int:
        return self._counters.human_wait_ms

    @property
    def human_asks(self) -> int:
        return self._counters.human_asks

    @property
    def agent_wall_clock_sec(self) -> float:
        """Wall clock with human wait removed — the agent's own elapsed time.

        Not used for enforcement (``exhausted_reason`` deliberately checks
        the undivided ``wall_clock_sec``); this is for reporting, so a
        summary can show both "the run took 42 minutes" and "the agent
        worked for 7 of them".
        """
        return max(0.0, self.wall_clock_sec - (self._counters.human_wait_ms / 1000.0))

    def remaining_turns(self) -> int:
        return max(0, self._caps.max_turns - self._counters.turns)

    def remaining_tokens(self) -> int:
        return max(0, self._caps.max_tokens - self._counters.tokens)

    def remaining_wall_clock_sec(self) -> float:
        return max(0.0, self._caps.max_wall_clock_sec - self.wall_clock_sec)

    # -- exhaustion -----------------------------------------------------

    def exhausted_reason(self) -> Optional[str]:
        """Return the FIRST cap that's been crossed, in fixed order, or
        ``None`` if all budgets still have room. Fixed order matters: if
        both turns and tokens are exhausted, the operator gets a stable
        attribution rather than whichever-was-checked-first.
        """
        if self._counters.turns >= self._caps.max_turns:
            return REASON_TURNS
        if self._counters.tokens >= self._caps.max_tokens:
            return REASON_TOKENS
        if self.wall_clock_sec >= self._caps.max_wall_clock_sec:
            return REASON_WALL_CLOCK
        if self._counters.last_rss_mb >= self._caps.max_rss_mb:
            return REASON_RSS
        return None

    def is_exhausted(self) -> bool:
        return self.exhausted_reason() is not None

    def snapshot(self) -> Dict[str, object]:
        """Render the current state as a JSON-safe dict — useful for
        summary.json and progress logging. Wall-clock is rounded to ms
        granularity so two snapshots in quick succession don't differ
        in the fifth decimal place."""
        return {
            "turns_used": self._counters.turns,
            "tokens_used": self._counters.tokens,
            "wall_clock_ms": int(self.wall_clock_sec * 1000),
            "rss_mb": self._counters.last_rss_mb,
            "remaining_turns": self.remaining_turns(),
            "remaining_tokens": self.remaining_tokens(),
            "remaining_wall_clock_sec": round(self.remaining_wall_clock_sec(), 2),
            # Interactive-mode observability. Zero on every non-interactive
            # run, so the keys are stable across modes and a dashboard
            # doesn't need to branch on whether ask_human was available.
            "human_wait_ms": self._counters.human_wait_ms,
            "human_asks": self._counters.human_asks,
            "agent_wall_clock_ms": int(self.agent_wall_clock_sec * 1000),
        }


# ============================================================================
# RSS provider — POSIX getrusage, with macOS/Linux unit reconciliation.
# ============================================================================

def _default_rss_mb() -> int:
    """Return current process RSS in MB.

    ``resource.getrusage(RUSAGE_SELF).ru_maxrss`` is in *kilobytes* on
    Linux, *bytes* on macOS. We reconcile so the rest of the code
    always sees MB. Falls back to 0 on platforms where ``resource``
    isn't available (Windows in some Python builds).
    """
    if resource is None:
        return 0
    try:
        ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (AttributeError, ValueError):
        return 0
    if sys.platform == "darwin":
        return int(ru // (1024 * 1024))
    return int(ru // 1024)


def rss_growth_provider() -> Callable[[], int]:
    """A provider of the MB the process's peak RSS grows from now on.

    ``ru_maxrss`` is a lifetime peak. A long-lived process (the MCP server
    outlives its sessions) would carry one session's peak into the next and
    refuse it at its first call, so a session measures its own growth.
    """
    baseline = _default_rss_mb()
    return lambda: max(0, _default_rss_mb() - baseline)


def estimate_tokens(text: str) -> int:
    """Cheap 1-token-per-4-chars heuristic. Good enough for budget
    enforcement, which is all it is used for."""
    if not text:
        return 0
    return max(1, len(text) // 4)
