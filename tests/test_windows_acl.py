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
    _chain_accessible,
    _copy_runtime_tree,
    _ensure_runtime_integrity,
    _remove_tree,
    ace_grants_access,
    acl_ancestor_paths,
    bwrap_mount_plan,
    canonicalize_acl_path,
    choose_copy_parent,
    dacl_has_inheritable_write,
    firewall_timeout_blocks_launch,
    landlock_rule_plan,
    owner_matches,
    reparse_point_blocks_acl,
    runtime_tree_digest,
    shared_parent_usable,
    toolcache_ancestor_paths,
    wfp_connect_layers,
    _PACKAGE_SID_TEXTS,
    _RUNTIME_COPY_VERSION,
    _drop_launch_aces_locked,
    _firewall_remove_command,
    acl_revoke_problem,
    acl_same,
    planned_acl_targets,
    refuse_acl_target,
    rewrite_pyvenv_home,
    runtime_relpath_is_copied,
    should_write_ace,
    container_mkdir_plan,
    conventional_container_folder,
    copy_parent_candidates,
    stage_unreachable_runtimes,
    translate_staged_path,
    _firewall_block_command,
    firewall_block_is_current,
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


def test_a_closed_volume_root_does_not_block_the_copy():
    version = r"C:\hostedtoolcache\windows\Python\3.12"
    stage = version.rsplit("\\", 1)[0] + r"\Strategy Builder\jail\runtime"
    closed = (version, r"C:\ProgramData", r"C:\Users\Public", r"C:\Program Files", r"C:\Program Files (x86)", r"C:\\")

    def accessible(path: str) -> bool:
        return not any(acl_same(path, item) for item in closed)

    environ = dict(ENV)
    environ["PUBLIC"] = r"C:\Users\Public"
    found = copy_parent_candidates(environ, [version + r"\x64"], accessible)
    assert acl_same(found[0], r"C:\hostedtoolcache\windows\Python")
    assert not any(_is_volume(item) for item in found)
    grant, copies = stage_unreachable_runtimes(
        [version + r"\x64"],
        allow=[version + r"\x64", stage],
        accessible=accessible,
        stage_root=stage,
    )
    assert len(copies) == 1
    assert acl_same(copies[0][1], stage)
    assert not any(_is_volume(item) for item in grant)
    assert not any(acl_same(item, r"C:\hostedtoolcache\windows\Python") for item in grant)


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


def test_a_closed_volume_root_does_not_fail_the_parent_chain():
    assert _chain_accessible(r"C:\ProgramData", lambda path: not _is_volume(path))
    assert not _chain_accessible(r"C:\ProgramData", lambda path: not acl_same(path, r"C:\ProgramData"))


def test_a_trusted_container_folder_does_not_grant_its_ancestors():
    stage = r"C:\Users\runner\AppData\Local\Packages\strategybuilderjail\runtime"
    version = r"C:\hostedtoolcache\windows\Python\3.12"

    def closed(_path: str) -> bool:
        return False

    grant, copies = stage_unreachable_runtimes(
        [PREFIX],
        allow=[stage],
        accessible=closed,
        stage_root=stage,
        trust_stage=True,
    )
    assert len(copies) == 1
    assert acl_same(copies[0][1], stage)
    assert _same_set(grant, [stage])
    for path in (r"C:\Users\runner", r"C:\Users\runner\AppData", version, r"C:\ProgramData", r"C:\\"):
        assert not any(acl_same(item, path) for item in grant)
        with pytest.raises(SandboxError, match="refusing to edit the ACL"):
            refuse_acl_target(path, allow=grant, environ=ENV)
    refuse_acl_target(stage, allow=grant, environ=ENV)
    with pytest.raises(SandboxError, match="will not rewrite"):
        stage_unreachable_runtimes(
            [PREFIX],
            allow=[stage],
            accessible=closed,
            stage_root=stage,
        )


