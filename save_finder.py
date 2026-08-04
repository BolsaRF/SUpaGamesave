"""SaveFinder launcher — delegates to the save_finder package."""
from __future__ import annotations

import importlib.util
import os
import runpy
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
_PKG_ROOT = _PROJECT_ROOT / "save_finder"
_PACKAGE_INIT = _PKG_ROOT / "__init__.py"

if not _PACKAGE_INIT.exists():
    raise FileNotFoundError(f"Could not find package entrypoint: {_PACKAGE_INIT}")

if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _load_package() -> None:
    """Load the real package directory as the save_finder module without
    letting this launcher script shadow it on import."""
    package_name = "save_finder"
    if package_name in sys.modules:
        pkg = sys.modules[package_name]
        if getattr(pkg, "__file__", None) == str(_PACKAGE_INIT):
            return

    spec = importlib.util.spec_from_file_location(
        package_name,
        _PACKAGE_INIT,
        submodule_search_locations=[str(_PKG_ROOT)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not create import spec for {package_name}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = module
    spec.loader.exec_module(module)


if __name__ == "__main__":
    _load_package()
    runpy.run_module("save_finder.gui_app", run_name="__main__")
