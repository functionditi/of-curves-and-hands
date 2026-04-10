#!/usr/bin/env python3
# Run: python3 test-a-3-plotters.py

"""
test-a-3-plotters.py

Connect to three AxiDraw units in interactive mode and draw the same 1 inch square.

Optional:
  python3 test-a-3-plotters.py --port1 "AxiDraw One" --port2 "AxiDraw Two" --port3 "AxiDraw Three"
"""

import argparse
import sys
import threading
from typing import Optional

from plotink import ebb_serial
from pyaxidraw import axidraw


def build_plotter(port: Optional[str], speed: int) -> axidraw.AxiDraw:
    ad = axidraw.AxiDraw()
    ad.interactive()
    if port:
        ad.options.port = port
    connected = ad.connect()
    if not connected:
        raise RuntimeError(f"Could not connect to AxiDraw ({port or 'first available'}).")

    ad.options.units = 0  # inches
    ad.options.speed_pendown = speed
    ad.update()
    return ad


def connected_port_name(ad: axidraw.AxiDraw) -> str:
    port_obj = ad.plot_status.port
    return str(getattr(port_obj, "port", port_obj))


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
    port1: Optional[str], port2: Optional[str], port3: Optional[str]
) -> tuple[str, str, str]:
    available_ports = list_axidraw_ports()
    selected: list[Optional[str]] = [port1, port2, port3]
    resolved_selected: list[Optional[str]] = [resolve_port(port) for port in selected]

    if all(selected):
        return selected[0], selected[1], selected[2]  # type: ignore[return-value]

    if len(available_ports) < 3:
        found = ", ".join(available_ports) if available_ports else "none"
        raise RuntimeError(f"Need 3 AxiDraws. Found {len(available_ports)} port(s): {found}.")

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

    return selected[0], selected[1], selected[2]  # type: ignore[return-value]


def draw_square(ad: axidraw.AxiDraw) -> None:
    ad.penup()
    ad.moveto(1.0, 1.0)
    ad.pendown()
    ad.lineto(2.0, 1.0)
    ad.lineto(2.0, 2.0)
    ad.lineto(1.0, 2.0)
    ad.lineto(1.0, 1.0)
    ad.penup()
    ad.moveto(0.0, 0.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect three AxiDraw units and draw the same 1 inch square."
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
        "--speed",
        type=int,
        default=25,
        help="Pen-down speed percentage (default: 25).",
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
    plotter_3 = None
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

        port_1_arg, port_2_arg, port_3_arg = choose_ports(args.port1, args.port2, args.port3)
        resolved_1 = resolve_port(port_1_arg)
        resolved_2 = resolve_port(port_2_arg)
        resolved_3 = resolve_port(port_3_arg)
        resolved_ports = [resolved_1, resolved_2, resolved_3]

        if len(set(resolved_ports)) != 3:
            print("Selected ports are not three distinct AxiDraw devices.")
            print("Use distinct values for --port1, --port2, and --port3.")
            return 1

        print(f"Using plotter 1: {port_1_arg}")
        print(f"Using plotter 2: {port_2_arg}")
        print(f"Using plotter 3: {port_3_arg}")

        plotter_1 = build_plotter(port_1_arg, args.speed)
        plotter_2 = build_plotter(port_2_arg, args.speed)
        plotter_3 = build_plotter(port_3_arg, args.speed)
        connected_ports = [
            connected_port_name(plotter_1),
            connected_port_name(plotter_2),
            connected_port_name(plotter_3),
        ]

        if len(set(connected_ports)) != 3:
            print("Connections resolved to fewer than three unique AxiDraw ports.")
            print("Pass --port1, --port2, and --port3 to target distinct machines.")
            return 1

        def worker(ad: axidraw.AxiDraw, label: str) -> None:
            try:
                start_event.wait()
                draw_square(ad)
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(f"{label}: {exc}")

        threads = [
            threading.Thread(target=worker, args=(plotter_1, "plotter_1"), daemon=True),
            threading.Thread(target=worker, args=(plotter_2, "plotter_2"), daemon=True),
            threading.Thread(target=worker, args=(plotter_3, "plotter_3"), daemon=True),
        ]

        for thread in threads:
            thread.start()

        # Release all worker threads together so the machines start nearly together.
        start_event.set()

        for thread in threads:
            thread.join()

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
            plotter_1.disconnect()
        if plotter_2 is not None:
            plotter_2.disconnect()
        if plotter_3 is not None:
            plotter_3.disconnect()


if __name__ == "__main__":
    sys.exit(main())
