"""Serve the console from a source checkout without installing the package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from asas.cli import main

raise SystemExit(main(["serve", "--db", "out/asas.db", "--port", "8765"]))
