"""Unit tests for knarr/explorer/trace.py.

Append order, fail-safe semantics, cyclic-payload swallow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from knarr.explorer.trace import (
    RunSummary,
    SummaryWriter,
    TraceWriter,
)


class _CapturingLogger:
    """Run-logger stand-in. Captures warnings for assertion."""

    def __init__(self) -> None:
        self.warnings: List[str] = []

    def log_warning(self, msg: str) -> None:
        self.warnings.append(msg)


# ============================================================================
# TraceWriter — happy path
# ============================================================================

def test_record_creates_file_lazily(tmp_path: Path):
    w = TraceWriter(tmp_path)
    assert not w.path.exists()
    w.record(
        turn=1,
        ts_ms=10,
        tool="navigate",
        args={"url": "https://app.example.com/"},
        result={"success": True},
    )
    assert w.path.exists()


def test_record_appends_one_line_per_call(tmp_path: Path):
    w = TraceWriter(tmp_path)
    for i in range(3):
        w.record(
            turn=i + 1,
            ts_ms=i * 100,
            tool="note",
            args={"text": f"step {i}"},
            result={"success": True},
        )
    lines = w.path.read_text().strip().split("\n")
    assert len(lines) == 3
    assert json.loads(lines[0])["args"]["text"] == "step 0"
    assert json.loads(lines[2])["args"]["text"] == "step 2"


def test_record_serializes_expected_schema(tmp_path: Path):
    w = TraceWriter(tmp_path)
    w.record(
        turn=5,
        ts_ms=1234,
        tool="screenshot",
        args={},
        result={"success": True},
        screenshot="005-001.png",
        claude_thinking_summary="page settled, taking shot",
        annotations=[{"kind": "intent", "method_param": "name"}],
    )
    record = json.loads(w.path.read_text().strip())
    assert record["turn"] == 5
    assert record["ts_ms"] == 1234
    assert record["tool"] == "screenshot"
    assert record["screenshot"] == "005-001.png"
    assert record["claude_thinking_summary"].startswith("page settled")
    assert record["annotations"][0]["kind"] == "intent"


def test_record_omits_optional_fields_when_none(tmp_path: Path):
    w = TraceWriter(tmp_path)
    w.record(
        turn=1, ts_ms=0, tool="note", args={"text": "hi"}, result={"success": True}
    )
    record = json.loads(w.path.read_text().strip())
    assert "screenshot" not in record
    assert "claude_thinking_summary" not in record
    assert "annotations" not in record


# ============================================================================
# TraceWriter — fail-safe semantics
# ============================================================================

def test_record_swallows_disk_error_silently(tmp_path: Path):
    """Path the writer can't open (because parent is a file, not a dir)
    must not raise. The whole purpose of this writer is fail-safety —
    a disk hiccup mid-run cannot escalate."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")  # blocker is a file, not a dir
    # Try to write to a path beneath the file — mkdir will fail.
    nested = blocker / "nested"
    logger = _CapturingLogger()
    w = TraceWriter(nested, run_logger=logger)
    w.record(
        turn=1, ts_ms=0, tool="note", args={"text": "hi"}, result={"success": True}
    )
    # No raise. A warning was logged so the operator knows.
    assert any("disk error" in msg or "dropped" in msg for msg in logger.warnings)


def test_cyclic_payload_is_dropped_subsequent_writes_succeed(tmp_path: Path):
    """A non-serializable arg breaks json.dumps; the writer must drop
    the bad record and keep going."""
    logger = _CapturingLogger()
    w = TraceWriter(tmp_path, run_logger=logger)
    cycle: dict = {}
    cycle["self"] = cycle
    w.record(turn=1, ts_ms=0, tool="note", args=cycle, result={"success": True})
    # The cycle entry was dropped — file may not even exist yet.
    # A subsequent clean write must succeed.
    w.record(
        turn=2,
        ts_ms=10,
        tool="note",
        args={"text": "clean"},
        result={"success": True},
    )
    lines = [
        json.loads(line)
        for line in w.path.read_text().strip().split("\n")
        if line
    ]
    assert len(lines) == 1
    assert lines[0]["args"]["text"] == "clean"
    assert any("not JSON-serializable" in msg for msg in logger.warnings)


def test_record_with_no_run_logger_still_fails_silently(tmp_path: Path):
    """Logger is optional — the writer must not crash if it's None."""
    w = TraceWriter(tmp_path, run_logger=None)
    cycle: dict = {}
    cycle["self"] = cycle
    # Should not raise even without a logger to receive the warning.
    w.record(turn=1, ts_ms=0, tool="note", args=cycle, result={"success": True})


# ============================================================================
# SummaryWriter
# ============================================================================

def test_summary_write_round_trip(tmp_path: Path):
    sw = SummaryWriter(tmp_path)
    summary = RunSummary(
        feature="offers",
        goal="Create an offer",
        starting_url="https://app.example.com/",
        success=True,
        total_turns=8,
        total_tokens=12_345,
        total_wall_clock_ms=45_000,
        claude_final_message="done",
    )
    sw.write(summary)
    payload = json.loads(sw.path.read_text())
    assert payload["feature"] == "offers"
    assert payload["success"] is True
    assert payload["total_turns"] == 8


