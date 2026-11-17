"""Verify-and-retry RPC auto-start, and exclusive port binding on Windows."""

import sys

import pytest

# Imported first: it puts addon/FreeCADMCP on sys.path.
from test_rpc_concurrency import filtered_server_class

from rpc_server.autostart import AutoStarter


class FakeTimers:
    def __init__(self) -> None:
        self.pending = []
        self.delays = []

    def schedule(self, ms, callback) -> None:
        self.delays.append(ms)
        self.pending.append(callback)

    def run_all(self, limit: int = 100) -> None:
        for _ in range(limit):
            if not self.pending:
                return
            self.pending.pop(0)()
        raise AssertionError("timer chain did not terminate")


def make_starter(start, is_running, max_attempts=5):
    timers = FakeTimers()
    logs = []
    starter = AutoStarter(
        start=start,
        is_running=is_running,
        schedule_ms=timers.schedule,
        log=lambda msg, warn: logs.append((msg, warn)),
        max_attempts=max_attempts,
        retry_ms=2000,
    )
    return starter, timers, logs


def test_starts_on_first_attempt_and_stops_retrying():
    state = {"running": False, "calls": 0}

    def start():
        state["calls"] += 1
        state["running"] = True
        return "RPC Server started"

    starter, timers, logs = make_starter(start, lambda: state["running"])
    starter.begin()
    timers.run_all()
    assert state["calls"] == 1
    assert starter.done
    assert timers.delays == [0]
    assert logs == [("attempt 1: RPC Server started", False)]


def test_retries_after_exception_until_it_succeeds():
    state = {"running": False, "calls": 0}

    def start():
        state["calls"] += 1
        if state["calls"] < 3:
            raise OSError("[WinError 10048] address in use")
        state["running"] = True
        return "RPC Server started"

    starter, timers, logs = make_starter(start, lambda: state["running"])
    starter.begin()
    timers.run_all()
    assert state["calls"] == 3
    assert timers.delays == [0, 2000, 2000]
    assert [warn for _, warn in logs] == [True, True, False]
    assert "attempt 1 failed" in logs[0][0]


def test_message_without_server_counts_as_failure():
    # start_rpc_server returns some refusals as text instead of raising.
    starter, timers, logs = make_starter(
        lambda: "RPC Server is still stopping", lambda: False, max_attempts=2
    )
    starter.begin()
    timers.run_all()
    assert starter.done
    assert "did not start the server" in logs[0][0]
    assert "giving up after 2 attempts" in logs[-1][0]
    assert logs[-1][1] is True


def test_gives_up_after_max_attempts():
    calls = []

    def start():
        calls.append(1)
        raise RuntimeError("boom")

    starter, timers, logs = make_starter(start, lambda: False, max_attempts=4)
    starter.begin()
    timers.run_all()
    assert len(calls) == 4
    assert not timers.pending
    assert "giving up after 4 attempts" in logs[-1][0]


def test_already_running_is_a_no_op():
    calls = []
    starter, timers, logs = make_starter(lambda: calls.append(1), lambda: True)
    starter.begin()
    timers.run_all()
    assert calls == []
    assert logs == [("server already running (attempt 1).", False)]


def test_port_in_use_is_explained():
    err = OSError(10048, "address in use")
    err.winerror = 10048

    def start():
        raise err

    starter, timers, logs = make_starter(start, lambda: False, max_attempts=1)
    starter.begin()
    timers.run_all()
    assert "Another FreeCAD instance" in logs[0][0]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only bind semantics")
def test_second_server_cannot_share_the_port_on_windows():
    cls = filtered_server_class()
    first = cls(("127.0.0.1", 0), allow_none=True, logRequests=False)
    try:
        port = first.server_address[1]
        with pytest.raises(OSError):
            second = cls(("127.0.0.1", port), allow_none=True, logRequests=False)
            second.server_close()
    finally:
        first.server_close()


def test_port_can_be_rebound_right_after_close():
    cls = filtered_server_class()
    first = cls(("127.0.0.1", 0), allow_none=True, logRequests=False)
    port = first.server_address[1]
    first.server_close()
    again = cls(("127.0.0.1", port), allow_none=True, logRequests=False)
    again.server_close()
