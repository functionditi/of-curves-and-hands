#!/usr/bin/env python3
# Run: python3 test-b-4-plotters-svg.py
#python3 test-b-4-plotters-svg.py \
#   --port1 /dev/cu.usbmodem21301 \
#   --port2 /dev/cu.usbmodem21401 \
#   --port3 /dev/cu.usbmodem1201 \
#   --port4 /dev/cu.usbmodem1301

"""
test-b-4-plotters-svg.py

python3 test-b-4-plotters-svg.py \
  --port1 /dev/cu.usbmodem21301 \
  --port2 /dev/cu.usbmodem21401 \
  --port3 /dev/cu.usbmodem1201 \
  --port4 /dev/cu.usbmodem1301

Connect to four AxiDraw units and plot one SVG file per machine.
By default:
  plotter 1 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-1.svg
  plotter 2 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-2.svg
  plotter 3 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-3.svg
  plotter 4 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-4.svg

Optional:
  python3 test-b-4-plotters-svg.py --port1 "AxiDraw One" --port2 "AxiDraw Two" --port3 "AxiDraw Three" --port4 "AxiDraw Four"
"""

import argparse
import sys
import threading
import time
import xml.etree.ElementTree as ET
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
TARGET_SVG_WIDTH_IN = 6.5
TARGET_SVG_HEIGHT_IN = 6.5


@dataclass
class PlotterStatus:
    port: str
    svg_path: Path
    state: str = "ready"
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    error: Optional[str] = None


def elapsed_seconds(started_at: Optional[float], ended_at: Optional[float] = None) -> str:
    if started_at is None:
        return "-"
    final_time = ended_at if ended_at is not None else time.monotonic()
    return f"{max(0.0, final_time - started_at):.1f}s"


def load_sized_svg(svg_path: Path, target_width_in: float, target_height_in: float) -> str:
    try:
        svg_tree = ET.parse(svg_path)
    except ET.ParseError as exc:
        raise RuntimeError(f"Could not parse SVG file: {svg_path}: {exc}") from exc

    root = svg_tree.getroot()
    root.set("width", f"{target_width_in:g}in")
    root.set("height", f"{target_height_in:g}in")

    return ET.tostring(root, encoding="unicode")


