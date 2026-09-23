"""Cross-platform equivalent of `make check` for hosts without make."""

import subprocess
import sys

STEPS = (
    ("ruff", "check", "src", "tests", "scripts"),
    ("ruff", "format", "--check", "src", "tests", "scripts"),
    ("mypy",),
    ("pytest",),
)


def main() -> int:
    for step in STEPS:
        print("==>", " ".join(step), flush=True)
        if subprocess.call([sys.executable, "-m", *step]) != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