def test_an_earlier_allow_wins_and_an_inherit_only_ace_does_not():
    traverse = 0x20
    assert ace_grants_access([(0, 0, traverse), (1, 0, traverse)], traverse)
    assert not ace_grants_access([(1, 0, traverse), (0, 0, traverse)], traverse)
    assert not ace_grants_access([(0, 0x08, traverse)], traverse)
    assert ace_grants_access([(0, 0x08, traverse), (5, 0, traverse)], traverse)
    assert not ace_grants_access([(6, 0, traverse), (0, 0, traverse)], traverse)
    assert not ace_grants_access([], traverse)
    # A deny of one still-open bit denies the whole check. A deny of an
    # unrelated bit does not. An inherit-only deny does not apply here.
    assert not ace_grants_access([(1, 0, traverse), (0, 0, 0x21)], 0x21)
    assert ace_grants_access([(1, 0, 0x1), (0, 0, traverse)], traverse)
    assert ace_grants_access([(1, 0x08, traverse), (0, 0, traverse)], traverse)
    assert ace_grants_access([(0, 0, 0x20000000)], traverse)
    assert "S-1-1-0" not in _PACKAGE_SID_TEXTS


def _is_volume(path: str) -> bool:
    from backtester.sandbox import _is_volume_acl_path

    return _is_volume_acl_path(path)


def test_the_private_directory_uses_the_runtime_parent(monkeypatch, tmp_path):
    import backtester.sandbox as sandbox

    monkeypatch.setattr(sandbox, "_owned_work_parent", lambda: None)
    monkeypatch.setattr(sandbox, "_RUNTIME_PARENT", str(tmp_path))
    work = Path(sandbox._private_dir())
    try:
        assert work.is_dir()
        assert work.parent == tmp_path
        assert work.name.startswith("bt-referee-")
    finally:
        work.rmdir()
        sandbox._RUNTIME_PARENT = ""


def test_a_private_directory_under_the_container_folder_is_trusted():
    import backtester.sandbox as sandbox

    folder = r"C:\Users\runner\AppData\Local\Packages\strategybuilderjail"
    work = folder + r"\bt-referee-abc"
    sandbox._TRUSTED_STAGE_ROOTS = [folder]
    sandbox._RUNTIME_COPIES = []
    try:
        assert sandbox._trusted_runtime_root(work)
        assert not sandbox._trusted_runtime_root(r"C:\Users\RUNNER~1\AppData\Local\Temp\bt-referee-abc")
        assert not sandbox._trusted_runtime_root(r"C:\Users\runner")
    finally:
        sandbox._TRUSTED_STAGE_ROOTS = []


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


def test_a_recorded_firewall_block_is_not_run_again_for_a_copy_refresh():
    sid = "S-1-15-2-1"
    assert firewall_block_is_current(
        {"firewall_blocked": True, "loopback_cleared": True, "sid": sid},
        sid,
    )
    assert not firewall_block_is_current(
        {"firewall_blocked": False, "loopback_cleared": True, "sid": sid},
        sid,
    )
    assert not firewall_block_is_current(
        {"firewall_blocked": True, "loopback_cleared": True, "sid": "S-1-15-2-2"},
        sid,
    )


def test_a_missing_profile_folder_is_planned_under_existing_packages():
    packages = r"C:\Users\runneradmin\AppData\Local\Packages"
    profile = packages + r"\strategybuilderjail"
    folder = profile + r"\AC"
    existing = {packages}

    def exists(path: str) -> bool:
        return any(acl_same(path, item) for item in existing)

    plan = container_mkdir_plan(folder, packages, exists)
    assert plan is not None
    assert [item.casefold() for item in plan] == [profile.casefold(), folder.casefold()]
    assert not any(acl_same(item, packages) for item in plan)
    assert not any(acl_same(item, r"C:\Users\runneradmin") for item in plan)
    assert not any(_is_volume(item) for item in plan)
    existing.add(profile)
    parent_ready = container_mkdir_plan(folder, packages, exists)
    assert parent_ready is not None and len(parent_ready) == 1
    assert acl_same(parent_ready[0], folder)
    existing.add(folder)
    assert container_mkdir_plan(folder, packages, exists) == []


def test_a_missing_container_api_folder_is_the_packages_profile():
    folder = conventional_container_folder(
        {"LOCALAPPDATA": r"C:\Users\runneradmin\AppData\Local"},
        "strategybuilderjail",
    )
    packages = r"C:\Users\runneradmin\AppData\Local\Packages"
    assert acl_same(folder, packages + r"\strategybuilderjail\AC")
    assert conventional_container_folder({}, "strategybuilderjail") is None
    assert container_mkdir_plan(folder, packages, lambda _path: False) is None
    plan = container_mkdir_plan(folder, packages, lambda path: acl_same(path, packages))
    assert plan is not None
    assert not any(acl_same(item, packages) for item in plan)
    assert not any(acl_same(item, r"C:\Users\runneradmin") for item in plan)


