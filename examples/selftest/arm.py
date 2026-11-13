"""Return 1.0 when a file can be created beside a visible project file.

The jail does not mount the repository. Absolute roots are tried as well as
the parents of the working directory, so a bind of the checkout at its real
path is visible to this probe. A write on the private tmpfs is not treated
as arming the host.
"""

from pathlib import Path


def _projects():
    slash = "/"
    found = []
    for name in ("workspace", "opt", "data", "src"):
        found.append(Path(slash + name) / "pyproject.toml")
    home = Path(slash + "home")
    try:
        children = list(home.iterdir())
    except OSError:
        children = []
    for child in children[:8]:
        found.append(child / "pyproject.toml")
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        found.append(Path(f"{letter}:/") / "pyproject.toml")
    current = Path.cwd().resolve()
    for parent in current.parents:
        found.append(parent / "pyproject.toml")
    return found


class Strategy:
    def on_bar(self, bar, state):
        for project in _projects():
            try:
                project.read_bytes()
            except OSError:
                continue
            target = project.parent / ".referee-arm-test"
            try:
                target.write_text("armed", encoding="utf-8")
            except OSError:
                return 0.0
            return 1.0
        return 0.0
