
from __future__ import annotations

import sys

import pytest

_JAIL_LAUNCHES = 0
_PROFILE_ERROR = None


def _count_jail() -> None:
    global _JAIL_LAUNCHES
    _JAIL_LAUNCHES += 1


def pytest_configure(config) -> None:
    from backtester.sandbox import set_launch_observer

    set_launch_observer(_count_jail)
    if sys.platform != "win32":
        return
    try:
        import pytest_timeout
    except ImportError:
        return
    _fail_one_windows_test(pytest_timeout)


def pytest_sessionstart(session) -> None:
    global _PROFILE_ERROR
    if sys.platform != "win32":
        return
    from backtester.sandbox import warm_windows_profile

    try:
        warm_windows_profile()
    except Exception as exc:
        _PROFILE_ERROR = exc


def pytest_runtest_setup(item) -> None:
    global _JAIL_LAUNCHES
    _JAIL_LAUNCHES = 0
    if _PROFILE_ERROR is not None:
        pytest.fail(f"Windows jail profile was not granted: {_PROFILE_ERROR}", pytrace=False)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.passed:
        return
    problem = _jail_mark_problem(item)
    if not problem:
        return
    report.outcome = "failed"
    report.longrepr = problem


def _jail_mark_problem(item) -> str | None:
    count = _JAIL_LAUNCHES
    if count <= 0:
        return None
    mark = item.get_closest_marker("windows_jails")
    allowed = 0
    if mark is not None and mark.args:
        allowed = int(mark.args[0])
    if allowed >= count:
        return None
    budget = 60 if allowed == 0 else 90 * allowed
    return (
        f"{item.nodeid} launched {count} jail(s) with windows_jails({allowed or 'missing'}), "
        f"which budgets {budget}s on Windows. Mark it @windows_jails({count}) or higher."
    )


def _fail_one_windows_test(pytest_timeout) -> None:
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