def build_plotter(
    port: Optional[str],
    speed: int,
    svg_path: Path,
    target_width_in: float,
    target_height_in: float,
) -> axidraw.AxiDraw:
    ad = axidraw.AxiDraw()
    ad.plot_setup(load_sized_svg(svg_path, target_width_in, target_height_in))
    if port:
        ad.options.port = port
    ad.options.speed_pendown = speed
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
        description="Connect four AxiDraw units and plot one SVG file on each."
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
        "--list-ports",
        action="store_true",
        help="List detected AxiDraw USB ports and exit.",
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=10.0,
        help="Seconds between periodic status snapshots during plotting (default: 10, use 0 to disable).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    plotter_1 = None
    plotter_2 = None
    plotter_3 = None
    plotter_4 = None
    errors = []
    start_event = threading.Event()
    status_lock = threading.Lock()
    print_lock = threading.Lock()
    plotter_status: dict[str, PlotterStatus] = {}

    def log(message: str) -> None:
        with print_lock:
            print(message, flush=True)

    def emit_status_snapshot(prefix: str = "status") -> None:
        with status_lock:
            snapshot = [
                (label, details.state, details.started_at, details.ended_at, details.error)
                for label, details in plotter_status.items()
            ]

        log(f"[{prefix}] Plotter states:")
        for label, state, started_at, ended_at, error in snapshot:
            line = f"[{prefix}]   {label}: {state:<7} elapsed={elapsed_seconds(started_at, ended_at)}"
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

        log(f"Using plotter 1: {port_1_arg}")
        log(f"  SVG: {svg_1_path}")
        log(f"  Size: {TARGET_SVG_WIDTH_IN:g}in x {TARGET_SVG_HEIGHT_IN:g}in")
        log(f"Using plotter 2: {port_2_arg}")
        log(f"  SVG: {svg_2_path}")
        log(f"  Size: {TARGET_SVG_WIDTH_IN:g}in x {TARGET_SVG_HEIGHT_IN:g}in")
        log(f"Using plotter 3: {port_3_arg}")
        log(f"  SVG: {svg_3_path}")
        log(f"  Size: {TARGET_SVG_WIDTH_IN:g}in x {TARGET_SVG_HEIGHT_IN:g}in")
        log(f"Using plotter 4: {port_4_arg}")
        log(f"  SVG: {svg_4_path}")
        log(f"  Size: {TARGET_SVG_WIDTH_IN:g}in x {TARGET_SVG_HEIGHT_IN:g}in")

        plotter_status = {
            "plotter_1": PlotterStatus(port=port_1_arg, svg_path=svg_1_path),
            "plotter_2": PlotterStatus(port=port_2_arg, svg_path=svg_2_path),
            "plotter_3": PlotterStatus(port=port_3_arg, svg_path=svg_3_path),
            "plotter_4": PlotterStatus(port=port_4_arg, svg_path=svg_4_path),
        }

        plotter_1 = build_plotter(
            port_1_arg, args.speed, svg_1_path, TARGET_SVG_WIDTH_IN, TARGET_SVG_HEIGHT_IN
        )
        plotter_2 = build_plotter(
            port_2_arg, args.speed, svg_2_path, TARGET_SVG_WIDTH_IN, TARGET_SVG_HEIGHT_IN
        )
        plotter_3 = build_plotter(
            port_3_arg, args.speed, svg_3_path, TARGET_SVG_WIDTH_IN, TARGET_SVG_HEIGHT_IN
        )
        plotter_4 = build_plotter(
            port_4_arg, args.speed, svg_4_path, TARGET_SVG_WIDTH_IN, TARGET_SVG_HEIGHT_IN
        )

        def worker(ad: axidraw.AxiDraw, label: str) -> None:
            try:
                start_event.wait()
                with status_lock:
                    plotter_status[label].state = "running"
                    plotter_status[label].started_at = time.monotonic()
                log(
                    f"[{label}] Started plotting on {plotter_status[label].port} "
                    f"({plotter_status[label].svg_path.name})"
                )
                ad.plot_run()
                with status_lock:
                    plotter_status[label].state = "done"
                    plotter_status[label].ended_at = time.monotonic()
                    started_at = plotter_status[label].started_at
                    ended_at = plotter_status[label].ended_at
                log(f"[{label}] Completed in {elapsed_seconds(started_at, ended_at)}")
            except Exception as exc:  # pylint: disable=broad-except
                with status_lock:
                    plotter_status[label].state = "error"
                    plotter_status[label].ended_at = time.monotonic()
                    plotter_status[label].error = str(exc)
                errors.append(f"{label}: {exc}")
                log(f"[{label}] ERROR: {exc}")

        threads = [
            threading.Thread(target=worker, args=(plotter_1, "plotter_1"), daemon=True),
            threading.Thread(target=worker, args=(plotter_2, "plotter_2"), daemon=True),
            threading.Thread(target=worker, args=(plotter_3, "plotter_3"), daemon=True),
            threading.Thread(target=worker, args=(plotter_4, "plotter_4"), daemon=True),
        ]

        for thread in threads:
            thread.start()

        # Release all worker threads together so the machines start nearly together.
        log("[status] Waiting for synchronized start...")
        start_event.set()
        log("[status] Start signal sent to all plotters.")
        emit_status_snapshot("status")

        next_snapshot = (
            time.monotonic() + args.status_interval if args.status_interval > 0 else None
        )
        while any(thread.is_alive() for thread in threads):
            for thread in threads:
                thread.join(timeout=0.2)
            if next_snapshot is not None and time.monotonic() >= next_snapshot:
                emit_status_snapshot("status")
                next_snapshot = time.monotonic() + args.status_interval

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
