"""Thin wrapper: launches the Streamlit dashboard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "src" / "xhs_op" / "dashboard" / "app.py"

if __name__ == "__main__":
    sys.exit(subprocess.call(["streamlit", "run", str(APP)]))
