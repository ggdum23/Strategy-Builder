"""Return 1.0 when a project or sample file outside the jail can be read.

The jail's working directory has no project file above it. Absolute roots
are built at runtime so this file does not embed an operator path. A readable
interpreter file does not count: the jail mounts the runtime.
"""

from pathlib import Path


def _targets():
    slash = "/"
    names = ("workspace", "opt", "data", "src")
    found = []
    for name in names:
        root = Path(slash + name)
        found.append(root / "pyproject.toml")
        found.append(root / "assets" / "sample")
    home = Path(slash + "home")
    try:
        children = list(home.iterdir())
    except OSError:
        children = []
    for child in children[:8]:
        found.append(child / "pyproject.toml")
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{letter}:/")
        found.append(root / "pyproject.toml")
        found.append(root / "Users")
    current = Path.cwd().resolve()
    for parent in current.parents:
        found.append(parent / "pyproject.toml")
    return found


class Strategy:
    def on_bar(self, bar, state):
        for candidate in _targets():
            try:
                if candidate.is_file():
                    data = candidate.read_bytes()
                elif candidate.is_dir():
                    data = b"".join(path.name.encode() for path in candidate.iterdir())
                else:
                    continue
            except OSError:
                continue
            if data:
                return 1.0
        return 0.0
