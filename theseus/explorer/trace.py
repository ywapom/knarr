"""Trace writer for Theseus exploration runs.

Append-only JSONL writer that produces ``trace.jsonl`` files: one record
per tool call. Fail-safe: any exception during file open or ``json.dumps``
is swallowed (silent drop) so a disk hiccup never escalates into a
Claude-disabled error during a long exploration run. A ``run_logger`` can
be injected for human-readable progress lines.
"""

from __future__ import annotations

import getpass
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from theseus.explorer.redact import redact


def utc_now() -> str:
    """The current time as an ISO 8601 UTC string, to the millisecond."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def run_user() -> str:
    """The login name of the account running Theseus: who the actions are for.

    This is the operating-system account, not the identity of the site login
    that was borrowed. "unknown" when the system can't say.
    """
    try:
        return getpass.getuser() or "unknown"
    except Exception:  # no passwd entry, no USER/LOGNAME in the environment
        return "unknown"


# ============================================================================
# TraceWriter — append one JSONL line per tool call.
# ============================================================================

class TraceWriter:
    """Append-only JSONL writer for a single exploration run.

    Lifecycle:
        1. Construct with the run directory and (optional) run_logger.
           The trace.jsonl file is created lazily on first record().
        2. Call ``record(...)`` per tool call.
        3. The writer is stateless across processes — multiple Python
           processes writing to the same file would corrupt it. Theseus
           runs are single-process so this is fine in practice.
    """

    def __init__(
        self,
        run_dir: Path,
        run_logger: Any = None,
        filename: str = "trace.jsonl",
    ) -> None:
        self._run_dir = Path(run_dir)
        self._run_logger = run_logger
        self._path = self._run_dir / filename
        # Count of records successfully written. This is the real "turns"
        # signal for the run summary: a runner's model call can drive its own
        # multi-tool loop inside a single request, so the runner's outer loop
        # iterates only once — one trace record per tool call is what
        # actually reflects how much work happened.
        self._record_count = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def record_count(self) -> int:
        return self._record_count

    def record(
        self,
        *,
        turn: int,
        tool: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        ts_ms: int,
        screenshot: Optional[str] = None,
        claude_thinking_summary: Optional[str] = None,
        annotations: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Write one tool-call record. Errors are swallowed so a disk
        problem doesn't kill the run.

        The schema is stable: a pattern miner or other tooling can read it back.
        Optional fields are omitted from the JSON when their value is
        None (cleaner reads in the log).
        """
        payload: Dict[str, Any] = {
            "turn": int(turn),
            "at": utc_now(),  # when it happened; ts_ms is the time since the run started
            "ts_ms": int(ts_ms),
            "tool": str(tool),
            "args": dict(args or {}),
            "result": dict(result or {}),
        }
        if screenshot is not None:
            payload["screenshot"] = screenshot
        if claude_thinking_summary is not None:
            payload["claude_thinking_summary"] = claude_thinking_summary
        if annotations:
            payload["annotations"] = list(annotations)

        try:
            # A URL in the file keeps no credential: see redact.py.
            serialized = json.dumps(redact(payload), ensure_ascii=False)
        except (TypeError, ValueError, RecursionError):
            # Most likely cause: a non-serializable arg slipped through
            # (e.g. a Path or a non-stringified object). Drop silently —
            # the run keeps going.
            self._log_warn(
                f"TraceWriter: dropped record turn={turn} tool={tool} "
                f"(payload not JSON-serializable)"
            )
            return
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(serialized)
                fh.write("\n")
            self._record_count += 1
        except OSError as exc:
            self._log_warn(f"TraceWriter: dropped record (disk error: {exc})")

    def _log_warn(self, msg: str) -> None:
        if self._run_logger is not None and hasattr(self._run_logger, "log_warning"):
            try:
                self._run_logger.log_warning(msg)
            except Exception:
                pass


# ============================================================================
# SummaryWriter — one summary.json per run.
# ============================================================================

@dataclass
class RunSummary:
    """In-memory summary of a Theseus run. Persisted via ``write()``.

    Kept as a mutable dataclass so the runner can update fields as the
    run progresses without juggling intermediate JSON state.
    """

    feature: str
    goal: str
    starting_url: str
    success: bool = False
    total_turns: int = 0
    total_tokens: int = 0
    total_wall_clock_ms: int = 0
    claude_final_message: str = ""
    abort_reason: Optional[str] = None
    # Path to the Playwright trace.zip when one was captured (real
    # a site adapter's session only). None for tests that inject a bare page
    # without a session lifecycle.
    playwright_trace_path: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "feature": self.feature,
            "goal": self.goal,
            "starting_url": self.starting_url,
            "success": self.success,
            "total_turns": self.total_turns,
            "total_tokens": self.total_tokens,
            "total_wall_clock_ms": self.total_wall_clock_ms,
            "claude_final_message": self.claude_final_message,
        }
        if self.abort_reason is not None:
            out["abort_reason"] = self.abort_reason
        if self.playwright_trace_path is not None:
            out["playwright_trace_path"] = self.playwright_trace_path
        if self.extra:
            out["extra"] = self.extra
        return out


class SummaryWriter:
    """Writes ``summary.json`` once at the end of a run.

    The summary file is the entry point for any downstream review
    tooling — a human grepping for "which Theseus runs of feature X
    succeeded" reads it before opening the trace.
    """

    def __init__(
        self,
        run_dir: Path,
        run_logger: Any = None,
        filename: str = "summary.json",
    ) -> None:
        self._run_dir = Path(run_dir)
        self._run_logger = run_logger
        self._path = self._run_dir / filename

    @property
    def path(self) -> Path:
        return self._path

    def write(self, summary: RunSummary) -> None:
        """Persist the summary, swallowing disk errors. Unlike record()
        in TraceWriter, summary.write() is called once per run, so a
        failed write *is* worth logging at warn level."""
        try:
            serialized = json.dumps(
                redact(summary.to_dict()), indent=2, ensure_ascii=False
            )
        except (TypeError, ValueError) as exc:
            self._log_warn(
                f"SummaryWriter: serialization failed ({exc}); summary "
                f"will be missing from {self._run_dir}"
            )
            return
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
            with self._path.open("w", encoding="utf-8") as fh:
                fh.write(serialized)
        except OSError as exc:
            self._log_warn(
                f"SummaryWriter: write failed ({exc}); summary missing "
                f"from {self._run_dir}"
            )

    def _log_warn(self, msg: str) -> None:
        if self._run_logger is not None and hasattr(self._run_logger, "log_warning"):
            try:
                self._run_logger.log_warning(msg)
            except Exception:
                pass
