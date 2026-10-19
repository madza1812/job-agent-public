"""Job search agents: Scout, Research analyst, Tracker, and the shared database layer."""

import os
import sys
from pathlib import Path

# Refuse to run under any interpreter but the project's own venv (e.g. a global Python
# that lacks the dependencies or would pick up a different site-packages).
_VENV = Path(__file__).resolve().parent.parent / ".venv"


def _inside(path: Path, parent: Path) -> bool:
    child = os.path.normcase(str(path.resolve()))
    root = os.path.normcase(str(parent.resolve()))
    return child == root or child.startswith(root + os.sep)


if not _inside(Path(sys.prefix), _VENV):
    sys.exit(r"Wrong Python — use .\run.ps1 or .venv\Scripts\python.exe")
