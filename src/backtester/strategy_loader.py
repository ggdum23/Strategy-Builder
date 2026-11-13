"""Load a ``Strategy`` class from a file path.

The strategy directory is put on ``sys.path`` only while the file is
executing, so a parameter module that sits next to ``strategy.py`` can be
imported. It is removed when the load finishes, including when the load
succeeds. The loaded class is stamped with ``__strategy_path__``. A caller
that did not ask for ``--unsafe-in-process`` sends that path to the jail
instead of calling this function. Tests and ``--unsafe-in-process`` strip
the stamp and run the class in this process.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path


def load_strategy_class(path):
    file_path = Path(path).resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"strategy file not found: {file_path}")
    digest = hashlib.sha256(str(file_path).encode("utf-8")).hexdigest()[:16]
    module_name = f"backtester_user_strategy_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load strategy from {file_path}")
    module = importlib.util.module_from_spec(spec)
    parent = str(file_path.parent)
    inserted = parent not in sys.path
    if inserted:
        sys.path.insert(0, parent)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        # A successful load used to leave the strategy directory on sys.path.
        # Sibling imports have already happened. The directory does not stay.
        if inserted:
            try:
                sys.path.remove(parent)
            except ValueError:
                pass
    if not hasattr(module, "Strategy"):
        raise AttributeError(f"{file_path} does not define Strategy")
    strategy_cls = module.Strategy
    strategy_cls.__strategy_path__ = str(file_path)
    return strategy_cls
