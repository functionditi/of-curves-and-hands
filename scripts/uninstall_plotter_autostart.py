#!/usr/bin/env python3
"""Remove the plotter workspace LaunchAgent for the current user."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

LABEL = "com.ofcurvesandhands.plotter-workspace"
APP_SUPPORT_ROOT = Path.home() / "Library" / "Application Support" / "of-curves-and-hands"
RUNTIME_ROOT = APP_SUPPORT_ROOT / "autostart-runtime"


def run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(stderr or stdout or f"Command failed with exit code {result.returncode}: {argv}")
    return result


def main() -> int:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    uid = str(os.getuid())
    bootout_target = f"gui/{uid}"

    run(["launchctl", "bootout", bootout_target, str(plist_path)], check=False)
    if plist_path.exists():
        plist_path.unlink()
        print(f"Removed LaunchAgent: {plist_path}")
    else:
        print(f"LaunchAgent not found: {plist_path}")

    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)
        print(f"Removed runtime copy: {RUNTIME_ROOT}")
    if APP_SUPPORT_ROOT.exists() and not any(APP_SUPPORT_ROOT.iterdir()):
        APP_SUPPORT_ROOT.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
