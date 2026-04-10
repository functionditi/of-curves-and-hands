#!/usr/bin/env python3
# Run: python3 test-b-2-plotters-svg.py

"""
test-b-2-plotters-svg.py

Connect to two AxiDraw units and plot one SVG file per machine.
By default:
  plotter 1 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-1.svg
  plotter 2 -> AxiDraw_API_396/todraw/kolam-blue-with-pullis-2.svg

Optional:
  python3 test-b-2-plotters-svg.py --port1 "AxiDraw One" --port2 "AxiDraw Two"
"""

import argparse
import sys
import threading
from pathlib import Path
from typing import Optional

from plotink import ebb_serial
from pyaxidraw import axidraw

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SVG_1 = SCRIPT_DIR.parent / "todraw" / "kolam-blue-with-pullis-1.svg"
DEFAULT_SVG_2 = SCRIPT_DIR.parent / "todraw" / "kolam-blue-with-pullis-2.svg"

def build_plotter(port: Optional[str], speed: int, svg_path: Path) -> axidraw.AxiDraw:
    ad = axidraw.AxiDraw()
    ad.plot_setup(str(svg_path))
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


def choose_ports(port1: Optional[str], port2: Optional[str]) -> tuple[str, str]:
    available_ports = list_axidraw_ports()

    if port1 and port2:
        return port1, port2

    if not port1 and not port2:
        if len(available_ports) < 2:
            found = ", ".join(available_ports) if available_ports else "none"
            raise RuntimeError(
                f"Need 2 AxiDraws. Found {len(available_ports)} port(s): {found}."
            )
        return available_ports[0], available_ports[1]

    if len(available_ports) < 2:
        found = ", ".join(available_ports) if available_ports else "none"
        raise RuntimeError(
            f"Need 2 AxiDraws. Found {len(available_ports)} port(s): {found}."
        )

    if not port1:
        resolved_2 = resolve_port(port2)
        for candidate in available_ports:
            if candidate != resolved_2:
                return candidate, port2  # type: ignore[return-value]
        raise RuntimeError("Could not auto-select a distinct port for plotter 1.")

    resolved_1 = resolve_port(port1)
    for candidate in available_ports:
        if candidate != resolved_1:
            return port1, candidate
    raise RuntimeError("Could not auto-select a distinct port for plotter 2.")


def resolve_svg_path(path_value: str) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect two AxiDraw units and plot one SVG file on each."
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
        "--list-ports",
        action="store_true",
        help="List detected AxiDraw USB ports and exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    plotter_1 = None
    plotter_2 = None
    errors = []
    start_event = threading.Event()

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

        svg_1_path = resolve_svg_path(args.svg1)
        svg_2_path = resolve_svg_path(args.svg2)

        if not svg_1_path.is_file():
            raise RuntimeError(f"SVG for plotter 1 not found: {svg_1_path}")
        if not svg_2_path.is_file():
            raise RuntimeError(f"SVG for plotter 2 not found: {svg_2_path}")

        port_1_arg, port_2_arg = choose_ports(args.port1, args.port2)
        resolved_1 = resolve_port(port_1_arg)
        resolved_2 = resolve_port(port_2_arg)

        if resolved_1 == resolved_2:
            print("Both selected ports resolve to the same AxiDraw.")
            print("Use two distinct values for --port1 and --port2.")
            return 1

        print(f"Using plotter 1: {port_1_arg}")
        print(f"  SVG: {svg_1_path}")
        print(f"Using plotter 2: {port_2_arg}")
        print(f"  SVG: {svg_2_path}")

        plotter_1 = build_plotter(port_1_arg, args.speed, svg_1_path)
        plotter_2 = build_plotter(port_2_arg, args.speed, svg_2_path)

        def worker(ad: axidraw.AxiDraw, label: str) -> None:
            try:
                start_event.wait()
                ad.plot_run()
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(f"{label}: {exc}")

        thread_1 = threading.Thread(target=worker, args=(plotter_1, "plotter_1"), daemon=True)
        thread_2 = threading.Thread(target=worker, args=(plotter_2, "plotter_2"), daemon=True)
        thread_1.start()
        thread_2.start()

        # Release both worker threads together so the machines start nearly together.
        start_event.set()

        thread_1.join()
        thread_2.join()

        if errors:
            for err in errors:
                print(err)
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


if __name__ == "__main__":
    sys.exit(main())