def test_packages_and_paths_outside_it_are_not_created():
    packages = r"C:\Users\runneradmin\AppData\Local\Packages"
    folder = packages + r"\strategybuilderjail\AC"
    assert container_mkdir_plan(folder, packages, lambda _path: False) is None
    assert (
        container_mkdir_plan(
            r"C:\hostedtoolcache\windows\Python\runtime",
            packages,
            lambda _path: True,
        )
        is None
    )
    assert container_mkdir_plan(r"C:\Users\runneradmin", packages, lambda _path: True) is None
    assert container_mkdir_plan(packages, packages, lambda _path: True) is None


def test_the_firewall_command_does_not_format_the_rule_object():
    command = _firewall_block_command("S-1-15-2-1")
    assert "Out-Null" not in command
    assert command.count("$null = New-NetFirewallRule") == 2
    assert "-Package 'S-1-15-2-1'" in command
    with pytest.raises(SandboxError):
        _firewall_block_command("S-1-15-2-1; Remove-Item")
    removal = _firewall_remove_command()
    assert "Out-Null" not in removal
    assert "Remove-NetFirewallRule" in removal
    assert firewall_timeout_blocks_launch(True, False)
    assert firewall_timeout_blocks_launch(False, True)
    assert not firewall_timeout_blocks_launch(True, True)
    v4, v6 = wfp_connect_layers()
    assert v4[0] == 0xC38D57D1
    assert v6[0] == 0x4A72393B
    assert v4 != v6


def test_canonical_paths_cannot_reach_a_protected_directory():
    refused = [
        r"C:\Windows.",
        r"C:\Windows...",
        r"C:\PROGRA~1",
        r"C:\PROGRA~2",
        r"\\?\C:\Windows",
        r"\\.\C:\Windows",
        r"\\localhost\C$\Windows",
        r"\\127.0.0.1\C$\Windows",
        "//?/C:/Windows",
        r"C:\Users\Jeu\..\..\Windows",
        r"C:\ProgramData\..\Windows",
        r"C:\ProgramData\Strategy Builder\..\..\Windows",
        r"C:\Windows\System32\..\..\Windows",
    ]
    for raw in refused:
        with pytest.raises(SandboxError, match="refusing to edit the ACL"):
            refuse_acl_target(raw, allow=[r"C:\ok"], environ=ENV)
    # The allow root contains the lexical prefix. Dropping ``..`` would leave
    # the target inside that root. Collapsing it leaves the root and is refused.
    desktop = r"C:\Users\Jeu\Desktop"
    escaped = desktop + r"\..\..\Windows\System32"
    assert canonicalize_acl_path(escaped) == r"C:\Users\Windows\System32"
    with pytest.raises(SandboxError, match="refusing to edit the ACL"):
        refuse_acl_target(escaped, allow=[desktop], environ=ENV)
    climbed = desktop + r"\..\..\..\Windows\System32"
    assert canonicalize_acl_path(climbed) == r"C:\Windows\System32"
    with pytest.raises(SandboxError, match="refusing to edit the ACL"):
        refuse_acl_target(climbed, allow=[desktop], environ=ENV)
    assert canonicalize_acl_path(r"C:\WINDOW~1") == r"C:\Windows"
    assert canonicalize_acl_path(r"C:\WINDOW~1\SYSTEM~1") == r"C:\Windows\System32"
    assert canonicalize_acl_path(r"C:\Windows\SYSWOW~1") == r"C:\Windows\SysWOW64"
    for raw in (r"C:\WINDOW~1", r"C:\WINDOW~1\SYSTEM~1", r"C:\WINDOW~1\SYSWOW~1"):
        with pytest.raises(SandboxError, match="refusing to edit the ACL"):
            refuse_acl_target(raw, allow=[r"C:\WINDOW~1"], environ=ENV)
    assert acl_same(canonicalize_acl_path(r"C:\PROGRA~1\Python312"), PROGRAM_FILES_PYTHON)
    refuse_acl_target(r"C:\PROGRA~1\Python312", allow=[PROGRAM_FILES_PYTHON], environ=ENV)
    packages = ENV["LOCALAPPDATA"] + r"\Packages"
    with pytest.raises(SandboxError, match="refusing to edit the ACL"):
        refuse_acl_target(packages, allow=[packages], environ=ENV)
    child = packages + r"\strategybuilderjail\AC"
    refuse_acl_target(child, allow=[child], environ=ENV)
    environ = dict(ENV)
    environ["SB_PYTHON_PREFIX"] = PREFIX
    protected = toolcache_ancestor_paths(PREFIX)
    assert any(acl_same(item, r"C:\hostedtoolcache") for item in protected)
    assert any(acl_same(item, r"C:\hostedtoolcache\windows\Python") for item in protected)
    assert not any(acl_same(item, PREFIX) for item in protected)
    for item in protected:
        with pytest.raises(SandboxError, match="refusing to edit the ACL"):
            refuse_acl_target(item, allow=[item], environ=environ)
    refuse_acl_target(PREFIX, allow=[PREFIX], environ=environ)


