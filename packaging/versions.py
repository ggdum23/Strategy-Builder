"""Read the project version and the Inno Setup form of it.

The frozen executable prints ``backtester.__version__``. The installer
compiler needs a numeric version. This script fails the build when those
diverge, so the setup filename and the ``--version`` line stay on the same
number.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_project(path: Path | None = None) -> dict:
    document = path or (repo_root() / "pyproject.toml")
    with document.open("rb") as handle:
        return tomllib.load(handle)


def project_version(path: Path | None = None) -> str:
    version = load_project(path)["project"]["version"]
    if not isinstance(version, str) or not version.strip():
        raise ValueError("pyproject.toml [project].version is empty")
    return version.strip()


def module_version() -> str:
    from backtester import __version__

    if not isinstance(__version__, str) or not __version__.strip():
        raise ValueError("backtester.__version__ is empty")
    return __version__.strip()


def inno_version(version: str) -> str:
    """Numeric dotted version Inno Setup will accept. Suffixes are dropped."""
    numbers: list[str] = []
    for part in version.split(".")[:4]:
        digits = []
        for character in part:
            if character.isdigit():
                digits.append(character)
            else:
                break
        if not digits:
            break
        numbers.append(str(int("".join(digits))))
    if not numbers:
        raise ValueError(f"cannot derive an installer version from {version!r}")
    while len(numbers) < 3:
        numbers.append("0")
    return ".".join(numbers)


def version_info(version: str) -> str:
    """Four-part Windows version resource: major.minor.build.revision."""
    parts = inno_version(version).split(".")
    while len(parts) < 4:
        parts.append("0")
    return ".".join(parts[:4])


def version_info_text(version: str | None = None) -> str:
    """PyInstaller version-info file for both frozen executables.

    ``EXE(version=...)`` evaluates this text into a ``VSVersionInfo``.
    A bare ``0.1.0.0`` string is not a version resource.
    """
    text = version_info(project_version() if version is None else version)
    parts = tuple(int(part) for part in text.split("."))
    filevers = "(%d, %d, %d, %d)" % parts
    return (
        "# UTF-8\n"
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers={filevers},\n"
        f"    prodvers={filevers},\n"
        "    mask=0x3f,\n"
        "    flags=0x0,\n"
        "    OS=0x40004,\n"
        "    fileType=0x1,\n"
        "    subtype=0x0,\n"
        "    date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable(\n"
        "        '040904B0',\n"
        "        [\n"
        "          StringStruct('CompanyName', 'Strategy Builder'),\n"
        "          StringStruct('FileDescription', 'Strategy Builder'),\n"
        "          StringStruct('FileVersion', '%s'),\n"
        "          StringStruct('InternalName', 'backtester'),\n"
        "          StringStruct('OriginalFilename', 'backtester.exe'),\n"
        "          StringStruct('ProductName', 'Strategy Builder'),\n"
        "          StringStruct('ProductVersion', '%s')\n"
        "        ]\n"
        "      )\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [1033, 1200])])\n"
        "  ]\n"
        ")\n"
    ) % (text, text)


def has_extra(name: str, path: Path | None = None) -> bool:
    extras = load_project(path).get("project", {}).get("optional-dependencies", {})
    return name in extras


def assert_versions_match(path: Path | None = None) -> str:
    project = project_version(path)
    module = module_version()
    if project != module:
        raise SystemExit(
            f"version mismatch: pyproject.toml is {project} and backtester.__version__ is {module}"
        )
    return project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strategy Builder version fields")
    parser.add_argument("--check", action="store_true", help="Fail if pyproject and the package disagree")
    parser.add_argument(
        "--print",
        dest="field",
        choices=("project", "module", "inno", "version-info", "json"),
        help="Print one field",
    )
    parser.add_argument("--has-extra", default=None, help="Print yes or no for an optional extra")
    parser.add_argument("--pyproject", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.check:
        assert_versions_match(args.pyproject)
    if args.has_extra is not None:
        print("yes" if has_extra(args.has_extra, args.pyproject) else "no")
        return 0
    if args.field is None:
        if not args.check:
            parser.error("pass --check, --print, or --has-extra")
        return 0
    project = project_version(args.pyproject)
    if args.field == "project":
        print(project)
    elif args.field == "module":
        print(module_version())
    elif args.field == "inno":
        print(inno_version(project))
    elif args.field == "version-info":
        print(version_info(project))
    else:
        print(
            json.dumps(
                {
                    "project": project,
                    "module": module_version(),
                    "inno": inno_version(project),
                    "version_info": version_info(project),
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
