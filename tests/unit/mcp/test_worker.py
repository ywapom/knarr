"""Unit tests for knarr/mcp/worker.py."""

from __future__ import annotations

import asyncio
import threading

import pytest

from knarr.mcp.worker import BrowserWorker


@pytest.fixture
def worker():
    w = BrowserWorker()
    yield w
    w.shutdown()


def test_every_job_runs_on_the_same_dedicated_thread(worker):
    ids = {worker.call(threading.get_ident) for _ in range(5)}
    assert len(ids) == 1
    assert ids != {threading.get_ident()}


def test_exceptions_reach_the_caller(worker):
    def boom():
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        worker.call(boom)
    assert worker.call(lambda: "still alive") == "still alive"


def test_stray_prints_go_to_stderr(worker, capsys):
    worker.call(print, "stray output")
    captured = capsys.readouterr()
    assert "stray output" not in captured.out
    assert "stray output" in captured.err


def test_async_callers_get_results_off_the_event_loop_thread(worker):
    async def main():
        loop_thread = threading.get_ident()
        job_thread = await worker.run(threading.get_ident)
        return loop_thread, job_thread, await worker.run(lambda a, b: a + b, 2, b=3)

    loop_thread, job_thread, total = asyncio.run(main())
    assert job_thread != loop_thread
    assert total == 5


def test_the_idle_function_runs_between_jobs_on_the_worker_thread(worker):
    import time

    seen = []
    worker.set_idle(lambda: (seen.append(threading.get_ident()), time.sleep(0.005)))
    time.sleep(0.1)
    assert worker.call(lambda: "job still runs") == "job still runs"
    worker.set_idle(None)
    count = len(seen)
    time.sleep(0.05)
    assert len(seen) == count  # stopped
    assert count > 3 and len(set(seen)) == 1 and seen[0] != threading.get_ident()


def test_an_idle_function_that_raises_is_tried_again_after_a_growing_pause(worker):
    import time

    calls = []

    def flaky():
        calls.append(time.monotonic())
        if len(calls) < 4:
            raise RuntimeError("page closed")

    worker.set_idle(flaky)
    time.sleep(0.8)
    assert len(calls) > 4  # it kept going after the failures, and after the last one
    assert worker.idle_errors == 3
    assert worker.idle_last_error == "RuntimeError: page closed"
    pauses = [later - earlier for earlier, later in zip(calls, calls[1:4])]
    assert pauses[0] >= 0.04 and pauses[1] > pauses[0] and pauses[2] > pauses[1]
    assert worker.call(lambda: 2) == 2


def test_a_failing_idle_function_does_not_delay_jobs(worker):
    import time

    def bad():
        raise RuntimeError("page closed")

    worker.set_idle(bad)
    time.sleep(0.5)  # the pause is now several hundred ms
    started = time.monotonic()
    assert worker.call(lambda: "job") == "job"
    assert time.monotonic() - started < 0.15


def test_setting_the_idle_function_again_resets_the_error_count(worker):
    import time

    worker.set_idle(lambda: 1 / 0)
    time.sleep(0.1)
    assert worker.idle_errors >= 1
    worker.set_idle(lambda: None)
    assert worker.idle_errors == 0 and worker.idle_last_error is None


def test_an_idle_function_that_returns_at_once_is_rate_limited(worker):
    import time

    calls = []
    worker.set_idle(lambda: calls.append(1))
    time.sleep(0.2)
    worker.set_idle(None)
    assert len(calls) <= 0.2 / 0.01 + 5  # the floor, not a busy loop


def test_a_failing_idle_function_is_reported_once_on_stderr(worker, capsys):
    import time

    def bad():
        raise RuntimeError("first tab closed")

    worker.set_idle(bad)
    time.sleep(0.3)
    err = capsys.readouterr().err
    assert err.count("the idle pump failed") == 1 and "first tab closed" in err


def test_submitting_after_shutdown_fails_fast():
    w = BrowserWorker()
    w.shutdown()
    with pytest.raises(RuntimeError, match="shut down"):
        w.call(lambda: 1)


def test_a_base_exception_from_the_idle_function_does_not_end_the_thread(worker):
    import time

    class Exit(BaseException):  # e.g. a greenlet exit
        pass

    def bad():
        raise Exit()

    worker.set_idle(bad)
    time.sleep(0.05)
    assert worker.call(lambda: "alive") == "alive"


def test_the_error_count_survives_switching_the_idle_function_off(worker):
    import time

    worker.set_idle(lambda: 1 / 0)
    time.sleep(0.15)
    worker.set_idle(None)
    assert worker.idle_errors >= 1 and worker.idle_last_error == "ZeroDivisionError: division by zero"
