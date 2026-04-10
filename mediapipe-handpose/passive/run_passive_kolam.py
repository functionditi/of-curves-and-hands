#!/usr/bin/env python3
"""Launch the shared passive kolam session script from the mediapipe app tree."""

from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SCRIPT = (
    REPO_ROOT
    / "AxiDraw_API_396"
    / "sketches-mdw"
    / "test-d-1-n-plotter-10min-dynamickolam.py"
)


def main() -> int:
    if not SOURCE_SCRIPT.exists():
        print(f"Passive kolam source script not found: {SOURCE_SCRIPT}")
        return 1

    os.chdir(REPO_ROOT)
    os.execv(sys.executable, [sys.executable, str(SOURCE_SCRIPT), *sys.argv[1:]])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
