"""Tests for the single-writer tracker command bus."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from command_bus import CommandBus
from config import Config
from database import Database
from tracker import ActivityTracker


class TestCommandBusCore(unittest.TestCase):
    def test_all_commands_run_on_one_worker_thread(self) -> None:
        ids: list[int] = []
        bus = CommandBus(tick_fn=lambda: None, get_interval=lambda: 10.0)
        bus.start()
        try:
            results = []
            for i in range(20):
                results.append(bus.invoke(lambda n=i: (ids.append(threading.get_ident()), n)[1]))
            self.assertEqual(results, list(range(20)))
            self.assertEqual(len(set(ids)), 1)
            self.assertNotEqual(ids[0], threading.get_ident())
        finally:
            bus.stop()

    def test_result_and_exception_travel_through_the_future(self) -> None:
        bus = CommandBus(tick_fn=lambda: None, get_interval=lambda: 10.0)
        bus.start()
        try:
            self.assertEqual(bus.invoke(lambda: 6 * 7), 42)

            def boom() -> None:
                raise ValueError("boom")

            with self.assertRaisesRegex(ValueError, "boom"):
                bus.invoke(boom)
            # A failing command must not kill the worker.
            self.assertEqual(bus.invoke(lambda: 1), 1)
        finally:
            bus.stop()

    def test_inline_fallback_when_bus_never_started(self) -> None:
        bus = CommandBus(tick_fn=lambda: None, get_interval=lambda: 10.0)
        seen: list[int] = []
        self.assertEqual(bus.invoke(lambda: 7), 7)
        bus.invoke(lambda: seen.append(threading.get_ident()))
        self.assertEqual(seen, [threading.get_ident()])
        self.assertFalse(bus.is_running)
        self.assertFalse(bus.is_owner())

    def test_owner_can_reenter_without_deadlock(self) -> None:
        bus = CommandBus(tick_fn=lambda: None, get_interval=lambda: 10.0)
        bus.start()
        try:
            def outer() -> tuple[int, str, int]:
                inner_id: list[int] = []

                def inner() -> str:
                    inner_id.append(threading.get_ident())
                    return "inner"

                value = bus.invoke(inner)
                return threading.get_ident(), value, inner_id[0]

            owner_id, value, inner_id = bus.invoke(outer)
            self.assertEqual(value, "inner")
            self.assertEqual(owner_id, inner_id)
        finally:
            bus.stop()

    def test_tick_runs_on_the_same_owner_thread(self) -> None:
        tick_ids: list[int] = []
        done = threading.Event()

        def tick() -> None:
            tick_ids.append(threading.get_ident())
            if len(tick_ids) >= 3:
                done.set()

        bus = CommandBus(tick_fn=tick, get_interval=lambda: 0.02)
        bus.start()
        try:
            self.assertTrue(done.wait(2.0), "periodic tick did not fire")
            command_id: list[int] = []
            bus.invoke(lambda: command_id.append(threading.get_ident()))
        finally:
            bus.stop(timeout=1.0)

        self.assertEqual(set(tick_ids), {tick_ids[0]})
        self.assertEqual(command_id, [tick_ids[0]])

    def test_stop_drains_commands_then_runs_final_on_owner(self) -> None:
        order: list[str] = []
        ids: list[int] = []

        def work(n: int) -> None:
            order.append(f"work{n}")
            ids.append(threading.get_ident())

        def final() -> None:
            order.append("final")
            ids.append(threading.get_ident())

        bus = CommandBus(tick_fn=lambda: None, get_interval=lambda: 10.0)
        bus.start()
        for i in range(5):
            bus.submit(work, i)
        bus.stop(final_fn=final)

        self.assertEqual(order, ["work0", "work1", "work2", "work3", "work4", "final"])
        self.assertEqual(set(ids), {ids[0]})
        self.assertFalse(bus.is_running)


class TestTrackerUsesBus(unittest.TestCase):
    def test_public_command_returns_value_and_mutates_on_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "bus.db")
            tracker = ActivityTracker(db, Config(active_check_enabled=False))
            tracker.start()
            try:
                self.assertTrue(tracker._bus.is_running)
                # submit+wait keeps the original synchronous signature.
                self.assertTrue(tracker.toggle_pause(15))
                self.assertTrue(tracker.is_paused())

                owner_check: list[bool] = []
                tracker._bus.invoke(lambda: owner_check.append(tracker._bus.is_owner()))
                self.assertEqual(owner_check, [True])
            finally:
                tracker.stop()

    def test_stop_closes_sessions_on_owner_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "bus.db")
            tracker = ActivityTracker(db, Config(active_check_enabled=False))
            tracker.start()

            closed_on: list[bool] = []
            original: Any = db.close_all_open_sessions

            def spy(*args: Any, **kwargs: Any) -> Any:
                closed_on.append(tracker._bus.is_owner())
                return original(*args, **kwargs)

            db.close_all_open_sessions = spy
            tracker.stop()
            self.assertEqual(closed_on, [True])


if __name__ == "__main__":
    unittest.main()
