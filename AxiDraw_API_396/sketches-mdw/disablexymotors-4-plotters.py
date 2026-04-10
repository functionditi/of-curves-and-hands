#!/usr/bin/env python3
# Run: python3 disablexymotors-4-plotters.py --count 4

"""
disablexymotors-4-plotters.py

Disable XY motors on one or more connected AxiDraw units.

Examples:
  python3 disablexymotors-4-plotters.py --count 2
  python3 disablexymotors-4-plotters.py --count 4 --ports "AxiDraw One" "AxiDraw Two" "AxiDraw Three" "AxiDraw Four"

Backwards compatible options:
  --port1 --port2 --port3 --port4
"""

import argparse
import sys
from typing import Optional

from plotink import ebb_motion, ebb_serial
from pyaxidraw import axidraw


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


def choose_ports(count: int, selected_ports: list[Optional[str]]) -> list[str]:
    available_ports = list_axidraw_ports()
    selected = selected_ports[:count]

    if len(selected) < count:
        selected.extend([None] * (count - len(selected)))

    resolved_selected: list[Optional[str]] = [resolve_port(port) for port in selected]

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
            found = ", ".join(available_ports) if available_ports else "none"
            raise RuntimeError(f"Need {count} AxiDraws. Found {len(available_ports)} port(s): {found}.")
        selected[idx] = candidate
        resolved_selected[idx] = candidate

    return [port for port in selected if port]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect one or more AxiDraw units and disable XY motors on all of them."
    )
    parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=4,
        help="Number of AxiDraw devices to target (default: 4).",
    )
    parser.add_argument(
        "--ports",
        nargs="*",
        default=None,
        help="Optional list of USB ports or nicknames, in order, matching --count.",
    )
    parser.add_argument(
        "--port1",
        type=str,
        default=None,
        help="USB port or USB nickname for plotter 1 (legacy option).",
    )
    parser.add_argument(
        "--port2",
        type=str,
        default=None,
        help="USB port or USB nickname for plotter 2 (legacy option).",
    )
    parser.add_argument(
        "--port3",
        type=str,
        default=None,
        help="USB port or USB nickname for plotter 3 (legacy option).",
    )
    parser.add_argument(
        "--port4",
        type=str,
        default=None,
        help="USB port or USB nickname for plotter 4 (legacy option).",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List detected AxiDraw USB ports and exit.",
    )
    return parser.parse_args()


def main() -> int:
    plotters: list[tuple[str, axidraw.AxiDraw]] = []

    try:
        args = parse_args()

        if args.count < 1:
            print("--count must be at least 1.")
            return 1

        available_ports = list_axidraw_ports()

        if args.list_ports:
            if not available_ports:
                print("No AxiDraw USB ports detected.")
            else:
                print("Detected AxiDraw ports:")
                for idx, port_name in enumerate(available_ports, start=1):
                    print(f"  {idx}. {port_name}")
            return 0

        explicit_ports: list[Optional[str]]
        if args.ports:
            if len(args.ports) > args.count:
                print("More entries were provided in --ports than --count.")
                return 1
            explicit_ports = list(args.ports)
        else:
            legacy_ports = [args.port1, args.port2, args.port3, args.port4]
            if args.count > len(legacy_ports) and any(legacy_ports):
                print("Legacy --port1..--port4 options only support up to 4 explicit ports.")
                print("Use --ports to provide more than four explicit targets.")
                return 1
            explicit_ports = legacy_ports

        selected_ports = choose_ports(args.count, explicit_ports)
        resolved_ports = [resolve_port(port) for port in selected_ports]

        if len(set(resolved_ports)) != args.count:
            print(f"Selected ports are not {args.count} distinct AxiDraw devices.")
            print("Use distinct values in --ports (or --port1..--port4).")
            return 1

        for idx, port in enumerate(selected_ports, start=1):
            print(f"Using plotter {idx}: {port}")

        for idx, port in enumerate(selected_ports, start=1):
            ad = build_plotter(port)
            plotters.append((f"plotter_{idx}", ad))

        connected_ports = [connected_port_name(ad) for _, ad in plotters]
        if len(set(connected_ports)) != args.count:
            print(f"Connections resolved to fewer than {args.count} unique AxiDraw ports.")
            print("Pass explicit --ports (or --port1..--port4) to target distinct machines.")
            return 1

        print("Disabling XY motors on selected plotters...")
        for label, ad in plotters:
            ebb_motion.sendDisableMotors(ad.plot_status.port, False)
            print(f"{label}: XY motors disabled")

        print("Done.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:  # pylint: disable=broad-except
        print(exc)
        return 1
    finally:
        for _, ad in plotters:
            try:
                ad.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass


if __name__ == "__main__":
    sys.exit(main())
