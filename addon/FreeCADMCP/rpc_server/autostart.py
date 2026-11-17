"""Verify-and-retry auto-start for the RPC server.

The original auto-start was one ``QTimer.singleShot(0, ...)`` scheduled while
InitGui.py executes, before the main event loop runs. When anything went wrong
(a swallowed timer, the port still held by a closing FreeCAD, an exception in
the except-handler itself) FreeCAD came up with ``auto_start_rpc`` on, nothing
listening, and no trace of why.

This module keeps retrying until the server object actually exists, gives up
after a bounded window, and appends every attempt to a log file next to the
settings file so a failed start is always diagnosable after the fact.

The retry logic lives in ``AutoStarter`` with injected callables so it can be
tested without FreeCAD or Qt. ``schedule()`` wires the real pieces.
"""

import datetime
import os
import traceback
from collections.abc import Callable

LOG_FILENAME = "freecad_mcp_autostart.log"
MAX_LOG_BYTES = 256 * 1024

# First attempt as soon as the event loop runs, then every RETRY_MS until
# MAX_ATTEMPTS. 15 attempts x 2 s covers a FreeCAD that is slow to finish
# loading and a previous instance that is still releasing the port.
FIRST_DELAY_MS = 0
RETRY_MS = 2000
MAX_ATTEMPTS = 15

# Strong reference so the starter and its pending callbacks survive after
# InitGui.py's exec namespace is discarded.
_active = None


class AutoStarter:
    def __init__(
        self,
        start: Callable[[], str],
        is_running: Callable[[], bool],
        schedule_ms: Callable[[int, Callable[[], None]], None],
        log: Callable[[str, bool], None],
        max_attempts: int = MAX_ATTEMPTS,
        retry_ms: int = RETRY_MS,
    ) -> None:
        self._start = start
        self._is_running = is_running
        self._schedule_ms = schedule_ms
        self._log = log
        self.max_attempts = max_attempts
        self.retry_ms = retry_ms
        self.attempt = 0
        self.done = False

    def begin(self, first_delay_ms: int = FIRST_DELAY_MS) -> None:
        self._schedule_ms(first_delay_ms, self.try_once)

    def try_once(self) -> None:
        if self.done:
            return
        self.attempt += 1
        try:
            if self._is_running():
                self._finish(f"server already running (attempt {self.attempt}).")
                return
            msg = self._start()
            if self._is_running():
                self._finish(f"attempt {self.attempt}: {msg}")
                return
            # start_rpc_server reports some refusals as a message, not an
            # exception (e.g. a previous stop still draining).
            self._log(f"attempt {self.attempt} did not start the server: {msg}", True)
        except Exception as exc:  # noqa: BLE001 - every failure must be logged
            detail = _describe(exc)
            self._log(f"attempt {self.attempt} failed: {detail}", True)
        if self.attempt >= self.max_attempts:
            self.done = True
            self._log(
                f"giving up after {self.attempt} attempts. Use FreeCAD MCP > "
                "Start RPC Server, and see the log for the errors above.",
                True,
            )
            return
        self._schedule_ms(self.retry_ms, self.try_once)

    def _finish(self, message: str) -> None:
        self.done = True
        self._log(message, False)


def _describe(exc: BaseException) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror == 10048 or getattr(exc, "errno", None) in (48, 98):
        return (
            f"{exc} - port already in use. Another FreeCAD instance (or another "
            "program) is already serving MCP on this port."
        )
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()


def _log_path() -> str:
    import FreeCAD

    return os.path.join(FreeCAD.getUserAppDataDir(), LOG_FILENAME)


def _write_log(line: str) -> None:
    try:
        path = _log_path()
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            os.replace(path, path + ".1")
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{stamp} pid={os.getpid()} {line}\n")
    except Exception:  # noqa: BLE001 - logging must never break startup
        pass


def _console_and_file(message: str, is_warning: bool) -> None:
    _write_log(message)
    try:
        import FreeCAD

        text = f"[MCP] Auto-start: {message}\n"
        if is_warning:
            FreeCAD.Console.PrintWarning(text)
        else:
            FreeCAD.Console.PrintMessage(text)
    except Exception:  # noqa: BLE001
        pass


def schedule() -> None:
    """Entry point called from InitGui.py."""
    global _active
    try:
        from PySide import QtCore
        from rpc_server.settings import load_settings

        if not load_settings().get("auto_start_rpc", False):
            return

        # rpc_server.rpc_server is imported lazily, inside the timer callbacks,
        # exactly as the original auto-start did: it registers GUI commands on
        # import and should not load while InitGui.py is still executing.
        def _start() -> str:
            from rpc_server import rpc_server

            return rpc_server.start_rpc_server()

        def _is_running() -> bool:
            from rpc_server import rpc_server

            return rpc_server.rpc_server_instance is not None

        _write_log("FreeCAD GUI loaded; auto_start_rpc is on, scheduling start.")
        _active = AutoStarter(
            start=_start,
            is_running=_is_running,
            schedule_ms=lambda ms, cb: QtCore.QTimer.singleShot(ms, cb),
            log=_console_and_file,
        )
        _active.begin()
    except Exception as exc:  # noqa: BLE001
        _console_and_file(f"could not schedule auto-start: {_describe(exc)}", True)
