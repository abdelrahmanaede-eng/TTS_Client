"""Single-writer command bus for mutable tracker state.

Every state mutation is executed on exactly one owner thread. Callers submit a
callable to one :class:`queue.Queue`; synchronous callers block on a
``concurrent.futures.Future`` carrying the result (or the exception).

Two deliberate fallbacks keep the existing callers and the test-suite working
without a running bus:

* Calls made *from the owner thread* run inline (no deadlock on re-entrancy,
  e.g. a command handler calling another command).
* Calls made while the bus was never started run inline (the unit tests drive
  ``ActivityTracker`` directly instead of starting its worker thread).

No third-party dependencies: only the Python standard library is used.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

logger = logging.getLogger(__name__)

_Task = tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], Future | None]
_SENTINEL = object()


class CommandBus:
    """One queue, one consumer thread, optional periodic tick on that thread."""

    def __init__(
        self,
        tick_fn: Callable[[], None],
        get_interval: Callable[[], float],
    ) -> None:
        self._queue: queue.Queue[_Task | object] = queue.Queue()
        self._tick_fn = tick_fn
        self._get_interval = get_interval
        self._thread: threading.Thread | None = None
        self._running = False
        self._accepting = False
        self._owner_ident: int | None = None
        self._lifecycle_lock = threading.Lock()

    # ---------------------------------------------------------
    # Status
    # ---------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    def is_owner(self) -> bool:
        """True when the caller is the single thread allowed to mutate state."""
        return self._owner_ident is not None and threading.get_ident() == self._owner_ident

    # ---------------------------------------------------------
    # Submission
    # ---------------------------------------------------------
    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future:
        """Queue ``fn`` for execution on the owner thread, return its Future.

        Inline execution happens only when no bus owns the state any more
        (tests / never started / already stopped) or when the caller already
        *is* the owner.
        """
        future: Future = Future()
        if self.is_owner() or not self._running:
            self._execute(fn, args, kwargs, future)
            return future
        if not self._accepting:
            raise RuntimeError("Tracker command bus is shutting down; cannot accept new commands")
        self._queue.put((fn, args, kwargs, future))
        return future

    def invoke(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Submit and block until the result (or exception) is available."""
        return self.submit(fn, *args, **kwargs).result()

    @staticmethod
    def _execute(
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        future: Future | None,
    ) -> None:
        if future is not None and future.cancelled():
            return
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:  # propagated through Future.result()
            if future is not None:
                future.set_exception(exc)
            else:
                logger.exception("Unhandled exception in tracker command")
        else:
            if future is not None:
                future.set_result(result)

    # ---------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------
    def start(self, name: str = "TrackerCommandBus") -> None:
        with self._lifecycle_lock:
            if self._running:
                return
            self._accepting = True
            self._running = True
            self._thread = threading.Thread(target=self._worker, name=name, daemon=True)
            self._thread.start()

    def stop(self, final_fn: Callable[[], Any] | None = None, timeout: float = 5.0) -> None:
        """Drain queued commands, optionally run ``final_fn`` last, then exit.

        ``final_fn`` and the sentinel are appended to the same FIFO queue, so
        the final state write always happens on the owner thread before it dies.
        """
        with self._lifecycle_lock:
            if not self._running:
                if final_fn is not None:
                    final_fn()  # never started: execute inline (tests)
                return
            self._accepting = False
            if final_fn is not None:
                self._queue.put((final_fn, (), {}, None))
            self._queue.put(_SENTINEL)
            thread = self._thread

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.error("Tracker command bus did not stop within %.1fs", timeout)

    def _worker(self) -> None:
        self._owner_ident = threading.get_ident()
        try:
            # First tick fires immediately, matching the old _loop() behaviour.
            next_tick = time.monotonic()
            while True:
                if time.monotonic() >= next_tick:
                    try:
                        self._tick_fn()
                    except Exception:
                        logger.exception("Exception in tracker tick")
                    try:
                        period = float(self._get_interval())
                    except (TypeError, ValueError):
                        period = 5.0
                    next_tick = time.monotonic() + max(0.05, period)
                    continue

                try:
                    item = self._queue.get(timeout=max(0.0, next_tick - time.monotonic()))
                except queue.Empty:
                    continue

                if item is _SENTINEL:
                    break
                fn, args, kwargs, future = item  # type: ignore[misc]
                self._execute(fn, args, kwargs, future)
        finally:
            self._owner_ident = None
            self._running = False
            self._accepting = False
