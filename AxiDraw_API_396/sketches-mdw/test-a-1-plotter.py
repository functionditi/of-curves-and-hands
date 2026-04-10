#!/usr/bin/env python3
# Run: python3 test-a-1-plotter.py

"""
test-a-1-plotter.py

Connect to an AxiDraw in interactive mode, lift/lower the pen,
and draw a 1 inch square.
"""

import sys
import time

from pyaxidraw import axidraw


def main() -> int:
    ad = axidraw.AxiDraw()
    ad.interactive()
    connected = ad.connect()

    if not connected:
        print("Could not connect to AxiDraw.")
        return 1

    try:
        ad.options.units = 0  # inches
        ad.options.speed_pendown = 25
        ad.update()

        # Start 1 inch from the home corner.
        ad.penup()
        ad.moveto(1.0, 1.0)
        time.sleep(0.25)

        # Draw a 1 inch square.
        ad.pendown()
        ad.lineto(2.0, 1.0)
        ad.lineto(2.0, 2.0)
        ad.lineto(1.0, 2.0)
        ad.lineto(1.0, 1.0)

        # Lift pen and return home.
        ad.penup()
        ad.moveto(0.0, 0.0)
    finally:
        ad.disconnect()

    return 0


if __name__ == "__main__":
    sys.exit(main())
