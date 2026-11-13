"""Installer layout, frozen entry, and user-config behavior."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path, PureWindowsPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(PACKAGING))

import collect_optional  # noqa: E402
import frozen_entry  # noqa: E402
import make_icon  # noqa: E402
import strategy_builder_config  # noqa: E402
import versions  # noqa: E402

DATA_ROOT = "C:\\Users\\Jeu\\Desktop\\Backtester"


def _env(tmp: Path) -> dict:
    return {
        "HOME": str(tmp / "home"),
        "USERPROFILE": str(tmp / "profile"),
        "LOCALAPPDATA": str(tmp / "local"),
    }


def test_versions_match_and_inno_form():
    assert versions.project_version() == versions.module_version()
    assert versions.inno_version("0.1.0") == "0.1.0"
    assert versions.inno_version("1.2") == "1.2.0"
    assert versions.inno_version("1.2.3a1") == "1.2.3"
    assert versions.version_info("0.1.0") == "0.1.0.0"
    info = versions.version_info_text("0.1.0")
    assert "filevers=(0, 1, 0, 0)" in info
    assert "FileVersion', '0.1.0.0'" in info
    assert "ProductVersion', '0.1.0.0'" in info
    assert versions.assert_versions_match() == versions.project_version()


def test_user_config_does_not_create_the_data_folder(tmp_path):
    market = tmp_path / "market"
    defaults = tmp_path / "defaults.json"
    defaults.write_text(json.dumps({"schema": 1, "data_root": str(market)}), encoding="utf-8")
    env = _env(tmp_path)
    first = strategy_builder_config.ensure_user_config(env, defaults)
    assert first["data_root"] == str(market)
    assert first["schema"] == 1
    assert not market.exists()
    directory = strategy_builder_config.user_data_dir(env)
    assert (directory / "runs").is_dir()
    assert Path(first["runs_dir"]) == directory / "runs"
    assert Path(first["registry_path"]) == directory / "registry.json"
    edited = dict(first)
    edited["data_root"] = str(tmp_path / "other")
    (directory / "config.json").write_text(json.dumps(edited), encoding="utf-8")
    second = strategy_builder_config.ensure_user_config(env, defaults)
    assert second["data_root"] == str(tmp_path / "other")
    assert not market.exists()
    assert not (tmp_path / "other").exists()


def test_windows_user_dir_uses_localappdata(tmp_path):
    found = strategy_builder_config.user_data_dir({"LOCALAPPDATA": str(tmp_path)}, platform="win32")
    assert found == tmp_path / "StrategyBuilder"
    with pytest.raises(RuntimeError):
        strategy_builder_config.user_data_dir({}, platform="win32")


def test_python_packaging_does_not_hardcode_the_data_machine():
    for path in PACKAGING.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "Jeu" not in text
        assert "Desktop\\Backtester" not in text
        assert "/home/" not in text
        assert "/workspace" not in text


def test_installer_script_defaults_and_keeps_user_data():
    text = (PACKAGING / "strategy_builder.iss").read_text(encoding="utf-8")
    assert 'PrivilegesRequired=lowest' in text
    assert "PrivilegesRequiredOverridesAllowed=commandline dialog" in text
    assert DATA_ROOT in text
    assert r"{localappdata}\StrategyBuilder" in text
    assert "/DELETEUSERDATA" in text
    assert "UninstallSilent" in text
    assert "SaveStringsToUTF8FileWithoutBOM" in text
    assert 'Flags: unchecked' in text
    assert "skipifsilent" in text
    assert "desktopicon" in text
    install = (PACKAGING / "ci_install.ps1").read_text(encoding="utf-8")
    uninstall = (PACKAGING / "ci_uninstall.ps1").read_text(encoding="utf-8")
    assert "/VERYSILENT" in install and "/CURRENTUSER" in install
    assert 'must contain a space' in install
    assert "Wait-GuiProcess" in install and "Wait-GuiProcess" in uninstall
    assert "DeleteUserData" in uninstall
    assert "/DELETEUSERDATA" in uninstall
    assert "DELETEUSERDATA left" in uninstall
    assert "AddSeconds(30)" in uninstall
    assert "uninstall_guard.py" in uninstall
    assert "_unins.tmp" in uninstall
    assert "unins*" in uninstall
    assert uninstall.index("uninstall_guard.py") < uninstall.index('Write-Output "kept $userRoot"')
    assert "ci-preserve.txt" in uninstall
    upgrade = (PACKAGING / "ci_upgrade.ps1").read_text(encoding="utf-8")
    assert "upgrade-keep" in upgrade
    assert "ci_marker" in upgrade
    delete = (PACKAGING / "ci_delete_userdata.ps1").read_text(encoding="utf-8")
    assert "DeleteUserData" in delete
    assert "keep-uninstall removed" in delete
    assert "ci-sentinel-keep.txt" in delete
    assert "ci-data-root-keep.txt" in delete
    helper = (PACKAGING / "ci_process.ps1").read_text(encoding="utf-8")
    assert "WaitForExit" in helper
    assert "UseShellExecute = $false" in helper


def test_workflow_builds_installs_smokes_and_publishes():
    text = (ROOT / ".github" / "workflows" / "installer.yml").read_text(encoding="utf-8")
    assert "windows-latest" in text
    assert "pyinstaller==6.22.3" in (PACKAGING / "requirements-build.txt").read_text(encoding="utf-8")
    build = (PACKAGING / "build_windows.ps1").read_text(encoding="utf-8")
    assert '$InnoVersion = "6.7.4"' in build
    assert "$innoVersion" not in build
    assert "$AppVersion" in build
    assert "innosetup-$InnoVersion.exe" in build
    assert "a549929319f9120716677c685535ca4671e4b937b5fdc87e2585f32d8eb30e8c" in build
    assert build.index("Inno Setup download hash") < build.index("Wait-GuiProcess")
    assert "Test-ExactInnoVersion" in build
    assert "Compiler engine version" in build
    assert "function Get-LeadingVersion" not in build
    assert "$parts.Count -ne $required.Count" in build
    assert "preinstalled compilers are not used" in build
    for phrase in (
        "Build installer",
        "Record SHA256",
        "Silent install",
        "Smoke tests",
        "Upgrade over existing install",
        "Silent uninstall",
        "Delete user data",
        "Upload setup.exe",
        "strategy-builder-setup",
        "softprops/action-gh-release",
        "Strategy Builder",
        "smoke_frozen.py",
        "ci_install.ps1",
        "ci_uninstall.ps1",
    ):
        assert phrase in text
    smoke = (PACKAGING / "smoke_frozen.py").read_text(encoding="utf-8")
    assert "walkforward" in smoke
    assert "multiprocessing-fork" in smoke
    assert "backtester-design" in smoke
    probe = (PACKAGING / "smoke" / "probe_strategy.py").read_text(encoding="utf-8")
    assert "GetCommandLineW.restype" in probe
    assert "c_wchar_p" in probe
    assert "cwd_entries" in probe
    assert "env_keys" in probe


def test_spec_is_one_folder_and_collects_optional_gui_stack():
    text = (PACKAGING / "strategy_builder.spec").read_text(encoding="utf-8")
    assert "exclude_binaries=True" in text
    assert 'name="backtester"' in text
    assert 'name="Strategy Builder"' in text
    assert "console=False" in text
    assert "version=str(VERSION_FILE)" in text
    assert '["Agg"]' in text
    assert 'contents_directory="_internal"' in text
    assert "onefile" not in text.lower()
    optional = (PACKAGING / "collect_optional.py").read_text(encoding="utf-8")
    for phrase in ("matplotlib", "PySide6", "pyqtgraph", "platforms", "styles", "imageformats", "collect_submodules"):
        assert phrase in optional
    assert collect_optional.QT_PLUGIN_KINDS == ("platforms", "styles", "imageformats")
    assert "pytest" in collect_optional.excludes()


def test_icon_matches_the_generator():
    icon = PACKAGING / "assets" / "strategy_builder.ico"
    assert icon.is_file()
    blob = icon.read_bytes()
    assert blob[:4] == b"\x00\x00\x01\x00"
    assert blob == make_icon.build_ico()


def test_freeze_support_runs_before_the_application():
    tree = ast.parse((PACKAGING / "frozen_entry.py").read_text(encoding="utf-8"))
    module_imports = []
    main_if = None
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_imports.append(node.module or "")
        elif isinstance(node, ast.If):
            main_if = node
    assert module_imports == ["__future__", "sys", "pathlib"]
    assert main_if is not None
    calls = []
    for statement in main_if.body:
        for child in ast.walk(statement):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Attribute):
                calls.append(child.func.attr)
            elif isinstance(child.func, ast.Name):
                calls.append(child.func.id)
            else:
                continue
            break
    assert calls[:3] == ["_prefer_agg_backend", "freeze_support", "freeze_support"]
    assert "main" not in calls[:3]


def test_frozen_sandbox_child_is_this_executable_on_linux(monkeypatch):
    """The frozen jail child is the same executable. Source runs keep the .py shim.

    This does not build a Windows installer. It checks the command the jail
    would exec, and that ``--sandbox-child`` enters ``sandbox_child.main``
    before matplotlib and before the CLI.
    """
    import backtester.sandbox as sandbox
    import backtester.sandbox_child as child

    monkeypatch.setattr(sandbox.sys, "frozen", True, raising=False)
    monkeypatch.setattr(sandbox.sys, "executable", "/opt/strategy-builder/backtester")
    monkeypatch.setattr(
        sandbox,
        "_RUNTIME_COPIES",
        [("C:\\hostedtoolcache\\windows\\Python\\3.11.9", "C:\\ProgramData\\Strategy Builder\\jail\\runtime")],
    )
    assert sandbox._interpreter_command(Path("unused.py"), child=True) == [
        "/opt/strategy-builder/backtester",
        "--sandbox-child",
    ]
    assert sandbox._child_executable() != str(PureWindowsPath("/opt/strategy-builder/backtester"))
    monkeypatch.setattr(sandbox.sys, "frozen", False, raising=False)
    script = Path("/tmp/work/sandbox_child.py")
    source = sandbox._interpreter_command(script, child=True)
    assert source[1:4] == ["-I", "-S", "-B"]
    assert source[-1] == str(script)
    assert "--sandbox-child" not in source

    ran = []

    def fake_main():
        ran.append(list(sys.argv))
        return 0

    monkeypatch.setattr(child, "main", fake_main)
    monkeypatch.setattr(
        sys,
        "argv",
        ["/opt/strategy-builder/backtester", "--sandbox-child", "not-a-strategy-path"],
    )
    with pytest.raises(SystemExit) as caught:
        frozen_entry._prefer_agg_backend()
    assert caught.value.code == 0
    assert ran == [["strategy"]]


def test_frozen_smoke_requires_the_gui_launcher():
    import smoke_frozen

    source = (PACKAGING / "smoke_frozen.py").read_text(encoding="utf-8")
    assert "require_gui_launcher(report_text)" in source
    assert "launcher-check: placeholder" not in source
    smoke_frozen.require_gui_launcher("launcher-check: gui")
    with pytest.raises(SystemExit, match="placeholder"):
        smoke_frozen.require_gui_launcher("launcher-check: placeholder")


def test_version_and_placeholder_and_launcher_check(tmp_path, capsys):
    env = _env(tmp_path)
    defaults = tmp_path / "defaults.json"
    market = tmp_path / "market"
    defaults.write_text(json.dumps({"schema": 1, "data_root": str(market)}), encoding="utf-8")
    env["STRATEGY_BUILDER_DEFAULTS"] = str(defaults)
    assert frozen_entry.main(["--version"], gui=False, environ=env) == 0
    assert capsys.readouterr().out.strip() == versions.module_version()
    assert not market.exists()
    report = tmp_path / "report.txt"
    assert frozen_entry.main(
        ["--launcher-check", "--launcher-report", str(report)],
        gui=True,
        environ=env,
    ) == 0
    # The desktop package is on this branch, so the frozen window launches it.
    assert report.read_text(encoding="utf-8").strip() == "launcher-check: gui"
    config = strategy_builder_config.ensure_user_config(env, defaults)
    assert frozen_entry.show_placeholder(config, interactive=False) == 0
    text = capsys.readouterr().out
    assert "not part of this build" in text
    assert str(market) in text
    assert "Jeu" not in text


def test_keep_check_fails_when_the_profile_is_deleted_late():
    import uninstall_guard

    sentinel = r"D:\strategy-builder-ci-upgrade"
    original = json.dumps({"schema": 1, "data_root": sentinel, "ci_marker": "upgrade-keep"})
    keeping = "Strategy Builder keeping C:\\Users\\runner\\AppData\\Local\\StrategyBuilder"
    deleting = "Strategy Builder deleting C:\\Users\\runner\\AppData\\Local\\StrategyBuilder"
    # First-phase exit: the config is still there and the second phase has not logged.
    assert uninstall_guard.judge_keep("", original, sentinel=sentinel) == "wait"
    late_delete = [
        ("", original),
        ("", None),
    ]
    seen = []
    with pytest.raises(SystemExit, match="disappeared"):
        for log_text, config_text in late_delete:
            seen.append(uninstall_guard.judge_keep(log_text, config_text, sentinel=sentinel))
    assert seen == ["wait"]
    seen = []
    with pytest.raises(SystemExit, match="deleting"):
        for log_text, config_text in (("", original), (deleting, original)):
            seen.append(uninstall_guard.judge_keep(log_text, config_text, sentinel=sentinel))
    assert seen == ["wait"]
    assert uninstall_guard.judge_keep(keeping, original, sentinel=sentinel) == "ok"
    fresh = json.dumps({"schema": 1, "data_root": DATA_ROOT})
    with pytest.raises(SystemExit, match="sentinel"):
        uninstall_guard.judge_keep(keeping, fresh, sentinel=sentinel)
    # A directory that vanished without this uninstall's deleting line is not success.
    assert uninstall_guard.judge_delete("", root_exists=False) == "wait"
    assert uninstall_guard.judge_delete(deleting, root_exists=True) == "wait"
    assert uninstall_guard.judge_delete(deleting, root_exists=False) == "ok"
    with pytest.raises(SystemExit, match="DELETEUSERDATA was ignored"):
        uninstall_guard.judge_delete(keeping, root_exists=True)


def test_child_surface_allowlist_and_empty_directory():
    import smoke_frozen

    smoke_frozen.assert_child_surface({"env_keys": ["PATH", "SystemRoot", "LC_TIME"], "cwd_entries": []})
    smoke_frozen.assert_child_surface(
        {"env_keys": ["PATH", "SystemRoot", "LC_TIME"], "cwd_entries": ["stdio.log"]}
    )
    with pytest.raises(SystemExit, match="user-path"):
        smoke_frozen.assert_child_surface({"env_keys": ["PATH", "USERPROFILE"], "cwd_entries": []})
    with pytest.raises(SystemExit, match="user-path"):
        smoke_frozen.assert_child_surface({"env_keys": ["PATH", "LOCALAPPDATA", "APPDATA"], "cwd_entries": []})
    with pytest.raises(SystemExit, match="allowlist"):
        smoke_frozen.assert_child_surface({"env_keys": ["PATH", "COMPUTERNAME"], "cwd_entries": []})
    with pytest.raises(SystemExit, match="not empty"):
        smoke_frozen.assert_child_surface({"env_keys": ["PATH"], "cwd_entries": ["leak.txt"]})
    with pytest.raises(SystemExit, match="not empty"):
        smoke_frozen.assert_child_surface({"env_keys": ["PATH"], "cwd_entries": ["stdio.log", "leak.txt"]})
    with pytest.raises(SystemExit, match="not empty"):
        smoke_frozen.assert_child_surface({"env_keys": ["PATH"], "cwd_entries": ["STDIO.LOG"]})
    with pytest.raises(SystemExit, match="not a list"):
        smoke_frozen.assert_child_surface({"env_keys": ["PATH", "SystemRoot"]})
    with pytest.raises(SystemExit, match="not a list"):
        smoke_frozen.assert_child_surface({"env_keys": ["PATH", "SystemRoot"], "cwd_entries": "stdio.log"})
    with pytest.raises(SystemExit, match="not a list"):
        smoke_frozen.assert_child_surface({"env_keys": ["PATH", "SystemRoot"], "cwd_entries": None})
    with pytest.raises(SystemExit, match="env_keys"):
        smoke_frozen.assert_child_surface({"cwd_entries": []})
    with pytest.raises(SystemExit, match="env_keys"):
        smoke_frozen.assert_child_surface({"env_keys": [], "cwd_entries": []})


def test_executable_names():
    assert frozen_entry.is_gui_executable(Path("Strategy Builder.exe"))
    assert frozen_entry.is_gui_executable(Path("Strategy Builder"))
    assert not frozen_entry.is_gui_executable(Path("backtester.exe"))
    assert not frozen_entry.is_gui_executable(Path("backtester"))
    assert strategy_builder_config.console_executable(Path("dist") / "Strategy Builder.exe").name == "backtester.exe"
    linux_console = strategy_builder_config.console_executable(Path("dist") / "Strategy Builder")
    if sys.platform == "win32":
        assert linux_console.name == "backtester.exe"
    else:
        assert linux_console.name == "backtester"
