#!/usr/bin/env python3
# Run: python3 test-c-4-plotters-30min.py

"""
test-c-4-plotters-30min.py

Connect to four AxiDraw units and continuously repeat one kolam SVG per machine
for a timed run (30 minutes by default).

Defaults:
  plotter 1 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-1.svg
  plotter 2 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-2.svg
  plotter 3 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-3.svg
  plotter 4 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-4.svg

Optional:
  python3 test-c-4-plotters-30min.py --port1 "AxiDraw One" --port2 "AxiDraw Two" --port3 "AxiDraw Three" --port4 "AxiDraw Four"
"""

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from plotink import ebb_serial
from pyaxidraw import axidraw

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SVG_1 = SCRIPT_DIR.parent / "todraw" / "kolam-blue-with-pullis-1.svg"
DEFAULT_SVG_2 = SCRIPT_DIR.parent / "todraw" / "kolam-blue-with-pullis-2.svg"
DEFAULT_SVG_3 = SCRIPT_DIR.parent / "todraw" / "kolam-blue-with-pullis-3.svg"
DEFAULT_SVG_4 = SCRIPT_DIR.parent / "todraw" / "kolam-blue-with-pullis-4.svg"


@dataclass
class PlotterStatus:
    port: str
    svg_path: Path
    state: str = "ready"
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    error: Optional[str] = None
    cycle: int = 0
    cycles_completed: int = 0
    cycle_started_at: Optional[float] = None
    last_cycle_seconds: Optional[float] = None


