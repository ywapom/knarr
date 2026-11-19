"""One thread that owns the browser.

Playwright's sync API must stay on one thread, and refuses to run on a thread
with a running asyncio loop. FastMCP calls synchronous tools on its event-loop
thread, so every browser call goes through ``BrowserWorker``: jobs run one at
a time on a single dedicated thread, and async callers await the result.
Running one job at a time also serializes the client's tool calls.

Between jobs the worker can run an idle function. The server sets it to the
session's pump while a session is active: with the sync API, Playwright only
handles browser events, routed requests included, while a call is in
progress, so without the pump a page's requests would wait until the agent's
next tool call. An idle function that raises is logged and tried again after
a growing pause (up to a second), never dropped; ``idle_errors`` and
``idle_last_error`` tell how it is doing.

Each job runs with ``sys.stdout`` pointed at stderr. In a stdio server stdout
carries the protocol; the protocol writer holds its own handle to the real
stream, so redirecting the name only catches stray prints.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import queue
import sys
import threading
import time
from typing import Any, Callable, Optional

# The idle function never runs more often than this, even if it returns at once.
IDLE_FLOOR_SEC = 0.01
# After it raises, the pause before the next try starts here and doubles up to the maximum.
IDLE_BACKOFF_START_SEC = 0.05
IDLE_BACKOFF_MAX_SEC = 1.0

_STOP = object()
_WAKE = object()  # makes a waiting loop look at the idle function again


class BrowserWorker:
    def __init__(self, name: str = "knarr-browser") -> None:
        self._jobs: "queue.Queue[Any]" = queue.Queue()
        self._idle: Optional[Callable[[], None]] = None
        self.idle_errors = 0
        self.idle_last_error: Optional[str] = None
        self._closed = False
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._thread.start()

    def set_idle(self, fn: Optional[Callable[[], None]]) -> None:
        """Run ``fn`` repeatedly while no job is waiting (None to stop).

        ``fn`` should return within a few tens of milliseconds; jobs wait for
        it. If it raises, the error is counted and it is tried again later.
        """
        if fn is not None:  # keep the counts when stopping: the final status still shows them
            self.idle_errors = 0
            self.idle_last_error = None
        self._idle = fn
        self._jobs.put(_WAKE)

    def _loop(self) -> None:
        wait = 0.0  # how long to wait for a job before the next idle call
        while True:
            idle = self._idle
            try:
                if idle is None:
                    job = self._jobs.get()
                elif wait > 0:
                    job = self._jobs.get(timeout=wait)
                else:
                    job = self._jobs.get_nowait()
            except queue.Empty:
                started = time.monotonic()
                try:
                    with contextlib.redirect_stdout(sys.stderr):
                        idle()
                except BaseException as exc:  # nothing may end this thread
                    self._idle_failed(exc)
                    wait = min(IDLE_BACKOFF_MAX_SEC, max(IDLE_BACKOFF_START_SEC, wait * 2))
                else:
                    wait = max(0.0, IDLE_FLOOR_SEC - (time.monotonic() - started))
                continue
            wait = 0.0
            if job is _STOP:
                return
            if job is _WAKE:
                continue
            fn, args, kwargs, future = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def _idle_failed(self, exc: BaseException) -> None:
        self.idle_errors += 1
        self.idle_last_error = f"{type(exc).__name__}: {exc}"
        if self.idle_errors in (1, 10, 100) or self.idle_errors % 1000 == 0:
            print(f"knarr: the idle pump failed {self.idle_errors} time(s), will retry: {exc!r}", file=sys.stderr)

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> concurrent.futures.Future:
        if self._closed:
            raise RuntimeError("BrowserWorker is shut down")
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((fn, args, kwargs, future))
        return future

    def call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` on the worker thread and block until it returns."""
        return self.submit(fn, *args, **kwargs).result()

    async def run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` on the worker thread without blocking the event loop."""
        return await asyncio.wrap_future(self.submit(fn, *args, **kwargs))

    def shutdown(self) -> None:
        self._closed = True
        self._idle = None
        self._jobs.put(_STOP)
        self._thread.join(timeout=30)
