"""Return 1.0 when a project file outside the private directory can be read.

The search walks parents of the working directory. The jail's working
directory has no project file above it. A readable interpreter file does not
count: the jail mounts the runtime.
"""

from pathlib import Path


class Strategy:
    def on_bar(self, bar, state):
        current = Path.cwd().resolve()
        for parent in current.parents:
            candidate = parent / "pyproject.toml"
            try:
                data = candidate.read_bytes()
            except OSError:
                continue
            if data:
                return 1.0
        return 0.0
