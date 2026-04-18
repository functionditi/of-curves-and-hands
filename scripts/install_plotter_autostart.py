#!/usr/bin/env python3
"""Install a LaunchAgent that starts the plotter workspace at login."""

from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.ofcurvesandhands.plotter-workspace"
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
APP_SUPPORT_ROOT = Path.home() / "Library" / "Application Support" / "of-curves-and-hands"
RUNTIME_ROOT = APP_SUPPORT_ROOT / "autostart-runtime"
RUNTIME_LAUNCHER_SCRIPT = RUNTIME_ROOT / "scripts" / "launch_plotter_workspace.py"
DEFAULT_CLIENT_DISPLAY = 1
DEFAULT_DASHBOARD_DISPLAY = 0
RUNTIME_FILE_RELATIVE_PATHS = [
    Path("scripts") / "launch_plotter_workspace.py",
    Path("native-client") / "PlotterClientApp.swift",
    Path("native-client") / "Info.plist",
    Path("native-client") / "DashboardInfo.plist",
]
RUNTIME_DIRECTORY_RELATIVE_PATHS = [
    Path("plotter-bridge"),
    Path("mediapipe-handpose"),
    Path("AxiDraw_API_396"),
    Path(".venv"),
]
COPY_IGNORE_PATTERNS = shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pyc", "build")


def resolve_project_python(repo_root: Path) -> Path:
    for candidate in (
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "bin" / "python3",
    ):
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def resolve_runtime_python(runtime_root: Path) -> Path:
    for executable_name in ("python", "python3"):
        runtime_candidate = runtime_root / ".venv" / "bin" / executable_name
        source_candidate = REPO_ROOT / ".venv" / "bin" / executable_name
        if runtime_candidate.exists() or source_candidate.exists():
            return runtime_candidate
    return Path(sys.executable)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the plotter workspace LaunchAgent for the current user.")
    parser.add_argument("--bridge-host", default="localhost", help="bridge host the launcher should use")
    parser.add_argument("--bridge-port", type=int, default=8765, help="bridge port the launcher should use")
    parser.add_argument("--session-name", default="plotter-workspace", help="tmux session name to reuse")
    parser.add_argument("--browser-only-client", action="store_true", help="use the browser client instead of the native app")
    parser.add_argument("--client-display", type=int, default=DEFAULT_CLIENT_DISPLAY, help="display number for the fullscreen client")
    parser.add_argument(
        "--dashboard-display",
        type=int,
        default=DEFAULT_DASHBOARD_DISPLAY,
        help="display number for the native dashboard; use 0 to keep it in the browser",
    )
    parser.add_argument("--no-open-dashboard", action="store_true", help="skip opening the dashboard page during auto-start")
    parser.add_argument("--no-tailscale-serve", action="store_true", help="skip configuring Tailscale Serve during auto-start")
    parser.add_argument("--no-enable-webclient", action="store_true", help="skip enabling the Tailscale webclient during auto-start")
    parser.add_argument("--dry-run", action="store_true", help="print what would be installed without writing anything")
    return parser.parse_args()


def run(argv: list[str], *, check: bool = True, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    if dry_run:
        print(f"[dry-run] {' '.join(shlex.quote(part) for part in argv)}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise RuntimeError(stderr or stdout or f"Command failed with exit code {result.returncode}: {argv}")
    return result


def reset_directory(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] reset directory {path}")
        return
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def sync_runtime_copy(*, dry_run: bool) -> Path:
    if dry_run:
        print(f"[dry-run] sync runtime copy to {RUNTIME_ROOT}")
    reset_directory(RUNTIME_ROOT, dry_run=dry_run)

    for relative_path in RUNTIME_DIRECTORY_RELATIVE_PATHS:
        source = REPO_ROOT / relative_path
        destination = RUNTIME_ROOT / relative_path
        if not source.exists():
            raise RuntimeError(f"Required runtime path not found: {source}")
        if dry_run:
            print(f"[dry-run] copy directory {source} -> {destination}")
            continue
        shutil.copytree(
            source,
            destination,
            ignore=COPY_IGNORE_PATTERNS,
            symlinks=relative_path == Path(".venv"),
        )

    for relative_path in RUNTIME_FILE_RELATIVE_PATHS:
        source = REPO_ROOT / relative_path
        destination = RUNTIME_ROOT / relative_path
        if dry_run:
            print(f"[dry-run] copy file {source} -> {destination}")
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    return RUNTIME_ROOT


def build_program_arguments(args: argparse.Namespace, launcher_script: Path, runtime_root: Path) -> list[str]:
    python_bin = resolve_runtime_python(runtime_root)
    program_arguments = [
        str(python_bin),
        str(launcher_script),
        "--replace-existing-bridge",
        "--bridge-host",
        args.bridge_host,
        "--bridge-port",
        str(args.bridge_port),
        "--session-name",
        args.session_name,
        "--client-display",
        str(args.client_display),
        "--dashboard-display",
        str(args.dashboard_display),
    ]
    if args.browser_only_client:
        program_arguments.append("--browser-only-client")
    if args.no_open_dashboard:
        program_arguments.append("--no-open-dashboard")
    if args.no_tailscale_serve:
        program_arguments.append("--no-tailscale-serve")
    if args.no_enable_webclient:
        program_arguments.append("--no-enable-webclient")
    return program_arguments


def build_launch_agent_payload(
    args: argparse.Namespace,
    runtime_root: Path,
    launcher_script: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": build_program_arguments(args, launcher_script, runtime_root),
        "WorkingDirectory": str(runtime_root),
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "EnvironmentVariables": {
            "PATH": DEFAULT_PATH,
        },
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }


def main() -> int:
    args = parse_args()

    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    logs_dir = Path.home() / "Library" / "Logs" / "of-curves-and-hands"
    plist_path = launch_agents_dir / f"{LABEL}.plist"
    stdout_path = logs_dir / "plotter-workspace.out.log"
    stderr_path = logs_dir / "plotter-workspace.err.log"
    runtime_root = sync_runtime_copy(dry_run=args.dry_run)
    payload = build_launch_agent_payload(args, runtime_root, RUNTIME_LAUNCHER_SCRIPT, stdout_path, stderr_path)

    if args.dry_run:
        print(f"[dry-run] write LaunchAgent plist to {plist_path}")
        print(plistlib.dumps(payload).decode("utf-8"))
        return 0

    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    APP_SUPPORT_ROOT.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")

    with plist_path.open("wb") as file_obj:
        plistlib.dump(payload, file_obj)

    uid = str(os.getuid())
    bootout_target = f"gui/{uid}"

    run(["launchctl", "bootout", bootout_target, str(plist_path)], check=False)
    run(["launchctl", "bootstrap", bootout_target, str(plist_path)])
    run(["launchctl", "enable", f"{bootout_target}/{LABEL}"], check=False)
    run(["launchctl", "kickstart", "-k", f"{bootout_target}/{LABEL}"])

    print(f"Installed LaunchAgent: {plist_path}")
    print(f"Runtime copy: {runtime_root}")
    print(f"Stdout log: {stdout_path}")
    print(f"Stderr log: {stderr_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
