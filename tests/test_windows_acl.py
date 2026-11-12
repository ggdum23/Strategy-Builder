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
    _copy_runtime_tree,
    acl_ancestor_paths,
    acl_revoke_problem,
    acl_same,
    planned_acl_targets,
    refuse_acl_target,
    rewrite_pyvenv_home,
    runtime_relpath_is_copied,
    should_write_ace,
    copy_parent_candidates,
    stage_unreachable_runtimes,
    translate_staged_path,
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


def _open(path: str):
    def accessible(candidate: str) -> bool:
        return not acl_same(candidate, path)

    return accessible


def test_a_blocked_toolcache_ancestor_is_replaced_by_an_app_owned_copy():
    stage = APP + r"\runtime"
    version = r"C:\hostedtoolcache\windows\Python\3.12"
    grant, copies = stage_unreachable_runtimes(
        [PREFIX],
        allow=[PREFIX, stage],
        accessible=_open(version),
        stage_root=stage,
    )
    assert len(copies) == 1
    assert acl_same(copies[0][0], PREFIX)
    assert acl_same(copies[0][1], stage)
    assert _same_set(grant, [stage])
    for path in (version, r"C:\hostedtoolcache", r"C:\ProgramData", SYSTEM, r"C:\\"):
        assert not any(acl_same(item, path) for item in grant)
        with pytest.raises(SandboxError, match="refusing to edit the ACL"):
            refuse_acl_target(path, allow=grant, environ=ENV)
    refuse_acl_target(stage, allow=grant, environ=ENV)
    assert acl_same(
        translate_staged_path(PREFIX + r"\Lib\site-packages\numpy", copies),
        stage + r"\Lib\site-packages\numpy",
    )
    assert translate_staged_path(SYSTEM, copies) == SYSTEM


def test_a_blocked_ancestor_is_not_granted_when_no_copy_parent_exists():
    with pytest.raises(SandboxError, match="3.12"):
        stage_unreachable_runtimes(
            [PREFIX],
            allow=[PREFIX],
            accessible=_open(r"C:\hostedtoolcache\windows\Python\3.12"),
            stage_root=None,
        )


def test_the_copy_parent_is_not_rewritten_when_it_also_blocks_traverse():
    stage = APP + r"\runtime"
    with pytest.raises(SandboxError, match="ProgramData"):
        stage_unreachable_runtimes(
            [PREFIX],
            allow=[PREFIX, r"C:\ProgramData\Strategy Builder", APP, stage],
            accessible=lambda path: not (
                acl_same(path, r"C:\hostedtoolcache\windows\Python\3.12")
                or acl_same(path, r"C:\ProgramData")
            ),
            stage_root=stage,
        )


def test_a_reachable_interpreter_is_not_copied():
    grant, copies = stage_unreachable_runtimes(
        [PREFIX],
        allow=[PREFIX],
        accessible=lambda _path: True,
        stage_root=APP + r"\runtime",
    )
    assert copies == []
    assert _same_set(grant, [PREFIX])


def test_two_unreachable_runtimes_stay_under_one_app_owned_directory():
    stage = APP + r"\runtime"
    grant, copies = stage_unreachable_runtimes(
        [PREFIX, VENV],
        allow=[PREFIX, VENV],
        accessible=lambda path: not (
            acl_same(path, r"C:\hostedtoolcache\windows\Python\3.12")
            or acl_same(path, r"C:\Users\runner")
        ),
        stage_root=stage,
    )
    assert len(copies) == 2
    assert acl_same(copies[0][0], PREFIX)
    assert acl_same(copies[1][0], VENV)
    assert _same_set(grant, [stage, stage + r"\0", stage + r"\1"])
    assert not any(acl_same(item, r"C:\Users\runner") for item in grant)
    assert not any(acl_same(item, PREFIX) for item in grant)


def test_the_staged_interpreter_keeps_the_child_files_only(tmp_path):
    prefix = tmp_path / "x64"
    numpy = prefix / "Lib" / "site-packages" / "numpy"
    numpy.mkdir(parents=True)
    (numpy / "__init__.py").write_text("numpy", encoding="utf-8")
    pip = prefix / "Lib" / "site-packages" / "pip"
    pip.mkdir(parents=True)
    (pip / "__init__.py").write_text("pip", encoding="utf-8")
    (prefix / "Lib" / "os.py").write_text("os", encoding="utf-8")
    regression = prefix / "Lib" / "test"
    regression.mkdir()
    (regression / "test_os.py").write_text("test", encoding="utf-8")
    cache = prefix / "Lib" / "__pycache__"
    cache.mkdir()
    (cache / "os.pyc").write_bytes(b"pyc")
    (prefix / "python.exe").write_bytes(b"exe")
    (prefix / "python311._pth").write_text("import site", encoding="utf-8")
    (prefix / "DLLs").mkdir()
    (prefix / "DLLs" / "_ctypes.pyd").write_bytes(b"dll")
    include = prefix / "include"
    include.mkdir()
    (include / "Python.h").write_text("h", encoding="utf-8")
    (prefix / "pyvenv.cfg").write_text(
        "home = C:\\hostedtoolcache\\windows\\Python\\3.12\\x64\ninclude-system-site-packages = false\n",
        encoding="utf-8",
    )
    stage = tmp_path / "runtime"
    _copy_runtime_tree(prefix, stage, [numpy])
    assert (stage / "python.exe").read_bytes() == b"exe"
    assert (stage / "Lib" / "os.py").is_file()
    assert (stage / "DLLs" / "_ctypes.pyd").is_file()
    assert (stage / "Lib" / "site-packages" / "numpy" / "__init__.py").is_file()
    assert not (stage / "Lib" / "site-packages" / "pip").exists()
    assert not (stage / "Lib" / "__pycache__").exists()
    assert not (stage / "Lib" / "test").exists()
    assert not (stage / "include").exists()
    assert not (stage / "python311._pth").exists()
    assert not runtime_relpath_is_copied(("python311._pth",), is_dir=False, package_prefixes=())
    rewritten = rewrite_pyvenv_home(
        (stage / "pyvenv.cfg").read_text(encoding="utf-8"),
        lambda home: translate_staged_path(home, [(PREFIX, r"C:\ProgramData\Strategy Builder\jail\runtime")]),
    )
    assert acl_same(
        rewritten.split("=", 1)[1].strip().splitlines()[0],
        r"C:\ProgramData\Strategy Builder\jail\runtime",
    )


