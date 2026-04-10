#!/usr/bin/env python3
# Run: python3 test-d-1-n-plotter-10min-dynamickolam.py -n 4

"""
test-d-1-n-plotter-10min-dynamickolam.py

Generate DFS-based kolam patterns (matching the p5.js logic), and run one or more
AxiDraw plotters for a timed session.

Draw order on hardware:
1) pulli grid markers
2) kolam lines and arcs
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    import tkinter as tk
except ImportError:
    tk = None  # type: ignore[assignment]

from plotink import ebb_serial
from pyaxidraw import axidraw

PROB_STRAIGHT = 0.45
PROB_TURN = 0.45
PROB_TERM = 0.10
CROSS_MARK_SCALE = 1.30
DEFAULT_SIZE_MIN = 3
DEFAULT_SIZE_MAX = 5
PATTERN_SEED_SCALE = 1_000_000_000_000
PLOTTER_SEED_SCALE = 1_000_000
DEFAULT_DURATION_MINUTES = 1.0


@dataclass(frozen=True)
class Dot:
    x: float
    y: float


@dataclass(frozen=True)
class Edge:
    x: int
    y: int
    reverse: bool
    vector: tuple[float, float]


@dataclass(frozen=True)
class CapArc:
    dot: Dot
    angle: float
    start: float
    stop: float


@dataclass(frozen=True)
class LineCommand:
    x1: float
    y1: float
    x2: float
    y2: float


@dataclass(frozen=True)
class ArcCommand:
    cx: float
    cy: float
    radius: float
    rotation: float
    start: float
    stop: float


@dataclass(frozen=True)
class DrawCommand:
    kind: str
    line: Optional[LineCommand] = None
    arc: Optional[ArcCommand] = None


@dataclass
class KolamPattern:
    pullis: list[Dot]
    framework: list[Edge]
    cap_arcs: list[CapArc]
    starting_points: list[int]
    commands: list[DrawCommand]
    width_px: float
    height_px: float


@dataclass
class PlotterRunResult:
    label: str
    patterns_drawn: int = 0
    error: Optional[str] = None


def approx_equal(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


class DFSKolamGenerator:
    """Port of the p5.js DFS kolam generator and decoration logic."""

    def __init__(self, size: int, spacing: float, rng: random.Random) -> None:
        self.size = size
        self.spacing = spacing
        self.rng = rng

        self.pullis: list[Dot] = []
        self.framework: list[Edge] = []
        self.cap_arcs: list[CapArc] = []
        self.starting_points: list[int] = [0]

        self.dfs_stack: list[int] = []
        self.visited: set[int] = set()
        self.last_vector: Optional[tuple[float, float]] = None

    def reset_pattern(self) -> None:
        self.pullis = []
        self.framework = []
        self.cap_arcs = []
        self.starting_points = [0]

        self.dfs_stack = []
        self.visited = set()
        self.last_vector = None

        for i in range(self.size):
            for j in range(self.size):
                self.pullis.append(Dot(i * self.spacing + self.spacing, j * self.spacing + self.spacing))

        self.visited.add(0)
        self.dfs_stack.append(0)

    def all_visited(self) -> bool:
        return len(self.visited) == len(self.pullis)

    def find_dot_by_coord(self, x: float, y: float) -> int:
        for idx, dot in enumerate(self.pullis):
            if dot.x == x and dot.y == y:
                return idx
        return -1

    def is_adjacent(self, a: Dot, b: Dot) -> bool:
        return (
            (abs(a.x - b.x) == self.spacing and a.y == b.y)
            or (a.x == b.x and abs(a.y - b.y) == self.spacing)
        )

    def get_adjacent_unvisited(self, dot: Dot) -> list[int]:
        out: list[int] = []
        for i, other in enumerate(self.pullis):
            if i not in self.visited and self.is_adjacent(dot, other):
                out.append(i)
        return out

    def add_edge(self, current_index: int, next_index: int, vector: tuple[float, float]) -> None:
        self.framework.append(Edge(current_index, next_index, False, vector))
        self.visited.add(next_index)
        self.dfs_stack.append(next_index)
        self.last_vector = vector

    def add_reverse_edge(self, current_index: int, parent_index: int, vector: tuple[float, float]) -> None:
        self.framework.append(Edge(current_index, parent_index, True, vector))
        self.last_vector = vector

    def extend_random(self, current_index: int) -> None:
        current_dot = self.pullis[current_index]
        neighbors = self.get_adjacent_unvisited(current_dot)
        if neighbors:
            next_index = self.rng.choice(neighbors)
            next_dot = self.pullis[next_index]
            dx = (next_dot.x - current_dot.x) / self.spacing
            dy = (next_dot.y - current_dot.y) / self.spacing
            self.add_edge(current_index, next_index, (dx, dy))
        else:
            self.terminate_branch()

    def extend_same_direction(self) -> None:
        if not self.dfs_stack:
            return

        current_index = self.dfs_stack[-1]
        current_dot = self.pullis[current_index]

        if self.last_vector is None:
            self.extend_random(current_index)
            return

        candidate_x = current_dot.x + self.last_vector[0] * self.spacing
        candidate_y = current_dot.y + self.last_vector[1] * self.spacing
        candidate_index = self.find_dot_by_coord(candidate_x, candidate_y)

        if candidate_index != -1 and candidate_index not in self.visited:
            self.add_edge(current_index, candidate_index, self.last_vector)
        else:
            self.extend_random(current_index)

    def extend_turn(self) -> None:
        if not self.dfs_stack:
            return

        current_index = self.dfs_stack[-1]
        current_dot = self.pullis[current_index]

        if self.last_vector is None:
            self.extend_random(current_index)
            return

        turn_left = self.rng.random() < 0.5
        if turn_left:
            vec = (-self.last_vector[1], self.last_vector[0])
        else:
            vec = (self.last_vector[1], -self.last_vector[0])

        candidate_x = current_dot.x + vec[0] * self.spacing
        candidate_y = current_dot.y + vec[1] * self.spacing
        candidate_index = self.find_dot_by_coord(candidate_x, candidate_y)

        if candidate_index != -1 and candidate_index not in self.visited:
            self.add_edge(current_index, candidate_index, vec)
            return

        if turn_left:
            vec = (self.last_vector[1], -self.last_vector[0])
        else:
            vec = (-self.last_vector[1], self.last_vector[0])

        candidate_x = current_dot.x + vec[0] * self.spacing
        candidate_y = current_dot.y + vec[1] * self.spacing
        candidate_index = self.find_dot_by_coord(candidate_x, candidate_y)

        if candidate_index != -1 and candidate_index not in self.visited:
            self.add_edge(current_index, candidate_index, vec)
        else:
            self.extend_random(current_index)

    def terminate_branch(self) -> None:
        if not self.dfs_stack:
            unvisited = [i for i in range(len(self.pullis)) if i not in self.visited]
            if unvisited:
                new_source = self.rng.choice(unvisited)
                self.visited.add(new_source)
                self.dfs_stack.append(new_source)
                self.starting_points.append(new_source)
            return

        current_index = self.dfs_stack[-1]
        current_dot = self.pullis[current_index]

        r_angle = 0.0
        if self.last_vector is not None:
            r_angle = math.atan2(self.last_vector[1], self.last_vector[0]) + math.pi

        stop = (9 * math.pi) / 4 if len(self.dfs_stack) == 1 else (7 * math.pi) / 4
        self.cap_arcs.append(CapArc(dot=current_dot, angle=r_angle, start=math.pi / 4, stop=stop))

        while len(self.dfs_stack) > 1:
            child_index = self.dfs_stack.pop()
            parent_index = self.dfs_stack[-1]
            child_dot = self.pullis[child_index]
            parent_dot = self.pullis[parent_index]

            reverse_dx = (parent_dot.x - child_dot.x) / self.spacing
            reverse_dy = (parent_dot.y - child_dot.y) / self.spacing
            self.add_reverse_edge(child_index, parent_index, (reverse_dx, reverse_dy))

        self.dfs_stack = []
        self.last_vector = None

        unvisited = [i for i in range(len(self.pullis)) if i not in self.visited]
        if unvisited:
            new_source = self.rng.choice(unvisited)
            self.visited.add(new_source)
            self.dfs_stack.append(new_source)
            self.starting_points.append(new_source)

    def generate(self) -> KolamPattern:
        self.reset_pattern()

        while True:
            if self.all_visited():
                if self.dfs_stack:
                    self.terminate_branch()
                else:
                    break
                continue

            if not self.dfs_stack:
                self.terminate_branch()
                continue

            r = self.rng.random()
            if r < PROB_STRAIGHT:
                self.extend_same_direction()
            elif r < PROB_STRAIGHT + PROB_TURN:
                self.extend_turn()
            else:
                self.terminate_branch()

        if self.all_visited():
            while self.dfs_stack:
                self.terminate_branch()

        commands = self.render_commands()
        canvas_side = (self.size + 1) * self.spacing

        return KolamPattern(
            pullis=list(self.pullis),
            framework=list(self.framework),
            cap_arcs=list(self.cap_arcs),
            starting_points=list(self.starting_points),
            commands=commands,
            width_px=canvas_side,
            height_px=canvas_side,
        )

    def render_commands(self) -> list[DrawCommand]:
        commands: list[DrawCommand] = []
        angle_array: list[float] = []
        for edge in self.framework:
            dot1 = self.pullis[edge.x]
            dot2 = self.pullis[edge.y]
            r_angle = math.atan2(dot2.y - dot1.y, dot2.x - dot1.x)
            angle_array.append(math.degrees(r_angle))

        framework_index = 0
        cap_arc_index = 0

        for branch_index, start_index in enumerate(self.starting_points):
            if commands:
                commands.append(DrawCommand(kind="break"))

            while framework_index < len(self.framework):
                edge = self.framework[framework_index]
                if edge.x in self.starting_points and edge.x != start_index:
                    break
                if edge.x != start_index and framework_index == 0:
                    break
                if edge.x != start_index and framework_index > 0:
                    previous_edge = self.framework[framework_index - 1]
                    if previous_edge.x in self.starting_points and previous_edge.x != start_index:
                        break

                dot1 = self.pullis[edge.x]
                dot2 = self.pullis[edge.y]
                mid_x = (dot1.x + dot2.x) / 2
                mid_y = (dot1.y + dot2.y) / 2
                r_angle = math.atan2(dot2.y - dot1.y, dot2.x - dot1.x)
                angle_deg = angle_array[framework_index]

                if edge.x == start_index:
                    line = self.draw_diagonal_by_angle(angle_deg, mid_x, mid_y, reverse=True)
                    commands.append(DrawCommand(kind="line", line=line))
                    commands.append(
                        DrawCommand(
                            kind="arc",
                            arc=self.loop_arc(dot1, r_angle, math.pi / 4, (7 * math.pi) / 4),
                        )
                    )
                else:
                    prev_a = angle_array[framework_index - 1]
                    a_diff = angle_deg - prev_a

                    if framework_index % 2 == 1:
                        if not (approx_equal(a_diff, 90.0) or approx_equal(a_diff, -270.0)):
                            commands.extend(self.apply_loops(a_diff, r_angle, dot1))
                        line = self.draw_diagonal_by_angle(angle_deg, mid_x, mid_y, reverse=False)
                        commands.append(DrawCommand(kind="line", line=line))
                    else:
                        if approx_equal(a_diff, 90.0) or approx_equal(a_diff, -270.0):
                            commands.extend(self.apply_loops(a_diff, r_angle, dot1))
                        elif approx_equal(a_diff, 0.0):
                            commands.extend(self.apply_loops(0.0, r_angle + math.pi, dot1))
                        line = self.draw_diagonal_by_angle(angle_deg, mid_x, mid_y, reverse=True)
                        commands.append(DrawCommand(kind="line", line=line))

                framework_index += 1
                if framework_index >= len(self.framework):
                    break
                next_edge = self.framework[framework_index]
                if next_edge.x in self.starting_points:
                    break

            if cap_arc_index < len(self.cap_arcs):
                cap_info = self.cap_arcs[cap_arc_index]
                commands.append(
                    DrawCommand(
                        kind="arc",
                        arc=self.loop_arc(cap_info.dot, cap_info.angle, cap_info.start, cap_info.stop),
                    )
                )
                cap_arc_index += 1

        return commands

    def draw_diagonal_by_angle(
        self, angle_deg: float, mid_x: float, mid_y: float, reverse: bool = False
    ) -> LineCommand:
        if approx_equal(angle_deg, 90.0) or approx_equal(angle_deg, -90.0):
            angle = (3 * math.pi) / 4 if reverse else math.pi / 4
        else:
            angle = math.pi / 4 if reverse else (3 * math.pi) / 4

        line_len = self.spacing * 0.33
        x1 = mid_x - math.cos(angle) * line_len
        y1 = mid_y - math.sin(angle) * line_len
        x2 = mid_x + math.cos(angle) * line_len
        y2 = mid_y + math.sin(angle) * line_len
        return LineCommand(x1=x1, y1=y1, x2=x2, y2=y2)

    def loop_arc(self, dot: Dot, angle: float, start: float, stop: float) -> ArcCommand:
        radius = (self.spacing * 0.66) / 2.0
        return ArcCommand(cx=dot.x, cy=dot.y, radius=radius, rotation=angle, start=start, stop=stop)

    def apply_loops(self, a_diff: float, r_angle: float, dot: Dot) -> list[DrawCommand]:
        if approx_equal(a_diff, 0.0):
            return [
                DrawCommand(
                    kind="arc",
                    arc=self.loop_arc(dot, r_angle, math.pi / 4, (3 * math.pi) / 4),
                )
            ]

        if approx_equal(a_diff, -90.0) or approx_equal(a_diff, 270.0):
            return [
                DrawCommand(
                    kind="arc",
                    arc=self.loop_arc(dot, r_angle, math.pi / 4, (5 * math.pi) / 4),
                )
            ]

        if approx_equal(a_diff, 90.0) or approx_equal(a_diff, -270.0):
            return [
                DrawCommand(
                    kind="arc",
                    arc=self.loop_arc(dot, r_angle + math.pi / 2, math.pi / 4, (5 * math.pi) / 4),
                )
            ]

        return []


def sample_arc_points(arc: ArcCommand, min_segments: int = 16) -> list[tuple[float, float]]:
    span = abs(arc.stop - arc.start)
    segments = max(min_segments, int(math.ceil(span / (math.pi / 18))))

    points: list[tuple[float, float]] = []
    for i in range(segments + 1):
        theta = arc.start + (arc.stop - arc.start) * (i / segments)
        local_x = arc.radius * math.cos(theta)
        local_y = arc.radius * math.sin(theta)

        rotated_x = math.cos(arc.rotation) * local_x - math.sin(arc.rotation) * local_y
        rotated_y = math.sin(arc.rotation) * local_x + math.cos(arc.rotation) * local_y

        points.append((arc.cx + rotated_x, arc.cy + rotated_y))

    return points


class PreviewWindow:
    def __init__(self) -> None:
        self.margin = 24
        self.closed = False
        self.root = None
        self.canvas = None
        self.label = None

        if tk is None:
            print("Tkinter is not available; skipping preview window.")
            self.closed = True
            return

        self.root = tk.Tk()
        self.root.title("Dynamic Kolam Preview")
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.canvas = tk.Canvas(self.root, width=640, height=640, bg="white", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.label = tk.Label(
            self.root,
            text="Preview window is live. Plotting starts without closing this window.",
            anchor="w",
            padx=8,
            pady=6,
        )
        self.label.pack(fill=tk.X)
        self.pump_events()

    def show_pattern(self, pattern: KolamPattern, title: str) -> None:
        if self.root is None or self.canvas is None or self.closed:
            return

        width = int(math.ceil(pattern.width_px + self.margin * 2))
        height = int(math.ceil(pattern.height_px + self.margin * 2))

        self.root.title(title)
        self.canvas.config(width=width, height=height)
        self.canvas.delete("all")

        for dot in pattern.pullis:
            r = 2.5
            self.canvas.create_oval(
                dot.x - r + self.margin,
                dot.y - r + self.margin,
                dot.x + r + self.margin,
                dot.y + r + self.margin,
                fill="black",
                outline="",
            )

        for command in pattern.commands:
            if command.kind == "line" and command.line is not None:
                line = command.line
                self.canvas.create_line(
                    line.x1 + self.margin,
                    line.y1 + self.margin,
                    line.x2 + self.margin,
                    line.y2 + self.margin,
                    fill="#0048FF",
                    width=1,
                )
            elif command.kind == "arc" and command.arc is not None:
                pts = sample_arc_points(command.arc)
                flat_points: list[float] = []
                for x, y in pts:
                    flat_points.append(x + self.margin)
                    flat_points.append(y + self.margin)
                self.canvas.create_line(*flat_points, fill="#0048FF", width=1)

        if self.label is not None:
            self.label.config(text=title)
        self.pump_events()

    def pump_events(self) -> None:
        if self.root is None or self.closed:
            return
        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:  # pylint: disable=broad-except
            self.closed = True
            self.root = None
            self.canvas = None
            self.label = None

    def wait_until_closed(self) -> None:
        while not self.closed and self.root is not None:
            self.pump_events()
            time.sleep(0.03)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:  # pylint: disable=broad-except
                pass
        self.root = None
        self.canvas = None
        self.label = None


def pump_preview(preview: Optional[PreviewWindow]) -> None:
    if preview is not None:
        preview.pump_events()


def wait_with_preview(preview: Optional[PreviewWindow], duration_seconds: float) -> None:
    end_time = time.monotonic() + max(0.0, duration_seconds)
    while time.monotonic() < end_time:
        pump_preview(preview)
        time.sleep(0.03)


def pixels_to_inches(px: float, pixels_per_inch: float) -> float:
    return px / pixels_per_inch


def to_plotter_xy(
    x_px: float,
    y_px: float,
    pixels_per_inch: float,
    x_offset_in: float,
    y_offset_in: float,
) -> tuple[float, float]:
    return (
        x_offset_in + pixels_to_inches(x_px, pixels_per_inch),
        y_offset_in + pixels_to_inches(y_px, pixels_per_inch),
    )


def list_axidraw_ports() -> list[str]:
    ports = ebb_serial.listEBBports() or []
    result: list[str] = []
    for entry in ports:
        device = getattr(entry, "device", None)
        if device is None:
            device = entry[0]
        result.append(str(device))
    return result


def connected_port_name(ad: axidraw.AxiDraw) -> str:
    port_obj = ad.plot_status.port
    return str(getattr(port_obj, "port", port_obj))


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


def build_plotter(
    port: Optional[str], speed_pendown: int, speed_penup: int, accel: int
) -> axidraw.AxiDraw:
    ad = axidraw.AxiDraw()
    ad.interactive()
    if port:
        ad.options.port = port

    if not ad.connect():
        raise RuntimeError(f"Could not connect to AxiDraw ({port or 'first available'}).")

    ad.options.units = 0  # inches
    ad.options.speed_pendown = speed_pendown
    ad.options.speed_penup = speed_penup
    ad.options.accel = accel
    ad.options.home_after = False
    ad.update()

    return ad


def draw_pulli_markers(
    ad: axidraw.AxiDraw,
    pullis: list[Dot],
    pixels_per_inch: float,
    x_offset_in: float,
    y_offset_in: float,
    mark_px: float,
    dot_style: str,
    preview: Optional[PreviewWindow] = None,
) -> None:
    half = mark_px / 2.0
    cross_half = half * CROSS_MARK_SCALE

    for dot in pullis:
        if dot_style == "circle":
            segments = 18
            points: list[tuple[float, float]] = []
            for i in range(segments + 1):
                theta = (2.0 * math.pi * i) / segments
                px = dot.x + half * math.cos(theta)
                py = dot.y + half * math.sin(theta)
                points.append((px, py))

            start_x, start_y = to_plotter_xy(
                points[0][0], points[0][1], pixels_per_inch, x_offset_in, y_offset_in
            )
            ad.penup()
            ad.moveto(start_x, start_y)
            ad.pendown()
            for px, py in points[1:]:
                tx, ty = to_plotter_xy(px, py, pixels_per_inch, x_offset_in, y_offset_in)
                ad.lineto(tx, ty)
            ad.penup()
            pump_preview(preview)
            continue

        marker_half = cross_half if dot_style == "cross" else half
        x1, y1 = to_plotter_xy(dot.x - marker_half, dot.y, pixels_per_inch, x_offset_in, y_offset_in)
        x2, y2 = to_plotter_xy(dot.x + marker_half, dot.y, pixels_per_inch, x_offset_in, y_offset_in)

        ad.penup()
        ad.moveto(x1, y1)
        ad.pendown()
        ad.lineto(x2, y2)
        ad.penup()

        if dot_style == "cross":
            x3, y3 = to_plotter_xy(
                dot.x, dot.y - marker_half, pixels_per_inch, x_offset_in, y_offset_in
            )
            x4, y4 = to_plotter_xy(
                dot.x, dot.y + marker_half, pixels_per_inch, x_offset_in, y_offset_in
            )
            ad.moveto(x3, y3)
            ad.pendown()
            ad.lineto(x4, y4)
            ad.penup()

        pump_preview(preview)


def draw_line_command(
    ad: axidraw.AxiDraw,
    command: LineCommand,
    pixels_per_inch: float,
    x_offset_in: float,
    y_offset_in: float,
    move_to_start: bool = True,
    lift_after: bool = True,
) -> None:
    sx, sy = to_plotter_xy(command.x1, command.y1, pixels_per_inch, x_offset_in, y_offset_in)
    ex, ey = to_plotter_xy(command.x2, command.y2, pixels_per_inch, x_offset_in, y_offset_in)

    if move_to_start:
        ad.penup()
        ad.moveto(sx, sy)
        ad.pendown()
    else:
        ad.lineto(sx, sy)
    ad.lineto(ex, ey)
    if lift_after:
        ad.penup()


def draw_arc_command(
    ad: axidraw.AxiDraw,
    command: ArcCommand,
    pixels_per_inch: float,
    x_offset_in: float,
    y_offset_in: float,
    arc_segments_min: int,
    move_to_start: bool = True,
    lift_after: bool = True,
) -> None:
    points = sample_arc_points(command, min_segments=arc_segments_min)
    if not points:
        return

    start_x, start_y = to_plotter_xy(
        points[0][0], points[0][1], pixels_per_inch, x_offset_in, y_offset_in
    )
    if move_to_start:
        ad.penup()
        ad.moveto(start_x, start_y)
        ad.pendown()
    else:
        ad.lineto(start_x, start_y)

    for x_px, y_px in points[1:]:
        x_in, y_in = to_plotter_xy(x_px, y_px, pixels_per_inch, x_offset_in, y_offset_in)
        ad.lineto(x_in, y_in)

    if lift_after:
        ad.penup()


def pattern_bounds(pattern: KolamPattern) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []

    for dot in pattern.pullis:
        xs.append(dot.x)
        ys.append(dot.y)

    for cmd in pattern.commands:
        if cmd.kind == "line" and cmd.line is not None:
            xs.extend([cmd.line.x1, cmd.line.x2])
            ys.extend([cmd.line.y1, cmd.line.y2])
        elif cmd.kind == "arc" and cmd.arc is not None:
            points = sample_arc_points(cmd.arc)
            for x, y in points:
                xs.append(x)
                ys.append(y)

    return min(xs), min(ys), max(xs), max(ys)


def draw_pattern(
    ad: axidraw.AxiDraw,
    pattern: KolamPattern,
    pixels_per_inch: float,
    x_offset_in: float,
    y_offset_in: float,
    dot_mark_px: float,
    dot_style: str,
    arc_segments_min: int,
    preview: Optional[PreviewWindow] = None,
    label: str = "",
) -> None:
    prefix = f"[{label}] " if label else ""
    print(f"{prefix}Drawing pulli grid markers...")
    draw_pulli_markers(
        ad,
        pattern.pullis,
        pixels_per_inch=pixels_per_inch,
        x_offset_in=x_offset_in,
        y_offset_in=y_offset_in,
        mark_px=dot_mark_px,
        dot_style=dot_style,
        preview=preview,
    )

    print(f"{prefix}Drawing kolam strokes...")
    move_to_start = True
    for cmd in pattern.commands:
        if cmd.kind == "break":
            ad.penup()
            move_to_start = True
            pump_preview(preview)
            continue

        if cmd.kind == "line" and cmd.line is not None:
            draw_line_command(
                ad,
                cmd.line,
                pixels_per_inch=pixels_per_inch,
                x_offset_in=x_offset_in,
                y_offset_in=y_offset_in,
                move_to_start=move_to_start,
                lift_after=False,
            )
            move_to_start = False
        elif cmd.kind == "arc" and cmd.arc is not None:
            draw_arc_command(
                ad,
                cmd.arc,
                pixels_per_inch=pixels_per_inch,
                x_offset_in=x_offset_in,
                y_offset_in=y_offset_in,
                arc_segments_min=arc_segments_min,
                move_to_start=move_to_start,
                lift_after=False,
            )
            move_to_start = False
        pump_preview(preview)

    ad.penup()
    ad.moveto(0.0, 0.0)
    pump_preview(preview)


def normalize_size_bounds(args: argparse.Namespace) -> tuple[int, int]:
    if args.size is not None:
        return args.size, args.size
    return args.size_min, args.size_max


def run_single_pattern(
    label: str,
    plotter_index: int,
    current_count: int,
    session_seed: int,
    session_start: float,
    session_deadline: float,
    size_min: int,
    size_max: int,
    spacing: float,
    pixels_per_inch: float,
    x_offset_in: float,
    y_offset_in: float,
    dot_mark_px: float,
    dot_style: str,
    arc_segments: int,
    ad: Optional[axidraw.AxiDraw],
    preview: Optional[PreviewWindow],
    preview_only: bool,
) -> int:
    if time.monotonic() >= session_deadline:
        return current_count

    pattern_count = current_count + 1
    pattern_seed = (
        session_seed * PATTERN_SEED_SCALE
        + (plotter_index + 1) * PLOTTER_SEED_SCALE
        + pattern_count
    )
    pattern_rng = random.Random(pattern_seed)
    size = pattern_rng.randint(size_min, size_max)

    generator = DFSKolamGenerator(size=size, spacing=spacing, rng=pattern_rng)
    pattern = generator.generate()

    print("")
    print(f"[{label}] Pattern {pattern_count} seed: {pattern_seed} | size: {size}x{size}")
    print(f"[{label}] Framework edges: {len(pattern.framework)} | Commands: {len(pattern.commands)}")

    min_x, min_y, max_x, max_y = pattern_bounds(pattern)
    width_in = (max_x - min_x) / pixels_per_inch
    height_in = (max_y - min_y) / pixels_per_inch
    print(f"[{label}] Approx pattern bounds: {width_in:.2f} in x {height_in:.2f} in")

    if preview is not None:
        preview_title = f"{label} | Pattern {pattern_count} | seed {pattern_seed} | {size}x{size}"
        preview.show_pattern(pattern, preview_title)

    if preview_only:
        remaining = max(0.0, session_deadline - time.monotonic())
        wait_with_preview(preview, min(2.0, remaining))
        return pattern_count

    if ad is None:
        raise RuntimeError(f"[{label}] AxiDraw connection unexpectedly missing.")

    draw_pattern(
        ad,
        pattern,
        pixels_per_inch=pixels_per_inch,
        x_offset_in=x_offset_in,
        y_offset_in=y_offset_in,
        dot_mark_px=dot_mark_px,
        dot_style=dot_style,
        arc_segments_min=arc_segments,
        preview=preview,
        label=label,
    )

    elapsed_minutes = (time.monotonic() - session_start) / 60.0
    remaining_minutes = max(0.0, (session_deadline - time.monotonic()) / 60.0)
    print(
        f"[{label}] Pattern {pattern_count} complete. Elapsed: {elapsed_minutes:.2f} min | "
        f"Remaining: {remaining_minutes:.2f} min"
    )
    return pattern_count


def run_plotter_session(
    label: str,
    plotter_index: int,
    session_seed: int,
    session_start: float,
    session_deadline: float,
    size_min: int,
    size_max: int,
    spacing: float,
    pixels_per_inch: float,
    x_offset_in: float,
    y_offset_in: float,
    dot_mark_px: float,
    dot_style: str,
    arc_segments: int,
    ad: Optional[axidraw.AxiDraw],
    preview: Optional[PreviewWindow],
    preview_only: bool,
    stop_event: Optional[threading.Event] = None,
    start_event: Optional[threading.Event] = None,
) -> int:
    if start_event is not None:
        start_event.wait()

    pattern_count = 0
    while time.monotonic() < session_deadline:
        if stop_event is not None and stop_event.is_set():
            break

        next_count = run_single_pattern(
            label=label,
            plotter_index=plotter_index,
            current_count=pattern_count,
            session_seed=session_seed,
            session_start=session_start,
            session_deadline=session_deadline,
            size_min=size_min,
            size_max=size_max,
            spacing=spacing,
            pixels_per_inch=pixels_per_inch,
            x_offset_in=x_offset_in,
            y_offset_in=y_offset_in,
            dot_mark_px=dot_mark_px,
            dot_style=dot_style,
            arc_segments=arc_segments,
            ad=ad,
            preview=preview,
            preview_only=preview_only,
        )
        if next_count == pattern_count:
            break
        pattern_count = next_count

    return pattern_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate dynamic DFS kolam patterns for a timed session, "
            "and draw them on one or more AxiDraw plotters in parallel. "
            "When --count is 4, the scheduler alternates 1<->3 and 2<->4 by kolam."
        )
    )
    parser.add_argument(
        "--count",
        "-n",
        type=int,
        default=1,
        help="Number of AxiDraw plotters to run in parallel (default: 1).",
    )
    parser.add_argument(
        "--ports",
        nargs="*",
        default=None,
        help="Optional list of USB ports or nicknames, in order, matching --count.",
    )
    parser.add_argument(
        "--port",
        type=str,
        default=None,
        help="Legacy single-plotter port option (same as first item in --ports).",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=None,
        help="Fixed grid size (dots per row). Overrides --size-min/--size-max.",
    )
    parser.add_argument(
        "--size-min",
        type=int,
        default=DEFAULT_SIZE_MIN,
        help=f"Minimum grid size (dots per row, default: {DEFAULT_SIZE_MIN}).",
    )
    parser.add_argument(
        "--size-max",
        type=int,
        default=DEFAULT_SIZE_MAX,
        help=f"Maximum grid size (dots per row, default: {DEFAULT_SIZE_MAX}).",
    )
    parser.add_argument("--spacing", type=float, default=50.0, help="Dot spacing in px (default: 50).")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Session random seed; pattern seeds are derived from this (default: random).",
    )
    parser.add_argument(
        "--duration-minutes",
        type=float,
        default=DEFAULT_DURATION_MINUTES,
        help=(
            "Session duration in minutes; generate and draw new kolams until this time is reached "
            f"(default: {DEFAULT_DURATION_MINUTES:.0f})."
        ),
    )
    parser.add_argument(
        "--pixels-per-inch",
        type=float,
        default=100.0,
        help="Model px to inches conversion for plotting (default: 100).",
    )
    parser.add_argument(
        "--x-offset",
        type=float,
        default=1.0,
        help="Plot origin X offset in inches (default: 1.0).",
    )
    parser.add_argument(
        "--y-offset",
        type=float,
        default=1.0,
        help="Plot origin Y offset in inches (default: 1.0).",
    )
    parser.add_argument(
        "--dot-mark-px",
        type=float,
        default=3.0,
        help="Pulli marker size in px on the AxiDraw (default: 3.0).",
    )
    parser.add_argument(
        "--dot-style",
        choices=["circle", "dash", "cross"],
        default="cross",
        help="Pulli marker style on hardware (default: cross).",
    )
    parser.add_argument(
        "--arc-segments",
        type=int,
        default=16,
        help="Minimum segments for arc polyline approximation (default: 16).",
    )
    parser.add_argument(
        "--speed-pendown",
        type=int,
        default=35,
        help="AxiDraw pen-down speed percentage (default: 35).",
    )
    parser.add_argument(
        "--speed-penup",
        type=int,
        default=75,
        help="AxiDraw pen-up speed percentage (default: 75).",
    )
    parser.add_argument(
        "--accel",
        type=int,
        default=75,
        help="AxiDraw acceleration percentage (default: 75).",
    )
    parser.add_argument(
        "--preview-only",
        action="store_true",
        help="Generate + preview only for the session duration; available only when --count=1.",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Skip preview window and go straight to plotting (forced for --count > 1).",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List detected AxiDraw USB ports and exit.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    preview: Optional[PreviewWindow] = None
    plotters: list[tuple[str, axidraw.AxiDraw]] = []
    active_threads: list[threading.Thread] = []
    active_stop_event: Optional[threading.Event] = None
    active_start_event: Optional[threading.Event] = None

    try:
        if args.list_ports:
            ports = list_axidraw_ports()
            if not ports:
                print("No AxiDraw USB ports detected.")
            else:
                print("Detected AxiDraw ports:")
                for idx, port_name in enumerate(ports, start=1):
                    print(f"  {idx}. {port_name}")
            return 0

        if args.count < 1:
            print("--count must be at least 1.")
            return 1
        if args.duration_minutes <= 0:
            print("--duration-minutes must be > 0.")
            return 1
        if args.arc_segments < 1:
            print("--arc-segments must be at least 1.")
            return 1

        size_min, size_max = normalize_size_bounds(args)
        if size_min < 1 or size_max < 1:
            print("Grid sizes must be at least 1.")
            return 1
        if size_min > size_max:
            print("--size-min must be <= --size-max.")
            return 1

        if args.preview_only and args.count != 1:
            print("--preview-only currently supports only --count 1.")
            return 1
        if args.count > 1 and not args.no_preview:
            print("Preview is only supported for one plotter. Continuing with --no-preview.")
            args.no_preview = True

        session_seed = args.seed if args.seed is not None else random.randint(1, 10_000_000)
        duration_seconds = max(0.0, args.duration_minutes * 60.0)
        session_start = time.monotonic()
        session_deadline = session_start + duration_seconds

        print(f"Session seed: {session_seed}")
        print(f"Session duration target: {args.duration_minutes:.2f} minute(s)")
        print(f"Grid size range: {size_min} to {size_max} dots per row")
        print(f"Target plotters: {args.count}")

        if args.preview_only:
            preview = None if args.no_preview else PreviewWindow()
            pattern_count = run_plotter_session(
                label="plotter_1",
                plotter_index=0,
                session_seed=session_seed,
                session_start=session_start,
                session_deadline=session_deadline,
                size_min=size_min,
                size_max=size_max,
                spacing=args.spacing,
                pixels_per_inch=args.pixels_per_inch,
                x_offset_in=args.x_offset,
                y_offset_in=args.y_offset,
                dot_mark_px=args.dot_mark_px,
                dot_style=args.dot_style,
                arc_segments=args.arc_segments,
                ad=None,
                preview=preview,
                preview_only=True,
            )
            total_elapsed = (time.monotonic() - session_start) / 60.0
            print("")
            print(
                f"Session complete. Patterns generated: {pattern_count}. "
                f"Elapsed: {total_elapsed:.2f} minute(s)."
            )
            if preview is not None:
                print("Close the preview window when you are ready to exit.")
                preview.wait_until_closed()
            return 0

        explicit_ports: list[Optional[str]]
        if args.ports:
            if len(args.ports) > args.count:
                print("More entries were provided in --ports than --count.")
                return 1
            explicit_ports = list(args.ports)
        else:
            explicit_ports = [args.port] if args.port else []

        selected_ports = choose_ports(args.count, explicit_ports)
        resolved_ports = [resolve_port(port) for port in selected_ports]
        if len(set(resolved_ports)) != args.count:
            print(f"Selected ports are not {args.count} distinct AxiDraw devices.")
            print("Use distinct values in --ports.")
            return 1

        for idx, port in enumerate(selected_ports, start=1):
            print(f"Using plotter {idx}: {port}")

        for idx, port in enumerate(selected_ports, start=1):
            ad = build_plotter(
                port=port,
                speed_pendown=args.speed_pendown,
                speed_penup=args.speed_penup,
                accel=args.accel,
            )
            label = f"plotter_{idx}"
            connected_port = connected_port_name(ad)
            print(f"[{label}] connected on {connected_port}")
            plotters.append((label, ad))

        connected_ports = [connected_port_name(ad) for _, ad in plotters]
        if len(set(connected_ports)) != args.count:
            raise RuntimeError(
                f"Connections resolved to fewer than {args.count} unique AxiDraw ports. "
                "Pass explicit --ports to target distinct machines."
            )

        if args.count == 1:
            preview = None if args.no_preview else PreviewWindow()
            label, ad = plotters[0]
            pattern_count = run_plotter_session(
                label=label,
                plotter_index=0,
                session_seed=session_seed,
                session_start=session_start,
                session_deadline=session_deadline,
                size_min=size_min,
                size_max=size_max,
                spacing=args.spacing,
                pixels_per_inch=args.pixels_per_inch,
                x_offset_in=args.x_offset,
                y_offset_in=args.y_offset,
                dot_mark_px=args.dot_mark_px,
                dot_style=args.dot_style,
                arc_segments=args.arc_segments,
                ad=ad,
                preview=preview,
                preview_only=False,
            )
            total_elapsed = (time.monotonic() - session_start) / 60.0
            print("")
            print(
                f"Session complete. Patterns generated: {pattern_count}. "
                f"Elapsed: {total_elapsed:.2f} minute(s)."
            )
            return 0

        def run_phase(phase_name: str, subset_indices: list[int], phase_deadline: float) -> list[PlotterRunResult]:
            nonlocal active_threads
            nonlocal active_start_event
            nonlocal active_stop_event

            results = [PlotterRunResult(label=plotters[idx][0]) for idx in subset_indices]
            active_threads = []
            active_stop_event = threading.Event()
            active_start_event = threading.Event()

            print(f"[schedule] Starting {phase_name} with {len(subset_indices)} plotter(s).")

            def worker(result: PlotterRunResult, idx: int, ad: axidraw.AxiDraw) -> None:
                try:
                    result.patterns_drawn = run_plotter_session(
                        label=result.label,
                        plotter_index=idx,
                        session_seed=session_seed,
                        session_start=session_start,
                        session_deadline=phase_deadline,
                        size_min=size_min,
                        size_max=size_max,
                        spacing=args.spacing,
                        pixels_per_inch=args.pixels_per_inch,
                        x_offset_in=args.x_offset,
                        y_offset_in=args.y_offset,
                        dot_mark_px=args.dot_mark_px,
                        dot_style=args.dot_style,
                        arc_segments=args.arc_segments,
                        ad=ad,
                        preview=None,
                        preview_only=False,
                        stop_event=active_stop_event,
                        start_event=active_start_event,
                    )
                except Exception as exc:  # pylint: disable=broad-except
                    result.error = str(exc)
                    if active_stop_event is not None:
                        active_stop_event.set()
                    print(f"[{result.label}] Error: {result.error}")

            for result, idx in zip(results, subset_indices):
                _, ad = plotters[idx]
                thread = threading.Thread(target=worker, args=(result, idx, ad), daemon=True)
                active_threads.append(thread)
                thread.start()

            if active_start_event is not None:
                active_start_event.set()
            for thread in active_threads:
                thread.join()

            active_threads = []
            active_stop_event = None
            active_start_event = None
            return results

        if args.count == 4:
            nonlocal_results: dict[int, PlotterRunResult] = {
                idx: PlotterRunResult(label=plotters[idx][0]) for idx in range(4)
            }
            lane_sequences: list[tuple[str, list[int]]] = [
                ("lane_A", [0, 2]),  # plotter_1, then plotter_3, repeat
                ("lane_B", [1, 3]),  # plotter_2, then plotter_4, repeat
            ]
            active_threads = []
            active_stop_event = threading.Event()
            active_start_event = threading.Event()

            print("[schedule] 4-plotter alternating mode enabled.")
            print("[schedule] lane_A: plotter_1 -> plotter_3 -> repeat")
            print("[schedule] lane_B: plotter_2 -> plotter_4 -> repeat")

            def lane_worker(lane_name: str, sequence: list[int]) -> None:
                turn = 0
                if active_start_event is not None:
                    active_start_event.wait()

                while time.monotonic() < session_deadline:
                    if active_stop_event is not None and active_stop_event.is_set():
                        break

                    idx = sequence[turn % len(sequence)]
                    label, ad = plotters[idx]
                    result = nonlocal_results[idx]

                    try:
                        next_count = run_single_pattern(
                            label=label,
                            plotter_index=idx,
                            current_count=result.patterns_drawn,
                            session_seed=session_seed,
                            session_start=session_start,
                            session_deadline=session_deadline,
                            size_min=size_min,
                            size_max=size_max,
                            spacing=args.spacing,
                            pixels_per_inch=args.pixels_per_inch,
                            x_offset_in=args.x_offset,
                            y_offset_in=args.y_offset,
                            dot_mark_px=args.dot_mark_px,
                            dot_style=args.dot_style,
                            arc_segments=args.arc_segments,
                            ad=ad,
                            preview=None,
                            preview_only=False,
                        )
                    except Exception as exc:  # pylint: disable=broad-except
                        result.error = str(exc)
                        if active_stop_event is not None:
                            active_stop_event.set()
                        print(f"[{lane_name}] [{label}] Error: {result.error}")
                        return

                    if next_count == result.patterns_drawn:
                        break

                    result.patterns_drawn = next_count
                    turn += 1

            for lane_name, sequence in lane_sequences:
                thread = threading.Thread(target=lane_worker, args=(lane_name, sequence), daemon=True)
                active_threads.append(thread)
                thread.start()

            if active_start_event is not None:
                active_start_event.set()
            for thread in active_threads:
                thread.join()

            active_threads = []
            active_stop_event = None
            active_start_event = None
            results = [nonlocal_results[idx] for idx in range(4)]
        else:
            results = run_phase("single phase", list(range(args.count)), session_deadline)

        errors = [result for result in results if result.error]
        total_elapsed = (time.monotonic() - session_start) / 60.0

        print("")
        if errors:
            print("Session ended with errors:")
            for result in errors:
                print(f"  {result.label}: {result.error}")
            return 1

        total_patterns = sum(result.patterns_drawn for result in results)
        print(
            f"Session complete. Total patterns generated across {args.count} plotters: "
            f"{total_patterns}. Elapsed: {total_elapsed:.2f} minute(s)."
        )
        for result in results:
            print(f"  {result.label}: {result.patterns_drawn} pattern(s)")
        return 0
    except KeyboardInterrupt:
        if active_stop_event is not None:
            active_stop_event.set()
        if active_start_event is not None:
            active_start_event.set()
        for thread in active_threads:
            thread.join()
        print("Interrupted by user.")
        return 130
    except Exception as exc:  # pylint: disable=broad-except
        print(exc)
        return 1
    finally:
        if preview is not None:
            preview.close()
        for _, ad in plotters:
            try:
                ad.penup()
            except Exception:  # pylint: disable=broad-except
                pass
            try:
                ad.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass


if __name__ == "__main__":
    sys.exit(main())