def test_summary_includes_abort_reason_only_when_set(tmp_path: Path):
    sw = SummaryWriter(tmp_path)
    sw.write(
        RunSummary(
            feature="offers",
            goal="x",
            starting_url="https://app.example.com/",
            success=False,
            abort_reason="boundary_violations_exceeded",
        )
    )
    payload = json.loads(sw.path.read_text())
    assert payload["abort_reason"] == "boundary_violations_exceeded"


def test_summary_omits_abort_reason_when_none(tmp_path: Path):
    sw = SummaryWriter(tmp_path)
    sw.write(
        RunSummary(
            feature="offers",
            goal="x",
            starting_url="https://app.example.com/",
            success=True,
        )
    )
    payload = json.loads(sw.path.read_text())
    assert "abort_reason" not in payload


def test_summary_write_swallows_disk_error(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    nested = blocker / "nested"
    logger = _CapturingLogger()
    sw = SummaryWriter(nested, run_logger=logger)
    sw.write(
        RunSummary(
            feature="offers",
            goal="x",
            starting_url="https://app.example.com/",
        )
    )
    assert logger.warnings  # we want a warning so the operator knows


def test_summary_includes_playwright_trace_path_when_set(tmp_path: Path):
    """When the site session captured a Playwright trace, its path
    lands in summary.json so reviewers can `playwright show-trace ...`
    without having to hunt for the file."""
    sw = SummaryWriter(tmp_path)
    trace_zip = tmp_path / "trace.zip"
    sw.write(
        RunSummary(
            feature="offers",
            goal="x",
            starting_url="https://app.example.com/",
            success=True,
            playwright_trace_path=str(trace_zip),
        )
    )
    payload = json.loads(sw.path.read_text())
    assert payload["playwright_trace_path"] == str(trace_zip)


def test_summary_omits_playwright_trace_path_when_none(tmp_path: Path):
    """A run that injected a page directly (no site-adapter session lifecycle)
    has no Playwright trace — that field should be absent from the
    JSON, not serialized as null."""
    sw = SummaryWriter(tmp_path)
    sw.write(
        RunSummary(
            feature="offers",
            goal="x",
            starting_url="https://app.example.com/",
        )
    )
    payload = json.loads(sw.path.read_text())
    assert "playwright_trace_path" not in payload


# ============================================================================
# What the files keep and leave out
# ============================================================================

def test_a_record_keeps_credentials_in_urls_out_of_the_file(tmp_path: Path):
    w = TraceWriter(tmp_path)
    w.record(turn=1, tool="navigate", args={"url": "https://idp.example/cb?code=abc123&type=RECURRENT"},
             result={"success": True, "url": "https://user:pw@idp.example/home#access_token=tok"}, ts_ms=5)
    raw = w.path.read_text()
    assert "abc123" not in raw and "pw@" not in raw and "access_token=tok" not in raw
    (line,) = raw.splitlines()
    record = json.loads(line)
    assert record["args"]["url"] == "https://idp.example/cb?code=redacted&type=RECURRENT"
    assert record["result"]["url"] == "https://[userinfo]@idp.example/home#access_token=redacted"


def test_each_record_carries_an_absolute_utc_time_besides_the_time_since_the_start(tmp_path: Path):
    from datetime import datetime, timedelta

    w = TraceWriter(tmp_path)
    before = datetime.now().astimezone()
    w.record(turn=1, tool="read_page", args={}, result={"success": True}, ts_ms=1234)
    record = json.loads(w.path.read_text())
    moment = datetime.fromisoformat(record["at"])
    assert moment.utcoffset() == timedelta(0) and record["ts_ms"] == 1234
    assert before - timedelta(seconds=1) <= moment <= datetime.now().astimezone() + timedelta(seconds=1)


def test_the_summary_keeps_credentials_in_urls_out_of_the_file(tmp_path: Path):
    writer = SummaryWriter(tmp_path)
    writer.write(RunSummary(feature="mcp", goal="g", starting_url="https://idp.example/cb?code=abc123",
                            claude_final_message="done at https://x.example/#id_token=eyJhbGciOiJIUzI1NiJ9abc123",
                            extra={"origin": "https://idp.example"}))
    raw = writer.path.read_text()
    assert "abc123" not in raw and "https://idp.example/cb?code=redacted" in raw
    assert json.loads(raw)["extra"]["origin"] == "https://idp.example"


def test_the_user_is_the_login_name_or_unknown(monkeypatch):
    import getpass

    from knarr.explorer.trace import run_user

    assert run_user() == getpass.getuser()
    monkeypatch.setattr(getpass, "getuser", lambda: (_ for _ in ()).throw(KeyError("no passwd entry")))
    assert run_user() == "unknown"
    monkeypatch.setattr(getpass, "getuser", lambda: "")
    assert run_user() == "unknown"
