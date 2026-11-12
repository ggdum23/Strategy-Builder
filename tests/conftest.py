"""Session hooks. The Windows jail grant runs once, before any timed test."""

from __future__ import annotations

import sys


def pytest_configure(config) -> None:
    if sys.platform != "win32":
        return
    try:
        import pytest_timeout
    except ImportError:
        return
    _fail_one_windows_test(pytest_timeout)


def pytest_sessionstart(session) -> None:
    if sys.platform != "win32":
        return
    from backtester.sandbox import warm_windows_profile

    warm_windows_profile()


def _fail_one_windows_test(pytest_timeout) -> None:
    """Turn a 60s timeout into a failure of that test.

    pytest-timeout's thread method calls ``os._exit(1)``. On Windows that
    ends the job at the first slow test, so a later ``xfail`` is never
    recorded. The wait loop in the runner returns every fifth of a second,
    and raising here lands on the next iteration.
    """
    import ctypes
    import threading

    from _pytest.outcomes import Failed

    note = {"msg": "Timeout from pytest-timeout."}

    class _WindowsTimeout(Failed):
        def __init__(self, msg=None, pytrace=True):
            super().__init__(note["msg"], pytrace=pytrace)

    api = ctypes.pythonapi.PyThreadState_SetAsyncExc
    api.argtypes = [ctypes.c_ulong, ctypes.py_object]
    api.restype = ctypes.c_int
    original = pytest_timeout.timeout_timer

    def timeout_timer(item, settings):
        ident = threading.main_thread().ident
        if ident is None:
            original(item, settings)
            return
        note["msg"] = f"Timeout (>{settings.timeout}s) from pytest-timeout: {item.nodeid}"
        raised = api(ctypes.c_ulong(ident), ctypes.py_object(_WindowsTimeout))
        if raised == 1:
            return
        if raised > 1:
            api(ctypes.c_ulong(ident), None)
        original(item, settings)

    pytest_timeout.timeout_timer = timeout_timer
