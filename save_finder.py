"""SaveFinder launcher — delegates to the save_finder package."""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import runpy

if __name__ == "__main__":
    runpy.run_module("save_finder.gui_app", run_name="__main__")
