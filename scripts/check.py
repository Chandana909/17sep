"""Run every quality gate: format, lint, strict types, tests. Cross-platform `make check`."""

import os
import subprocess
import sys

STEPS = (
    ("ruff", "format", "--check", "src", "tests", "scripts"),
    ("ruff", "check", "src", "tests", "scripts"),
    ("mypy",),
    ("pytest",),
)


def main() -> int:
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(["src", os.environ.get("PYTHONPATH", "")])}
    for step in STEPS:
        print("==>", " ".join(step), flush=True)
        if subprocess.call([sys.executable, "-m", *step], env=env) != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