def elapsed_seconds(started_at: Optional[float], ended_at: Optional[float] = None) -> str:
    if started_at is None:
        return "-"
    final_time = ended_at if ended_at is not None else time.monotonic()
    return f"{max(0.0, final_time - started_at):.1f}s"


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    total = int(max(0.0, seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def open_serial_port(port_label: str):
    resolved = resolve_port(port_label) or port_label
    serial_port = ebb_serial.testPort(resolved)
    if serial_port is None:
        raise RuntimeError(
            f"Could not open AxiDraw port {port_label} (resolved to {resolved})."
        )
    return serial_port


def build_plotter(port: str, speed: int, svg_path: Path) -> axidraw.AxiDraw:
    ad = axidraw.AxiDraw()
    ad.plot_setup(str(svg_path))
    # Use a persistent opened serial object so repeated plot_run() calls stay
    # pinned to the same device instead of re-selecting "first available".
    ad.options.port = open_serial_port(port)
    ad.options.speed_pendown = speed
    # Fail fast if a specific port cannot be reached or gets disconnected.
    ad.errors.connect = True
    ad.errors.disconnect = True
    return ad


def list_axidraw_ports() -> list[str]:
    ports = ebb_serial.listEBBports() or []
    result = []
    for entry in ports:
        device = getattr(entry, "device", None)
        if device is None:
            device = entry[0]
        result.append(str(device))
    return result


def resolve_port(port_value: Optional[str]) -> Optional[str]:
    if not port_value:
        return None
    resolved = ebb_serial.find_named_ebb(port_value)
    return str(resolved) if resolved else port_value


def choose_ports(
    port1: Optional[str], port2: Optional[str], port3: Optional[str], port4: Optional[str]
) -> tuple[str, str, str, str]:
    available_ports = list_axidraw_ports()
    selected: list[Optional[str]] = [port1, port2, port3, port4]
    resolved_selected: list[Optional[str]] = [resolve_port(port) for port in selected]

    if all(selected):
        return selected[0], selected[1], selected[2], selected[3]  # type: ignore[return-value]

    if len(available_ports) < 4:
        found = ", ".join(available_ports) if available_ports else "none"
        raise RuntimeError(f"Need 4 AxiDraws. Found {len(available_ports)} port(s): {found}.")

    for idx, current in enumerate(selected):
        if current:
            continue

        used_ports = {port for port in selected if port}
        used_resolved_ports = {port for port in resolved_selected if port}
        candidate = next(
            (
                available
                for available in available_ports
                if available not in used_ports and available not in used_resolved_ports
            ),
            None,
        )

        if candidate is None:
            raise RuntimeError(f"Could not auto-select a distinct port for plotter {idx + 1}.")

        selected[idx] = candidate
        resolved_selected[idx] = candidate

    return selected[0], selected[1], selected[2], selected[3]  # type: ignore[return-value]


def resolve_svg_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Connect four AxiDraw units and keep repeating one SVG on each machine "
            "for a fixed duration."
        )
    )
    parser.add_argument(
        "--port1",
        type=str,
        default=None,
        help="USB port or USB nickname for plotter 1 (optional).",
    )
    parser.add_argument(
        "--port2",
        type=str,
        default=None,
        help="USB port or USB nickname for plotter 2 (optional).",
    )
    parser.add_argument(
        "--port3",
        type=str,
        default=None,
        help="USB port or USB nickname for plotter 3 (optional).",
    )
    parser.add_argument(
        "--port4",
        type=str,
        default=None,
        help="USB port or USB nickname for plotter 4 (optional).",
    )
    parser.add_argument(
        "--speed",
        type=int,
        default=25,
        help="Pen-down speed percentage (default: 25).",
    )
    parser.add_argument(
        "--svg1",
        type=str,
        default=str(DEFAULT_SVG_1),
        help=f"SVG for plotter 1 (default: {DEFAULT_SVG_1}).",
    )
    parser.add_argument(
        "--svg2",
        type=str,
        default=str(DEFAULT_SVG_2),
        help=f"SVG for plotter 2 (default: {DEFAULT_SVG_2}).",
    )
    parser.add_argument(
        "--svg3",
        type=str,
        default=str(DEFAULT_SVG_3),
        help=f"SVG for plotter 3 (default: {DEFAULT_SVG_3}).",
    )
    parser.add_argument(
        "--svg4",
        type=str,
        default=str(DEFAULT_SVG_4),
        help=f"SVG for plotter 4 (default: {DEFAULT_SVG_4}).",
    )
    parser.add_argument(
        "--duration-minutes",
        type=float,
        default=30.0,
        help="Total run duration in minutes (default: 30).",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List detected AxiDraw USB ports and exit.",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=10.0,
        help="Seconds between periodic status snapshots (default: 10, use 0 to disable).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    plotter_1 = None
    plotter_2 = None
    plotter_3 = None
    plotter_4 = None
    errors: list[str] = []
    start_event = threading.Event()
    stop_event = threading.Event()
    status_lock = threading.Lock()
    print_lock = threading.Lock()
    plotter_status: dict[str, PlotterStatus] = {}
    run_started_at: Optional[float] = None
    deadline_at: Optional[float] = None

    def log(message: str) -> None:
        with print_lock:
            print(message, flush=True)

    def emit_status_snapshot(prefix: str = "status") -> None:
        with status_lock:
            snapshot = [
                (
                    label,
                    details.state,
                    details.cycle,
                    details.cycles_completed,
                    details.cycle_started_at,
                    details.last_cycle_seconds,
                    details.started_at,
                    details.ended_at,
                    details.error,
                )
                for label, details in plotter_status.items()
            ]

        now = time.monotonic()
        elapsed_run = max(0.0, now - run_started_at) if run_started_at is not None else None
        remaining_run = max(0.0, deadline_at - now) if deadline_at is not None else None
        log(
            f"[{prefix}] elapsed={format_duration(elapsed_run)} "
            f"remaining={format_duration(remaining_run)}"
        )
        log(f"[{prefix}] Plotter states:")

        for (
            label,
            state,
            cycle,
            cycles_completed,
            cycle_started_at,
            last_cycle_seconds,
            started_at,
            ended_at,
            error,
        ) in snapshot:
            cycle_elapsed = elapsed_seconds(cycle_started_at) if cycle_started_at else "-"
            line = (
                f"[{prefix}]   {label}: state={state:<7} cycle={cycle:>2} "
                f"completed={cycles_completed:>2} cycle_elapsed={cycle_elapsed:>6} "
                f"total_elapsed={elapsed_seconds(started_at, ended_at)}"
            )
            if last_cycle_seconds is not None:
                line = f"{line} last_cycle={format_duration(last_cycle_seconds)}"
            if error:
                line = f"{line} error={error}"
            log(line)

    try:
        available_ports = list_axidraw_ports()
        if args.list_ports:
            if not available_ports:
                print("No AxiDraw USB ports detected.")
            else:
                print("Detected AxiDraw ports:")
                for idx, port_name in enumerate(available_ports, start=1):
                    print(f"  {idx}. {port_name}")
            return 0

        if args.status_interval < 0:
            raise RuntimeError("--status-interval must be >= 0.")
        if args.duration_minutes <= 0:
            raise RuntimeError("--duration-minutes must be > 0.")

        run_seconds = args.duration_minutes * 60.0
        svg_1_path = resolve_svg_path(args.svg1)
        svg_2_path = resolve_svg_path(args.svg2)
        svg_3_path = resolve_svg_path(args.svg3)
        svg_4_path = resolve_svg_path(args.svg4)

        if not svg_1_path.is_file():
            raise RuntimeError(f"SVG for plotter 1 not found: {svg_1_path}")
        if not svg_2_path.is_file():
            raise RuntimeError(f"SVG for plotter 2 not found: {svg_2_path}")
        if not svg_3_path.is_file():
            raise RuntimeError(f"SVG for plotter 3 not found: {svg_3_path}")
        if not svg_4_path.is_file():
            raise RuntimeError(f"SVG for plotter 4 not found: {svg_4_path}")

        port_1_arg, port_2_arg, port_3_arg, port_4_arg = choose_ports(
            args.port1, args.port2, args.port3, args.port4
        )
        resolved_1 = resolve_port(port_1_arg)
        resolved_2 = resolve_port(port_2_arg)
        resolved_3 = resolve_port(port_3_arg)
        resolved_4 = resolve_port(port_4_arg)
        resolved_ports = [resolved_1, resolved_2, resolved_3, resolved_4]

        if len(set(resolved_ports)) != 4:
            print("Selected ports are not four distinct AxiDraw devices.")
            print("Use distinct values for --port1, --port2, --port3, and --port4.")
            return 1

        log(f"[setup] Run duration: {args.duration_minutes:.1f} min ({int(run_seconds)}s)")
        log(f"[setup] Using plotter 1: {port_1_arg}")
        log(f"[setup]   SVG: {svg_1_path}")
        log(f"[setup] Using plotter 2: {port_2_arg}")
        log(f"[setup]   SVG: {svg_2_path}")
        log(f"[setup] Using plotter 3: {port_3_arg}")
        log(f"[setup]   SVG: {svg_3_path}")
        log(f"[setup] Using plotter 4: {port_4_arg}")
        log(f"[setup]   SVG: {svg_4_path}")

        plotter_status = {
            "plotter_1": PlotterStatus(port=port_1_arg, svg_path=svg_1_path),
            "plotter_2": PlotterStatus(port=port_2_arg, svg_path=svg_2_path),
            "plotter_3": PlotterStatus(port=port_3_arg, svg_path=svg_3_path),
            "plotter_4": PlotterStatus(port=port_4_arg, svg_path=svg_4_path),
        }

        plotter_1 = build_plotter(port_1_arg, args.speed, svg_1_path)
        plotter_2 = build_plotter(port_2_arg, args.speed, svg_2_path)
        plotter_3 = build_plotter(port_3_arg, args.speed, svg_3_path)
        plotter_4 = build_plotter(port_4_arg, args.speed, svg_4_path)

        def worker(ad: axidraw.AxiDraw, label: str) -> None:
            try:
                start_event.wait()
                with status_lock:
                    details = plotter_status[label]
                    details.state = "waiting"
                    details.started_at = time.monotonic()
                    port = details.port
                    svg_path = details.svg_path
                log(f"[{label}] Ready on {port} with {svg_path.name}")

                while True:
                    if stop_event.is_set():
                        break

                    now = time.monotonic()
                    if deadline_at is not None and now >= deadline_at:
                        break

                    with status_lock:
                        details = plotter_status[label]
                        details.state = "running"
                        details.cycle += 1
                        cycle_number = details.cycle
                        details.cycle_started_at = time.monotonic()

                    if deadline_at is not None:
                        remaining = max(0.0, deadline_at - time.monotonic())
                        log(
                            f"[{label}] Cycle {cycle_number} start "
                            f"(remaining window {format_duration(remaining)})"
                        )
                    else:
                        log(f"[{label}] Cycle {cycle_number} start")

                    cycle_started = time.monotonic()
                    ad.plot_run()
                    cycle_ended = time.monotonic()

                    with status_lock:
                        details = plotter_status[label]
                        details.cycles_completed += 1
                        details.last_cycle_seconds = cycle_ended - cycle_started
                        details.cycle_started_at = None
                        details.ended_at = cycle_ended
                        details.state = "waiting"
                        completed = details.cycles_completed

                    log(
                        f"[{label}] Cycle {cycle_number} complete in "
                        f"{format_duration(cycle_ended - cycle_started)} "
                        f"(completed {completed})"
                    )

                with status_lock:
                    details = plotter_status[label]
                    if details.state != "error":
                        details.state = "done"
                        details.ended_at = time.monotonic()
                        details.cycle_started_at = None
                        completed = details.cycles_completed

                log(f"[{label}] Finished run window with {completed} completed cycle(s).")
            except Exception as exc:  # pylint: disable=broad-except
                with status_lock:
                    details = plotter_status[label]
                    details.state = "error"
                    details.ended_at = time.monotonic()
                    details.cycle_started_at = None
                    details.error = str(exc)
                errors.append(f"{label}: {exc}")
                stop_event.set()
                log(f"[{label}] ERROR: {exc}")

        threads = [
            threading.Thread(target=worker, args=(plotter_1, "plotter_1"), daemon=True),
            threading.Thread(target=worker, args=(plotter_2, "plotter_2"), daemon=True),
            threading.Thread(target=worker, args=(plotter_3, "plotter_3"), daemon=True),
            threading.Thread(target=worker, args=(plotter_4, "plotter_4"), daemon=True),
        ]

        for thread in threads:
            thread.start()

        run_started_at = time.monotonic()
        deadline_at = run_started_at + run_seconds
        deadline_wall_clock = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + run_seconds))
        log("[status] Waiting for synchronized start...")
        log(f"[status] Deadline (wall clock): {deadline_wall_clock}")
        start_event.set()
        log("[status] Start signal sent to all plotters.")
        emit_status_snapshot("status")

        next_snapshot = (
            time.monotonic() + args.status_interval if args.status_interval > 0 else None
        )
        deadline_notified = False
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.2)

            now = time.monotonic()
            if not deadline_notified and deadline_at is not None and now >= deadline_at:
                deadline_notified = True
                stop_event.set()
                log(
                    "[status] Target duration reached. "
                    "No new cycles will start; waiting for current cycles to finish."
                )

            if next_snapshot is not None and now >= next_snapshot:
                emit_status_snapshot("status")
                next_snapshot = now + args.status_interval

        emit_status_snapshot("final")

        if errors:
            for err in errors:
                log(err)
            return 1

        return 0
    except Exception as exc:  # pylint: disable=broad-except
        print(exc)
        return 1
    finally:
        if plotter_1 is not None:
            try:
                plotter_1.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass
        if plotter_2 is not None:
            try:
                plotter_2.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass
        if plotter_3 is not None:
            try:
                plotter_3.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass
        if plotter_4 is not None:
            try:
                plotter_4.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass


if __name__ == "__main__":
    sys.exit(main())