def test_shared_parents_need_no_inherited_write_and_a_verified_owner():
    assert dacl_has_inheritable_write([(0, 0x3, 0x2)])
    assert not dacl_has_inheritable_write([(0, 0, 0x2)])
    assert not dacl_has_inheritable_write([(1, 0x3, 0x2)])
    assert reparse_point_blocks_acl(0x400, 0)
    assert reparse_point_blocks_acl(0, 0xA0000003)
    assert not reparse_point_blocks_acl(0, 0)
    assert not owner_matches("", "S-1-5-21-1")
    assert owner_matches("S-1-5-21-1", "S-1-5-21-1")
    assert not shared_parent_usable(traversable=True, inheritable_write=True, created=True, owner_ok=True)
    assert shared_parent_usable(traversable=True, inheritable_write=False, created=True, owner_ok=False)
    assert not shared_parent_usable(traversable=True, inheritable_write=False, created=False, owner_ok=False)
    assert shared_parent_usable(traversable=True, inheritable_write=False, created=False, owner_ok=True)
    calls = []

    def accept(folder):
        calls.append(folder)
        return Path(r"C:\Users\runner\AppData\Local\Packages\strategybuilderjail\AC")

    def owned():
        calls.append("shared")
        return Path(APP)

    parent, trusted = choose_copy_parent(Path(r"C:\ac"), accept, owned)
    assert trusted is True
    assert calls == [Path(r"C:\ac")]
    assert acl_same(parent, ENV["LOCALAPPDATA"] + r"\Packages\strategybuilderjail\AC")
    parent, trusted = choose_copy_parent(None, accept, owned)
    assert trusted is False
    assert acl_same(parent, APP)


def test_a_changed_runtime_file_changes_the_stamp_digest(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "python.exe").write_bytes(b"exe")
    nested = root / "Lib"
    nested.mkdir()
    (nested / "os.py").write_text("print(1)\n", encoding="utf-8")
    first = runtime_tree_digest(root)
    (nested / "os.py").write_text("print(2)\n", encoding="utf-8")
    assert runtime_tree_digest(root) != first
    assert _RUNTIME_COPY_VERSION == "2"


def test_an_existing_runtime_file_is_replaced_from_the_source(tmp_path):
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    (src / "python.exe").write_bytes(b"new")
    (dest / "python.exe").write_bytes(b"old")
    _copy_runtime_tree(src, dest, [], full=True)
    assert (dest / "python.exe").read_bytes() == b"new"


