"""ACL targets stay inside directories the app owns.

``C:\\Windows`` was an explicit ``SystemRoot`` candidate in the historical
icacls list. On a GitHub runner the interpreter is under
``C:\\hostedtoolcache``, and walking that prefix's parents does not reach
``C:\\Windows``. The grant must drop the system directory instead of
writing it.
"""

import sys
from pathlib import Path

import pytest

from backtester.sandbox import (
    SandboxError,
    acl_ancestor_paths,
    acl_revoke_problem,
    acl_same,
    planned_acl_targets,
    refuse_acl_target,
    should_write_ace,
)

ENV = {
    "SystemRoot": r"C:\Windows",
    "WINDIR": r"C:\Windows",
    "SystemDrive": "C:",
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "ProgramW6432": r"C:\Program Files",
    "USERPROFILE": r"C:\Users\runner",
    "LOCALAPPDATA": r"C:\Users\runner\AppData\Local",
    "ProgramData": r"C:\ProgramData",
}

PREFIX = r"C:\hostedtoolcache\windows\Python\3.12\x64"
SYSTEM = r"C:\Windows"
PROGRAM_FILES_PYTHON = r"C:\Program Files\Python312"
INSIDE_WINDOWS = r"C:\Windows\System32"
VENV = r"C:\Users\runner\proj\.venv"
FROZEN = r"C:\Program Files\Strategy Builder\app"
WORK = r"C:\Users\runner\AppData\Local\Temp\bt-referee-abc"
APP = r"C:\ProgramData\Strategy Builder\jail"
LOCAL = r"C:\Users\runner\AppData\Local"
USER_PYTHON = r"C:\Users\runner\AppData\Local\Programs\Python\Python312"

# The old icacls roots: interpreter, SystemRoot, a Program Files install,
# the user-profile Local directory that used to be denied, plus the
# ancestors those roots produced.
OLD_CANDIDATES = [PREFIX, SYSTEM, PROGRAM_FILES_PYTHON, INSIDE_WINDOWS, VENV, FROZEN, LOCAL]


def _same_set(left, right) -> bool:
    if len(left) != len(right):
        return False
    return all(any(acl_same(item, other) for other in right) for item in left)


def _targets(**kwargs):
    return planned_acl_targets(OLD_CANDIDATES, environ=ENV, **kwargs)


def test_system_root_is_not_an_ancestor_of_the_toolcache_prefix():
    ancestors = acl_ancestor_paths(PREFIX)
    assert not any(acl_same(item, SYSTEM) for item in ancestors)
    assert any(acl_same(item, r"C:\hostedtoolcache") for item in ancestors)
    assert any(acl_same(item, r"C:\\") for item in ancestors)


def test_acl_targets_stay_inside_the_app_owned_allow_list():
    kept = _targets(active={PREFIX}, venv={VENV}, frozen={FROZEN}, work=WORK, app_data=[APP])
    expected = [PREFIX, VENV, FROZEN, WORK, APP]
    assert _same_set(kept, expected)
    protected = [
        SYSTEM,
        INSIDE_WINDOWS,
        r"C:\Windows\SysWOW64",
        r"C:\Program Files",
        r"C:\Program Files (x86)",
        PROGRAM_FILES_PYTHON,
        r"C:\\",
        r"C:\Users\runner",
        LOCAL,
        r"C:\Users\runner\AppData",
        r"C:\Users\runner\AppData\Local\Temp",
        r"C:\hostedtoolcache",
        r"C:\hostedtoolcache\windows",
        r"C:\ProgramData",
    ]
    for path in protected:
        assert not any(acl_same(item, path) for item in kept)
        with pytest.raises(SandboxError, match="refusing to edit the ACL"):
            refuse_acl_target(path, allow=kept, environ=ENV)
    for path in expected:
        refuse_acl_target(path, allow=kept, environ=ENV)
    refuse_acl_target(WORK + r"\pydeps", allow=kept, environ=ENV)
    refuse_acl_target(PREFIX + r"\python.exe", allow=kept, environ=ENV)


def test_a_protected_path_is_refused_even_when_listed_as_allowed():
    for path in (SYSTEM, INSIDE_WINDOWS, r"C:\Program Files", r"C:\\", r"C:\Users\runner"):
        with pytest.raises(SandboxError, match="refusing to edit the ACL"):
            refuse_acl_target(path, allow=[path], environ=ENV)


def test_a_prefix_inside_windows_is_not_a_target():
    kept = planned_acl_targets([INSIDE_WINDOWS, SYSTEM], environ=ENV, active={INSIDE_WINDOWS, SYSTEM})
    assert kept == []


def test_a_program_files_interpreter_is_not_a_target():
    kept = planned_acl_targets(
        [PROGRAM_FILES_PYTHON],
        environ=ENV,
        active={PROGRAM_FILES_PYTHON},
    )
    assert kept == []


def test_a_user_python_is_granted_without_its_profile_ancestors():
    kept = planned_acl_targets([USER_PYTHON, SYSTEM], environ=ENV, active={USER_PYTHON})
    assert _same_set(kept, [USER_PYTHON])
    with pytest.raises(SandboxError, match="refusing to edit the ACL"):
        refuse_acl_target(r"C:\Users\runner", allow=kept, environ=ENV)


def test_a_package_inside_an_owned_prefix_is_not_a_separate_target():
    site = PREFIX + r"\Lib\site-packages\numpy"
    outside = r"C:\other\numpy"
    kept = planned_acl_targets([PREFIX, site, outside, SYSTEM], environ=ENV, active={PREFIX})
    assert _same_set(kept, [PREFIX])


def test_cleanup_still_refuses_a_system_directory():
    assert acl_revoke_problem(SYSTEM, environ=ENV)
    assert acl_revoke_problem(r"C:\\", environ=ENV)
    assert acl_revoke_problem(r"C:\Users\runner", environ=ENV)
    assert acl_revoke_problem(WORK, environ=ENV) is None
    assert acl_revoke_problem(PREFIX, environ=ENV) is None


def test_an_existing_ace_is_not_rewritten():
    assert should_write_ace(False) is True
    assert should_write_ace(True) is False


def _suite_conftest():
    """Load tests/conftest.py even when gui/conftest.py owns the name ``conftest``."""
    import importlib.util

    path = Path(__file__).resolve().parent / "conftest.py"
    for module in sys.modules.values():
        file = getattr(module, "__file__", None)
        if not file:
            continue
        try:
            if Path(file).resolve() == path:
                return module
        except OSError:
            continue
    spec = importlib.util.spec_from_file_location("suite_conftest_acl", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_grant_refusal_fails_each_test_and_does_not_abort(monkeypatch):
    suite = _suite_conftest()
    monkeypatch.setattr(suite.sys, "platform", "win32")

    def boom():
        raise SandboxError("refusing to edit the ACL on C:\\Windows: protected directory")

    monkeypatch.setattr("backtester.sandbox.warm_windows_profile", boom)
    suite._PROFILE_ERROR = None
    suite.pytest_sessionstart(None)
    try:
        assert isinstance(suite._PROFILE_ERROR, SandboxError)
        with pytest.raises(pytest.fail.Exception, match="Windows jail profile was not granted"):
            suite.pytest_runtest_setup(object())
    finally:
        suite._PROFILE_ERROR = None