def test_programdata_is_the_copy_parent_when_its_chain_is_open():
    version = r"C:\hostedtoolcache\windows\Python\3.12"
    found = copy_parent_candidates(ENV, [version + r"\x64"], _open(version))
    assert acl_same(found[0], r"C:\ProgramData")
    assert any(acl_same(item, r"C:\hostedtoolcache\windows\Python") for item in found)
    assert not any(acl_same(item, version) for item in found)
    assert not any(_is_volume(item) for item in found)


def test_a_closed_programdata_uses_the_nearest_open_ancestor():
    version = r"C:\hostedtoolcache\windows\Python\3.12"
    closed = (version, r"C:\ProgramData", r"C:\Users\Public")

    def accessible(path: str) -> bool:
        return not any(acl_same(path, item) for item in closed)

    environ = dict(ENV)
    environ["PUBLIC"] = r"C:\Users\Public"
    found = copy_parent_candidates(environ, [version + r"\x64"], accessible)
    assert acl_same(found[0], r"C:\hostedtoolcache\windows\Python")
    assert not any(acl_same(item, r"C:\ProgramData") for item in found)
    assert not any(acl_same(item, r"C:\Users\Public") for item in found)
    assert not any(acl_same(item, version) for item in found)
    assert not any(_is_volume(item) for item in found)


def test_the_actions_temp_directory_is_not_the_copy_parent():
    blocked = r"D:\a\_temp"
    closed = (blocked, r"C:\ProgramData", r"C:\Users\Public", r"C:\Program Files", r"C:\Program Files (x86)")

    def accessible(path: str) -> bool:
        return not any(acl_same(path, item) for item in closed)

    environ = dict(ENV)
    environ["PUBLIC"] = r"C:\Users\Public"
    found = copy_parent_candidates(environ, [blocked + r"\Strategy Builder\_internal"], accessible)
    assert acl_same(found[0], r"D:\a")
    assert not any(acl_same(item, blocked) for item in found)
    assert not any(_is_volume(item) for item in found)


def test_windows_is_not_a_copy_parent():
    environ = {
        "SystemRoot": r"C:\Windows",
        "WINDIR": r"C:\Windows",
        "SystemDrive": "C:",
        "USERPROFILE": r"C:\Users\runner",
    }
    found = copy_parent_candidates(
        environ,
        [r"C:\Windows\Temp\py"],
        _open(r"C:\Windows\Temp"),
    )
    assert found == []


def test_a_frozen_bundle_copy_keeps_nested_directories(tmp_path):
    bundle = tmp_path / "app"
    internal = bundle / "_internal" / "numpy"
    internal.mkdir(parents=True)
    (internal / "__init__.py").write_text("numpy", encoding="utf-8")
    (bundle / "backtester.exe").write_bytes(b"exe")
    (bundle / "app.pth").write_text("x", encoding="utf-8")
    stage = tmp_path / "runtime"
    _copy_runtime_tree(bundle, stage, [], full=True)
    assert (stage / "backtester.exe").read_bytes() == b"exe"
    assert (stage / "_internal" / "numpy" / "__init__.py").is_file()
    assert not (stage / "app.pth").exists()
    assert runtime_relpath_is_copied(("_internal", "numpy"), is_dir=True, package_prefixes=(), full=True)
    assert not runtime_relpath_is_copied(("_internal",), is_dir=True, package_prefixes=())


def _is_volume(path: str) -> bool:
    from backtester.sandbox import _is_volume_acl_path

    return _is_volume_acl_path(path)


def test_the_child_command_uses_the_staged_interpreter(monkeypatch):
    import backtester.sandbox as sandbox

    sandbox._RUNTIME_COPIES = [(PREFIX, APP + r"\runtime")]
    monkeypatch.setattr(sandbox.sys, "frozen", False, raising=False)
    monkeypatch.setattr(sandbox.sys, "executable", PREFIX + r"\python.exe")
    try:
        command = sandbox._interpreter_command(Path(r"C:\work\sandbox_child.py"), child=True)
        assert acl_same(command[0], APP + r"\runtime\python.exe")
        assert command[1:4] == ["-I", "-S", "-B"]
    finally:
        sandbox._RUNTIME_COPIES = []


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