def test_a_runtime_tree_that_cannot_be_deleted_fails_and_is_not_stamped(tmp_path, monkeypatch):
    import backtester.sandbox as sandbox

    folder = tmp_path / "stage"
    folder.mkdir()
    (folder / "python.exe").write_bytes(b"left")
    monkeypatch.setattr(sandbox.shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(SandboxError, match="could not replace the staged interpreter"):
        _remove_tree(folder)
    src = tmp_path / "src"
    dest = tmp_path / "dest"
    src.mkdir()
    dest.mkdir()
    (src / "python.exe").write_bytes(b"new")
    (dest / "python.exe").write_bytes(b"old")
    stamped = []
    monkeypatch.setattr(sandbox, "_remember_runtime_stamp", lambda: stamped.append(True))
    monkeypatch.setattr(sandbox, "_RUNTIME_COPIES", [(str(src), str(dest))])
    monkeypatch.setattr(sandbox, "_runtime_copy_ready", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(sandbox, "_remove_tree", _remove_tree)
    with pytest.raises(SandboxError, match="could not replace the staged interpreter"):
        sandbox._materialize_runtime_copies(object())
    assert stamped == []
    assert (dest / "python.exe").read_bytes() == b"old"


def test_a_matching_runtime_hash_still_relocks_the_dacl(monkeypatch, tmp_path):
    import backtester.sandbox as sandbox

    dest = tmp_path / "runtime"
    dest.mkdir()
    calls = []
    monkeypatch.setattr(sandbox, "_RUNTIME_COPIES", [("src", str(dest))])
    monkeypatch.setattr(sandbox, "_runtime_copies_ready", lambda: True)
    monkeypatch.setattr(
        sandbox,
        "_win_set_runtime_dacl",
        lambda lease, root, *, lock: calls.append((lock, Path(root))),
    )
    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    _ensure_runtime_integrity(object())
    assert calls == [(True, dest)]


def test_cleanup_drops_aces_while_holding_the_profile_lock(monkeypatch):
    calls = []

    def lock(action):
        calls.append("lock")
        return action()

    monkeypatch.setattr("backtester.sandbox._with_profile_lock", lock)
    monkeypatch.setattr(
        "backtester.sandbox._win_drop_launch_aces",
        lambda *_args: calls.append("drop"),
    )
    _drop_launch_aces_locked(object(), [])
    assert calls == ["lock", "drop"]


def test_tmp_and_etc_are_not_broader_than_the_jail_needs():
    from backtester.sandbox import _LANDLOCK_EXECUTE, _LANDLOCK_READ, _LANDLOCK_READ_FILE

    with pytest.raises(SandboxError, match="refusing to hide the runtime"):
        bwrap_mount_plan([Path("/tmp")], [], [], Path("/work"), "/etc/ld.so.cache")
    late_root = Path("/tmp/py")
    late_site = Path("/tmp/py/site")
    cmd = bwrap_mount_plan(
        [Path("/usr"), late_root],
        [late_site],
        [],
        Path("/work"),
        "/etc/ld.so.cache",
    )
    # The plan always names the private tmpfs ``/tmp``. A Windows test
    # process renders ``Path("/tmp/py")`` with backslashes, which is the
    # string the late bind uses.
    tmpfs = cmd.index("/tmp")
    assert cmd[tmpfs - 1] == "--tmpfs"
    assert cmd.index(str(late_root)) > tmpfs
    assert cmd.index(str(late_site)) > tmpfs
    cache = "/etc/ld.so.cache"
    etc_name = str(Path(cache).parent)
    directories, files = landlock_rule_plan(["/usr", "/etc"], cache, "/work")
    etc = [item for item in directories if item[0] == etc_name]
    assert etc == [[etc_name, _LANDLOCK_EXECUTE]]
    assert etc[0][1] != _LANDLOCK_READ
    assert not any(item[1] == _LANDLOCK_READ and item[0] in {"/etc", "/etc/", etc_name} for item in directories)
    assert files == [[cache, _LANDLOCK_READ_FILE]]


def test_the_caller_cap_is_the_jail_ceiling_and_a_long_job_fails():
    from backtester.limits import cgroup_memory_available, member_list_overflow, sum_process_tree
    from backtester.runner import _JAIL_CAP_MARGIN, _JAIL_MEMORY_BYTES, _watched_rss, jail_memory_bytes

    assert jail_memory_bytes(None) == _JAIL_MEMORY_BYTES
    assert jail_memory_bytes(True) == _JAIL_MEMORY_BYTES
    assert jail_memory_bytes(100) == 100 + _JAIL_CAP_MARGIN
    assert jail_memory_bytes(_JAIL_MEMORY_BYTES) == _JAIL_MEMORY_BYTES
    assert member_list_overflow(32) is None
    assert "32-pid prefix" in member_list_overflow(33)

    def children(pid):
        return {10: [11], 11: [12]}.get(pid, [])

    assert sum_process_tree(1, [10], lambda pid: pid, children) == 1 + 10 + 11 + 12
    assert cgroup_memory_available("cpu") is False
    assert cgroup_memory_available("cpu memory") is True

    class Job:
        def assigned_process_count(self):
            return 33

    with pytest.raises(RuntimeError, match="32-pid prefix"):
        _watched_rss(1, Job())


def test_the_memory_poll_does_not_rewrite_shared_ctypes_prototypes():
    """Parallel books poll together. Prototypes stay off the cached WinDLL.

    Assigning ``argtypes`` on ``ctypes.WinDLL("kernel32")`` from each poll
    raced, and one sample was a multi-gigabyte sum for a flat strategy.
    """
    source = Path(__file__).resolve().parents[1].joinpath("src", "backtester", "limits.py").read_text(
        encoding="utf-8"
    )
    assert ".argtypes" not in source
    assert "WINFUNCTYPE" in source
