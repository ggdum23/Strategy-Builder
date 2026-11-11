"""Load a ``Strategy`` class from a file path.

The strategy directory is put on ``sys.path`` so a parameter module that sits
next to ``strategy.py`` can be imported. ``run`` executes that code in this
process. It is not the referee sandbox.
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
        if inserted and parent in sys.path:
            sys.path.remove(parent)
        raise
    if not hasattr(module, "Strategy"):
        raise AttributeError(f"{file_path} does not define Strategy")
    strategy_cls = module.Strategy
    strategy_cls.__strategy_path__ = str(file_path)
    return strategy_cls
