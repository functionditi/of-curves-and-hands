#!/usr/bin/env python3
# Run: python3 pendown-4-plotters.py

"""
pendown-4-plotters.py

Connect to one to four AxiDraw units, put all pens down, then wait for terminal input.
When you type "u" and press Enter, all pens are raised and the script exits.

Optional:
  python3 pendown-4-plotters.py --count 2
  python3 pendown-4-plotters.py --count 3 --port1 "AxiDraw One" --port2 "AxiDraw Two" --port3 "AxiDraw Three"
"""

import argparse
import sys
from typing import Optional

from plotink import ebb_serial
from pyaxidraw import axidraw

MAX_PLOTTERS = 4


def build_plotter(port: Optional[str]) -> axidraw.AxiDraw:
    ad = axidraw.AxiDraw()
    ad.interactive()
    if port:
        ad.options.port = port
    connected = ad.connect()
    if not connected:
        raise RuntimeError(f"Could not connect to AxiDraw ({port or 'first available'}).")
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


def choose_ports(requested_count: int, port_values: list[Optional[str]]) -> list[str]:
    if len(port_values) != MAX_PLOTTERS:
        raise ValueError(f"Expected {MAX_PLOTTERS} port values, got {len(port_values)}.")

    if any(port_values[idx] for idx in range(requested_count, MAX_PLOTTERS)):
        raise RuntimeError(
            "You set a port argument above --count. Increase --count or remove extra --portX values."
        )

    available_ports = list_axidraw_ports()
    selected = port_values[:requested_count]
    resolved_selected: list[Optional[str]] = [resolve_port(port) for port in selected]

    if all(selected):
        return [port for port in selected if port]

    if len(available_ports) < requested_count:
        found = ", ".join(available_ports) if available_ports else "none"
        raise RuntimeError(
            f"Need {requested_count} AxiDraw(s). Found {len(available_ports)} port(s): {found}."
        )

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

    if not all(selected):
        raise RuntimeError("Failed to select all requested ports.")

    return [port for port in selected if port]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect one to four AxiDraw units, lower all pens, wait for 'u', then raise all pens."
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        choices=range(1, MAX_PLOTTERS + 1),
        default=MAX_PLOTTERS,
        help=f"Number of plotters to control (1-{MAX_PLOTTERS}).",
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
        "--list-ports",
        action="store_true",
        help="List detected AxiDraw USB ports and exit.",
    )
    return parser.parse_args()


def main() -> int:
    plotters: list[tuple[str, axidraw.AxiDraw]] = []
    pens_lowered = False

    try:
        args = parse_args()
        available_ports = list_axidraw_ports()

        if args.list_ports:
            if not available_ports:
                print("No AxiDraw USB ports detected.")
            else:
                print("Detected AxiDraw ports:")
                for idx, port_name in enumerate(available_ports, start=1):
                    print(f"  {idx}. {port_name}")
            return 0

        selected_ports = choose_ports(args.count, [args.port1, args.port2, args.port3, args.port4])
        resolved_ports = [resolve_port(port_value) for port_value in selected_ports]

        if args.count > 1 and len(set(resolved_ports)) != args.count:
            port_args = ", ".join(f"--port{idx}" for idx in range(1, args.count + 1))
            print(f"Selected ports are not {args.count} distinct AxiDraw devices.")
            print(f"Use distinct values for {port_args}.")
            return 1

        for idx, port_value in enumerate(selected_ports, start=1):
            print(f"Using plotter {idx}: {port_value}")

        for idx, port_value in enumerate(selected_ports, start=1):
            plotters.append((f"plotter_{idx}", build_plotter(port_value)))

        connected_ports = [connected_port_name(ad) for _, ad in plotters]
        if args.count > 1 and len(set(connected_ports)) != args.count:
            port_args = ", ".join(f"--port{idx}" for idx in range(1, args.count + 1))
            print(f"Connections resolved to fewer than {args.count} unique AxiDraw ports.")
            print(f"Pass {port_args} to target distinct machines.")
            return 1

        print(f"Lowering pens on {args.count} plotter(s)...")
        for label, ad in plotters:
            ad.pendown()
            print(f"{label}: pen down")
        pens_lowered = True

        print("Type 'u' and press Enter to raise all pens.")
        while True:
            command = input("> ").strip().lower()
            if command == "u":
                break
            print("Input not recognized. Type 'u' to raise all pens.")

        print(f"Raising pens on {args.count} plotter(s)...")
        for label, ad in plotters:
            ad.penup()
            print(f"{label}: pen up")
        pens_lowered = False

        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except EOFError:
        print("\nInput ended unexpectedly.")
        return 1
    except Exception as exc:  # pylint: disable=broad-except
        print(exc)
        return 1
    finally:
        if pens_lowered:
            for label, ad in plotters:
                try:
                    ad.penup()
                    print(f"{label}: pen up (cleanup)")
                except Exception:  # pylint: disable=broad-except
                    pass

        for _, ad in plotters:
            try:
                ad.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass


if __name__ == "__main__":
    sys.exit(main())
