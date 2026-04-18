#!/usr/bin/env python3
"""Launch the plotter bridge workspace, local browser tabs, and Tailscale access."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHELL = shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "of-curves-and-hands"
NATIVE_CLIENT_DIR = REPO_ROOT / "native-client"
NATIVE_CLIENT_SOURCE = NATIVE_CLIENT_DIR / "PlotterClientApp.swift"
NATIVE_CLIENT_PLIST = NATIVE_CLIENT_DIR / "Info.plist"
NATIVE_DASHBOARD_PLIST = NATIVE_CLIENT_DIR / "DashboardInfo.plist"
NATIVE_CLIENT_BUILD_DIR = APP_SUPPORT_DIR / "native-apps"
NATIVE_CLIENT_APP_BUNDLE = NATIVE_CLIENT_BUILD_DIR / "Plotter Client.app"
NATIVE_CLIENT_APP_EXECUTABLE = NATIVE_CLIENT_APP_BUNDLE / "Contents" / "MacOS" / "PlotterClientApp"
NATIVE_CLIENT_BUNDLE_ID = "com.ofcurvesandhands.plotterclient"
NATIVE_DASHBOARD_APP_BUNDLE = NATIVE_CLIENT_BUILD_DIR / "Plotter Dashboard.app"
NATIVE_DASHBOARD_APP_EXECUTABLE = NATIVE_DASHBOARD_APP_BUNDLE / "Contents" / "MacOS" / "PlotterDashboardApp"
NATIVE_DASHBOARD_BUNDLE_ID = "com.ofcurvesandhands.plotterdashboard"
DEFAULT_SESSION_NAME = "plotter-workspace"
DEFAULT_BRIDGE_HOST = "localhost"
DEFAULT_BRIDGE_PORT = 8765
DEFAULT_TAILSCALE_LOCAL_WEB_URL = "http://100.100.100.100"
DEFAULT_CLIENT_DISPLAY = 1
DEFAULT_DASHBOARD_DISPLAY = 0


def resolve_project_python(repo_root: Path) -> Path:
    for candidate in (
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "bin" / "python3",
    ):
        if candidate.exists():
            return candidate
    return Path(sys.executable)


PYTHON_BIN = str(resolve_project_python(REPO_ROOT))


@dataclass(frozen=True)
class TailscaleInfo:
    ip4: str | None
    dns_name: str | None


@dataclass(frozen=True)
class PortOwner:
    pid: int
    command: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or reuse a tmux workspace for the plotter bridge, open the local browser pages, "
            "and print the Tailscale URLs for remote monitoring."
        )
    )
    parser.add_argument("--session-name", default=DEFAULT_SESSION_NAME, help="tmux session name to create or reuse")
    parser.add_argument("--bridge-host", default=DEFAULT_BRIDGE_HOST, help="host to bind the plotter bridge to")
    parser.add_argument("--bridge-port", type=int, default=DEFAULT_BRIDGE_PORT, help="port to bind the plotter bridge to")
    parser.add_argument("--bridge-start-timeout", type=float, default=20.0, help="seconds to wait for /health")
    parser.add_argument("--force-restart-bridge", action="store_true", help="restart the bridge window even if /health is already up")
    parser.add_argument("--browser-only-client", action="store_true", help="open the primary client in the browser instead of the native client app")
    parser.add_argument(
        "--client-display",
        type=int,
        default=DEFAULT_CLIENT_DISPLAY,
        help="1-based display number for the fullscreen native client",
    )
    parser.add_argument(
        "--dashboard-display",
        type=int,
        default=DEFAULT_DASHBOARD_DISPLAY,
        help="1-based display number for the fullscreen native dashboard; set to 0 to keep the dashboard in the browser",
    )
    parser.add_argument(
        "--fullscreen-primary",
        action="store_true",
        help="put the primary client browser window into fullscreen after launch when supported",
    )
    parser.add_argument(
        "--replace-existing-bridge",
        action="store_true",
        help="if the target port is owned by an older plotter bridge process, stop it and replace it",
    )
    parser.add_argument("--force-serve", action="store_true", help="replace an existing Tailscale Serve root proxy")
    parser.add_argument("--no-open-browser", action="store_true", help="do not open local browser tabs")
    parser.add_argument("--no-open-dashboard", action="store_true", help="do not open the dashboard page")
    parser.add_argument(
        "--open-local-tailscale-page",
        action="store_true",
        help="open the local Tailscale web interface in the browser after launch",
    )
    parser.add_argument("--no-tailscale-serve", action="store_true", help="skip configuring Tailscale Serve")
    parser.add_argument("--no-enable-webclient", action="store_true", help="skip enabling the Tailscale webclient on port 5252")
    parser.add_argument("--dry-run", action="store_true", help="print the actions without starting or changing anything")
    return parser.parse_args()


def require_command(command: str) -> None:
    if shutil.which(command):
        return
    raise RuntimeError(f"Required command not found on PATH: {command}")


def run_command(
    argv: list[str],
    *,
    check: bool = True,
    capture_output: bool = True,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    if dry_run:
        print(f"[dry-run] {' '.join(shlex.quote(part) for part in argv)}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    result = subprocess.run(
        argv,
        check=False,
        capture_output=capture_output,
        text=True,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        details = stderr or stdout or f"exit code {result.returncode}"
        raise RuntimeError(f"Command failed: {' '.join(shlex.quote(part) for part in argv)}\n{details}")
    return result


def safe_run_text(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return run_command(argv, check=False)
    except Exception:  # pylint: disable=broad-except
        return None


def tmux_session_exists(session_name: str) -> bool:
    result = run_command(["tmux", "has-session", "-t", session_name], check=False)
    return result.returncode == 0


def tmux_window_names(session_name: str) -> set[str]:
    if not tmux_session_exists(session_name):
        return set()
    result = run_command(["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"])
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def build_bridge_shell_command(repo_root: Path, host: str, port: int) -> str:
    return (
        f"cd {shlex.quote(str(repo_root))} && "
        f"export PLOTTER_BRIDGE_HOST={shlex.quote(host)} PLOTTER_BRIDGE_PORT={shlex.quote(str(port))} && "
        f"exec {shlex.quote(PYTHON_BIN)} plotter-bridge/app.py"
    )


def build_shell_window_command(repo_root: Path) -> str:
    return f"cd {shlex.quote(str(repo_root))} && exec {shlex.quote(DEFAULT_SHELL)} -l"


def ensure_tmux_workspace(
    session_name: str,
    bridge_command: str,
    shell_command: str,
    *,
    manage_bridge_window: bool,
    restart_bridge: bool,
    dry_run: bool,
) -> None:
    session_exists = tmux_session_exists(session_name)
    if not session_exists:
        if manage_bridge_window:
            run_command(
                ["tmux", "new-session", "-d", "-s", session_name, "-n", "bridge", bridge_command],
                dry_run=dry_run,
            )
            run_command(
                ["tmux", "new-window", "-d", "-t", session_name, "-n", "shell", shell_command],
                dry_run=dry_run,
            )
        else:
            run_command(
                ["tmux", "new-session", "-d", "-s", session_name, "-n", "shell", shell_command],
                dry_run=dry_run,
            )
        return

    windows = tmux_window_names(session_name)
    if "shell" not in windows:
        run_command(
            ["tmux", "new-window", "-d", "-t", session_name, "-n", "shell", shell_command],
            dry_run=dry_run,
        )

    if not manage_bridge_window:
        return

    if "bridge" not in windows:
        run_command(
            ["tmux", "new-window", "-d", "-t", session_name, "-n", "bridge", bridge_command],
            dry_run=dry_run,
        )
        return

    if restart_bridge:
        run_command(
            ["tmux", "respawn-window", "-k", "-t", f"{session_name}:bridge", bridge_command],
            dry_run=dry_run,
        )


def wait_for_http_ok(url: str, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    request = Request(url, headers={"Accept": "application/json"})
    while time.monotonic() < deadline:
        try:
            with urlopen(request, timeout=1.5) as response:
                if 200 <= response.status < 300:
                    return True
        except (HTTPError, URLError, TimeoutError):
            time.sleep(0.4)
    return False


def native_app_needs_rebuild(app_executable: Path, source_paths: list[Path]) -> bool:
    if not app_executable.exists():
        return True

    executable_mtime = app_executable.stat().st_mtime
    source_mtimes = [
        path.stat().st_mtime
        for path in source_paths
        if path.exists()
    ]
    return any(mtime > executable_mtime for mtime in source_mtimes)


def wait_for_port_to_clear(port: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if find_port_owner(port) is None:
            return True
        time.sleep(0.25)
    return False


def find_port_owner(port: int) -> PortOwner | None:
    lsof_result = safe_run_text(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    if lsof_result is None or lsof_result.returncode != 0:
        return None

    pid_text = (lsof_result.stdout or "").strip().splitlines()
    if not pid_text:
        return None

    try:
        pid = int(pid_text[0].strip())
    except ValueError:
        return None

    ps_result = safe_run_text(["ps", "-p", str(pid), "-o", "command="])
    command = ""
    if ps_result is not None and ps_result.returncode == 0:
        command = (ps_result.stdout or "").strip()
    return PortOwner(pid=pid, command=command)


def maybe_replace_existing_bridge(*, port: int, enabled: bool) -> PortOwner | None:
    owner = find_port_owner(port)
    if owner is None:
        return None

    if not enabled:
        return owner

    if "plotter-bridge/app.py" not in owner.command:
        raise RuntimeError(
            f"Port {port} is in use by PID {owner.pid} ({owner.command}). "
            "Refusing to replace it automatically because it is not the plotter bridge."
        )

    os.kill(owner.pid, signal.SIGTERM)
    if not wait_for_port_to_clear(port, 5.0):
        raise RuntimeError(
            f"Tried to stop the existing bridge on port {port}, but PID {owner.pid} is still listening."
        )
    return None


def build_native_app(
    *,
    app_bundle: Path,
    app_executable: Path,
    plist_path: Path,
    dry_run: bool,
) -> None:
    if not NATIVE_CLIENT_SOURCE.exists():
        raise RuntimeError(f"Native client source file not found: {NATIVE_CLIENT_SOURCE}")
    if not plist_path.exists():
        raise RuntimeError(f"Native app Info.plist not found: {plist_path}")

    if dry_run:
        print(f"[dry-run] build native app at {app_bundle}")
        return

    (app_bundle / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (app_bundle / "Contents" / "Resources").mkdir(parents=True, exist_ok=True)
    shutil.copy2(plist_path, app_bundle / "Contents" / "Info.plist")

    run_command(
        [
            "xcrun",
            "swiftc",
            "-target",
            "arm64-apple-macos15.0",
            "-sdk",
            str(run_command(["xcrun", "--show-sdk-path"]).stdout.strip()),
            "-module-cache-path",
            "/tmp/swift-module-cache",
            str(NATIVE_CLIENT_SOURCE),
            "-o",
            str(app_executable),
        ],
        capture_output=True,
    )


def ensure_native_apps(*, dry_run: bool) -> tuple[bool, str | None]:
    if platform.system().lower() != "darwin":
        return False, "Native window apps are only available on macOS."
    if shutil.which("swiftc") is None:
        return False, "Swift compiler is unavailable, so the launcher is falling back to the browser windows."

    messages: list[str] = []

    if native_app_needs_rebuild(NATIVE_CLIENT_APP_EXECUTABLE, [NATIVE_CLIENT_SOURCE, NATIVE_CLIENT_PLIST]):
        build_native_app(
            app_bundle=NATIVE_CLIENT_APP_BUNDLE,
            app_executable=NATIVE_CLIENT_APP_EXECUTABLE,
            plist_path=NATIVE_CLIENT_PLIST,
            dry_run=dry_run,
        )
        messages.append("Built the native Plotter Client app.")

    if native_app_needs_rebuild(NATIVE_DASHBOARD_APP_EXECUTABLE, [NATIVE_CLIENT_SOURCE, NATIVE_DASHBOARD_PLIST]):
        build_native_app(
            app_bundle=NATIVE_DASHBOARD_APP_BUNDLE,
            app_executable=NATIVE_DASHBOARD_APP_EXECUTABLE,
            plist_path=NATIVE_DASHBOARD_PLIST,
            dry_run=dry_run,
        )
        messages.append("Built the native Plotter Dashboard app.")

    return True, " ".join(messages) or None


def quit_native_app(bundle_id: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] quit app id {bundle_id}")
        return
    safe_run_text(["osascript", "-e", f'tell application id "{bundle_id}" to quit'])
    time.sleep(0.4)


def activate_native_app(bundle_id: str, *, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry-run] activate app id {bundle_id}")
        return
    safe_run_text(["osascript", "-e", f'tell application id "{bundle_id}" to activate'])


def launch_native_app(
    *,
    app_bundle: Path,
    bundle_id: str,
    url: str,
    title: str,
    screen_number: int,
    requires_camera: bool,
    session_name: str,
    dry_run: bool,
) -> None:
    tmux_path = shutil.which("tmux")
    if not tmux_path:
        raise RuntimeError("Could not find tmux on PATH for native workspace shutdown.")

    command = [
        "open",
        "-n",
        str(app_bundle),
        "--args",
        url,
        "--title",
        title,
        "--screen",
        str(screen_number),
        "--session-name",
        session_name,
        "--tmux-path",
        tmux_path,
    ]
    if requires_camera:
        command.append("--camera")

    if dry_run:
        print(f"[dry-run] {' '.join(shlex.quote(part) for part in command)}")
        return
    quit_native_app(bundle_id, dry_run=False)
    run_command(command, capture_output=False)


def fetch_tailscale_info() -> TailscaleInfo:
    result = run_command(["tailscale", "status", "--json"])
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        compact_output = (result.stdout or result.stderr or "").strip().replace("\n", " ")
        raise RuntimeError(f"tailscale status --json returned non-JSON output: {compact_output}") from exc
    self_payload = payload.get("Self", {}) or {}
    ip4 = None
    for candidate in self_payload.get("TailscaleIPs", []) or payload.get("TailscaleIPs", []):
        if isinstance(candidate, str) and "." in candidate:
            ip4 = candidate
            break
    dns_name = self_payload.get("DNSName")
    if isinstance(dns_name, str):
        dns_name = dns_name.rstrip(".")
    else:
        dns_name = None
    return TailscaleInfo(ip4=ip4, dns_name=dns_name)


def fetch_serve_status() -> dict:
    result = run_command(["tailscale", "serve", "status", "--json"])
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        compact_output = (result.stdout or result.stderr or "").strip().replace("\n", " ")
        raise RuntimeError(f"tailscale serve status --json returned non-JSON output: {compact_output}") from exc


def serve_root_proxy_target(status_payload: dict) -> str | None:
    web_payload = status_payload.get("Web", {})
    if not isinstance(web_payload, dict):
        return None
    for host_payload in web_payload.values():
        if not isinstance(host_payload, dict):
            continue
        handlers = host_payload.get("Handlers", {})
        if not isinstance(handlers, dict):
            continue
        root_handler = handlers.get("/")
        if isinstance(root_handler, dict):
            proxy = root_handler.get("Proxy")
            if isinstance(proxy, str) and proxy:
                return proxy
    return None


def normalize_proxy_target(target_url: str) -> tuple[str, str, int | None, str]:
    parsed = urlsplit(target_url)
    hostname = (parsed.hostname or "").lower()
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        hostname = "loopback"
    path = parsed.path or "/"
    return parsed.scheme.lower(), hostname, parsed.port, path


def configure_tailscale_serve(target_url: str, *, force: bool, dry_run: bool) -> tuple[bool, str]:
    current_status = fetch_serve_status()
    current_proxy = serve_root_proxy_target(current_status)
    if current_proxy and normalize_proxy_target(current_proxy) == normalize_proxy_target(target_url):
        return True, "Tailscale Serve already points at the bridge."

    if current_proxy and not force:
        return False, (
            "Tailscale Serve already exposes a different root proxy "
            f"({current_proxy}). Re-run with --force-serve to replace it."
        )

    run_command(["tailscale", "serve", "--bg", "--yes", target_url], dry_run=dry_run)
    if dry_run:
        return True, f"[dry-run] Would configure Tailscale Serve to proxy / to {target_url}."

    updated_status = fetch_serve_status()
    updated_proxy = serve_root_proxy_target(updated_status)
    if updated_proxy != target_url:
        raise RuntimeError(
            "Tailscale Serve did not end up on the expected root proxy. "
            f"Expected {target_url}, found {updated_proxy or 'nothing'}."
        )
    return True, f"Tailscale Serve is now proxying / to {target_url}."


def enable_tailscale_webclient(*, dry_run: bool) -> str:
    run_command(["tailscale", "set", "--webclient=true"], dry_run=dry_run)
    if dry_run:
        return "[dry-run] Would enable the Tailscale webclient on port 5252."
    return "Enabled the Tailscale webclient on port 5252."


def open_urls(
    urls: list[str],
    *,
    dry_run: bool,
    fullscreen_primary: bool = False,
    activate_browser: bool = True,
) -> None:
    if dry_run:
        for url in urls:
            print(f"[dry-run] open {url}")
        return

    system_name = platform.system().lower()
    if system_name == "darwin":
        primary_url = urls[0] if urls else None
        if shutil.which("osascript") and Path("/Applications/Safari.app").exists():
            safari_lines = ['tell application "Safari"']
            if activate_browser:
                safari_lines.append("activate")
            first_url = apple_script_string(urls[0]) if urls else '""'
            safari_lines.extend(
                [
                    "if (count of windows) is 0 then",
                    f"  make new document with properties {{URL:{first_url}}}",
                    "else",
                    f"  set current tab of front window to (make new tab at end of tabs of front window with properties {{URL:{first_url}}})",
                    "end if",
                    "set primaryTab to current tab of front window",
                ]
            )
            for url in urls[1:]:
                safari_lines.append(
                    f"make new tab at end of tabs of front window with properties {{URL:{apple_script_string(url)}}}"
                )
            safari_lines.extend(
                [
                    "set current tab of front window to primaryTab",
                    "end tell",
                ]
            )
            script = "\n".join(
                [
                    "ignoring application responses",
                    *safari_lines,
                    "end ignoring",
                ]
            )
            safari_result = subprocess.run(
                ["osascript", "-e", script],
                check=False,
                capture_output=True,
                text=True,
            )
            if safari_result.returncode == 0:
                if primary_url and activate_browser:
                    focus_safari_tab(primary_url)
                    if fullscreen_primary:
                        fullscreen_safari_front_window()
                return
        opener = ["open"]
    elif shutil.which("xdg-open"):
        opener = ["xdg-open"]
    else:
        raise RuntimeError("Could not find a browser opener command (`open` or `xdg-open`).")

    for url in urls:
        run_command([*opener, url], capture_output=False)

    if (
        system_name == "darwin"
        and urls
        and activate_browser
        and shutil.which("osascript")
        and Path("/Applications/Safari.app").exists()
    ):
        focus_safari_tab(urls[0])
        if fullscreen_primary:
            fullscreen_safari_front_window()


def apple_script_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def focus_safari_tab(target_url: str, timeout_seconds: float = 5.0) -> bool:
    script = "\n".join(
        [
            'tell application "Safari"',
            f"set targetUrl to {apple_script_string(target_url)}",
            "repeat with w in windows",
            "  repeat with t in tabs of w",
            "    try",
            "      if URL of t is targetUrl then",
            "        set current tab of w to t",
            "        set index of w to 1",
            "        activate",
            '        return "focused"',
            "      end if",
            "    end try",
            "  end repeat",
            "end repeat",
            'return "not-found"',
            "end tell",
        ]
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and (result.stdout or "").strip() == "focused":
            return True
        time.sleep(0.25)
    return False


def fullscreen_safari_front_window() -> bool:
    script = "\n".join(
        [
            'tell application "Safari" to activate',
            'tell application "System Events"',
            '  keystroke "f" using {command down, control down}',
            "end tell",
        ]
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def print_summary(
    *,
    session_name: str,
    health_ready: bool,
    local_dashboard_url: str,
    local_client_url: str,
    local_health_url: str,
    local_tailscale_web_url: str,
    tailscale_info: TailscaleInfo | None,
    serve_active: bool,
    reused_existing_bridge: bool,
    native_client_mode: bool,
    native_dashboard_mode: bool,
    serve_message: str | None,
    webclient_message: str | None,
    native_client_message: str | None,
) -> None:
    print()
    print("Workspace")
    print(f"  tmux attach -t {session_name}")
    print()
    print("Local")
    print(f"  Dashboard: {local_dashboard_url}")
    print(f"  Client:    {local_client_url}")
    print(f"  Health:    {local_health_url}")
    print(f"  Tailscale: {local_tailscale_web_url}")
    if native_client_mode and native_dashboard_mode:
        primary_label = "native apps on separate displays"
    elif native_client_mode:
        primary_label = "native app"
    else:
        primary_label = "browser tab"
    print(f"  Primary:   {primary_label}")
    print(f"  Bridge healthy: {'yes' if health_ready else 'no'}")

    if tailscale_info and tailscale_info.dns_name:
        print()
        print("Remote")
        if serve_active:
            remote_root = f"https://{tailscale_info.dns_name}"
            print(f"  Dashboard: {remote_root}/dashboard")
            print(f"  Client:    {remote_root}/client/")
            print(f"  Health:    {remote_root}/health")
        else:
            print("  Serve:     not configured by this run")
        if tailscale_info.ip4:
            print(f"  Webclient: http://{tailscale_info.ip4}:5252 (when enabled)")

    notes = [message for message in [native_client_message, serve_message, webclient_message] if message]
    if reused_existing_bridge:
        notes.insert(0, "Reused an already-healthy bridge on the requested host/port instead of starting a second copy.")
    if notes:
        print()
        print("Notes")
        for message in notes:
            print(f"  {message}")


def main() -> int:
    args = parse_args()

    require_command("python3")
    require_command("tmux")

    bridge_base_url = f"http://{args.bridge_host}:{args.bridge_port}"
    local_dashboard_url = f"{bridge_base_url}/dashboard"
    local_client_url = f"{bridge_base_url}/client/"
    local_health_url = f"{bridge_base_url}/health"
    local_tailscale_web_url = DEFAULT_TAILSCALE_LOCAL_WEB_URL

    bridge_command = build_bridge_shell_command(REPO_ROOT, args.bridge_host, args.bridge_port)
    shell_command = build_shell_window_command(REPO_ROOT)

    existing_bridge_healthy = wait_for_http_ok(local_health_url, 1.0)
    existing_client_route_ready = wait_for_http_ok(local_client_url, 1.0) if existing_bridge_healthy else False

    if args.replace_existing_bridge and existing_bridge_healthy and not existing_client_route_ready:
        maybe_replace_existing_bridge(port=args.bridge_port, enabled=True)
        existing_bridge_healthy = False
        existing_client_route_ready = False

    manage_bridge_window = args.force_restart_bridge or not (existing_bridge_healthy and existing_client_route_ready)
    restart_bridge = manage_bridge_window
    ensure_tmux_workspace(
        args.session_name,
        bridge_command,
        shell_command,
        manage_bridge_window=manage_bridge_window,
        restart_bridge=restart_bridge,
        dry_run=args.dry_run,
    )

    health_ready = True
    if not args.dry_run:
        health_ready = wait_for_http_ok(local_health_url, args.bridge_start_timeout)
        if not health_ready:
            raise RuntimeError(
                f"Plotter bridge did not become healthy within {args.bridge_start_timeout:.1f}s at {local_health_url}."
            )
        client_route_ready = wait_for_http_ok(local_client_url, 2.0)
        if not client_route_ready:
            port_owner = find_port_owner(args.bridge_port)
            owner_details = ""
            if port_owner is not None:
                owner_details = (
                    f" Current listener: PID {port_owner.pid}"
                    + (f" ({port_owner.command})" if port_owner.command else "")
                    + "."
                )
            raise RuntimeError(
                f"The bridge at {bridge_base_url} is responding, but {local_client_url} is unavailable. "
                "An older bridge process may already be bound to this port."
                f"{owner_details} Stop that process or choose a different --bridge-port such as "
                f"`python3 scripts/launch_plotter_workspace.py --bridge-port {args.bridge_port + 1}`."
            )

    serve_active = False
    serve_message = None
    webclient_message = None
    native_client_message = None
    native_dashboard_mode = False
    tailscale_info: TailscaleInfo | None = None

    tailscale_available = shutil.which("tailscale") is not None
    if tailscale_available:
        try:
            tailscale_info = fetch_tailscale_info()
        except Exception as exc:  # pylint: disable=broad-except
            serve_message = f"Could not read Tailscale status: {exc}"
        else:
            if not args.no_tailscale_serve:
                try:
                    serve_active, serve_message = configure_tailscale_serve(
                        bridge_base_url,
                        force=args.force_serve,
                        dry_run=args.dry_run,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    serve_message = f"Could not configure Tailscale Serve: {exc}"

            if not args.no_enable_webclient:
                try:
                    webclient_message = enable_tailscale_webclient(dry_run=args.dry_run)
                except Exception as exc:  # pylint: disable=broad-except
                    webclient_message = f"Could not enable the Tailscale webclient: {exc}"
    else:
        serve_message = "Tailscale CLI is not installed, so remote URLs were not configured."

    native_client_mode = False
    if not args.no_open_browser:
        if not args.browser_only_client:
            native_client_mode, native_client_message = ensure_native_apps(dry_run=args.dry_run)
            if native_client_mode:
                try:
                    launch_native_app(
                        app_bundle=NATIVE_CLIENT_APP_BUNDLE,
                        bundle_id=NATIVE_CLIENT_BUNDLE_ID,
                        url=local_client_url,
                        title="Plotter Client",
                        screen_number=max(args.client_display, 1),
                        requires_camera=True,
                        session_name=args.session_name,
                        dry_run=args.dry_run,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    native_client_mode = False
                    fallback_note = (
                        "Native Plotter Client launch failed, so the launcher fell back to the browser client: "
                        f"{exc}"
                    )
                    if native_client_message:
                        native_client_message = f"{native_client_message} {fallback_note}"
                    else:
                        native_client_message = fallback_note
                else:
                    if not args.no_open_dashboard and args.dashboard_display > 0:
                        try:
                            launch_native_app(
                                app_bundle=NATIVE_DASHBOARD_APP_BUNDLE,
                                bundle_id=NATIVE_DASHBOARD_BUNDLE_ID,
                                url=local_dashboard_url,
                                title="Plotter Dashboard",
                                screen_number=args.dashboard_display,
                                requires_camera=False,
                                session_name=args.session_name,
                                dry_run=args.dry_run,
                            )
                            native_dashboard_mode = True
                        except Exception as exc:  # pylint: disable=broad-except
                            dashboard_note = (
                                "Native Plotter Dashboard launch failed, so the launcher fell back to the browser "
                                f"dashboard: {exc}"
                            )
                            if native_client_message:
                                native_client_message = f"{native_client_message} {dashboard_note}"
                            else:
                                native_client_message = dashboard_note

        browser_urls: list[str] = []
        if not native_client_mode:
            browser_urls.insert(0, local_client_url)
        if not args.no_open_dashboard and not native_dashboard_mode:
            insert_index = 1 if not native_client_mode else 0
            browser_urls.insert(insert_index, local_dashboard_url)
        if args.open_local_tailscale_page:
            browser_urls.append(local_tailscale_web_url)

        if browser_urls:
            open_urls(
                browser_urls,
                dry_run=args.dry_run,
                fullscreen_primary=args.fullscreen_primary and not native_client_mode,
                activate_browser=not native_client_mode,
            )
        if native_client_mode:
            activate_native_app(NATIVE_CLIENT_BUNDLE_ID, dry_run=args.dry_run)

    print_summary(
        session_name=args.session_name,
        health_ready=health_ready,
        local_dashboard_url=local_dashboard_url,
        local_client_url=local_client_url,
        local_health_url=local_health_url,
        local_tailscale_web_url=local_tailscale_web_url,
        tailscale_info=tailscale_info,
        serve_active=serve_active,
        reused_existing_bridge=existing_bridge_healthy and existing_client_route_ready and not args.force_restart_bridge,
        native_client_mode=native_client_mode,
        native_dashboard_mode=native_dashboard_mode,
        serve_message=serve_message,
        webclient_message=webclient_message,
        native_client_message=native_client_message,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
