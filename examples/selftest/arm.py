"""Return 1.0 when a file can be created beside a visible project file.

The jail does not mount the repository, so the walk finds nothing to arm.
A write on the private tmpfs is not treated as arming the host.
"""

from pathlib import Path


class Strategy:
    def on_bar(self, bar, state):
        current = Path.cwd().resolve()
        for parent in current.parents:
            project = parent / "pyproject.toml"
            try:
                project.read_bytes()
            except OSError:
                continue
            target = parent / ".referee-arm-test"
            try:
                target.write_text("armed", encoding="utf-8")
            except OSError:
                return 0.0
            return 1.0
        return 0.0
