#!/usr/bin/env python3
"""Local HTTP bridge that receives guided kolam payloads and plots on one to four AxiDraws."""

from __future__ import annotations

import copy
import importlib.util
import html
import json
import math
import mimetypes
import os
import random
import signal
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[1]
AXIDRAW_ROOT = REPO_ROOT / "AxiDraw_API_396"
CLIENT_APP_ROOT = REPO_ROOT / "mediapipe-handpose"
CLIENT_APP_ROOT_RESOLVED = CLIENT_APP_ROOT.resolve()


def preferred_repo_python(repo_root: Path) -> Path | None:
    current_python = Path(sys.executable)
    for candidate in (
        repo_root / ".venv" / "bin" / "python",
        repo_root / ".venv" / "bin" / "python3",
    ):
        if candidate.exists() and candidate != current_python:
            return candidate
    return None


def maybe_reexec_with_repo_python() -> None:
    if __name__ != "__main__":
        return
    if os.environ.get("OF_CURVES_HANDS_REEXEC") == "1":
        return
    repo_python = preferred_repo_python(REPO_ROOT)
    if repo_python is None:
        return
    env = os.environ.copy()
    env["OF_CURVES_HANDS_REEXEC"] = "1"
    os.execve(str(repo_python), [str(repo_python), __file__, *sys.argv[1:]], env)


maybe_reexec_with_repo_python()

if str(AXIDRAW_ROOT) not in sys.path:
    sys.path.insert(0, str(AXIDRAW_ROOT))

IMPORT_ERROR: Exception | None = None
try:
    from plotink import ebb_motion, ebb_serial
    from pyaxidraw import axidraw
except Exception as exc:  # pylint: disable=broad-except
    IMPORT_ERROR = exc
    ebb_motion = None  # type: ignore[assignment]
    ebb_serial = None  # type: ignore[assignment]
    axidraw = None  # type: ignore[assignment]

SERIAL_IMPORT_ERROR: Exception | None = None
try:
    import serial
    from serial.tools import list_ports
except Exception as exc:  # pylint: disable=broad-except
    SERIAL_IMPORT_ERROR = exc
    serial = None  # type: ignore[assignment]
    list_ports = None  # type: ignore[assignment]

HOST = os.environ.get("PLOTTER_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PLOTTER_BRIDGE_PORT", "8765"))
MAX_BODY_BYTES = 8_000_000
STATE_LOCK = threading.Lock()
BUSY_PORTS: set[str] = set()
MAX_SUPPORTED_PLOTTERS = 4
SERVO_CHANNEL_NAMES = "ABCD"
SVG_NS = "http://www.w3.org/2000/svg"
NEXT_PLOTTER_INDEX = 0
PLOTTER_INDEX_BY_PORT: dict[str, int] = {}
ACTIVE_GUIDED_PLOT_REQUESTS_BY_CLIENT_ID: dict[str, dict[str, Any]] = {}
PASSIVE_WRAPPER = REPO_ROOT / "mediapipe-handpose" / "passive" / "run_passive_kolam.py"
PASSIVE_SOURCE_SCRIPT = AXIDRAW_ROOT / "sketches-mdw" / "test-d-1-n-plotter-10min-dynamickolam.py"
DASHBOARD_HTML = REPO_ROOT / "plotter-bridge" / "dashboard.html"
PLOTTER_SPEED_SCALE = 3.0
DEDICATED_PASSIVE_PLOTTER_SLOT = max(
    1,
    min(MAX_SUPPORTED_PLOTTERS, int(os.environ.get("DEDICATED_PASSIVE_PLOTTER_SLOT", "3"))),
)


def scaled_plotter_speed(env_name: str, default: int) -> int:
    base_speed = int(os.environ.get(env_name, str(default)))
    return max(1, min(100, int(round(base_speed * PLOTTER_SPEED_SCALE))))


PASSIVE_DURATION_MINUTES = float(os.environ.get("PASSIVE_MODE_DURATION_MINUTES", "1440"))
PASSIVE_STOP_HOME_RETRIES = int(os.environ.get("PASSIVE_STOP_HOME_RETRIES", "12"))
PASSIVE_STOP_HOME_RETRY_DELAY_SECONDS = float(
    os.environ.get("PASSIVE_STOP_HOME_RETRY_DELAY_SECONDS", "0.25")
)
PLOTTER_HOME_STEP_RATE = int(os.environ.get("PLOTTER_HOME_STEP_RATE", "3200"))
PLOTTER_HOME_PEN_DELAY_MS = int(os.environ.get("PLOTTER_HOME_PEN_DELAY_MS", "250"))
PLOTTER_HOME_TIMEOUT_SECONDS = float(os.environ.get("PLOTTER_HOME_TIMEOUT_SECONDS", "15.0"))
PLOTTER_SPEED_PENDOWN = scaled_plotter_speed("PLOTTER_SPEED_PENDOWN", 35)
PLOTTER_SPEED_PENUP = scaled_plotter_speed("PLOTTER_SPEED_PENUP", 75)
GUIDED_ARC_MIN_SEGMENTS = int(os.environ.get("GUIDED_ARC_MIN_SEGMENTS", "8"))
ARDUINO_REDRAW_PAUSE_SECONDS = float(os.environ.get("ARDUINO_REDRAW_PAUSE_SECONDS", "2.0"))
PASSIVE_PROCESS: subprocess.Popen[str] | None = None
PASSIVE_RESERVED_PORTS: list[str] = []
PASSIVE_SESSION_SEED: int | None = None
PASSIVE_PATTERN_COUNTS_BY_PORT: dict[str, int] = {}
PASSIVE_LAST_PATTERN_INFO_BY_PORT: dict[str, dict[str, Any]] = {}
PASSIVE_GENERATOR_MODULE: Any = None
PASSIVE_PREVIEW_SPACING = float(os.environ.get("PASSIVE_PREVIEW_SPACING", "50.0"))
PASSIVE_PREVIEW_DOT_STYLE = os.environ.get("PASSIVE_PREVIEW_DOT_STYLE", "circle").strip().lower() or "circle"
PASSIVE_PREVIEW_ARC_SEGMENTS = int(os.environ.get("PASSIVE_PREVIEW_ARC_SEGMENTS", "16"))
ARDUINO_LOCK = threading.Lock()
ARDUINO_SERIAL = None
ARDUINO_CONNECTED_PORT: str | None = None
ARDUINO_BAUD_RATE = int(os.environ.get("ARDUINO_BAUD_RATE", "9600"))
ARDUINO_PORT = os.environ.get("ARDUINO_PORT", "").strip() or None


def normalize_servo_channel(channel_value: int) -> int:
    return max(1, min(len(SERVO_CHANNEL_NAMES), int(channel_value)))


def servo_channel_name(channel_value: int) -> str:
    return SERVO_CHANNEL_NAMES[normalize_servo_channel(channel_value) - 1]


def build_default_servo_channel_by_plotter() -> dict[int, int]:
    # Current physical mounting:
    # - logical plotter 1 -> servo channel 3 (C)
    # - logical plotter 2 -> servo channel 1 (A)
    # - logical plotter 3 -> servo channel 2 (B)
    # - logical plotter 4 -> servo channel 4 (D)
    defaults = {
        1: 3,
        2: 1,
        3: 2,
        4: 4,
    }
    return {
        plotter_index: normalize_servo_channel(
            int(os.environ.get(f"ARDUINO_PLOTTER_{plotter_index}_SERVO_CHANNEL", str(default_channel)))
        )
        for plotter_index, default_channel in defaults.items()
    }


ARDUINO_SERVO_CHANNEL_BY_PLOTTER = build_default_servo_channel_by_plotter()
PLOTTER_INDEX_BY_ARDUINO_SERVO_CHANNEL = {
    servo_channel: plotter_index
    for plotter_index, servo_channel in ARDUINO_SERVO_CHANNEL_BY_PLOTTER.items()
}


def plotter_index_for_arduino_servo_channel(channel_value: int) -> int | None:
    return PLOTTER_INDEX_BY_ARDUINO_SERVO_CHANNEL.get(normalize_servo_channel(channel_value))


def parse_comma_separated_ints(env_name: str) -> tuple[int, ...]:
    raw_value = os.environ.get(env_name, "").strip()
    if not raw_value:
        return ()
    values: list[int] = []
    for chunk in raw_value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        values.append(int(chunk))
    return tuple(values)


def normalized_plotter_indices(values: tuple[int, ...]) -> tuple[int, ...]:
    normalized: list[int] = []
    for value in values:
        clamped = max(1, min(MAX_SUPPORTED_PLOTTERS, int(value)))
        if clamped not in normalized:
            normalized.append(clamped)
    return tuple(normalized)


def plotter_indices_for_servo_channels(channel_values: tuple[int, ...]) -> tuple[int, ...]:
    indices: list[int] = []
    for channel_value in channel_values:
        plotter_index = plotter_index_for_arduino_servo_channel(channel_value)
        if plotter_index is None or plotter_index in indices:
            continue
        indices.append(plotter_index)
    return tuple(indices)


DEFAULT_DEDICATED_PASSIVE_SERVO_CHANNEL = normalize_servo_channel(
    int(os.environ.get("DEDICATED_PASSIVE_SERVO_CHANNEL", "3"))
)
DEFAULT_ACTIVE_MODE_SERVO_CHANNELS = tuple(
    normalize_servo_channel(channel_value)
    for channel_value in parse_comma_separated_ints("ACTIVE_MODE_SERVO_CHANNELS") or (1, 2)
)
DEDICATED_PASSIVE_PLOTTER_INDEX = normalized_plotter_indices(
    parse_comma_separated_ints("DEDICATED_PASSIVE_PLOTTER_INDEX")
)[:1]
DEDICATED_PASSIVE_PLOTTER_INDEX = (
    DEDICATED_PASSIVE_PLOTTER_INDEX[0]
    if DEDICATED_PASSIVE_PLOTTER_INDEX
    else (
        plotter_index_for_arduino_servo_channel(DEFAULT_DEDICATED_PASSIVE_SERVO_CHANNEL)
        or 1
    )
)
ACTIVE_MODE_PLOTTER_INDICES = normalized_plotter_indices(
    parse_comma_separated_ints("ACTIVE_MODE_PLOTTER_INDICES")
) or tuple(
    plotter_index
    for plotter_index in plotter_indices_for_servo_channels(DEFAULT_ACTIVE_MODE_SERVO_CHANNELS)
    if plotter_index != DEDICATED_PASSIVE_PLOTTER_INDEX
)
if not ACTIVE_MODE_PLOTTER_INDICES:
    ACTIVE_MODE_PLOTTER_INDICES = tuple(
        plotter_index
        for plotter_index in range(1, MAX_SUPPORTED_PLOTTERS + 1)
        if plotter_index != DEDICATED_PASSIVE_PLOTTER_INDEX
    )


def default_arduino_toggle_command_for_plotter(plotter_index: int) -> str:
    servo_channel = ARDUINO_SERVO_CHANNEL_BY_PLOTTER.get(plotter_index, plotter_index)
    return f"t{normalize_servo_channel(servo_channel)}"


def default_arduino_mode_command_for_plotter(plotter_index: int, mode: str) -> str:
    normalized_plotter_index = max(1, min(MAX_SUPPORTED_PLOTTERS, int(plotter_index)))
    normalized_mode = mode.strip().lower()
    if normalized_mode == "marker":
        return f"P{normalized_plotter_index}M"
    if normalized_mode == "erase":
        return f"P{normalized_plotter_index}E"
    return f"P{normalized_plotter_index}T"


ARDUINO_SERVO_UNKNOWN_MODE = "unknown"
ARDUINO_SERVO_INITIAL_MODE = (
    os.environ.get("ARDUINO_SERVO_INITIAL_MODE", "marker").strip().lower() or "marker"
)
if ARDUINO_SERVO_INITIAL_MODE not in {"marker", "erase", ARDUINO_SERVO_UNKNOWN_MODE}:
    ARDUINO_SERVO_INITIAL_MODE = ARDUINO_SERVO_UNKNOWN_MODE
ARDUINO_SERVO_MODE_BY_PLOTTER = {
    index: ARDUINO_SERVO_INITIAL_MODE for index in range(1, MAX_SUPPORTED_PLOTTERS + 1)
}
ARDUINO_READY_DELAY_SECONDS = float(os.environ.get("ARDUINO_READY_DELAY_SECONDS", "2.0"))
ARDUINO_RESPONSE_TIMEOUT_SECONDS = float(os.environ.get("ARDUINO_RESPONSE_TIMEOUT_SECONDS", "0.75"))
ARDUINO_COMMAND_SETTLE_SECONDS = float(os.environ.get("ARDUINO_COMMAND_SETTLE_SECONDS", "0.7"))
ARDUINO_RESPONSE_IDLE_SECONDS = float(os.environ.get("ARDUINO_RESPONSE_IDLE_SECONDS", "0.15"))
ARDUINO_TOGGLE_COMMAND = (
    os.environ.get("ARDUINO_TOGGLE_COMMAND", default_arduino_toggle_command_for_plotter(1)).strip()
    or default_arduino_toggle_command_for_plotter(1)
)
ARDUINO_MARKER_ANGLE = int(os.environ.get("ARDUINO_MARKER_ANGLE", "0"))
ARDUINO_ERASE_ANGLE = int(os.environ.get("ARDUINO_ERASE_ANGLE", "180"))
ARDUINO_PLOTTER_TOGGLE_COMMANDS = {
    1: os.environ.get("ARDUINO_PLOTTER_1_TOGGLE_COMMAND", default_arduino_toggle_command_for_plotter(1)).strip()
    or default_arduino_toggle_command_for_plotter(1),
    2: os.environ.get("ARDUINO_PLOTTER_2_TOGGLE_COMMAND", default_arduino_toggle_command_for_plotter(2)).strip()
    or default_arduino_toggle_command_for_plotter(2),
    3: os.environ.get("ARDUINO_PLOTTER_3_TOGGLE_COMMAND", default_arduino_toggle_command_for_plotter(3)).strip()
    or default_arduino_toggle_command_for_plotter(3),
    4: os.environ.get("ARDUINO_PLOTTER_4_TOGGLE_COMMAND", default_arduino_toggle_command_for_plotter(4)).strip()
    or default_arduino_toggle_command_for_plotter(4),
}
ARDUINO_PLOTTER_SERVO_COMMANDS = {
    1: {
        "marker": os.environ.get("ARDUINO_PLOTTER_1_MARKER_COMMAND", default_arduino_mode_command_for_plotter(1, "marker")).strip()
        or default_arduino_mode_command_for_plotter(1, "marker"),
        "erase": os.environ.get("ARDUINO_PLOTTER_1_ERASE_COMMAND", default_arduino_mode_command_for_plotter(1, "erase")).strip()
        or default_arduino_mode_command_for_plotter(1, "erase"),
    },
    2: {
        "marker": os.environ.get("ARDUINO_PLOTTER_2_MARKER_COMMAND", default_arduino_mode_command_for_plotter(2, "marker")).strip()
        or default_arduino_mode_command_for_plotter(2, "marker"),
        "erase": os.environ.get("ARDUINO_PLOTTER_2_ERASE_COMMAND", default_arduino_mode_command_for_plotter(2, "erase")).strip()
        or default_arduino_mode_command_for_plotter(2, "erase"),
    },
    3: {
        "marker": os.environ.get("ARDUINO_PLOTTER_3_MARKER_COMMAND", default_arduino_mode_command_for_plotter(3, "marker")).strip()
        or default_arduino_mode_command_for_plotter(3, "marker"),
        "erase": os.environ.get("ARDUINO_PLOTTER_3_ERASE_COMMAND", default_arduino_mode_command_for_plotter(3, "erase")).strip()
        or default_arduino_mode_command_for_plotter(3, "erase"),
    },
    4: {
        "marker": os.environ.get("ARDUINO_PLOTTER_4_MARKER_COMMAND", default_arduino_mode_command_for_plotter(4, "marker")).strip()
        or default_arduino_mode_command_for_plotter(4, "marker"),
        "erase": os.environ.get("ARDUINO_PLOTTER_4_ERASE_COMMAND", default_arduino_mode_command_for_plotter(4, "erase")).strip()
        or default_arduino_mode_command_for_plotter(4, "erase"),
    },
}
PLOTTER_PORTS_BY_INDEX = {
    index: os.environ.get(f"PLOTTER_{index}_PORT", "").strip() or None
    for index in range(1, MAX_SUPPORTED_PLOTTERS + 1)
}
GUIDED_KOLAM_X_OFFSET_IN = float(os.environ.get("GUIDED_KOLAM_X_OFFSET_IN", "1.0"))
GUIDED_KOLAM_Y_OFFSET_IN = float(os.environ.get("GUIDED_KOLAM_Y_OFFSET_IN", "1.0"))
ACTIVE_GUIDED_AREA_MODE = True
ACTIVE_GUIDED_AREA_STATE_BY_PORT: dict[str, dict[str, int]] = {}
ACTIVE_GUIDED_AREA_SLOT_PREVIEWS_BY_PORT: dict[str, list[dict[str, Any] | None]] = {}
ACTIVE_GUIDED_AREA_SLOT_GUIDED_KOLAMS_BY_PORT: dict[str, list[dict[str, Any] | None]] = {}
CONTROLLER_SELECTED_PLOTTER_INDEX: int | None = None
CONTROLLER_LAST_EVENT: dict[str, Any] | None = None
LEGACY_ACTIVE_GUIDED_AREA_SIZE_IN = os.environ.get("ACTIVE_GUIDED_AREA_SIZE_IN")
DEFAULT_ACTIVE_GUIDED_AREA_WIDTH_IN = 8.75
DEFAULT_ACTIVE_GUIDED_AREA_HEIGHT_IN = 2.75
DEFAULT_ACTIVE_GUIDED_AREA_COLUMNS = 3
DEFAULT_ACTIVE_GUIDED_AREA_ROWS = 1
ACTIVE_GUIDED_AREA_WIDTH_IN = float(
    os.environ.get(
        "ACTIVE_GUIDED_AREA_WIDTH_IN",
        LEGACY_ACTIVE_GUIDED_AREA_SIZE_IN or str(DEFAULT_ACTIVE_GUIDED_AREA_WIDTH_IN),
    )
)
ACTIVE_GUIDED_AREA_HEIGHT_IN = float(
    os.environ.get(
        "ACTIVE_GUIDED_AREA_HEIGHT_IN",
        LEGACY_ACTIVE_GUIDED_AREA_SIZE_IN or str(DEFAULT_ACTIVE_GUIDED_AREA_HEIGHT_IN),
    )
)
ACTIVE_GUIDED_AREA_COLUMNS = int(
    os.environ.get("ACTIVE_GUIDED_AREA_COLUMNS", str(DEFAULT_ACTIVE_GUIDED_AREA_COLUMNS))
)
ACTIVE_GUIDED_AREA_ROWS = int(
    os.environ.get("ACTIVE_GUIDED_AREA_ROWS", str(DEFAULT_ACTIVE_GUIDED_AREA_ROWS))
)
ACTIVE_GUIDED_AREA_MARGIN_IN = float(os.environ.get("ACTIVE_GUIDED_AREA_MARGIN_IN", "0.0"))
ACTIVE_GUIDED_AREA_GAP_IN = float(os.environ.get("ACTIVE_GUIDED_AREA_GAP_IN", "0.25"))
ACTIVE_GUIDED_AREA_ORIGIN_X_IN = float(
    os.environ.get("ACTIVE_GUIDED_AREA_ORIGIN_X_IN", str(GUIDED_KOLAM_X_OFFSET_IN))
)
ACTIVE_GUIDED_AREA_ORIGIN_Y_IN = float(
    os.environ.get("ACTIVE_GUIDED_AREA_ORIGIN_Y_IN", str(GUIDED_KOLAM_Y_OFFSET_IN))
)
PASSIVE_PACKED_AREA_SIZE_MIN = int(os.environ.get("PASSIVE_PACKED_AREA_SIZE_MIN", "2"))
PASSIVE_PACKED_AREA_SIZE_MAX = int(os.environ.get("PASSIVE_PACKED_AREA_SIZE_MAX", "4"))
ACTIVE_GUIDED_ERASE_SWEEP_STEP_IN = float(os.environ.get("ACTIVE_GUIDED_ERASE_SWEEP_STEP_IN", "0.12"))
LEGACY_ACTIVE_GUIDED_ERASE_X_OVERSCAN_IN = os.environ.get("ACTIVE_GUIDED_ERASE_X_OVERSCAN_IN")
ACTIVE_GUIDED_ERASE_X_OVERSCAN_LEFT_IN = float(
    os.environ.get("ACTIVE_GUIDED_ERASE_X_OVERSCAN_LEFT_IN", LEGACY_ACTIVE_GUIDED_ERASE_X_OVERSCAN_IN or "0.5")
)
ACTIVE_GUIDED_ERASE_X_OVERSCAN_RIGHT_IN = float(
    os.environ.get("ACTIVE_GUIDED_ERASE_X_OVERSCAN_RIGHT_IN", LEGACY_ACTIVE_GUIDED_ERASE_X_OVERSCAN_IN or "0.5")
)
LEGACY_ACTIVE_GUIDED_ERASE_Y_OVERSCAN_IN = os.environ.get("ACTIVE_GUIDED_ERASE_Y_OVERSCAN_IN")
ACTIVE_GUIDED_ERASE_Y_OVERSCAN_BOTTOM_IN = float(
    os.environ.get("ACTIVE_GUIDED_ERASE_Y_OVERSCAN_BOTTOM_IN", LEGACY_ACTIVE_GUIDED_ERASE_Y_OVERSCAN_IN or "0.5")
)
ACTIVE_GUIDED_ERASE_Y_OVERSCAN_TOP_IN = float(
    os.environ.get("ACTIVE_GUIDED_ERASE_Y_OVERSCAN_TOP_IN", LEGACY_ACTIVE_GUIDED_ERASE_Y_OVERSCAN_IN or "0.5")
)
ACTIVE_GUIDED_ERASE_OFFSET_X_IN = float(os.environ.get("ACTIVE_GUIDED_ERASE_OFFSET_X_IN", "0.0"))
ACTIVE_GUIDED_ERASE_OFFSET_Y_IN = float(os.environ.get("ACTIVE_GUIDED_ERASE_OFFSET_Y_IN", "0.0"))
ERASE_TRACE_OFFSET_X_IN = float(os.environ.get("ERASE_TRACE_OFFSET_X_IN", str(7.5 / 25.4)))
ERASE_SWEEP_DEMO_PADDING_IN = float(os.environ.get("ERASE_SWEEP_DEMO_PADDING_IN", str(20.0 / 25.4)))
ACTIVE_GUIDED_ERASE_PREP_X_IN = float(os.environ.get("ACTIVE_GUIDED_ERASE_PREP_X_IN", "8.0"))
ACTIVE_GUIDED_ERASE_PREP_Y_IN = float(os.environ.get("ACTIVE_GUIDED_ERASE_PREP_Y_IN", "0.0"))
GUIDED_KOLAM_DOT_STYLE = os.environ.get("GUIDED_KOLAM_DOT_STYLE", "circle").strip().lower() or "circle"
GUIDED_KOLAM_DOT_MARK_UNITS = float(os.environ.get("GUIDED_KOLAM_DOT_MARK_UNITS", "3.0"))
CROSS_MARK_SCALE = 1.30

ET.register_namespace("", SVG_NS)


def list_axidraw_ports() -> list[str]:
    if IMPORT_ERROR is not None:
        return []

    ports = ebb_serial.listEBBports() or []
    result: list[str] = []
    for entry in ports:
        device = getattr(entry, "device", None)
        if device is None:
            device = entry[0]
        result.append(str(device))
    return result


def resolve_port(port_value: str | None) -> str | None:
    if not port_value or IMPORT_ERROR is not None:
        return port_value
    resolved = ebb_serial.find_named_ebb(port_value)
    return str(resolved) if resolved else str(port_value)


def resolve_static_file(root: Path, relative_path: str, default_name: str = "index.html") -> Path | None:
    trimmed_path = unquote(relative_path).lstrip("/")
    normalized_path = trimmed_path or default_name
    candidate = (root / normalized_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def serial_port_aliases(port_value: str | None) -> set[str]:
    if not port_value:
        return set()

    aliases: set[str] = set()
    pending = [str(port_value).strip()]
    while pending:
        candidate = pending.pop()
        if not candidate or candidate in aliases:
            continue
        aliases.add(candidate)

        if candidate.startswith("/dev/tty."):
            pending.append("/dev/cu." + candidate[len("/dev/tty."):])
        elif candidate.startswith("/dev/cu."):
            pending.append("/dev/tty." + candidate[len("/dev/cu."):])

    resolved = resolve_port(str(port_value).strip())
    if resolved:
        resolved_text = str(resolved).strip()
        if resolved_text not in aliases:
            pending.append(resolved_text)
            while pending:
                candidate = pending.pop()
                if not candidate or candidate in aliases:
                    continue
                aliases.add(candidate)
                if candidate.startswith("/dev/tty."):
                    pending.append("/dev/cu." + candidate[len("/dev/tty."):])
                elif candidate.startswith("/dev/cu."):
                    pending.append("/dev/tty." + candidate[len("/dev/cu."):])

    return aliases


def open_serial_port(port_label: str):
    resolved = resolve_port(port_label) or port_label
    serial_port = ebb_serial.testPort(resolved)
    if serial_port is None:
        raise RuntimeError(
            f"Could not open AxiDraw port {port_label} (resolved to {resolved})."
        )
    return serial_port


def list_connectable_axidraw_ports(candidate_ports: list[str] | None = None) -> list[str]:
    if IMPORT_ERROR is not None:
        return []

    ports = candidate_ports if candidate_ports is not None else list_axidraw_ports()
    connectable: list[str] = []
    for port in ports:
        serial_port = ebb_serial.testPort(resolve_port(port) or port)
        if serial_port is None:
            continue
        connectable.append(port)
        ebb_serial.closePort(serial_port)
    return connectable


def load_passive_generator_module() -> Any:
    global PASSIVE_GENERATOR_MODULE  # pylint: disable=global-statement

    if PASSIVE_GENERATOR_MODULE is not None:
        return PASSIVE_GENERATOR_MODULE
    if not PASSIVE_SOURCE_SCRIPT.exists():
        raise RuntimeError(f"Passive source script not found: {PASSIVE_SOURCE_SCRIPT}")

    module_name = "plotter_bridge_passive_generator"
    spec = importlib.util.spec_from_file_location(module_name, PASSIVE_SOURCE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load passive generator from {PASSIVE_SOURCE_SCRIPT}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    PASSIVE_GENERATOR_MODULE = module
    return module


def canonicalize_plotter_port(port_value: str | None, candidate_ports: list[str] | None = None) -> str | None:
    if not port_value:
        return None

    normalized_port = str(port_value).strip()
    aliases = serial_port_aliases(normalized_port)
    ports_to_check: list[str] = []
    if candidate_ports:
        ports_to_check.extend(str(port).strip() for port in candidate_ports if port)
    ports_to_check.extend(str(port).strip() for port in PASSIVE_RESERVED_PORTS if port)
    ports_to_check.extend(str(port).strip() for port in PLOTTER_INDEX_BY_PORT if port)
    ports_to_check.extend(str(port).strip() for port in list_axidraw_ports() if port)

    for candidate in dict.fromkeys(ports_to_check):
        if aliases.intersection(serial_port_aliases(candidate)):
            return candidate
    return normalized_port


def render_passive_pattern_preview_svg(module: Any, pattern: Any, title: str) -> str:
    margin = 24.0
    width = float(pattern.width_px) + (margin * 2.0)
    height = float(pattern.height_px) + (margin * 2.0)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.2f} {height:.2f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" aria-label="{title}">',
        f'<rect x="0" y="0" width="{width:.2f}" height="{height:.2f}" fill="#f6f1e8"/>',
        (
            f'<rect x="{margin:.2f}" y="{margin:.2f}" width="{float(pattern.width_px):.2f}" '
            f'height="{float(pattern.height_px):.2f}" fill="#fffaf0" stroke="#d8c6a0" stroke-width="1"/>'
        ),
    ]

    visible_pullis = getattr(pattern, "visible_pullis", pattern.pullis)
    for dot in visible_pullis:
        parts.append(
            f'<circle cx="{dot.x + margin:.2f}" cy="{dot.y + margin:.2f}" r="2.4" fill="#16211f"/>'
        )

    for command in pattern.commands:
        if command.kind == "break":
            continue
        if command.kind == "line" and command.line is not None:
            line = command.line
            parts.append(
                (
                    f'<line x1="{line.x1 + margin:.2f}" y1="{line.y1 + margin:.2f}" '
                    f'x2="{line.x2 + margin:.2f}" y2="{line.y2 + margin:.2f}" '
                    'stroke="#2151d1" stroke-width="1.2" stroke-linecap="round"/>'
                )
            )
            continue
        if command.kind == "arc" and command.arc is not None:
            points = module.sample_arc_points(command.arc, min_segments=PASSIVE_PREVIEW_ARC_SEGMENTS)
            serialized_points = " ".join(f"{x + margin:.2f},{y + margin:.2f}" for x, y in points)
            parts.append(
                f'<polyline points="{serialized_points}" fill="none" stroke="#2151d1" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>'
            )

    parts.append("</svg>")
    return "".join(parts)


def preview_svg_num(value: float) -> str:
    text = f"{float(value):.2f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def generate_passive_preview_payload(plotter_index: int, patterns_drawn: int) -> dict[str, Any]:
    with STATE_LOCK:
        session_seed = PASSIVE_SESSION_SEED

    if session_seed is None:
        raise RuntimeError("Passive session seed is unavailable.")

    module = load_passive_generator_module()
    pattern_number = int(patterns_drawn) + 1
    pattern_seed = (
        session_seed * module.PATTERN_SEED_SCALE
        + plotter_index * module.PLOTTER_SEED_SCALE
        + pattern_number
    )
    pattern_rng = random.Random(pattern_seed)
    size = pattern_rng.randint(PASSIVE_PACKED_AREA_SIZE_MIN, PASSIVE_PACKED_AREA_SIZE_MAX)
    generator = module.DFSKolamGenerator(size=size, spacing=PASSIVE_PREVIEW_SPACING, rng=pattern_rng)
    pattern = generator.generate()
    min_x, min_y, max_x, max_y = module.pattern_bounds(pattern)

    return {
        "pattern_number": pattern_number,
        "pattern_seed": pattern_seed,
        "size": size,
        "svg": render_passive_pattern_preview_svg(
            module,
            pattern,
            f"Plotter {plotter_label(plotter_index - 1)} next passive kolam preview",
        ),
        "bounds_px": {
            "min_x": float(min_x),
            "min_y": float(min_y),
            "max_x": float(max_x),
            "max_y": float(max_y),
            "width": float(max_x - min_x),
            "height": float(max_y - min_y),
        },
    }


def update_passive_plotter_progress(
    port_value: str,
    patterns_drawn: int,
    pattern_seed: int | None = None,
    size: int | None = None,
    slot_index: int | None = None,
    slot_count: int | None = None,
    cycles_completed: int | None = None,
) -> str:
    canonical_port = canonicalize_plotter_port(port_value)
    if canonical_port is None:
        raise RuntimeError("Could not resolve passive plotter port.")

    payload: dict[str, Any] = {
        "patterns_drawn": int(patterns_drawn),
    }
    if pattern_seed is not None:
        payload["pattern_seed"] = int(pattern_seed)
    if size is not None:
        payload["size"] = int(size)
    if slot_index is not None:
        payload["slot_index"] = int(slot_index)
    if slot_count is not None:
        payload["slot_count"] = int(slot_count)
    if cycles_completed is not None:
        payload["cycles_completed"] = int(cycles_completed)

    with STATE_LOCK:
        PASSIVE_PATTERN_COUNTS_BY_PORT[canonical_port] = int(patterns_drawn)
        PASSIVE_LAST_PATTERN_INFO_BY_PORT[canonical_port] = payload

    return canonical_port


def render_guided_kolam_preview_svg(guided_kolam: dict[str, Any], title: str) -> str:
    margin = max(12.0, min(guided_kolam["width"], guided_kolam["height"]) * 0.06)
    width = guided_kolam["width"] + (margin * 2.0)
    height = guided_kolam["height"] + (margin * 2.0)
    safe_title = html.escape(title, quote=True)
    dot_style = str(guided_kolam.get("dot_style", GUIDED_KOLAM_DOT_STYLE)).strip().lower()
    dot_half = max(1.0, float(guided_kolam.get("dot_mark_units", GUIDED_KOLAM_DOT_MARK_UNITS)) / 2.0)
    cross_half = dot_half * CROSS_MARK_SCALE

    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {preview_svg_num(width)} {preview_svg_num(height)}" '
            f'width="{preview_svg_num(width)}" height="{preview_svg_num(height)}" preserveAspectRatio="xMidYMid meet" '
            f'role="img" aria-label="{safe_title}">'
        ),
        f'<rect x="0" y="0" width="{preview_svg_num(width)}" height="{preview_svg_num(height)}" rx="18" fill="#fffaf0"/>',
        (
            f'<rect x="{preview_svg_num(margin)}" y="{preview_svg_num(margin)}" '
            f'width="{preview_svg_num(guided_kolam["width"])}" height="{preview_svg_num(guided_kolam["height"])}" '
            'rx="12" fill="#fffdf8" stroke="#d8c6a0" stroke-width="1"/>'
        ),
    ]

    visible_pullis = guided_kolam.get("visible_pullis", guided_kolam["pullis"])
    for dot in visible_pullis:
        cx = float(dot["x"]) + margin
        cy = float(dot["y"]) + margin
        if dot_style == "circle":
            parts.append(
                f'<circle cx="{preview_svg_num(cx)}" cy="{preview_svg_num(cy)}" r="{preview_svg_num(dot_half)}" fill="#16211f" fill-opacity="0.72"/>'
            )
            continue

        marker_half = cross_half if dot_style == "cross" else dot_half
        parts.append(
            (
                f'<line x1="{preview_svg_num(cx - marker_half)}" y1="{preview_svg_num(cy)}" '
                f'x2="{preview_svg_num(cx + marker_half)}" y2="{preview_svg_num(cy)}" '
                'stroke="#16211f" stroke-width="1.35" stroke-linecap="round"/>'
            )
        )
        if dot_style == "cross":
            parts.append(
                (
                    f'<line x1="{preview_svg_num(cx)}" y1="{preview_svg_num(cy - marker_half)}" '
                    f'x2="{preview_svg_num(cx)}" y2="{preview_svg_num(cy + marker_half)}" '
                    'stroke="#16211f" stroke-width="1.35" stroke-linecap="round"/>'
                )
            )

    commands = guided_kolam.get("commands", [])
    if commands:
        for command in commands:
            kind = command.get("kind")
            if kind == "line":
                line = command["line"]
                parts.append(
                    (
                        f'<line x1="{preview_svg_num(float(line["x1"]) + margin)}" y1="{preview_svg_num(float(line["y1"]) + margin)}" '
                        f'x2="{preview_svg_num(float(line["x2"]) + margin)}" y2="{preview_svg_num(float(line["y2"]) + margin)}" '
                        'stroke="#2151d1" stroke-width="1.4" stroke-linecap="round"/>'
                    )
                )
                continue
            if kind == "arc":
                points = sample_guided_arc_points(command["arc"], min_segments=PASSIVE_PREVIEW_ARC_SEGMENTS)
                if not points:
                    continue
                serialized_points = " ".join(
                    f"{preview_svg_num(float(point['x']) + margin)},{preview_svg_num(float(point['y']) + margin)}"
                    for point in points
                )
                parts.append(
                    (
                        f'<polyline points="{serialized_points}" fill="none" '
                        'stroke="#2151d1" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>'
                    )
                )
    else:
        for branch in guided_kolam["branches"]:
            points = branch.get("points", [])
            if len(points) < 2:
                continue
            serialized_points = " ".join(
                f"{preview_svg_num(float(point['x']) + margin)},{preview_svg_num(float(point['y']) + margin)}"
                for point in points
            )
            parts.append(
                (
                    f'<polyline points="{serialized_points}" fill="none" '
                    'stroke="#2151d1" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>'
                )
            )

    parts.append("</svg>")
    return "".join(parts)


def snapshot_dashboard_state() -> dict[str, Any]:
    refresh_passive_mode_state()
    detected_ports = sorted(list_axidraw_ports())
    busy_ports = sorted(snapshot_busy_ports())
    idle_ports = [port for port in detected_ports if port not in busy_ports]
    connectable_ports = sorted(list_connectable_axidraw_ports(idle_ports))
    reserved_passive_port = dedicated_passive_port(detected_ports)
    plotter_indices = plotter_indices_by_port(detected_ports)
    reverse_plotter_indices = {index: port for port, index in plotter_indices.items()}
    active_guided_area_mode = snapshot_active_guided_area_mode()
    passive_mode = snapshot_passive_mode()
    arduino_state = snapshot_arduino_state()
    controller_state = snapshot_controller_state()

    with STATE_LOCK:
        passive_seed = PASSIVE_SESSION_SEED
        passive_pattern_counts = dict(PASSIVE_PATTERN_COUNTS_BY_PORT)
        passive_last_pattern_info = {
            port: dict(info)
            for port, info in PASSIVE_LAST_PATTERN_INFO_BY_PORT.items()
        }

    configured_indices = {index for index, port in PLOTTER_PORTS_BY_INDEX.items() if port}
    plotter_numbers = sorted(set(reverse_plotter_indices) | configured_indices)
    plotters: list[dict[str, Any]] = []
    passive_ports = [str(port) for port in passive_mode.get("ports", [])]
    slot_count = int(active_guided_area_mode.get("slot_count", 1) or 1)

    for plotter_index in plotter_numbers:
        detected_port = reverse_plotter_indices.get(plotter_index)
        configured_port = PLOTTER_PORTS_BY_INDEX.get(plotter_index)
        display_port = detected_port or configured_port
        passive_port = canonicalize_plotter_port(display_port, passive_ports) if display_port else None
        passive_running = bool(passive_mode.get("running")) and bool(
            passive_port and passive_port in passive_ports
        )
        patterns_drawn = None
        last_pattern_info: dict[str, Any] | None = None
        for candidate in [passive_port, detected_port, configured_port]:
            if candidate and candidate in passive_pattern_counts:
                patterns_drawn = int(passive_pattern_counts[candidate])
                last_pattern_info = passive_last_pattern_info.get(candidate, {})
                break
        if patterns_drawn is None and passive_running:
            patterns_drawn = 0

        preview_payload: dict[str, Any] | None = None
        preview_error: str | None = None
        if passive_running and patterns_drawn is not None and passive_seed is not None:
            try:
                preview_payload = generate_passive_preview_payload(plotter_index, patterns_drawn)
            except Exception as exc:  # pylint: disable=broad-except
                preview_error = str(exc)

        active_state = (
            active_guided_area_mode.get("states_by_port", {}).get(detected_port or "", {})
            if detected_port
            else {}
        )
        next_slot_index = int(active_state.get("next_slot_index", 0))
        cycles_completed = int(active_state.get("cycles_completed", 0))
        if passive_running and patterns_drawn is not None:
            next_slot_index = patterns_drawn % slot_count
            cycles_completed = patterns_drawn // slot_count
        active_area_slot_previews = (
            snapshot_active_guided_area_slot_previews(detected_port or display_port, slot_count)
            if (detected_port or display_port)
            else empty_active_guided_area_slot_previews(slot_count)
        )

        status = "unconfigured"
        if detected_port in busy_ports:
            status = "busy"
        elif detected_port in connectable_ports:
            status = "ready"
        elif detected_port:
            status = "detected_only"
        elif configured_port:
            status = "configured"

        plotters.append(
            {
                "plotter_index": plotter_index,
                "plotter_label": plotter_label(plotter_index - 1),
                "active_mode_enabled": not port_matches(display_port, reserved_passive_port),
                "passive_mode_reserved": port_matches(display_port, reserved_passive_port),
                "port": display_port,
                "detected_port": detected_port,
                "configured_port": configured_port,
                "detected": detected_port is not None,
                "connectable": detected_port in connectable_ports,
                "busy": detected_port in busy_ports,
                "status": status,
                "toggle_command": arduino_plotter_toggle_command(plotter_index),
                "servo_mode": arduino_state.get("servo_modes_by_plotter", {}).get(
                    plotter_index,
                    ARDUINO_SERVO_UNKNOWN_MODE,
                ),
                "active_area": {
                    "next_slot_index": next_slot_index,
                    "slot_count": slot_count,
                    "cycles_completed": cycles_completed,
                    "slot_previews": active_area_slot_previews,
                },
                "passive": {
                    "running": passive_running,
                    "patterns_drawn": patterns_drawn,
                    "session_seed": passive_seed,
                    "last_pattern": last_pattern_info,
                    "next_preview": preview_payload,
                    "preview_error": preview_error,
                },
            }
        )

    return {
        "status": "ok",
        "host": HOST,
        "port": PORT,
        "detected_ports": detected_ports,
        "connectable_ports": connectable_ports,
        "busy_ports": busy_ports,
        "plotters": plotters,
        "plotter_count": len(plotters),
        "active_mode_plotter_indices": list(plotter_indices_by_port(active_mode_ports(detected_ports, detected_ports)).values()),
        "dedicated_passive_plotter_index": (
            plotter_indices_by_port([reserved_passive_port]).get(reserved_passive_port)
            if reserved_passive_port
            else None
        ),
        "dedicated_passive_port": reserved_passive_port,
        "active_guided_area_mode": active_guided_area_mode,
        "passive_mode": {
            **passive_mode,
            "session_seed": passive_seed,
            "pattern_counts_by_port": passive_pattern_counts,
        },
        "active_guided_plot_requests": snapshot_active_guided_plot_requests(),
        "arduino": arduino_state,
        "controller": controller_state,
    }


def build_plotter(svg_text: str, port: str) -> axidraw.AxiDraw:
    ad = axidraw.AxiDraw()
    # plot_setup() reloads AxiDraw options, so assign the target port after it.
    ad.plot_setup(svg_text)
    ad.options.port = open_serial_port(port)
    ad.options.speed_pendown = PLOTTER_SPEED_PENDOWN
    ad.options.speed_penup = PLOTTER_SPEED_PENUP
    ad.options.accel = int(os.environ.get("PLOTTER_ACCEL", "75"))
    ad.options.preview = False
    ad.options.report_time = False
    ad.errors.connect = True
    ad.errors.disconnect = True
    return ad


def build_interactive_plotter(port: str) -> axidraw.AxiDraw:
    ad = axidraw.AxiDraw()
    ad.interactive()
    ad.options.port = resolve_port(port) or port
    ad.errors.connect = True
    connected = ad.connect()
    if not connected:
        raise RuntimeError(f"Could not connect to AxiDraw ({port}).")
    ad.options.units = 0  # inches
    ad.options.speed_pendown = PLOTTER_SPEED_PENDOWN
    ad.options.speed_penup = PLOTTER_SPEED_PENUP
    ad.options.accel = int(os.environ.get("PLOTTER_ACCEL", "75"))
    ad.options.home_after = False
    ad.update()
    return ad


def return_interactive_plotter_to_origin(ad: axidraw.AxiDraw) -> None:
    ad.penup()
    ad.moveto(0.0, 0.0)
    ad.block()


def return_plotter_to_origin(port: str) -> None:
    serial_port = open_serial_port(port)
    try:
        motor_state = ebb_motion.query_enable_motors(serial_port, verbose=False)
        if motor_state == (0, 0):
            raise RuntimeError(
                "XY motors are already disabled, so the stored home position is unavailable."
            )

        ebb_motion.sendPenUp(serial_port, PLOTTER_HOME_PEN_DELAY_MS, verbose=False)
        ebb_serial.command(serial_port, f"HM,{PLOTTER_HOME_STEP_RATE}\r", False)

        deadline = time.monotonic() + max(0.1, PLOTTER_HOME_TIMEOUT_SECONDS)
        while time.monotonic() < deadline:
            steps_1, steps_2 = ebb_motion.query_steps(serial_port, verbose=False)
            if steps_1 == 0 and steps_2 == 0:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("Timed out while waiting for the plotter to reach home.")

        ebb_motion.sendDisableMotors(serial_port, False)
    finally:
        ebb_serial.closePort(serial_port)


def control_command_message(command_name: str, result_count: int) -> str:
    if command_name == "disable_motors":
        return (
            f"Returned {result_count} plotter(s) to origin and disabled XY motors. "
            "If you move a carriage by hand afterward, place it back at home before the next draw."
        )
    if command_name == "erase_trace":
        return f"Drew 1 passive-mode kolam and erased it by retracing on {result_count} plotter(s)."
    if command_name == "erase_sweep_demo":
        return (
            "Drew 1 passive-mode kolam and erased it with a bounded vertical sweep "
            f"on {result_count} plotter(s)."
        )
    if command_name == "horizontal_sweep_area_test":
        return (
            "Drew a rectangle in the configured guided kolam area, then flipped into erase mode "
            "and retraced it "
            f"on {result_count} plotter(s)."
        )
    if command_name == "erase_area":
        return (
            f"Swept the packed area on {result_count} plotter(s) in parallel. "
            "Each plotter flipped into erase mode for the sweep and then returned to marker mode."
        )
    return f"Ran {command_name} on {result_count} plotter(s)."


def set_arduino_servo_mode(
    context_label: str,
    plotter_index: int,
    target_mode: str,
    action_label: str,
) -> str:
    normalized_mode = target_mode.strip().lower()
    print(f"{context_label} {action_label}...")
    result = ensure_arduino_servo_mode(normalized_mode, plotter_index=plotter_index, force=True)
    message = str(result.get("message", "Arduino servo toggle failed."))
    if result.get("status") != "done":
        raise RuntimeError(f"Arduino servo toggle failed: {message}")
    print(f"{context_label} Arduino servo response: {message}")
    print(
        f"{context_label} Plotter {plotter_label(plotter_index - 1)} servo rotated to {normalized_mode} mode."
    )
    return message


def plotter_uses_toggle_servo_transitions(plotter_index: int) -> bool:
    servo_channel = ARDUINO_SERVO_CHANNEL_BY_PLOTTER.get(plotter_index, plotter_index)
    return normalize_servo_channel(servo_channel) == 4


def ensure_plotter_servo_ready_for_drawing(context_label: str, plotter_index: int) -> str:
    result = ensure_arduino_servo_mode("marker", plotter_index=plotter_index, force=True)
    message = str(result.get("message", "Arduino servo prep failed."))
    if result.get("status") != "done":
        raise RuntimeError(f"Arduino servo prep failed: {message}")
    print(f"{context_label} Arduino servo ready for drawing: {message}")
    return message


def toggle_plotter_servo(context_label: str, plotter_index: int, action_label: str) -> str:
    print(f"{context_label} {action_label}...")
    result = toggle_arduino_servos(plotter_indices=[plotter_index])
    message = str(result.get("message", "Arduino servo toggle failed."))
    if result.get("status") != "done":
        raise RuntimeError(f"Arduino servo toggle failed: {message}")
    print(f"{context_label} Arduino servo response: {message}")
    current_mode = ARDUINO_SERVO_MODE_BY_PLOTTER.get(plotter_index, ARDUINO_SERVO_UNKNOWN_MODE)
    print(
        f"{context_label} Plotter {plotter_label(plotter_index - 1)} servo toggled to {current_mode} mode."
    )
    return message


def active_mode_servo_plotter_index(plotter_index: int) -> int:
    normalized_plotter_index = max(1, min(MAX_SUPPORTED_PLOTTERS, int(plotter_index)))
    return {1: 3, 3: 1}.get(normalized_plotter_index, normalized_plotter_index)


def active_mode_plotter_uses_toggle_servo_transitions(plotter_index: int) -> bool:
    return plotter_uses_toggle_servo_transitions(active_mode_servo_plotter_index(plotter_index))


def ensure_active_mode_plotter_servo_ready_for_drawing(
    context_label: str,
    plotter_index: int,
) -> str:
    servo_plotter_index = active_mode_servo_plotter_index(plotter_index)
    result = ensure_arduino_servo_mode("marker", plotter_index=servo_plotter_index, force=True)
    message = str(result.get("message", "Arduino servo prep failed."))
    if result.get("status") != "done":
        raise RuntimeError(f"Arduino servo prep failed: {message}")
    print(
        f"{context_label} Active-mode servo ready for drawing: {message} "
        f"(logical plotter {plotter_index} -> servo plotter {servo_plotter_index})."
    )
    return message


def set_active_mode_arduino_servo_mode(
    context_label: str,
    plotter_index: int,
    target_mode: str,
    action_label: str,
) -> str:
    normalized_mode = target_mode.strip().lower()
    servo_plotter_index = active_mode_servo_plotter_index(plotter_index)
    print(
        f"{context_label} {action_label} "
        f"(logical plotter {plotter_index} -> servo plotter {servo_plotter_index})..."
    )
    result = ensure_arduino_servo_mode(normalized_mode, plotter_index=servo_plotter_index, force=True)
    message = str(result.get("message", "Arduino servo toggle failed."))
    if result.get("status") != "done":
        raise RuntimeError(f"Arduino servo toggle failed: {message}")
    print(f"{context_label} Arduino servo response: {message}")
    print(
        f"{context_label} Active-mode servo for logical plotter {plotter_index} "
        f"rotated to {normalized_mode} mode via servo plotter {servo_plotter_index}."
    )
    return message


def toggle_active_mode_plotter_servo(context_label: str, plotter_index: int, action_label: str) -> str:
    servo_plotter_index = active_mode_servo_plotter_index(plotter_index)
    print(
        f"{context_label} {action_label} "
        f"(logical plotter {plotter_index} -> servo plotter {servo_plotter_index})..."
    )
    result = toggle_arduino_servos(plotter_indices=[servo_plotter_index])
    message = str(result.get("message", "Arduino servo toggle failed."))
    if result.get("status") != "done":
        raise RuntimeError(f"Arduino servo toggle failed: {message}")
    print(f"{context_label} Arduino servo response: {message}")
    current_mode = ARDUINO_SERVO_MODE_BY_PLOTTER.get(servo_plotter_index, ARDUINO_SERVO_UNKNOWN_MODE)
    print(
        f"{context_label} Active-mode servo for logical plotter {plotter_index} "
        f"toggled to {current_mode} mode via servo plotter {servo_plotter_index}."
    )
    return message


def draw_svg_pass_on_plotter(svg_text: str, port: str) -> None:
    ad = build_plotter(svg_text, port)

    try:
        ad.plot_run()
    finally:
        try:
            ad.disconnect()
        except Exception:  # pylint: disable=broad-except
            pass


def draw_svg_on_plotter(svg_text: str, port: str, plotter_index: int) -> str:
    context_label = f"[plotter {port}]"
    ensure_active_mode_plotter_servo_ready_for_drawing(context_label, plotter_index)
    print(f"{context_label} Pass 1/2: drawing kolam.")
    draw_svg_pass_on_plotter(svg_text, port)
    servo_message = set_active_mode_arduino_servo_mode(
        context_label,
        plotter_index,
        "erase",
        "Rotating Arduino servo before redraw",
    )
    print(f"{context_label} Waiting {ARDUINO_REDRAW_PAUSE_SECONDS:.1f}s before redraw...")
    time.sleep(max(0.0, ARDUINO_REDRAW_PAUSE_SECONDS))
    print(f"{context_label} Pass 2/2: redrawing the same kolam.")
    try:
        draw_svg_pass_on_plotter(svg_text, port)
    finally:
        set_active_mode_arduino_servo_mode(
            context_label,
            plotter_index,
            "marker",
            "Returning Arduino servo to its original position",
        )
    print(f"{context_label} Two-pass kolam complete and servo restored.")
    return servo_message


def normalize_guided_kolam_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Field 'guidedKolam' must be an object.")

    width = payload.get("width")
    height = payload.get("height")
    width_in = payload.get("widthIn")
    height_in = payload.get("heightIn")
    if not all(isinstance(value, (int, float)) and value > 0 for value in (width, height, width_in, height_in)):
        raise ValueError("guidedKolam width/height and widthIn/heightIn must be positive numbers.")

    raw_pullis = payload.get("pullis")
    raw_visible_pullis = payload.get("visiblePullis")
    if raw_visible_pullis is None:
        raw_visible_pullis = payload.get("visible_pullis")
    raw_branches = payload.get("branches")
    raw_commands = payload.get("commands")
    if not isinstance(raw_pullis, list):
        raise ValueError("guidedKolam pullis must be an array.")
    if raw_visible_pullis is not None and not isinstance(raw_visible_pullis, list):
        raise ValueError("guidedKolam visiblePullis must be an array when provided.")
    if raw_branches is not None and not isinstance(raw_branches, list):
        raise ValueError("guidedKolam branches must be an array.")
    if raw_commands is not None and not isinstance(raw_commands, list):
        raise ValueError("guidedKolam commands must be an array.")

    def parse_point(point: Any) -> dict[str, float]:
        if not isinstance(point, dict):
            raise ValueError("guidedKolam points must be objects.")
        x = point.get("x")
        y = point.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError("guidedKolam point coordinates must be numbers.")
        return {"x": float(x), "y": float(y)}

    pullis = [parse_point(point) for point in raw_pullis]
    visible_pullis = pullis if raw_visible_pullis is None else [parse_point(point) for point in raw_visible_pullis]
    commands: list[dict[str, Any]] = []
    if raw_commands is not None:
        for command in raw_commands:
            if not isinstance(command, dict):
                raise ValueError("guidedKolam commands must be objects.")

            kind = command.get("kind")
            if kind == "break":
                commands.append({"kind": "break"})
                continue

            if kind == "line":
                line = command.get("line")
                if not isinstance(line, dict):
                    raise ValueError("guidedKolam line commands must include a line object.")
                x1 = line.get("x1")
                y1 = line.get("y1")
                x2 = line.get("x2")
                y2 = line.get("y2")
                if not all(isinstance(value, (int, float)) for value in (x1, y1, x2, y2)):
                    raise ValueError("guidedKolam line coordinates must be numbers.")
                commands.append(
                    {
                        "kind": "line",
                        "line": {
                            "x1": float(x1),
                            "y1": float(y1),
                            "x2": float(x2),
                            "y2": float(y2),
                        },
                    }
                )
                continue

            if kind == "arc":
                arc = command.get("arc")
                if not isinstance(arc, dict):
                    raise ValueError("guidedKolam arc commands must include an arc object.")
                cx = arc.get("cx")
                cy = arc.get("cy")
                radius = arc.get("radius")
                rotation = arc.get("rotation")
                start = arc.get("start")
                stop = arc.get("stop")
                if not all(
                    isinstance(value, (int, float))
                    for value in (cx, cy, radius, rotation, start, stop)
                ):
                    raise ValueError("guidedKolam arc values must be numbers.")
                commands.append(
                    {
                        "kind": "arc",
                        "arc": {
                            "cx": float(cx),
                            "cy": float(cy),
                            "radius": float(radius),
                            "rotation": float(rotation),
                            "start": float(start),
                            "stop": float(stop),
                        },
                    }
                )
                continue

            raise ValueError("guidedKolam command kind must be line, arc, or break.")

    branches: list[dict[str, Any]] = []
    if raw_branches is not None:
        for branch in raw_branches:
            if not isinstance(branch, dict):
                raise ValueError("guidedKolam branches must be objects.")
            branch_id = branch.get("branchId")
            raw_points = branch.get("points")
            if not isinstance(branch_id, int):
                raise ValueError("guidedKolam branchId must be an integer.")
            if not isinstance(raw_points, list):
                raise ValueError("guidedKolam branch points must be an array.")
            points = [parse_point(point) for point in raw_points]
            if len(points) < 2:
                continue
            branches.append({"branchId": branch_id, "points": points})

    if not commands and not branches:
        raise ValueError("guidedKolam must contain stitched commands or branch points.")

    dot_style = payload.get("dotStyle")
    if not isinstance(dot_style, str) or dot_style.strip().lower() not in {"circle", "dash", "cross"}:
        dot_style = GUIDED_KOLAM_DOT_STYLE
    else:
        dot_style = dot_style.strip().lower()

    dot_mark_units = payload.get("dotMarkPx")
    if not isinstance(dot_mark_units, (int, float)) or dot_mark_units <= 0:
        dot_mark_units = GUIDED_KOLAM_DOT_MARK_UNITS

    return {
        "width": float(width),
        "height": float(height),
        "width_in": float(width_in),
        "height_in": float(height_in),
        "pullis": pullis,
        "visible_pullis": visible_pullis,
        "commands": commands,
        "branches": branches,
        "dot_style": dot_style,
        "dot_mark_units": float(dot_mark_units),
    }


def guided_point_to_inches(
    point: dict[str, float],
    model_width: float,
    model_height: float,
    output_width_in: float,
    output_height_in: float,
    x_offset_in: float,
    y_offset_in: float,
) -> tuple[float, float]:
    x = x_offset_in + (point["x"] / model_width) * output_width_in
    y = y_offset_in + (point["y"] / model_height) * output_height_in
    return x, y


def sample_guided_arc_points(
    arc: dict[str, float], min_segments: int = GUIDED_ARC_MIN_SEGMENTS
) -> list[dict[str, float]]:
    span = abs(arc["stop"] - arc["start"])
    segments = max(min_segments, int(math.ceil(span / (math.pi / 18))))
    points: list[dict[str, float]] = []

    for idx in range(segments + 1):
        theta = arc["start"] + (arc["stop"] - arc["start"]) * (idx / segments)
        local_x = arc["radius"] * math.cos(theta)
        local_y = arc["radius"] * math.sin(theta)
        rotated_x = math.cos(arc["rotation"]) * local_x - math.sin(arc["rotation"]) * local_y
        rotated_y = math.sin(arc["rotation"]) * local_x + math.cos(arc["rotation"]) * local_y
        points.append({"x": arc["cx"] + rotated_x, "y": arc["cy"] + rotated_y})

    return points


def draw_guided_line_command(
    ad: axidraw.AxiDraw,
    command: dict[str, Any],
    model_width: float,
    model_height: float,
    output_width_in: float,
    output_height_in: float,
    x_offset_in: float,
    y_offset_in: float,
    move_to_start: bool = True,
) -> None:
    line = command["line"]
    start_x, start_y = guided_point_to_inches(
        {"x": line["x1"], "y": line["y1"]},
        model_width,
        model_height,
        output_width_in,
        output_height_in,
        x_offset_in,
        y_offset_in,
    )
    end_x, end_y = guided_point_to_inches(
        {"x": line["x2"], "y": line["y2"]},
        model_width,
        model_height,
        output_width_in,
        output_height_in,
        x_offset_in,
        y_offset_in,
    )

    if move_to_start:
        ad.penup()
        ad.moveto(start_x, start_y)
        ad.pendown()
    else:
        ad.lineto(start_x, start_y)
    ad.lineto(end_x, end_y)


def draw_guided_arc_command(
    ad: axidraw.AxiDraw,
    command: dict[str, Any],
    model_width: float,
    model_height: float,
    output_width_in: float,
    output_height_in: float,
    x_offset_in: float,
    y_offset_in: float,
    min_segments: int = GUIDED_ARC_MIN_SEGMENTS,
    move_to_start: bool = True,
) -> None:
    points = sample_guided_arc_points(command["arc"], min_segments=min_segments)
    if not points:
        return

    start_x, start_y = guided_point_to_inches(
        points[0],
        model_width,
        model_height,
        output_width_in,
        output_height_in,
        x_offset_in,
        y_offset_in,
    )
    if move_to_start:
        ad.penup()
        ad.moveto(start_x, start_y)
        ad.pendown()
    else:
        ad.lineto(start_x, start_y)

    for point in points[1:]:
        x_in, y_in = guided_point_to_inches(
            point,
            model_width,
            model_height,
            output_width_in,
            output_height_in,
            x_offset_in,
            y_offset_in,
        )
        ad.lineto(x_in, y_in)


def draw_guided_dot_markers(
    ad: axidraw.AxiDraw,
    pullis: list[dict[str, float]],
    model_width: float,
    model_height: float,
    output_width_in: float,
    output_height_in: float,
    x_offset_in: float,
    y_offset_in: float,
    mark_units: float,
    dot_style: str,
) -> None:
    half = mark_units / 2.0
    cross_half = half * CROSS_MARK_SCALE

    for dot in pullis:
        if dot_style == "circle":
            segments = 18
            points: list[dict[str, float]] = []
            for idx in range(segments + 1):
                theta = (2.0 * 3.141592653589793 * idx) / segments
                points.append(
                    {
                        "x": dot["x"] + half * math.cos(theta),
                        "y": dot["y"] + half * math.sin(theta),
                    }
                )

            start_x, start_y = guided_point_to_inches(
                points[0],
                model_width,
                model_height,
                output_width_in,
                output_height_in,
                x_offset_in,
                y_offset_in,
            )
            ad.penup()
            ad.moveto(start_x, start_y)
            ad.pendown()
            for point in points[1:]:
                x_in, y_in = guided_point_to_inches(
                    point,
                    model_width,
                    model_height,
                    output_width_in,
                    output_height_in,
                    x_offset_in,
                    y_offset_in,
                )
                ad.lineto(x_in, y_in)
            ad.penup()
            continue

        marker_half = cross_half if dot_style == "cross" else half
        start_x, start_y = guided_point_to_inches(
            {"x": dot["x"] - marker_half, "y": dot["y"]},
            model_width,
            model_height,
            output_width_in,
            output_height_in,
            x_offset_in,
            y_offset_in,
        )
        end_x, end_y = guided_point_to_inches(
            {"x": dot["x"] + marker_half, "y": dot["y"]},
            model_width,
            model_height,
            output_width_in,
            output_height_in,
            x_offset_in,
            y_offset_in,
        )
        ad.penup()
        ad.moveto(start_x, start_y)
        ad.pendown()
        ad.lineto(end_x, end_y)
        ad.penup()

        if dot_style == "cross":
            start_x, start_y = guided_point_to_inches(
                {"x": dot["x"], "y": dot["y"] - marker_half},
                model_width,
                model_height,
                output_width_in,
                output_height_in,
                x_offset_in,
                y_offset_in,
            )
            end_x, end_y = guided_point_to_inches(
                {"x": dot["x"], "y": dot["y"] + marker_half},
                model_width,
                model_height,
                output_width_in,
                output_height_in,
                x_offset_in,
                y_offset_in,
            )
            ad.moveto(start_x, start_y)
            ad.pendown()
            ad.lineto(end_x, end_y)
            ad.penup()


def draw_guided_kolam_pass_on_plotter(
    ad: axidraw.AxiDraw,
    guided_kolam: dict[str, Any],
    context_label: str,
    pass_label: str,
    output_width_in: float | None = None,
    output_height_in: float | None = None,
    x_offset_in: float | None = None,
    y_offset_in: float | None = None,
    return_home: bool = True,
) -> None:
    actual_output_width_in = guided_kolam["width_in"] if output_width_in is None else output_width_in
    actual_output_height_in = guided_kolam["height_in"] if output_height_in is None else output_height_in
    actual_x_offset_in = GUIDED_KOLAM_X_OFFSET_IN if x_offset_in is None else x_offset_in
    actual_y_offset_in = GUIDED_KOLAM_Y_OFFSET_IN if y_offset_in is None else y_offset_in

    print(f"{context_label} {pass_label}: drawing pulli grid markers.")
    draw_guided_dot_markers(
        ad,
        guided_kolam["pullis"],
        model_width=guided_kolam["width"],
        model_height=guided_kolam["height"],
        output_width_in=actual_output_width_in,
        output_height_in=actual_output_height_in,
        x_offset_in=actual_x_offset_in,
        y_offset_in=actual_y_offset_in,
        mark_units=guided_kolam["dot_mark_units"],
        dot_style=guided_kolam["dot_style"],
    )

    print(f"{context_label} {pass_label}: drawing kolam strokes.")
    commands = guided_kolam.get("commands", [])
    if commands:
        move_to_start = True
        for command in commands:
            if command["kind"] == "break":
                ad.penup()
                move_to_start = True
                continue

            if command["kind"] == "line":
                draw_guided_line_command(
                    ad,
                    command,
                    model_width=guided_kolam["width"],
                    model_height=guided_kolam["height"],
                    output_width_in=actual_output_width_in,
                    output_height_in=actual_output_height_in,
                    x_offset_in=actual_x_offset_in,
                    y_offset_in=actual_y_offset_in,
                    move_to_start=move_to_start,
                )
                move_to_start = False
                continue

            if command["kind"] == "arc":
                draw_guided_arc_command(
                    ad,
                    command,
                    model_width=guided_kolam["width"],
                    model_height=guided_kolam["height"],
                    output_width_in=actual_output_width_in,
                    output_height_in=actual_output_height_in,
                    x_offset_in=actual_x_offset_in,
                    y_offset_in=actual_y_offset_in,
                    min_segments=GUIDED_ARC_MIN_SEGMENTS,
                    move_to_start=move_to_start,
                )
                move_to_start = False

        ad.penup()
    else:
        for branch in guided_kolam["branches"]:
            points = branch["points"]
            start_x, start_y = guided_point_to_inches(
                points[0],
                guided_kolam["width"],
                guided_kolam["height"],
                actual_output_width_in,
                actual_output_height_in,
                actual_x_offset_in,
                actual_y_offset_in,
            )
            ad.penup()
            ad.moveto(start_x, start_y)
            ad.pendown()
            for point in points[1:]:
                x_in, y_in = guided_point_to_inches(
                    point,
                    guided_kolam["width"],
                    guided_kolam["height"],
                    actual_output_width_in,
                    actual_output_height_in,
                    actual_x_offset_in,
                    actual_y_offset_in,
                )
                ad.lineto(x_in, y_in)
            ad.penup()

    if return_home:
        return_interactive_plotter_to_origin(ad)
    else:
        ad.penup()


def active_guided_area_slot_origin(slot_index: int) -> tuple[float, float]:
    columns = max(1, ACTIVE_GUIDED_AREA_COLUMNS)
    rows = max(1, ACTIVE_GUIDED_AREA_ROWS)
    clamped_index = max(0, min(slot_index, (columns * rows) - 1))
    column = clamped_index % columns
    row = clamped_index // columns
    cell_size_in = active_guided_area_cell_size_in()
    x_offset_in = ACTIVE_GUIDED_AREA_ORIGIN_X_IN + ACTIVE_GUIDED_AREA_MARGIN_IN + (
        column * (cell_size_in + ACTIVE_GUIDED_AREA_GAP_IN)
    )
    y_offset_in = ACTIVE_GUIDED_AREA_ORIGIN_Y_IN + ACTIVE_GUIDED_AREA_MARGIN_IN + (
        row * (cell_size_in + ACTIVE_GUIDED_AREA_GAP_IN)
    )
    return x_offset_in, y_offset_in


def active_guided_area_slot_bounds(slot_index: int) -> tuple[float, float, float, float]:
    left, bottom = active_guided_area_slot_origin(slot_index)
    cell_size_in = active_guided_area_cell_size_in()
    right = left + cell_size_in
    top = bottom + cell_size_in
    return left, right, bottom, top


def active_guided_area_draw_bounds() -> tuple[float, float, float, float]:
    columns = max(1, ACTIVE_GUIDED_AREA_COLUMNS)
    rows = max(1, ACTIVE_GUIDED_AREA_ROWS)
    cell_size_in = active_guided_area_cell_size_in()
    used_width = (columns * cell_size_in) + (max(0, columns - 1) * ACTIVE_GUIDED_AREA_GAP_IN)
    used_height = (rows * cell_size_in) + (max(0, rows - 1) * ACTIVE_GUIDED_AREA_GAP_IN)
    left = ACTIVE_GUIDED_AREA_ORIGIN_X_IN + ACTIVE_GUIDED_AREA_MARGIN_IN
    bottom = ACTIVE_GUIDED_AREA_ORIGIN_Y_IN + ACTIVE_GUIDED_AREA_MARGIN_IN
    right = left + used_width
    top = bottom + used_height
    return left, right, bottom, top


def active_guided_area_configured_bounds() -> tuple[float, float, float, float]:
    left = ACTIVE_GUIDED_AREA_ORIGIN_X_IN
    bottom = ACTIVE_GUIDED_AREA_ORIGIN_Y_IN
    right = left + ACTIVE_GUIDED_AREA_WIDTH_IN
    top = bottom + ACTIVE_GUIDED_AREA_HEIGHT_IN
    return left, right, bottom, top


def erase_active_guided_area(ad: axidraw.AxiDraw, context_label: str) -> None:
    area_left, area_right, area_bottom, area_top = active_guided_area_configured_bounds()
    left_overscan = max(0.0, ACTIVE_GUIDED_ERASE_X_OVERSCAN_LEFT_IN)
    right_overscan = max(0.0, ACTIVE_GUIDED_ERASE_X_OVERSCAN_RIGHT_IN)
    bottom_overscan = max(0.0, ACTIVE_GUIDED_ERASE_Y_OVERSCAN_BOTTOM_IN)
    top_overscan = max(0.0, ACTIVE_GUIDED_ERASE_Y_OVERSCAN_TOP_IN)
    left = max(0.0, area_left + ACTIVE_GUIDED_ERASE_OFFSET_X_IN - left_overscan)
    right = area_right + ACTIVE_GUIDED_ERASE_OFFSET_X_IN + right_overscan
    bottom = max(0.0, area_bottom + ACTIVE_GUIDED_ERASE_OFFSET_Y_IN - bottom_overscan)
    top = area_top + ACTIVE_GUIDED_ERASE_OFFSET_Y_IN + top_overscan
    sweep_step = max(0.05, ACTIVE_GUIDED_ERASE_SWEEP_STEP_IN)
    x_positions: list[float] = []
    current_x = left
    while current_x < right:
        x_positions.append(current_x)
        current_x += sweep_step
    x_positions.append(right)

    print(
        f"{context_label} Erasing packed draw area from ({left:.2f}, {bottom:.2f}) to ({right:.2f}, {top:.2f}) "
        f"with vertical sweeps every {sweep_step:.2f} in."
    )
    for sweep_index, x_position in enumerate(x_positions):
        start_y = bottom if sweep_index % 2 == 0 else top
        end_y = top if sweep_index % 2 == 0 else bottom
        ad.penup()
        ad.moveto(x_position, start_y)
        ad.pendown()
        ad.lineto(x_position, end_y)
        ad.penup()

    return_interactive_plotter_to_origin(ad)


def prepare_plotter_for_erase_trace(ad: axidraw.AxiDraw, context_label: str, plotter_index: int) -> None:
    ensure_active_mode_plotter_servo_ready_for_drawing(context_label, plotter_index)
    print(
        f"{context_label} Moving to erase prep point "
        f"({ACTIVE_GUIDED_ERASE_PREP_X_IN:.2f}, {ACTIVE_GUIDED_ERASE_PREP_Y_IN:.2f}) in pen mode."
    )
    ad.penup()
    ad.moveto(ACTIVE_GUIDED_ERASE_PREP_X_IN, ACTIVE_GUIDED_ERASE_PREP_Y_IN)
    ad.block()

    if active_mode_plotter_uses_toggle_servo_transitions(plotter_index):
        toggle_active_mode_plotter_servo(
            context_label,
            plotter_index,
            "Toggling Arduino servo into erase mode after packed-area draw",
        )
    else:
        set_active_mode_arduino_servo_mode(
            context_label,
            plotter_index,
            "erase",
            "Rotating Arduino servo into erase mode",
        )
    print(f"{context_label} Waiting {ARDUINO_REDRAW_PAUSE_SECONDS:.1f}s for eraser to settle...")
    time.sleep(max(0.0, ARDUINO_REDRAW_PAUSE_SECONDS))

    print(f"{context_label} Tapping pen at erase prep point before erase trace.")
    ad.pendown()
    ad.penup()
    ad.block()


def build_erase_trace_demo_pattern(plotter_index: int) -> tuple[Any, Any, int, int]:
    module = load_passive_generator_module()
    demo_session_seed = time.time_ns() % 1_000_000_000
    pattern_number = 1
    pattern_seed = (
        demo_session_seed * module.PATTERN_SEED_SCALE
        + plotter_index * module.PLOTTER_SEED_SCALE
        + pattern_number
    )
    pattern_rng = random.Random(pattern_seed)
    size = pattern_rng.randint(PASSIVE_PACKED_AREA_SIZE_MIN, PASSIVE_PACKED_AREA_SIZE_MAX)
    generator = module.DFSKolamGenerator(size=size, spacing=PASSIVE_PREVIEW_SPACING, rng=pattern_rng)
    pattern = generator.generate()
    return module, pattern, pattern_seed, size


def build_erase_demo_pattern_layout(plotter_index: int) -> dict[str, Any]:
    module, pattern, pattern_seed, size = build_erase_trace_demo_pattern(plotter_index)
    min_x, min_y, max_x, max_y = module.pattern_bounds(pattern)
    cell_size_in = active_guided_area_cell_size_in()
    max_dimension_px = max(max_x - min_x, max_y - min_y, 1.0)
    packed_pixels_per_inch = max_dimension_px / cell_size_in
    normalized_pattern = module.shifted_pattern(pattern, -min_x, -min_y)
    slot_count = active_guided_area_slot_count()
    slot_x_in, slot_y_in = active_guided_area_slot_origin(0)
    return {
        "module": module,
        "pattern_seed": pattern_seed,
        "size": size,
        "normalized_pattern": normalized_pattern,
        "pixels_per_inch": packed_pixels_per_inch,
        "slot_count": slot_count,
        "slot_x_in": slot_x_in,
        "slot_y_in": slot_y_in,
        "pattern_width_in": (max_x - min_x) / packed_pixels_per_inch,
        "pattern_height_in": (max_y - min_y) / packed_pixels_per_inch,
    }


def erase_sweep_bounds(
    ad: axidraw.AxiDraw,
    context_label: str,
    left_in: float,
    right_in: float,
    bottom_in: float,
    top_in: float,
    sweep_step_in: float | None = None,
) -> None:
    left = max(0.0, left_in)
    right = max(left, right_in)
    bottom = max(0.0, bottom_in)
    top = max(bottom, top_in)
    sweep_step = max(0.05, ACTIVE_GUIDED_ERASE_SWEEP_STEP_IN if sweep_step_in is None else sweep_step_in)
    x_positions: list[float] = []
    current_x = left
    while current_x < right:
        x_positions.append(current_x)
        current_x += sweep_step
    x_positions.append(right)

    print(
        f"{context_label} Erasing demo area from ({left:.2f}, {bottom:.2f}) to ({right:.2f}, {top:.2f}) "
        f"with vertical sweeps every {sweep_step:.2f} in."
    )
    for sweep_index, x_position in enumerate(x_positions):
        start_y = bottom if sweep_index % 2 == 0 else top
        end_y = top if sweep_index % 2 == 0 else bottom
        ad.penup()
        ad.moveto(x_position, start_y)
        ad.pendown()
        ad.lineto(x_position, end_y)
        ad.penup()


def draw_rectangle_bounds(
    ad: axidraw.AxiDraw,
    context_label: str,
    left_in: float,
    right_in: float,
    bottom_in: float,
    top_in: float,
) -> None:
    left = max(0.0, left_in)
    right = max(left, right_in)
    bottom = max(0.0, bottom_in)
    top = max(bottom, top_in)

    print(
        f"{context_label} Drawing rectangle in area from ({left:.2f}, {bottom:.2f}) "
        f"to ({right:.2f}, {top:.2f})."
    )
    ad.penup()
    ad.moveto(left, bottom)
    ad.pendown()
    ad.lineto(right, bottom)
    ad.lineto(right, top)
    ad.lineto(left, top)
    ad.lineto(left, bottom)
    ad.penup()


def erase_trace_active_guided_area(
    ad: axidraw.AxiDraw,
    port: str,
    context_label: str,
    plotter_index: int,
) -> int:
    slot_count = active_guided_area_slot_count()
    stored_guided_kolams = snapshot_active_guided_area_slot_guided_kolams(port, slot_count)
    occupied_slots = [
        (slot_index, guided_kolam)
        for slot_index, guided_kolam in enumerate(stored_guided_kolams)
        if isinstance(guided_kolam, dict)
    ]
    if not occupied_slots:
        print(f"{context_label} Packed area is empty. Nothing to erase trace.")
        return 0

    prepare_plotter_for_erase_trace(ad, context_label, plotter_index)
    cell_size_in = active_guided_area_cell_size_in()
    try:
        print(
            f"{context_label} Erase trace will retrace {len(occupied_slots)}/{slot_count} "
            "stored packed-area kolam(s)."
        )
        for slot_index, guided_kolam in occupied_slots:
            x_offset_in, y_offset_in = active_guided_area_slot_origin(slot_index)
            print(
                f"{context_label} Erase trace retracing area slot {slot_index + 1}/{slot_count}."
            )
            draw_guided_kolam_pass_on_plotter(
                ad,
                guided_kolam,
                context_label,
                f"Erase trace slot {slot_index + 1}/{slot_count}",
                output_width_in=cell_size_in,
                output_height_in=cell_size_in,
                x_offset_in=x_offset_in + ERASE_TRACE_OFFSET_X_IN,
                y_offset_in=y_offset_in,
                return_home=False,
            )
    finally:
        try:
            set_active_mode_arduino_servo_mode(
                context_label,
                plotter_index,
                "marker",
                "Returning Arduino servo to marker mode after erase trace",
            )
        finally:
            return_interactive_plotter_to_origin(ad)

    return len(occupied_slots)


def sweep_active_guided_area(
    ad: axidraw.AxiDraw,
    context_label: str,
    plotter_index: int,
) -> None:
    prepare_plotter_for_erase_trace(ad, context_label, plotter_index)
    try:
        erase_active_guided_area(ad, context_label)
    finally:
        try:
            set_active_mode_arduino_servo_mode(
                context_label,
                plotter_index,
                "marker",
                "Returning Arduino servo to marker mode after erase sweep",
            )
        finally:
            return_interactive_plotter_to_origin(ad)


def trace_active_guided_area_on_plotter(port: str, plotter_index: int) -> str:
    context_label = f"[plotter {port}]"
    port_state = get_active_guided_area_port_state(port)
    set_active_guided_area_port_state(port, 0, port_state["cycles_completed"])
    clear_active_guided_area_slot_previews(port)
    layout = build_erase_demo_pattern_layout(plotter_index)
    print(
        f"{context_label} Drawing passive-mode erase trace demo into area slot 1/{layout['slot_count']}. "
        f"Seed {layout['pattern_seed']} | size {layout['size']}x{layout['size']}."
    )

    ad = build_interactive_plotter(port)
    try:
        ensure_active_mode_plotter_servo_ready_for_drawing(context_label, plotter_index)
        layout["module"].draw_pattern(
            ad,
            layout["normalized_pattern"],
            pixels_per_inch=layout["pixels_per_inch"],
            x_offset_in=layout["slot_x_in"],
            y_offset_in=layout["slot_y_in"],
            dot_mark_px=GUIDED_KOLAM_DOT_MARK_UNITS,
            dot_style=PASSIVE_PREVIEW_DOT_STYLE,
            arc_segments_min=PASSIVE_PREVIEW_ARC_SEGMENTS,
            preview=None,
            label=f"plotter {port}",
            pass_label=f"Area slot 1/{layout['slot_count']}",
        )
        prepare_plotter_for_erase_trace(ad, context_label, plotter_index)
        try:
            layout["module"].draw_pattern(
                ad,
                layout["normalized_pattern"],
                pixels_per_inch=layout["pixels_per_inch"],
                x_offset_in=layout["slot_x_in"] + ERASE_TRACE_OFFSET_X_IN,
                y_offset_in=layout["slot_y_in"],
                dot_mark_px=GUIDED_KOLAM_DOT_MARK_UNITS,
                dot_style=PASSIVE_PREVIEW_DOT_STYLE,
                arc_segments_min=PASSIVE_PREVIEW_ARC_SEGMENTS,
                preview=None,
                label=f"plotter {port}",
                pass_label="Erase trace retrace",
            )
        finally:
            try:
                set_active_mode_arduino_servo_mode(
                    context_label,
                    plotter_index,
                    "marker",
                    "Returning Arduino servo to marker mode after erase trace",
                )
            finally:
                return_interactive_plotter_to_origin(ad)
        set_active_guided_area_port_state(port, 0, port_state["cycles_completed"])
        clear_active_guided_area_slot_previews(port)
        print(f"{context_label} Erase trace demo finished. Packed area slot state reset for this plotter.")
        return (
            f"drew 1 passive-mode kolam (seed {layout['pattern_seed']}, size {layout['size']}x{layout['size']}), "
            "then erased it by retracing in erase mode and reset packed-area slot state"
        )
    finally:
        try:
            ad.disconnect()
        except Exception:  # pylint: disable=broad-except
            pass


def sweep_demo_on_plotter(port: str, plotter_index: int) -> str:
    context_label = f"[plotter {port}]"
    port_state = get_active_guided_area_port_state(port)
    set_active_guided_area_port_state(port, 0, port_state["cycles_completed"])
    clear_active_guided_area_slot_previews(port)
    layout = build_erase_demo_pattern_layout(plotter_index)
    print(
        f"{context_label} Drawing passive-mode erase sweep demo into area slot 1/{layout['slot_count']}. "
        f"Seed {layout['pattern_seed']} | size {layout['size']}x{layout['size']}."
    )

    sweep_left_in = layout["slot_x_in"] + ERASE_TRACE_OFFSET_X_IN - ERASE_SWEEP_DEMO_PADDING_IN
    sweep_right_in = (
        layout["slot_x_in"]
        + ERASE_TRACE_OFFSET_X_IN
        + layout["pattern_width_in"]
        + ERASE_SWEEP_DEMO_PADDING_IN
    )
    sweep_bottom_in = layout["slot_y_in"] - ERASE_SWEEP_DEMO_PADDING_IN
    sweep_top_in = layout["slot_y_in"] + layout["pattern_height_in"] + ERASE_SWEEP_DEMO_PADDING_IN

    ad = build_interactive_plotter(port)
    try:
        ensure_active_mode_plotter_servo_ready_for_drawing(context_label, plotter_index)
        layout["module"].draw_pattern(
            ad,
            layout["normalized_pattern"],
            pixels_per_inch=layout["pixels_per_inch"],
            x_offset_in=layout["slot_x_in"],
            y_offset_in=layout["slot_y_in"],
            dot_mark_px=GUIDED_KOLAM_DOT_MARK_UNITS,
            dot_style=PASSIVE_PREVIEW_DOT_STYLE,
            arc_segments_min=PASSIVE_PREVIEW_ARC_SEGMENTS,
            preview=None,
            label=f"plotter {port}",
            pass_label=f"Area slot 1/{layout['slot_count']}",
        )
        prepare_plotter_for_erase_trace(ad, context_label, plotter_index)
        try:
            erase_sweep_bounds(
                ad,
                context_label,
                sweep_left_in,
                sweep_right_in,
                sweep_bottom_in,
                sweep_top_in,
                sweep_step_in=ACTIVE_GUIDED_ERASE_SWEEP_STEP_IN,
            )
        finally:
            try:
                set_active_mode_arduino_servo_mode(
                    context_label,
                    plotter_index,
                    "marker",
                    "Returning Arduino servo to marker mode after erase sweep demo",
                )
            finally:
                return_interactive_plotter_to_origin(ad)
        set_active_guided_area_port_state(port, 0, port_state["cycles_completed"])
        clear_active_guided_area_slot_previews(port)
        print(f"{context_label} Erase sweep demo finished. Packed area slot state reset for this plotter.")
        return (
            f"drew 1 passive-mode kolam (seed {layout['pattern_seed']}, size {layout['size']}x{layout['size']}), "
            "then erased it with a vertical sweep over the kolam bounds plus 2 cm padding on all sides"
        )
    finally:
        try:
            ad.disconnect()
        except Exception:  # pylint: disable=broad-except
            pass


def trace_active_guided_area_on_plotters(plotter_indices_by_port: dict[str, int]) -> dict[str, str]:
    ordered_ports = sorted(plotter_indices_by_port)
    start_event = threading.Event()
    errors: list[str] = []
    actions_by_port: dict[str, str] = {}
    results_lock = threading.Lock()

    def worker(port: str) -> None:
        plotter_index = plotter_indices_by_port[port]
        try:
            start_event.wait()
            action = trace_active_guided_area_on_plotter(port, plotter_index)
            with results_lock:
                actions_by_port[port] = action
        except Exception as exc:  # pylint: disable=broad-except
            with results_lock:
                errors.append(f"{port}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(port,), daemon=True)
        for port in ordered_ports
    ]
    for thread in threads:
        thread.start()

    start_event.set()

    for thread in threads:
        thread.join()

    if errors:
        raise RuntimeError("; ".join(errors))

    return actions_by_port


def sweep_demo_on_plotters(plotter_indices_by_port: dict[str, int]) -> dict[str, str]:
    ordered_ports = sorted(plotter_indices_by_port)
    start_event = threading.Event()
    errors: list[str] = []
    actions_by_port: dict[str, str] = {}
    results_lock = threading.Lock()

    def worker(port: str) -> None:
        plotter_index = plotter_indices_by_port[port]
        try:
            start_event.wait()
            action = sweep_demo_on_plotter(port, plotter_index)
            with results_lock:
                actions_by_port[port] = action
        except Exception as exc:  # pylint: disable=broad-except
            with results_lock:
                errors.append(f"{port}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(port,), daemon=True)
        for port in ordered_ports
    ]
    for thread in threads:
        thread.start()

    start_event.set()

    for thread in threads:
        thread.join()

    if errors:
        raise RuntimeError("; ".join(errors))

    return actions_by_port


def sweep_active_guided_area_on_plotter(port: str, plotter_index: int) -> str:
    ad = build_interactive_plotter(port)
    context_label = f"[plotter {port}]"
    try:
        sweep_active_guided_area(ad, context_label, plotter_index)
        port_state = get_active_guided_area_port_state(port)
        set_active_guided_area_port_state(port, 0, port_state["cycles_completed"])
        clear_active_guided_area_slot_previews(port)
        print(f"{context_label} Manual sweep finished. Packed area slot state reset for this plotter.")
        return "swept packed area with vertical sweeps and reset packed-area slot state"
    finally:
        try:
            ad.disconnect()
        except Exception:  # pylint: disable=broad-except
            pass


def horizontal_sweep_active_guided_area_on_plotter(port: str, plotter_index: int) -> str:
    ad = build_interactive_plotter(port)
    context_label = f"[plotter {port}]"
    try:
        ensure_active_mode_plotter_servo_ready_for_drawing(context_label, plotter_index)
        left, right, bottom, top = active_guided_area_configured_bounds()
        draw_rectangle_bounds(
            ad,
            context_label,
            left,
            right,
            bottom,
            top,
        )
        try:
            set_active_mode_arduino_servo_mode(
                context_label,
                plotter_index,
                "erase",
                "Rotating Arduino servo into erase mode for rectangle erase",
            )
            print(f"{context_label} Waiting {ARDUINO_REDRAW_PAUSE_SECONDS:.1f}s for eraser to settle...")
            time.sleep(max(0.0, ARDUINO_REDRAW_PAUSE_SECONDS))
            draw_rectangle_bounds(
                ad,
                context_label,
                left,
                right,
                bottom,
                top,
            )
        finally:
            set_active_mode_arduino_servo_mode(
                context_label,
                plotter_index,
                "marker",
                "Returning Arduino servo to marker mode after rectangle erase test",
            )
        port_state = get_active_guided_area_port_state(port)
        set_active_guided_area_port_state(port, 0, port_state["cycles_completed"])
        clear_active_guided_area_slot_previews(port)
        print(
            f"{context_label} Rectangle erase test finished. "
            "Packed area slot state reset for this plotter."
        )
        return_interactive_plotter_to_origin(ad)
        return (
            "drew a rectangle in the configured guided kolam area, flipped to erase mode, "
            "retraced it, and reset packed-area slot state"
        )
    finally:
        try:
            ad.disconnect()
        except Exception:  # pylint: disable=broad-except
            pass


def horizontal_sweep_active_guided_area_on_plotters(
    plotter_indices_by_port: dict[str, int],
) -> dict[str, str]:
    ordered_ports = sorted(plotter_indices_by_port)
    start_event = threading.Event()
    errors: list[str] = []
    actions_by_port: dict[str, str] = {}
    results_lock = threading.Lock()

    def worker(port: str) -> None:
        plotter_index = plotter_indices_by_port[port]
        try:
            start_event.wait()
            action = horizontal_sweep_active_guided_area_on_plotter(port, plotter_index)
            with results_lock:
                actions_by_port[port] = action
        except Exception as exc:  # pylint: disable=broad-except
            with results_lock:
                errors.append(f"{port}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(port,), daemon=True)
        for port in ordered_ports
    ]
    for thread in threads:
        thread.start()

    start_event.set()

    for thread in threads:
        thread.join()

    if errors:
        raise RuntimeError("; ".join(errors))

    return actions_by_port


def sweep_active_guided_area_on_plotters(
    ports: list[str],
    *,
    mapping_ports: list[str] | None = None,
) -> dict[str, str]:
    ordered_ports = sorted(ports)
    plotter_indices = plotter_indices_by_port(mapping_ports if mapping_ports is not None else ordered_ports)
    start_event = threading.Event()
    errors: list[str] = []
    actions_by_port: dict[str, str] = {}
    results_lock = threading.Lock()

    def worker(port: str) -> None:
        try:
            start_event.wait()
            plotter_index = plotter_indices.get(port)
            if plotter_index is None:
                raise RuntimeError(f"Could not determine plotter index for {port}.")
            action = sweep_active_guided_area_on_plotter(port, plotter_index)
            with results_lock:
                actions_by_port[port] = action
        except Exception as exc:  # pylint: disable=broad-except
            with results_lock:
                errors.append(f"{port}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(port,), daemon=True)
        for port in ordered_ports
    ]
    for thread in threads:
        thread.start()

    start_event.set()

    for thread in threads:
        thread.join()

    if errors:
        raise RuntimeError("; ".join(errors))

    return actions_by_port


def draw_guided_kolam_in_active_area(
    guided_kolam: dict[str, Any],
    port: str,
    plotter_index: int,
) -> dict[str, Any]:
    ad = build_interactive_plotter(port)
    context_label = f"[plotter {port}]"
    port_state = get_active_guided_area_port_state(port)
    slot_index = port_state["next_slot_index"]
    cycles_completed = port_state["cycles_completed"]
    slot_count = active_guided_area_slot_count()
    cell_size_in = active_guided_area_cell_size_in()
    x_offset_in, y_offset_in = active_guided_area_slot_origin(slot_index)
    slot_label = f"Area slot {slot_index + 1}/{slot_count}"
    message = ""

    try:
        ensure_active_mode_plotter_servo_ready_for_drawing(context_label, plotter_index)
        draw_guided_kolam_pass_on_plotter(
            ad,
            guided_kolam,
            context_label,
            slot_label,
            output_width_in=cell_size_in,
            output_height_in=cell_size_in,
            x_offset_in=x_offset_in,
            y_offset_in=y_offset_in,
            return_home=True,
        )
        set_active_guided_area_slot_preview(
            port,
            slot_index,
            {
                "slot_index": slot_index,
                "svg": render_guided_kolam_preview_svg(
                    guided_kolam,
                    f"Plotter {plotter_label(plotter_index - 1)} area slot {slot_index + 1} preview",
                ),
            },
        )
        set_active_guided_area_slot_guided_kolam(port, slot_index, guided_kolam)

        if slot_index + 1 >= slot_count:
            print(f"{context_label} Active area is full after {slot_label.lower()}.")
            sweep_active_guided_area(ad, context_label, plotter_index)
            cycles_completed += 1
            set_active_guided_area_port_state(port, 0, cycles_completed)
            clear_active_guided_area_slot_previews(port)
            message = (
                f"Placed kolam in area slot {slot_index + 1}/{slot_count} on this "
                f"{ACTIVE_GUIDED_AREA_WIDTH_IN:.1f}x{ACTIVE_GUIDED_AREA_HEIGHT_IN:.1f} in prototype area. "
                "The area filled up, then the bridge swept the packed area in erase mode "
                "and reset the layout."
            )
            next_slot_index = 0
        else:
            next_slot_index = slot_index + 1
            set_active_guided_area_port_state(port, next_slot_index, cycles_completed)
            message = (
                f"Placed kolam in area slot {slot_index + 1}/{slot_count} on this "
                f"{ACTIVE_GUIDED_AREA_WIDTH_IN:.1f}x{ACTIVE_GUIDED_AREA_HEIGHT_IN:.1f} in prototype area. "
                f"Next slot on this plotter: {next_slot_index + 1}/{slot_count}."
            )

        print(f"{context_label} {message}")
        return {
            "message": message,
            "slot_index": slot_index,
            "slot_count": slot_count,
            "next_slot_index": next_slot_index,
            "cycles_completed": cycles_completed,
            "cell_size_in": cell_size_in,
        }
    finally:
        try:
            ad.disconnect()
        except Exception:  # pylint: disable=broad-except
            pass


def draw_guided_kolam_on_plotter(guided_kolam: dict[str, Any], port: str, plotter_index: int) -> str:
    ad = build_interactive_plotter(port)
    context_label = f"[plotter {port}]"

    try:
        ensure_active_mode_plotter_servo_ready_for_drawing(context_label, plotter_index)
        draw_guided_kolam_pass_on_plotter(ad, guided_kolam, context_label, "Pass 1/2")
        servo_message = set_active_mode_arduino_servo_mode(
            context_label,
            plotter_index,
            "erase",
            "Rotating Arduino servo before redraw",
        )
        print(f"{context_label} Waiting {ARDUINO_REDRAW_PAUSE_SECONDS:.1f}s before redraw...")
        time.sleep(max(0.0, ARDUINO_REDRAW_PAUSE_SECONDS))
        try:
            draw_guided_kolam_pass_on_plotter(ad, guided_kolam, context_label, "Pass 2/2")
        finally:
            set_active_mode_arduino_servo_mode(
                context_label,
                plotter_index,
                "marker",
                "Returning Arduino servo to its original position",
            )
        print(f"{context_label} Two-pass kolam complete and servo restored.")
        return servo_message
    finally:
        try:
            ad.disconnect()
        except Exception:  # pylint: disable=broad-except
            pass


def plotter_label(index: int) -> str:
    return chr(ord("A") + index)


def ordered_unique_ports(ports: list[str]) -> list[str]:
    return list(dict.fromkeys(sorted(ports)))


def dedicated_passive_port(ports: list[str]) -> str | None:
    ordered_ports = ordered_unique_ports(ports)
    if len(ordered_ports) < DEDICATED_PASSIVE_PLOTTER_SLOT:
        return None
    return ordered_ports[DEDICATED_PASSIVE_PLOTTER_SLOT - 1]


def port_matches(candidate_port: str | None, expected_port: str | None) -> bool:
    if not candidate_port or not expected_port:
        return False
    return bool(serial_port_aliases(candidate_port).intersection(serial_port_aliases(expected_port)))


def active_mode_plotter_labels(detected_ports: list[str]) -> str:
    active_ports = active_mode_ports(detected_ports, detected_ports)
    labels_by_port = {
        port: plotter_label(plotter_index - 1)
        for port, plotter_index in plotter_indices_by_port(detected_ports).items()
        if port in active_ports
    }
    labels = [labels_by_port[port] for port in active_ports if port in labels_by_port]
    if not labels:
        return "the active plotters"
    return ", ".join(f"Plotter {label}" for label in labels)


def sync_plotter_indices_locked(ports: list[str]) -> None:
    global PLOTTER_INDEX_BY_PORT  # pylint: disable=global-statement

    ordered_ports = list(dict.fromkeys(sorted(ports)))
    port_aliases = {port: serial_port_aliases(port) for port in ordered_ports}
    assignments: dict[str, int] = {}

    for plotter_index, configured_port in PLOTTER_PORTS_BY_INDEX.items():
        if configured_port is None:
            continue
        configured_aliases = serial_port_aliases(configured_port)
        for port in ordered_ports:
            if port in assignments:
                continue
            if port_aliases[port].intersection(configured_aliases):
                assignments[port] = plotter_index
                break

    if len(ordered_ports) == 1:
        only_port = ordered_ports[0]
        if only_port not in assignments:
            assignments[only_port] = 1
        PLOTTER_INDEX_BY_PORT = assignments
        return

    used_indices = set(assignments.values())
    for port, plotter_index in PLOTTER_INDEX_BY_PORT.items():
        if port not in ordered_ports or port in assignments:
            continue
        if plotter_index < 1 or plotter_index in used_indices:
            continue
        assignments[port] = plotter_index
        used_indices.add(plotter_index)

    next_plotter_index = 1
    for port in ordered_ports:
        if port in assignments:
            continue
        while next_plotter_index in used_indices:
            next_plotter_index += 1
        assignments[port] = next_plotter_index
        used_indices.add(next_plotter_index)

    PLOTTER_INDEX_BY_PORT = assignments


def plotter_indices_by_port(ports: list[str]) -> dict[str, int]:
    ordered_ports = list(dict.fromkeys(sorted(ports)))
    if not ordered_ports:
        return {}

    with STATE_LOCK:
        sync_plotter_indices_locked(ordered_ports)
        return {
            port: PLOTTER_INDEX_BY_PORT[port]
            for port in ordered_ports
            if port in PLOTTER_INDEX_BY_PORT
        }


def plotter_labels_by_port(ports: list[str]) -> dict[str, str]:
    return {
        port: plotter_label(plotter_index - 1)
        for port, plotter_index in plotter_indices_by_port(ports).items()
    }


def active_mode_ports(ports: list[str], all_ports: list[str] | None = None) -> list[str]:
    ordered_ports = ordered_unique_ports(ports)
    reserved_port = dedicated_passive_port(all_ports if all_ports is not None else ordered_ports)
    return [port for port in ordered_ports if not port_matches(port, reserved_port)]


def active_mode_availability_payload(
    detected_ports: list[str],
    connectable_ports: list[str],
    busy_ports: list[str],
) -> dict[str, Any]:
    reserved_port = dedicated_passive_port(detected_ports)
    active_detected_ports = active_mode_ports(detected_ports, detected_ports)
    active_connectable_ports = active_mode_ports(connectable_ports, detected_ports)
    active_busy_ports = active_mode_ports(busy_ports, detected_ports)
    active_plotter_text = active_mode_plotter_labels(detected_ports)
    reserved_plotter_index = (
        plotter_indices_by_port([reserved_port]).get(reserved_port)
        if reserved_port
        else None
    )
    reserved_plotter_label = (
        plotter_label(reserved_plotter_index - 1)
        if isinstance(reserved_plotter_index, int)
        else f"#{DEDICATED_PASSIVE_PLOTTER_SLOT}"
    )

    if active_connectable_ports:
        return {
            "status": "done",
            "active_detected_ports": active_detected_ports,
            "active_connectable_ports": active_connectable_ports,
            "active_busy_ports": active_busy_ports,
            "dedicated_passive_port": reserved_port,
        }

    if active_busy_ports:
        return {
            "status": "busy",
            "message": f"{active_plotter_text} are currently busy.",
            "busy_ports": active_busy_ports,
            "detected_ports": detected_ports,
            "active_detected_ports": active_detected_ports,
            "active_connectable_ports": active_connectable_ports,
            "dedicated_passive_port": reserved_port,
        }

    if active_detected_ports:
        return {
            "status": "plotter_unavailable",
            "message": (
                f"{active_plotter_text} were detected, but their connections could not be opened."
            ),
            "ports": active_detected_ports,
            "detected_ports": detected_ports,
            "active_detected_ports": active_detected_ports,
            "active_connectable_ports": active_connectable_ports,
            "dedicated_passive_port": reserved_port,
        }

    if detected_ports:
        return {
            "status": "no_active_plotter",
            "message": (
                f"Active mode is limited to {active_plotter_text}. "
                f"Plotter {reserved_plotter_label} stays reserved for passive mode."
            ),
            "detected_ports": detected_ports,
            "active_detected_ports": active_detected_ports,
            "active_connectable_ports": active_connectable_ports,
            "dedicated_passive_port": reserved_port,
        }

    return {
        "status": "no_plotter",
        "message": "No plotter connected.",
        "detected_ports": detected_ports,
        "active_detected_ports": active_detected_ports,
        "active_connectable_ports": active_connectable_ports,
        "dedicated_passive_port": reserved_port,
    }


def arduino_plotter_index(plotter_index: int | None) -> int:
    selected_index = 1 if plotter_index is None else int(plotter_index)
    if selected_index < 1 or selected_index > MAX_SUPPORTED_PLOTTERS:
        raise RuntimeError(
            f"Plotter index must be between 1 and {MAX_SUPPORTED_PLOTTERS}. Got {selected_index}."
        )
    return selected_index


def controller_event_payload(
    action: str,
    plotter_index: int | None,
    source: str,
    message: str,
) -> dict[str, Any]:
    plotter_label_value = (
        plotter_label(plotter_index - 1)
        if isinstance(plotter_index, int) and plotter_index >= 1
        else None
    )
    return {
        "action": action,
        "plotter_index": plotter_index,
        "plotter_label": plotter_label_value,
        "source": source,
        "message": message,
        "timestamp": time.time(),
    }


def snapshot_controller_state() -> dict[str, Any]:
    with STATE_LOCK:
        selected_plotter_index = CONTROLLER_SELECTED_PLOTTER_INDEX
        last_event = dict(CONTROLLER_LAST_EVENT) if isinstance(CONTROLLER_LAST_EVENT, dict) else None

    selected_plotter_label = (
        plotter_label(selected_plotter_index - 1)
        if isinstance(selected_plotter_index, int) and selected_plotter_index >= 1
        else None
    )
    return {
        "selected_plotter_index": selected_plotter_index,
        "selected_plotter_label": selected_plotter_label,
        "last_event": last_event,
    }


def set_controller_selected_plotter(plotter_index: int, source: str, message: str) -> None:
    global CONTROLLER_SELECTED_PLOTTER_INDEX, CONTROLLER_LAST_EVENT  # pylint: disable=global-statement

    with STATE_LOCK:
        CONTROLLER_SELECTED_PLOTTER_INDEX = plotter_index
        CONTROLLER_LAST_EVENT = controller_event_payload(
            "select_plotter",
            plotter_index,
            source,
            message,
        )


def record_controller_command(action: str, plotter_index: int | None, source: str, message: str) -> None:
    global CONTROLLER_SELECTED_PLOTTER_INDEX, CONTROLLER_LAST_EVENT  # pylint: disable=global-statement

    with STATE_LOCK:
        if isinstance(plotter_index, int):
            CONTROLLER_SELECTED_PLOTTER_INDEX = plotter_index
        CONTROLLER_LAST_EVENT = controller_event_payload(action, plotter_index, source, message)


def resolve_controller_plotter_index(explicit_plotter_index: int | None = None) -> int | None:
    if explicit_plotter_index is not None:
        return arduino_plotter_index(explicit_plotter_index)

    with STATE_LOCK:
        selected_plotter_index = CONTROLLER_SELECTED_PLOTTER_INDEX

    if selected_plotter_index is None:
        return None
    return arduino_plotter_index(selected_plotter_index)


def run_controller_command(
    action: str,
    *,
    plotter_index: int | None = None,
    source: str = "controller",
) -> dict[str, Any]:
    if action == "select_plotter":
        if plotter_index is None:
            return {"status": "error", "message": "Field 'plotter_index' is required for controller plotter selection."}
        normalized_plotter_index = arduino_plotter_index(plotter_index)
        message = f"Controller selected Plotter {plotter_label(normalized_plotter_index - 1)}."
        set_controller_selected_plotter(normalized_plotter_index, source, message)
        result = {
            "status": "done",
            "command": action,
            "message": message,
            "plotter_index": normalized_plotter_index,
        }
        result["controller"] = snapshot_controller_state()
        return result

    normalized_plotter_index = resolve_controller_plotter_index(plotter_index)
    if normalized_plotter_index is None:
        return {
            "status": "error",
            "message": "No controller plotter is selected. Choose a plotter first.",
            "controller": snapshot_controller_state(),
        }

    print(
        f"[controller] source={source} action={action} plotter={plotter_label(normalized_plotter_index - 1)} "
        f"({normalized_plotter_index})"
    )
    if action == "servo_toggle":
        result = toggle_arduino_servos(plotter_indices=[normalized_plotter_index])
    else:
        result = run_bridge_control_command(action, plotter_indices=[normalized_plotter_index])
    if result.get("status") == "done":
        message = str(result.get("message", ""))
        record_controller_command(action, normalized_plotter_index, source, message)
    result["controller"] = snapshot_controller_state()
    return result


def reset_arduino_servo_modes_locked() -> None:
    global ARDUINO_SERVO_MODE_BY_PLOTTER  # pylint: disable=global-statement

    ARDUINO_SERVO_MODE_BY_PLOTTER = {
        index: ARDUINO_SERVO_INITIAL_MODE for index in range(1, MAX_SUPPORTED_PLOTTERS + 1)
    }


def set_arduino_plotter_mode_locked(plotter_index: int, mode: str) -> None:
    ARDUINO_SERVO_MODE_BY_PLOTTER[plotter_index] = mode


def set_all_arduino_plotter_modes_locked(mode: str) -> None:
    for plotter_index in range(1, MAX_SUPPORTED_PLOTTERS + 1):
        set_arduino_plotter_mode_locked(plotter_index, mode)


def arduino_plotter_toggle_command(plotter_index: int) -> str:
    return ARDUINO_PLOTTER_TOGGLE_COMMANDS.get(plotter_index, ARDUINO_TOGGLE_COMMAND)


def arduino_toggle_commands_for_plotter_indices(plotter_indices: list[int]) -> list[str]:
    commands: list[str] = []
    seen_commands: set[str] = set()
    for plotter_index in plotter_indices:
        command_text = arduino_plotter_toggle_command(plotter_index).strip()
        normalized_command = normalized_arduino_command(command_text)
        if not normalized_command or normalized_command in seen_commands:
            continue
        commands.append(command_text)
        seen_commands.add(normalized_command)
    return commands


def arduino_toggle_commands_for_detected_plotters() -> list[str]:
    detected_ports = sorted(list_axidraw_ports())
    if detected_ports:
        mapping = plotter_indices_by_port(detected_ports)
        detected_indices = [
            mapping[port]
            for port in detected_ports
            if port in mapping
        ]
        commands = arduino_toggle_commands_for_plotter_indices(detected_indices)
        if commands:
            return commands
    return arduino_toggle_commands_for_plotter_indices(sorted(ARDUINO_PLOTTER_TOGGLE_COMMANDS))


def toggle_arduino_servos(
    plotter_indices: list[int] | None = None,
    port: str | None = None,
    baud_rate: int | None = None,
) -> dict[str, Any]:
    commands = (
        arduino_toggle_commands_for_plotter_indices(plotter_indices or [])
        if plotter_indices
        else arduino_toggle_commands_for_detected_plotters()
    )
    if not commands:
        return arduino_result("error", "No Arduino toggle commands are configured.")

    for command_text in commands:
        result = send_arduino_command(
            command_text,
            port=port,
            baud_rate=baud_rate,
        )
        if result["status"] != "done":
            return result

    return arduino_result(
        "done",
        f"Arduino toggled servo command(s): {', '.join(commands)}.",
        commands=commands,
    )


def toggle_arduino_plotter_mode_locked(plotter_index: int) -> None:
    current = ARDUINO_SERVO_MODE_BY_PLOTTER.get(plotter_index, ARDUINO_SERVO_INITIAL_MODE)
    ARDUINO_SERVO_MODE_BY_PLOTTER[plotter_index] = "erase" if current == "marker" else "marker"


def normalized_arduino_command(command_text: str) -> str:
    return "".join(command_text.strip().upper().split())


def arduino_angle_mode(angle_value: int) -> str | None:
    if angle_value == ARDUINO_MARKER_ANGLE:
        return "marker"
    if angle_value == ARDUINO_ERASE_ANGLE:
        return "erase"
    return None


def sync_arduino_state_from_command_locked(command_text: str) -> None:
    normalized = normalized_arduino_command(command_text)
    if not normalized:
        return

    for plotter_index in range(1, MAX_SUPPORTED_PLOTTERS + 1):
        if normalized == normalized_arduino_command(arduino_plotter_toggle_command(plotter_index)):
            toggle_arduino_plotter_mode_locked(plotter_index)
            return

    if len(normalized) == 2 and normalized[0] in "ABCD" and normalized[1] in "MET":
        servo_channel = SERVO_CHANNEL_NAMES.index(normalized[0]) + 1
        plotter_index = plotter_index_for_arduino_servo_channel(servo_channel)
        if plotter_index is None:
            return
        action = normalized[1]
        if action == "M":
            set_arduino_plotter_mode_locked(plotter_index, "marker")
        elif action == "E":
            set_arduino_plotter_mode_locked(plotter_index, "erase")
        else:
            toggle_arduino_plotter_mode_locked(plotter_index)
        return

    if len(normalized) == 3 and normalized[0] == "P" and normalized[1].isdigit() and normalized[2] in "MET":
        plotter_index = int(normalized[1])
        if 1 <= plotter_index <= MAX_SUPPORTED_PLOTTERS:
            action = normalized[2]
            if action == "M":
                set_arduino_plotter_mode_locked(plotter_index, "marker")
            elif action == "E":
                set_arduino_plotter_mode_locked(plotter_index, "erase")
            else:
                toggle_arduino_plotter_mode_locked(plotter_index)
        return

    if normalized in {"ABM", "ABE", "ABT"}:
        target_indices = [
            plotter_index
            for plotter_index in (
                plotter_index_for_arduino_servo_channel(1),
                plotter_index_for_arduino_servo_channel(2),
            )
            if plotter_index is not None
        ]
        if normalized.endswith("M"):
            for plotter_index in target_indices:
                set_arduino_plotter_mode_locked(plotter_index, "marker")
        elif normalized.endswith("E"):
            for plotter_index in target_indices:
                set_arduino_plotter_mode_locked(plotter_index, "erase")
        else:
            for plotter_index in target_indices:
                toggle_arduino_plotter_mode_locked(plotter_index)
        return

    if normalized in {"ALLM", "ALLE", "ALLT", "T", "R"}:
        if normalized.endswith("M"):
            for plotter_index in range(1, MAX_SUPPORTED_PLOTTERS + 1):
                set_arduino_plotter_mode_locked(plotter_index, "marker")
        elif normalized.endswith("E"):
            for plotter_index in range(1, MAX_SUPPORTED_PLOTTERS + 1):
                set_arduino_plotter_mode_locked(plotter_index, "erase")
        else:
            for plotter_index in range(1, MAX_SUPPORTED_PLOTTERS + 1):
                toggle_arduino_plotter_mode_locked(plotter_index)


def sync_arduino_state_from_messages_locked(messages: list[str]) -> bool:
    updated = False
    for raw_message in messages:
        normalized_message = raw_message.strip().upper().replace("(", " ").replace(")", " ")
        toggle_plotter_index: int | None = None
        for token in normalized_message.split():
            if len(token) == 2 and token[0] == "T" and token[1].isdigit():
                candidate_channel = int(token[1])
                if 1 <= candidate_channel <= MAX_SUPPORTED_PLOTTERS:
                    toggle_plotter_index = plotter_index_for_arduino_servo_channel(candidate_channel)
                continue
            if token.startswith("ANGLE="):
                try:
                    angle_value = int(token.split("=", 1)[1])
                except ValueError:
                    continue
                mode = arduino_angle_mode(angle_value)
                if mode is None:
                    continue
                if toggle_plotter_index is not None:
                    set_arduino_plotter_mode_locked(toggle_plotter_index, mode)
                    updated = True
                continue
            if len(token) >= 4 and token[0] == "P" and token[1].isdigit() and token[2] == "=":
                plotter_index = plotter_index_for_arduino_servo_channel(int(token[1]))
                mode = token[3:]
                if plotter_index is not None and mode in {"MARKER", "ERASE"}:
                    set_arduino_plotter_mode_locked(plotter_index, mode.lower())
                    updated = True
                continue
            if len(token) < 3 or token[1] != "=":
                continue
            servo_name = token[0]
            mode = token[2:]
            if servo_name not in "ABCD" or mode not in {"MARKER", "ERASE"}:
                continue
            plotter_index = plotter_index_for_arduino_servo_channel(SERVO_CHANNEL_NAMES.index(servo_name) + 1)
            if plotter_index is None:
                continue
            set_arduino_plotter_mode_locked(plotter_index, mode.lower())
            updated = True
    return updated


def estimated_arduino_servo_moves(command_text: str) -> int:
    normalized = normalized_arduino_command(command_text)
    if not normalized:
        return 0
    if normalized == normalized_arduino_command(ARDUINO_TOGGLE_COMMAND):
        return 1
    if any(
        normalized == normalized_arduino_command(toggle_command)
        for toggle_command in ARDUINO_PLOTTER_TOGGLE_COMMANDS.values()
    ):
        return 1
    if len(normalized) == 2 and normalized[0] in "ABCD" and normalized[1] in "MET":
        return 1
    if len(normalized) == 3 and normalized[0] == "P" and normalized[1].isdigit() and normalized[2] in "MET":
        return 1
    if normalized in {"ABM", "ABE", "ABT"}:
        return 2
    if normalized in {"ALLM", "ALLE", "ALLT", "T", "R"}:
        return MAX_SUPPORTED_PLOTTERS
    return 0


def arduino_command_timeout_seconds(command_text: str) -> float:
    estimated_move_seconds = estimated_arduino_servo_moves(command_text) * max(0.0, ARDUINO_COMMAND_SETTLE_SECONDS)
    if estimated_move_seconds <= 0:
        return max(0.0, ARDUINO_RESPONSE_TIMEOUT_SECONDS)
    return max(
        ARDUINO_RESPONSE_TIMEOUT_SECONDS,
        estimated_move_seconds + max(0.0, ARDUINO_RESPONSE_IDLE_SECONDS) + 0.5,
    )


def arduino_startup_timeout_seconds() -> float:
    estimated_startup_seconds = max(1, MAX_SUPPORTED_PLOTTERS) * max(0.0, ARDUINO_COMMAND_SETTLE_SECONDS) + 1.0
    return max(ARDUINO_READY_DELAY_SECONDS, estimated_startup_seconds)


def arduino_response_error(messages: list[str]) -> str | None:
    for message in messages:
        if message.strip().upper().startswith("ERR"):
            return message
    return None


def arduino_acknowledgement(messages: list[str]) -> str | None:
    for message in messages:
        if message.strip().upper().startswith("OK"):
            return message
    return None


def clear_passive_state_locked() -> None:
    global PASSIVE_PROCESS, PASSIVE_RESERVED_PORTS, PASSIVE_SESSION_SEED  # pylint: disable=global-statement

    PASSIVE_PROCESS = None
    PASSIVE_RESERVED_PORTS = []
    PASSIVE_SESSION_SEED = None
    PASSIVE_PATTERN_COUNTS_BY_PORT.clear()
    PASSIVE_LAST_PATTERN_INFO_BY_PORT.clear()


def refresh_passive_mode_state() -> None:
    with STATE_LOCK:
        process = PASSIVE_PROCESS
        reserved_ports = list(PASSIVE_RESERVED_PORTS)
        if process is None or process.poll() is None:
            return

        for port in reserved_ports:
            BUSY_PORTS.discard(port)
        clear_passive_state_locked()


def snapshot_passive_mode() -> dict[str, Any]:
    refresh_passive_mode_state()
    with STATE_LOCK:
        process = PASSIVE_PROCESS
        reserved_ports = list(PASSIVE_RESERVED_PORTS)

    is_running = process is not None and process.poll() is None
    return {
        "running": is_running,
        "pid": process.pid if is_running else None,
        "ports": reserved_ports,
        "duration_minutes": PASSIVE_DURATION_MINUTES,
        "wrapper": str(PASSIVE_WRAPPER),
    }


def passive_mode_running() -> bool:
    return bool(snapshot_passive_mode()["running"])


def active_guided_area_slot_count() -> int:
    return max(1, ACTIVE_GUIDED_AREA_COLUMNS) * max(1, ACTIVE_GUIDED_AREA_ROWS)


def active_guided_area_cell_size_in() -> float:
    columns = max(1, ACTIVE_GUIDED_AREA_COLUMNS)
    rows = max(1, ACTIVE_GUIDED_AREA_ROWS)
    usable_width = (
        ACTIVE_GUIDED_AREA_WIDTH_IN
        - (2.0 * ACTIVE_GUIDED_AREA_MARGIN_IN)
        - (max(0, columns - 1) * ACTIVE_GUIDED_AREA_GAP_IN)
    )
    usable_height = (
        ACTIVE_GUIDED_AREA_HEIGHT_IN
        - (2.0 * ACTIVE_GUIDED_AREA_MARGIN_IN)
        - (max(0, rows - 1) * ACTIVE_GUIDED_AREA_GAP_IN)
    )
    return max(0.25, min(usable_width / columns, usable_height / rows))


def snapshot_active_guided_area_mode() -> dict[str, Any]:
    with STATE_LOCK:
        enabled = ACTIVE_GUIDED_AREA_MODE
        states = {
            port: {
                "next_slot_index": int(state.get("next_slot_index", 0)),
                "cycles_completed": int(state.get("cycles_completed", 0)),
            }
            for port, state in ACTIVE_GUIDED_AREA_STATE_BY_PORT.items()
        }

    draw_left, draw_right, draw_bottom, draw_top = active_guided_area_draw_bounds()
    area_left, area_right, area_bottom, area_top = active_guided_area_configured_bounds()
    return {
        "enabled": enabled,
        "area_width_in": ACTIVE_GUIDED_AREA_WIDTH_IN,
        "area_height_in": ACTIVE_GUIDED_AREA_HEIGHT_IN,
        "columns": ACTIVE_GUIDED_AREA_COLUMNS,
        "rows": ACTIVE_GUIDED_AREA_ROWS,
        "slot_count": active_guided_area_slot_count(),
        "cell_size_in": active_guided_area_cell_size_in(),
        "origin_x_in": ACTIVE_GUIDED_AREA_ORIGIN_X_IN,
        "origin_y_in": ACTIVE_GUIDED_AREA_ORIGIN_Y_IN,
        "gap_in": ACTIVE_GUIDED_AREA_GAP_IN,
        "margin_in": ACTIVE_GUIDED_AREA_MARGIN_IN,
        "erase_sweep_step_in": ACTIVE_GUIDED_ERASE_SWEEP_STEP_IN,
        "erase_x_overscan_left_in": ACTIVE_GUIDED_ERASE_X_OVERSCAN_LEFT_IN,
        "erase_x_overscan_right_in": ACTIVE_GUIDED_ERASE_X_OVERSCAN_RIGHT_IN,
        "erase_y_overscan_bottom_in": ACTIVE_GUIDED_ERASE_Y_OVERSCAN_BOTTOM_IN,
        "erase_y_overscan_top_in": ACTIVE_GUIDED_ERASE_Y_OVERSCAN_TOP_IN,
        "erase_offset_x_in": ACTIVE_GUIDED_ERASE_OFFSET_X_IN,
        "erase_offset_y_in": ACTIVE_GUIDED_ERASE_OFFSET_Y_IN,
        "configured_bounds": {
            "left_in": area_left,
            "right_in": area_right,
            "bottom_in": area_bottom,
            "top_in": area_top,
        },
        "draw_bounds": {
            "left_in": draw_left,
            "right_in": draw_right,
            "bottom_in": draw_bottom,
            "top_in": draw_top,
        },
        "states_by_port": states,
    }


def toggle_active_guided_area_mode() -> dict[str, Any]:
    global ACTIVE_GUIDED_AREA_MODE, ACTIVE_GUIDED_AREA_STATE_BY_PORT, ACTIVE_GUIDED_AREA_SLOT_PREVIEWS_BY_PORT, ACTIVE_GUIDED_AREA_SLOT_GUIDED_KOLAMS_BY_PORT  # pylint: disable=global-statement

    with STATE_LOCK:
        ACTIVE_GUIDED_AREA_MODE = True
        ACTIVE_GUIDED_AREA_STATE_BY_PORT = {}
        ACTIVE_GUIDED_AREA_SLOT_PREVIEWS_BY_PORT = {}
        ACTIVE_GUIDED_AREA_SLOT_GUIDED_KOLAMS_BY_PORT = {}
    state = snapshot_active_guided_area_mode()
    message = (
        "Packed draw/erase mode is permanently enabled. Guided/browser kolams and passive-mode launches "
        f"will use a {state['area_width_in']:.1f}x{state['area_height_in']:.1f} in "
        f"{state['columns']}x{state['rows']} packed area with erase traces. "
        "Packed area state was reset."
    )

    return {
        "status": "done",
        "message": message,
        "active_guided_area_mode": state,
    }


def get_active_guided_area_port_state(port: str) -> dict[str, int]:
    with STATE_LOCK:
        state = ACTIVE_GUIDED_AREA_STATE_BY_PORT.setdefault(
            port,
            {"next_slot_index": 0, "cycles_completed": 0},
        )
        return {
            "next_slot_index": int(state["next_slot_index"]),
            "cycles_completed": int(state["cycles_completed"]),
        }


def empty_active_guided_area_slot_previews(slot_count: int | None = None) -> list[dict[str, Any] | None]:
    normalized_slot_count = active_guided_area_slot_count() if slot_count is None else max(1, int(slot_count))
    return [None for _ in range(normalized_slot_count)]


def normalize_active_guided_area_slot_previews(
    previews: list[dict[str, Any] | None] | None,
    slot_count: int | None = None,
) -> list[dict[str, Any] | None]:
    normalized_slot_count = active_guided_area_slot_count() if slot_count is None else max(1, int(slot_count))
    normalized = list(previews or [])
    if len(normalized) < normalized_slot_count:
        normalized.extend([None] * (normalized_slot_count - len(normalized)))
    elif len(normalized) > normalized_slot_count:
        normalized = normalized[:normalized_slot_count]
    return normalized


def snapshot_active_guided_area_slot_previews(port: str, slot_count: int | None = None) -> list[dict[str, Any] | None]:
    normalized_slot_count = active_guided_area_slot_count() if slot_count is None else max(1, int(slot_count))
    with STATE_LOCK:
        previews = normalize_active_guided_area_slot_previews(
            ACTIVE_GUIDED_AREA_SLOT_PREVIEWS_BY_PORT.get(port),
            normalized_slot_count,
        )
        ACTIVE_GUIDED_AREA_SLOT_PREVIEWS_BY_PORT[port] = previews
        return [dict(preview) if isinstance(preview, dict) else None for preview in previews]


def empty_active_guided_area_slot_guided_kolams(slot_count: int | None = None) -> list[dict[str, Any] | None]:
    normalized_slot_count = active_guided_area_slot_count() if slot_count is None else max(1, int(slot_count))
    return [None for _ in range(normalized_slot_count)]


def normalize_active_guided_area_slot_guided_kolams(
    guided_kolams: list[dict[str, Any] | None] | None,
    slot_count: int | None = None,
) -> list[dict[str, Any] | None]:
    normalized_slot_count = active_guided_area_slot_count() if slot_count is None else max(1, int(slot_count))
    normalized = list(guided_kolams or [])
    if len(normalized) < normalized_slot_count:
        normalized.extend([None] * (normalized_slot_count - len(normalized)))
    elif len(normalized) > normalized_slot_count:
        normalized = normalized[:normalized_slot_count]
    return normalized


def snapshot_active_guided_area_slot_guided_kolams(
    port: str,
    slot_count: int | None = None,
) -> list[dict[str, Any] | None]:
    normalized_slot_count = active_guided_area_slot_count() if slot_count is None else max(1, int(slot_count))
    with STATE_LOCK:
        guided_kolams = normalize_active_guided_area_slot_guided_kolams(
            ACTIVE_GUIDED_AREA_SLOT_GUIDED_KOLAMS_BY_PORT.get(port),
            normalized_slot_count,
        )
        ACTIVE_GUIDED_AREA_SLOT_GUIDED_KOLAMS_BY_PORT[port] = guided_kolams
        return [copy.deepcopy(guided_kolam) if isinstance(guided_kolam, dict) else None for guided_kolam in guided_kolams]


def set_active_guided_area_slot_preview(port: str, slot_index: int, preview_payload: dict[str, Any]) -> None:
    slot_count = active_guided_area_slot_count()
    normalized_slot_index = max(0, min(int(slot_index), slot_count - 1))
    with STATE_LOCK:
        previews = normalize_active_guided_area_slot_previews(
            ACTIVE_GUIDED_AREA_SLOT_PREVIEWS_BY_PORT.get(port),
            slot_count,
        )
        previews[normalized_slot_index] = dict(preview_payload)
        ACTIVE_GUIDED_AREA_SLOT_PREVIEWS_BY_PORT[port] = previews


def set_active_guided_area_slot_guided_kolam(port: str, slot_index: int, guided_kolam: dict[str, Any]) -> None:
    slot_count = active_guided_area_slot_count()
    normalized_slot_index = max(0, min(int(slot_index), slot_count - 1))
    with STATE_LOCK:
        guided_kolams = normalize_active_guided_area_slot_guided_kolams(
            ACTIVE_GUIDED_AREA_SLOT_GUIDED_KOLAMS_BY_PORT.get(port),
            slot_count,
        )
        guided_kolams[normalized_slot_index] = copy.deepcopy(guided_kolam)
        ACTIVE_GUIDED_AREA_SLOT_GUIDED_KOLAMS_BY_PORT[port] = guided_kolams


def clear_active_guided_area_slot_previews(port: str | None = None) -> None:
    slot_count = active_guided_area_slot_count()
    with STATE_LOCK:
        if port is None:
            ACTIVE_GUIDED_AREA_SLOT_PREVIEWS_BY_PORT.clear()
            ACTIVE_GUIDED_AREA_SLOT_GUIDED_KOLAMS_BY_PORT.clear()
            return
        ACTIVE_GUIDED_AREA_SLOT_PREVIEWS_BY_PORT[port] = empty_active_guided_area_slot_previews(slot_count)
        ACTIVE_GUIDED_AREA_SLOT_GUIDED_KOLAMS_BY_PORT[port] = empty_active_guided_area_slot_guided_kolams(slot_count)


def set_active_guided_area_port_state(port: str, next_slot_index: int, cycles_completed: int) -> None:
    with STATE_LOCK:
        ACTIVE_GUIDED_AREA_STATE_BY_PORT[port] = {
            "next_slot_index": int(next_slot_index),
            "cycles_completed": int(cycles_completed),
        }


def snapshot_busy_ports() -> set[str]:
    refresh_passive_mode_state()
    with STATE_LOCK:
        return set(BUSY_PORTS)


def set_active_guided_plot_request(
    client_request_id: str | None,
    *,
    selected_port: str,
    assigned_label: str,
    next_label: str,
    selected_plotter_index: int,
    mode: str,
    plot_mode: str,
) -> None:
    if not client_request_id:
        return

    with STATE_LOCK:
        ACTIVE_GUIDED_PLOT_REQUESTS_BY_CLIENT_ID[client_request_id] = {
            "client_request_id": client_request_id,
            "port": selected_port,
            "assigned_plotter_label": assigned_label,
            "next_plotter_label": next_label,
            "plotter_index": int(selected_plotter_index),
            "mode": mode,
            "plot_mode": plot_mode,
            "assigned_at_ms": int(time.time() * 1000),
        }


def clear_active_guided_plot_request(client_request_id: str | None) -> None:
    if not client_request_id:
        return

    with STATE_LOCK:
        ACTIVE_GUIDED_PLOT_REQUESTS_BY_CLIENT_ID.pop(client_request_id, None)


def snapshot_active_guided_plot_requests() -> list[dict[str, Any]]:
    with STATE_LOCK:
        requests = [
            dict(request_payload)
            for request_payload in ACTIVE_GUIDED_PLOT_REQUESTS_BY_CLIENT_ID.values()
        ]

    requests.sort(key=lambda request_payload: int(request_payload.get("assigned_at_ms", 0)))
    return requests


def list_serial_port_details() -> list[dict[str, Any]]:
    if SERIAL_IMPORT_ERROR is not None:
        return []

    return [
        {
            "device": str(entry.device),
            "description": str(entry.description or ""),
            "manufacturer": str(entry.manufacturer or ""),
            "product": str(getattr(entry, "product", "") or ""),
            "hwid": str(entry.hwid or ""),
        }
        for entry in list_ports.comports()
    ]


def list_candidate_arduino_ports() -> list[str]:
    configured_port = str(ARDUINO_PORT) if ARDUINO_PORT else None
    axidraw_ports = {resolve_port(port) or port for port in list_axidraw_ports()}
    preferred_ports: list[str] = []
    fallback_ports: list[str] = []

    for entry in list_serial_port_details():
        port = str(entry["device"])
        if configured_port and port == configured_port:
            return [port]
        if port in axidraw_ports:
            continue

        description = " ".join(
            str(entry.get(field, ""))
            for field in ("device", "description", "manufacturer", "product", "hwid")
        ).lower()
        if any(keyword in description for keyword in ("arduino", "wch", "usb serial", "cp210", "ch340")):
            preferred_ports.append(port)
        else:
            fallback_ports.append(port)

    if preferred_ports:
        return preferred_ports
    if len(fallback_ports) == 1:
        return fallback_ports
    return fallback_ports


def snapshot_arduino_state() -> dict[str, Any]:
    with ARDUINO_LOCK:
        connected_port = ARDUINO_CONNECTED_PORT
        serial_port = ARDUINO_SERIAL
        connected = bool(serial_port is not None and getattr(serial_port, "is_open", False))
        servo_modes = dict(ARDUINO_SERVO_MODE_BY_PLOTTER)

    return {
        "connected": connected,
        "port": connected_port,
        "connected_port": connected_port,
        "configured_port": ARDUINO_PORT,
        "selected_port": ARDUINO_PORT,
        "baud_rate": ARDUINO_BAUD_RATE,
        "toggle_command": ARDUINO_TOGGLE_COMMAND,
        "servo_initial_mode": ARDUINO_SERVO_INITIAL_MODE,
        "servo_mode": servo_modes.get(1, ARDUINO_SERVO_UNKNOWN_MODE),
        "servo_modes_by_plotter": servo_modes,
        "servo_channel_by_plotter": ARDUINO_SERVO_CHANNEL_BY_PLOTTER,
        "servo_commands_by_plotter": ARDUINO_PLOTTER_SERVO_COMMANDS,
        "import_error": str(SERIAL_IMPORT_ERROR) if SERIAL_IMPORT_ERROR else None,
        "candidate_ports": list_candidate_arduino_ports(),
    }


def arduino_result(status: str, message: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "status": status,
        "message": message,
        "arduino": snapshot_arduino_state(),
    }
    payload.update(extra)
    return payload


def close_arduino_connection_locked() -> None:
    global ARDUINO_SERIAL, ARDUINO_CONNECTED_PORT  # pylint: disable=global-statement

    if ARDUINO_SERIAL is not None:
        try:
            ARDUINO_SERIAL.close()
        except Exception:  # pylint: disable=broad-except
            pass
    ARDUINO_SERIAL = None
    ARDUINO_CONNECTED_PORT = None
    reset_arduino_servo_modes_locked()


def connect_arduino(
    port: str | None = None,
    baud_rate: int | None = None,
    reconnect: bool = False,
) -> dict[str, Any]:
    global ARDUINO_SERIAL, ARDUINO_CONNECTED_PORT  # pylint: disable=global-statement

    if SERIAL_IMPORT_ERROR is not None:
        return arduino_result("error", f"PySerial is unavailable: {SERIAL_IMPORT_ERROR}")

    selected_port = str(port) if port else ARDUINO_PORT
    candidate_ports = list_candidate_arduino_ports()
    if not selected_port:
        if not candidate_ports:
            return arduino_result("no_arduino", "No Arduino serial port detected.")
        if len(candidate_ports) > 1:
            return arduino_result(
                "multiple_ports",
                "Multiple Arduino serial ports found. Set ARDUINO_PORT or send a specific port.",
                ports=candidate_ports,
            )
        selected_port = candidate_ports[0]

    target_baud_rate = ARDUINO_BAUD_RATE if baud_rate is None else baud_rate
    opened_connection = None
    open_error: str | None = None
    with ARDUINO_LOCK:
        already_connected = bool(
            ARDUINO_SERIAL is not None
            and getattr(ARDUINO_SERIAL, "is_open", False)
            and ARDUINO_CONNECTED_PORT == selected_port
            and int(getattr(ARDUINO_SERIAL, "baudrate", target_baud_rate)) == target_baud_rate
        )
        if already_connected and not reconnect:
            opened_connection = ARDUINO_SERIAL
        else:
            close_arduino_connection_locked()
            try:
                opened_connection = serial.Serial(
                    selected_port,
                    target_baud_rate,
                    timeout=0.2,
                    write_timeout=0.5,
                )
                ARDUINO_SERIAL = opened_connection
                ARDUINO_CONNECTED_PORT = selected_port
                reset_arduino_servo_modes_locked()
            except Exception as exc:  # pylint: disable=broad-except
                close_arduino_connection_locked()
                open_error = f"Could not open Arduino port {selected_port}: {exc}"

    if open_error is not None:
        return arduino_result("error", open_error)

    if opened_connection is not None and not already_connected:
        startup_messages: list[str] = []
        with ARDUINO_LOCK:
            if ARDUINO_SERIAL is opened_connection and getattr(opened_connection, "is_open", False):
                startup_messages = read_arduino_messages(
                    opened_connection,
                    timeout_seconds=arduino_startup_timeout_seconds(),
                )
                sync_arduino_state_from_messages_locked(startup_messages)
                try:
                    opened_connection.reset_output_buffer()
                except Exception:  # pylint: disable=broad-except
                    pass
        ready_message = next(
            (message for message in startup_messages if message.strip().upper().startswith("READY")),
            None,
        )
        return arduino_result(
            "done",
            (
                f"Arduino connected on {selected_port}. {ready_message}"
                if ready_message
                else (
                    f"Arduino connected on {selected_port}. "
                    + (
                        "Plotter servo positions are unknown until the first explicit mode command."
                        if ARDUINO_SERVO_INITIAL_MODE == ARDUINO_SERVO_UNKNOWN_MODE
                        else f"Plotter servo modes assumed in {ARDUINO_SERVO_INITIAL_MODE} mode."
                    )
                )
            ),
            port=selected_port,
            responses=startup_messages,
        )

    return arduino_result("already_connected", f"Arduino already connected on {selected_port}.", port=selected_port)


def disconnect_arduino() -> dict[str, Any]:
    with ARDUINO_LOCK:
        connected_port = ARDUINO_CONNECTED_PORT
        was_connected = bool(ARDUINO_SERIAL is not None and getattr(ARDUINO_SERIAL, "is_open", False))
        close_arduino_connection_locked()

    if was_connected:
        return arduino_result("done", f"Arduino disconnected from {connected_port}.")
    return arduino_result("not_connected", "Arduino was not connected.")


def read_arduino_messages(connection: Any, timeout_seconds: float | None = None) -> list[str]:
    messages: list[str] = []
    deadline = time.monotonic() + max(
        0.0,
        ARDUINO_RESPONSE_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds,
    )
    idle_deadline: float | None = None

    while time.monotonic() < deadline:
        try:
            waiting = int(getattr(connection, "in_waiting", 0) or 0)
        except Exception:  # pylint: disable=broad-except
            waiting = 0

        if waiting <= 0:
            if messages and idle_deadline is None:
                idle_deadline = time.monotonic() + max(0.0, ARDUINO_RESPONSE_IDLE_SECONDS)
            if idle_deadline is not None and time.monotonic() >= idle_deadline:
                break
            time.sleep(0.05)
            continue

        idle_deadline = None
        try:
            line = connection.readline()
        except Exception:  # pylint: disable=broad-except
            break
        if not line:
            continue

        message = line.decode("utf-8", errors="replace").strip()
        if message:
            messages.append(message)

    return messages


def send_arduino_command(
    command_text: str,
    port: str | None = None,
    baud_rate: int | None = None,
) -> dict[str, Any]:
    normalized_command = command_text.strip()
    if not normalized_command:
        return arduino_result("error", "Arduino command must not be empty.")

    connection_result = connect_arduino(port=port, baud_rate=baud_rate)
    if connection_result["status"] not in {"done", "already_connected"}:
        return connection_result

    messages: list[str] = []
    command_error: str | None = None
    not_connected = False
    connection = None
    connected_port = None
    with ARDUINO_LOCK:
        connection = ARDUINO_SERIAL
        connected_port = ARDUINO_CONNECTED_PORT
        if connection is None or not getattr(connection, "is_open", False):
            not_connected = True
        else:
            try:
                try:
                    connection.reset_input_buffer()
                except Exception:  # pylint: disable=broad-except
                    pass
                payload = (
                    normalized_command.encode("utf-8")
                    if len(normalized_command) == 1
                    else f"{normalized_command}\n".encode("utf-8")
                )
                connection.write(payload)
                connection.flush()
                messages = read_arduino_messages(
                    connection,
                    timeout_seconds=arduino_command_timeout_seconds(normalized_command),
                )
            except Exception as exc:  # pylint: disable=broad-except
                close_arduino_connection_locked()
                command_error = f"Arduino command failed: {exc}"

    if not_connected:
        return arduino_result("not_connected", "Arduino is not connected.")
    if command_error is not None:
        return arduino_result("error", command_error)

    response_error = arduino_response_error(messages)
    if response_error is not None:
        return arduino_result(
            "error",
            f"Arduino rejected '{normalized_command}': {response_error}",
            port=connected_port,
            command=normalized_command,
            responses=messages,
        )

    acknowledgement = arduino_acknowledgement(messages)
    if acknowledgement is None:
        with ARDUINO_LOCK:
            if connection is ARDUINO_SERIAL and getattr(connection, "is_open", False):
                close_arduino_connection_locked()
        detail = f"Arduino did not acknowledge '{normalized_command}'."
        if messages:
            detail = f"{detail} Responses: {' | '.join(messages)}"
        return arduino_result(
            "error",
            detail,
            port=connected_port,
            command=normalized_command,
            responses=messages,
        )

    with ARDUINO_LOCK:
        if connection is ARDUINO_SERIAL and getattr(connection, "is_open", False):
            if not sync_arduino_state_from_messages_locked(messages):
                sync_arduino_state_from_command_locked(normalized_command)

    message = messages[-1] if messages else acknowledgement
    return arduino_result(
        "done",
        message,
        port=connected_port,
        command=normalized_command,
        responses=messages,
    )


def arduino_unknown_command_result(result: dict[str, Any]) -> bool:
    if result.get("status") != "error":
        return False
    message = str(result.get("message", "")).upper()
    if "ERR UNKNOWN" in message:
        return True
    responses = result.get("responses", [])
    if isinstance(responses, list):
        return any("ERR UNKNOWN" in str(response).upper() for response in responses)
    return False


def ensure_arduino_servo_mode(
    target_mode: str,
    plotter_index: int | None = None,
    port: str | None = None,
    baud_rate: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    selected_plotter_index = arduino_plotter_index(plotter_index)
    normalized_mode = target_mode.strip().lower()
    if normalized_mode not in {"marker", "erase"}:
        return arduino_result("error", "Servo mode must be 'marker' or 'erase'.")

    connection_result = connect_arduino(port=port, baud_rate=baud_rate)
    if connection_result["status"] not in {"done", "already_connected"}:
        return connection_result

    with ARDUINO_LOCK:
        current_mode = ARDUINO_SERVO_MODE_BY_PLOTTER.get(selected_plotter_index, ARDUINO_SERVO_UNKNOWN_MODE)

    command_text = ARDUINO_PLOTTER_SERVO_COMMANDS[selected_plotter_index][normalized_mode]
    toggle_command_text = arduino_plotter_toggle_command(selected_plotter_index)
    toggle_only_command = normalized_arduino_command(command_text) == normalized_arduino_command(toggle_command_text)
    if (
        plotter_uses_toggle_servo_transitions(selected_plotter_index)
        and current_mode in {"marker", "erase"}
        and current_mode != normalized_mode
    ):
        command_text = toggle_command_text
        toggle_only_command = True

    if current_mode == normalized_mode and (not force or toggle_only_command):
        return arduino_result(
            "done",
            f"Arduino servo for plotter {selected_plotter_index} already in {normalized_mode} mode.",
            target_mode=normalized_mode,
            plotter_index=selected_plotter_index,
        )

    result = send_arduino_command(
        command_text,
        port=port,
        baud_rate=baud_rate,
    )
    if result["status"] != "done" and not toggle_only_command and arduino_unknown_command_result(result):
        if current_mode == normalized_mode:
            return arduino_result(
                "done",
                (
                    f"Arduino servo for plotter {selected_plotter_index} assumed in {normalized_mode} mode. "
                    f"Explicit command '{command_text}' is unsupported by the current Arduino firmware."
                ),
                target_mode=normalized_mode,
                actual_mode=current_mode,
                plotter_index=selected_plotter_index,
                command=command_text,
                fallback_command=None,
            )

        fallback_command = arduino_plotter_toggle_command(selected_plotter_index)
        fallback_result = send_arduino_command(
            fallback_command,
            port=port,
            baud_rate=baud_rate,
        )
        if fallback_result["status"] != "done":
            return fallback_result

        with ARDUINO_LOCK:
            current_mode = ARDUINO_SERVO_MODE_BY_PLOTTER.get(selected_plotter_index, ARDUINO_SERVO_UNKNOWN_MODE)

        if current_mode != normalized_mode:
            return arduino_result(
                "error",
                (
                    f"Arduino servo mode sync failed for plotter {selected_plotter_index} after fallback. "
                    f"Expected {normalized_mode}, got {current_mode}."
                ),
                target_mode=normalized_mode,
                actual_mode=current_mode,
                plotter_index=selected_plotter_index,
                command=command_text,
                fallback_command=fallback_command,
            )

        return arduino_result(
            "done",
            (
                f"Arduino servo for plotter {selected_plotter_index} set to {normalized_mode} mode "
                f"using fallback toggle '{fallback_command}' because '{command_text}' is unsupported."
            ),
            target_mode=normalized_mode,
            actual_mode=current_mode,
            plotter_index=selected_plotter_index,
            command=command_text,
            fallback_command=fallback_command,
        )

    if result["status"] != "done":
        return result

    with ARDUINO_LOCK:
        current_mode = ARDUINO_SERVO_MODE_BY_PLOTTER.get(selected_plotter_index, ARDUINO_SERVO_UNKNOWN_MODE)

    if current_mode != normalized_mode:
        return arduino_result(
            "error",
            (
                f"Arduino servo mode sync failed for plotter {selected_plotter_index}. "
                f"Expected {normalized_mode}, got {current_mode}."
            ),
            target_mode=normalized_mode,
            actual_mode=current_mode,
            plotter_index=selected_plotter_index,
        )

    return arduino_result(
        "done",
        (
            f"Arduino servo for plotter {selected_plotter_index} forced to {normalized_mode} mode."
            if force
            else f"Arduino servo for plotter {selected_plotter_index} set to {normalized_mode} mode."
        ),
        target_mode=normalized_mode,
        actual_mode=current_mode,
        plotter_index=selected_plotter_index,
        command=command_text,
    )


def ensure_arduino_ready_for_plotting(action_label: str) -> dict[str, Any]:
    result = connect_arduino()
    if result["status"] in {"done", "already_connected"}:
        return result

    message = str(result.get("message", "Arduino connection failed."))
    normalized_result = dict(result)
    normalized_result["status"] = "error"
    normalized_result["message"] = f"{action_label}: {message}"
    return normalized_result


def release_busy_port(port: str | None) -> None:
    if port is None:
        return
    with STATE_LOCK:
        BUSY_PORTS.discard(port)


def release_busy_ports(ports: list[str]) -> None:
    with STATE_LOCK:
        for port in ports:
            BUSY_PORTS.discard(port)


def should_round_robin_plotters(port_count: int) -> bool:
    return port_count > 1 and port_count < MAX_SUPPORTED_PLOTTERS


def reserve_specific_ports(ports: list[str]) -> bool:
    with STATE_LOCK:
        if any(port in BUSY_PORTS for port in ports):
            return False
        BUSY_PORTS.update(ports)
        return True


def choose_plotter_for_job(
    ports: list[str],
    *,
    mapping_ports: list[str] | None = None,
) -> tuple[str, str, str, int] | None:
    global NEXT_PLOTTER_INDEX  # pylint: disable=global-statement

    ordered_ports = sorted(ports)
    ordered_mapping_ports = sorted(mapping_ports) if mapping_ports else ordered_ports
    with STATE_LOCK:
        sync_plotter_indices_locked(ordered_mapping_ports)
        use_round_robin = should_round_robin_plotters(len(ordered_ports))
        start_index = NEXT_PLOTTER_INDEX % len(ordered_ports) if use_round_robin else 0
        for offset in range(len(ordered_ports)):
            selected_index = (start_index + offset) % len(ordered_ports)
            selected_port = ordered_ports[selected_index]
            if selected_port in BUSY_PORTS:
                continue

            BUSY_PORTS.add(selected_port)
            assigned_plotter_index = PLOTTER_INDEX_BY_PORT.get(selected_port, selected_index + 1)
            if use_round_robin:
                NEXT_PLOTTER_INDEX = (selected_index + 1) % len(ordered_ports)
                next_port = ordered_ports[NEXT_PLOTTER_INDEX % len(ordered_ports)]
                next_plotter_index = PLOTTER_INDEX_BY_PORT.get(
                    next_port,
                    (NEXT_PLOTTER_INDEX % len(ordered_ports)) + 1,
                )
            else:
                next_plotter_index = assigned_plotter_index
            assigned_label = plotter_label(assigned_plotter_index - 1)
            next_label = plotter_label(next_plotter_index - 1)
            return selected_port, assigned_label, next_label, assigned_plotter_index

    return None


def choose_target_ports(
    count: int,
    requested_ports: list[str],
    available_ports: list[str],
) -> list[str]:
    if count < 1:
        raise RuntimeError("count must be at least 1.")

    selected = requested_ports[:count]
    if len(selected) < count:
        selected.extend([None] * (count - len(selected)))  # type: ignore[list-item]

    available_aliases = {port: serial_port_aliases(port) for port in available_ports}

    for idx, current in enumerate(selected):
        if current:
            current_aliases = serial_port_aliases(current)
            if not any(current_aliases.intersection(aliases) for aliases in available_aliases.values()):
                raise RuntimeError(f"Requested plotter {current} is not currently available.")
            continue

        used_ports = {port for port in selected if port}
        used_aliases: set[str] = set()
        for port in selected:
            if not port:
                continue
            used_aliases.update(serial_port_aliases(port))
        candidate = next(
            (
                available
                for available in available_ports
                if available not in used_ports and not available_aliases[available].intersection(used_aliases)
            ),
            None,
        )
        if candidate is None:
            found = ", ".join(available_ports) if available_ports else "none"
            raise RuntimeError(f"Need {count} idle AxiDraw(s). Found {len(available_ports)}: {found}.")
        selected[idx] = candidate

    return [port for port in selected if port]


def start_passive_mode(
    count: int | None = None,
    requested_ports: list[str] | None = None,
    plotter_indices: list[int] | None = None,
) -> dict[str, Any]:
    global PASSIVE_PROCESS, PASSIVE_RESERVED_PORTS, PASSIVE_SESSION_SEED  # pylint: disable=global-statement

    refresh_passive_mode_state()
    requested_ports = requested_ports or []
    requested_plotter_indices = plotter_indices or []
    passive_state = snapshot_passive_mode()
    if passive_state["running"]:
        return {
            "status": "already_running",
            "message": "Passive mode is already running.",
            "passive_mode": passive_state,
        }

    if not PASSIVE_WRAPPER.exists():
        return {
            "status": "error",
            "message": f"Passive mode wrapper not found: {PASSIVE_WRAPPER}",
        }

    arduino_result_payload = ensure_arduino_ready_for_plotting("Could not prepare Arduino before passive mode")
    if arduino_result_payload["status"] == "error":
        return arduino_result_payload

    detected_ports = list_axidraw_ports()
    busy_ports = sorted(snapshot_busy_ports())
    idle_ports = [port for port in detected_ports if port not in busy_ports]
    connectable_ports = sorted(list_connectable_axidraw_ports(idle_ports))

    if not connectable_ports:
        if busy_ports:
            return {
                "status": "busy",
                "message": "Plotters are still busy with active work. Passive mode did not start.",
                "busy_ports": busy_ports,
                "detected_ports": detected_ports,
            }
        if detected_ports:
            return {
                "status": "plotter_unavailable",
                "message": "AxiDraw USB device detected, but connection could not be opened for passive mode.",
                "ports": detected_ports,
            }
        return {"status": "no_plotter", "message": "No plotter connected."}

    if len(connectable_ports) > MAX_SUPPORTED_PLOTTERS:
        return {
            "status": "multiple_plotters",
            "message": f"Up to {MAX_SUPPORTED_PLOTTERS} plotters are supported in passive mode.",
            "ports": connectable_ports,
        }

    try:
        if requested_plotter_indices:
            selected_ports = select_plotter_ports_by_index(
                detected_ports,
                connectable_ports,
                requested_plotter_indices,
            )
        else:
            target_count = len(connectable_ports) if count is None else count
            selected_ports = choose_target_ports(target_count, requested_ports, connectable_ports)
    except RuntimeError as exc:
        return {"status": "error", "message": str(exc)}

    selected_indices = plotter_indices_by_port(selected_ports)

    missing_mapping_ports = [port for port in selected_ports if port not in selected_indices]
    if missing_mapping_ports:
        return {
            "status": "error",
            "message": f"Could not map passive-mode plotter port(s): {', '.join(missing_mapping_ports)}",
        }
    selected_ports = sorted(selected_ports, key=lambda port: selected_indices[port])

    if not reserve_specific_ports(selected_ports):
        return {
            "status": "busy",
            "message": "One or more passive-mode plotters are currently busy.",
            "busy_ports": sorted(snapshot_busy_ports()),
            "requested_ports": selected_ports,
        }

    try:
        for port in sorted(selected_ports):
            plotter_index = selected_indices.get(port)
            if plotter_index is None:
                raise RuntimeError(f"Could not map {port} to a plotter servo.")
            context_label = f"[passive] Plotter {plotter_label(plotter_index - 1)}"
            ensure_plotter_servo_ready_for_drawing(context_label, plotter_index)
    except Exception as exc:  # pylint: disable=broad-except
        release_busy_ports(selected_ports)
        return {
            "status": "error",
            "message": f"Could not prepare Arduino servos for passive mode: {exc}",
        }

    session_seed = random.randint(1, 10_000_000)
    command = [
        sys.executable,
        str(PASSIVE_WRAPPER),
        "--count",
        str(len(selected_ports)),
        "--no-preview",
        "--duration-minutes",
        str(PASSIVE_DURATION_MINUTES),
        "--seed",
        str(session_seed),
        "--speed-pendown",
        str(PLOTTER_SPEED_PENDOWN),
        "--speed-penup",
        str(PLOTTER_SPEED_PENUP),
    ]
    if ACTIVE_GUIDED_AREA_MODE:
        command.extend(
            [
                "--packed-area-mode",
                "--size-min",
                str(PASSIVE_PACKED_AREA_SIZE_MIN),
                "--size-max",
                str(PASSIVE_PACKED_AREA_SIZE_MAX),
                "--packed-area-width-in",
                str(ACTIVE_GUIDED_AREA_WIDTH_IN),
                "--packed-area-height-in",
                str(ACTIVE_GUIDED_AREA_HEIGHT_IN),
                "--packed-area-columns",
                str(ACTIVE_GUIDED_AREA_COLUMNS),
                "--packed-area-rows",
                str(ACTIVE_GUIDED_AREA_ROWS),
                "--packed-area-margin-in",
                str(ACTIVE_GUIDED_AREA_MARGIN_IN),
                "--packed-area-gap-in",
                str(ACTIVE_GUIDED_AREA_GAP_IN),
                "--packed-area-origin-x-in",
                str(ACTIVE_GUIDED_AREA_ORIGIN_X_IN),
                "--packed-area-origin-y-in",
                str(ACTIVE_GUIDED_AREA_ORIGIN_Y_IN),
                "--packed-area-erase-sweep-step-in",
                str(ACTIVE_GUIDED_ERASE_SWEEP_STEP_IN),
                "--packed-area-erase-offset-x-in",
                str(ACTIVE_GUIDED_ERASE_OFFSET_X_IN),
                "--packed-area-erase-offset-y-in",
                str(ACTIVE_GUIDED_ERASE_OFFSET_Y_IN),
            ]
        )
    if selected_ports:
        command.append("--ports")
        command.extend(selected_ports)
        command.append("--plotter-indices")
        command.extend(str(selected_indices[port]) for port in selected_ports)

    try:
        process = subprocess.Popen(command, cwd=str(REPO_ROOT))
    except Exception as exc:  # pylint: disable=broad-except
        release_busy_ports(selected_ports)
        return {"status": "error", "message": f"Could not start passive mode: {exc}"}

    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass

    if process.poll() is not None:
        release_busy_ports(selected_ports)
        return {
            "status": "error",
            "message": f"Passive mode exited immediately with code {process.returncode}.",
        }

    with STATE_LOCK:
        PASSIVE_PROCESS = process
        PASSIVE_RESERVED_PORTS = list(selected_ports)
        PASSIVE_SESSION_SEED = session_seed
        PASSIVE_PATTERN_COUNTS_BY_PORT.clear()
        PASSIVE_LAST_PATTERN_INFO_BY_PORT.clear()
        for port in selected_ports:
            PASSIVE_PATTERN_COUNTS_BY_PORT[port] = 0

    labels_by_port = plotter_labels_by_port(selected_ports)
    assigned_labels = [labels_by_port[port] for port in selected_ports if port in labels_by_port]
    label_text = ", ".join(f"Plotter {label}" for label in assigned_labels)
    mode_suffix = ""
    if ACTIVE_GUIDED_AREA_MODE:
        mode_suffix = " Passive packed draw/erase mode is enabled for this session."
    return {
        "status": "done",
        "message": f"Passive mode started on {label_text}.{mode_suffix}",
        "ports": selected_ports,
        "plotter_count": len(selected_ports),
        "session_seed": session_seed,
        "passive_mode": snapshot_passive_mode(),
    }


def stop_passive_mode() -> dict[str, Any]:
    global PASSIVE_PROCESS, PASSIVE_RESERVED_PORTS  # pylint: disable=global-statement

    refresh_passive_mode_state()
    with STATE_LOCK:
        process = PASSIVE_PROCESS
        reserved_ports = list(PASSIVE_RESERVED_PORTS)

    if process is None or process.poll() is not None:
        release_busy_ports(reserved_ports)
        with STATE_LOCK:
            clear_passive_state_locked()
        return {
            "status": "not_running",
            "message": "Passive mode is not running.",
            "passive_mode": snapshot_passive_mode(),
        }

    error_message: str | None = None
    try:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
    except Exception as exc:  # pylint: disable=broad-except
        error_message = str(exc)
    finally:
        with STATE_LOCK:
            clear_passive_state_locked()

    if error_message:
        release_busy_ports(reserved_ports)
        return {
            "status": "error",
            "message": f"Passive mode stop failed cleanly: {error_message}",
            "passive_mode": snapshot_passive_mode(),
        }

    homing_results: list[dict[str, str]] = []
    unhomed_ports: list[str] = []
    home_errors: dict[str, str] = {}
    try:
        if reserved_ports:
            pending_ports = sorted(set(reserved_ports))
            attempts = max(1, PASSIVE_STOP_HOME_RETRIES)
            for attempt in range(attempts):
                if not pending_ports:
                    break

                connectable_ports = set(list_connectable_axidraw_ports(pending_ports))
                next_pending_ports: list[str] = []
                for port in pending_ports:
                    if port not in connectable_ports:
                        next_pending_ports.append(port)
                        continue
                    try:
                        homing_results.extend(run_control_command("disable_motors", [port]))
                        home_errors.pop(port, None)
                    except Exception as exc:  # pylint: disable=broad-except
                        home_errors[port] = str(exc)
                        next_pending_ports.append(port)

                pending_ports = next_pending_ports
                if pending_ports and attempt < attempts - 1:
                    time.sleep(PASSIVE_STOP_HOME_RETRY_DELAY_SECONDS)

            unhomed_ports = pending_ports
    finally:
        release_busy_ports(reserved_ports)

    if reserved_ports and unhomed_ports:
        message = "Passive mode stopped, but some plotters could not be homed."
    elif reserved_ports:
        message = "Passive mode stopped. Plotters returned home. Active mode is ready."
    else:
        message = "Passive mode stopped. Active mode is ready."

    payload: dict[str, Any] = {
        "status": "done",
        "message": message,
        "passive_mode": snapshot_passive_mode(),
        "results": homing_results,
    }
    if unhomed_ports:
        payload["unhomed_ports"] = unhomed_ports
    if home_errors:
        payload["home_errors"] = home_errors
    return payload


def run_control_command(command_name: str, ports: list[str]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    ordered_ports = sorted(ports)
    detected_plotter_indices = plotter_indices_by_port(list_axidraw_ports())
    selected_plotter_indices = plotter_indices_by_port(ordered_ports)
    plotter_indices = {
        port: detected_plotter_indices.get(port, selected_plotter_indices.get(port))
        for port in ordered_ports
    }
    labels = {
        port: plotter_label(plotter_index - 1) if isinstance(plotter_index, int) else "?"
        for port, plotter_index in plotter_indices.items()
    }

    if command_name == "disable_motors":
        for port in ordered_ports:
            return_plotter_to_origin(port)
            results.append(
                {
                    "port": port,
                    "plotter_label": labels[port],
                    "action": "returned home and disabled XY motors",
                }
            )
        return results

    if command_name == "erase_area":
        actions_by_port = sweep_active_guided_area_on_plotters(
            ordered_ports,
            mapping_ports=list(detected_plotter_indices),
        )
        for port in ordered_ports:
            action = actions_by_port.get(port)
            if action is None:
                raise RuntimeError(f"Could not collect erase result for {port}.")
            results.append(
                {
                    "port": port,
                    "plotter_label": labels[port],
                    "action": action,
                }
            )
        return results

    if command_name == "erase_trace":
        port_plotter_indices = {
            port: int(plotter_indices[port])
            for port in ordered_ports
            if isinstance(plotter_indices.get(port), int)
        }
        if len(port_plotter_indices) != len(ordered_ports):
            unresolved_ports = [port for port in ordered_ports if port not in port_plotter_indices]
            raise RuntimeError(
                "Could not determine plotter indices for erase trace on: "
                + ", ".join(unresolved_ports)
            )
        actions_by_port = trace_active_guided_area_on_plotters(port_plotter_indices)
        for port in ordered_ports:
            action = actions_by_port.get(port)
            if action is None:
                raise RuntimeError(f"Could not collect erase trace result for {port}.")
            results.append(
                {
                    "port": port,
                    "plotter_label": labels[port],
                    "action": action,
                }
            )
        return results

    if command_name == "erase_sweep_demo":
        port_plotter_indices = {
            port: int(plotter_indices[port])
            for port in ordered_ports
            if isinstance(plotter_indices.get(port), int)
        }
        if len(port_plotter_indices) != len(ordered_ports):
            unresolved_ports = [port for port in ordered_ports if port not in port_plotter_indices]
            raise RuntimeError(
                "Could not determine plotter indices for erase sweep demo on: "
                + ", ".join(unresolved_ports)
            )
        actions_by_port = sweep_demo_on_plotters(port_plotter_indices)
        for port in ordered_ports:
            action = actions_by_port.get(port)
            if action is None:
                raise RuntimeError(f"Could not collect erase sweep demo result for {port}.")
            results.append(
                {
                    "port": port,
                    "plotter_label": labels[port],
                    "action": action,
                }
            )
        return results

    if command_name == "horizontal_sweep_area_test":
        port_plotter_indices = {
            port: int(plotter_indices[port])
            for port in ordered_ports
            if isinstance(plotter_indices.get(port), int)
        }
        if len(port_plotter_indices) != len(ordered_ports):
            unresolved_ports = [port for port in ordered_ports if port not in port_plotter_indices]
            raise RuntimeError(
                "Could not determine plotter indices for horizontal sweep area test on: "
                + ", ".join(unresolved_ports)
            )
        actions_by_port = horizontal_sweep_active_guided_area_on_plotters(port_plotter_indices)
        for port in ordered_ports:
            action = actions_by_port.get(port)
            if action is None:
                raise RuntimeError(f"Could not collect horizontal sweep area test result for {port}.")
            results.append(
                {
                    "port": port,
                    "plotter_label": labels[port],
                    "action": action,
                }
            )
        return results

    plotters: list[tuple[str, axidraw.AxiDraw]] = []
    try:
        for port in ordered_ports:
            plotters.append((port, build_interactive_plotter(port)))

        for port, ad in plotters:
            if command_name == "pen_down":
                ad.pendown()
                action = "pen down"
            elif command_name == "pen_up":
                ad.penup()
                action = "pen up"
            else:
                raise RuntimeError(f"Unsupported command: {command_name}")

            results.append(
                {
                    "port": port,
                    "plotter_label": labels[port],
                    "action": action,
                }
            )

        return results
    finally:
        for _, ad in plotters:
            try:
                ad.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass


def select_plotter_ports_by_index(
    detected_ports: list[str],
    connectable_ports: list[str],
    plotter_indices: list[int],
) -> list[str]:
    if not plotter_indices:
        return []

    mapping = plotter_indices_by_port(detected_ports)
    selected_ports: list[str] = []
    for plotter_index in plotter_indices:
        try:
            normalized_index = arduino_plotter_index(plotter_index)
        except RuntimeError as exc:
            raise RuntimeError(str(exc)) from exc

        port = next((candidate for candidate, index in mapping.items() if index == normalized_index), None)
        if port is None:
            raise RuntimeError(
                f"Plotter {plotter_label(normalized_index - 1)} is not currently detected."
            )
        if port not in connectable_ports:
            raise RuntimeError(
                f"Plotter {plotter_label(normalized_index - 1)} is detected but not currently available."
            )
        selected_ports.append(port)

    return selected_ports


def run_bridge_control_command(
    command_name: str,
    count: int | None = None,
    plotter_indices: list[int] | None = None,
) -> dict[str, Any]:
    refresh_passive_mode_state()
    detected_ports = list_axidraw_ports()
    busy_ports = sorted(snapshot_busy_ports())
    idle_ports = [port for port in detected_ports if port not in busy_ports]
    connectable_ports = sorted(list_connectable_axidraw_ports(idle_ports))
    active_mode_availability = active_mode_availability_payload(
        detected_ports,
        connectable_ports,
        busy_ports,
    )
    active_connectable_ports = list(active_mode_availability.get("active_connectable_ports", []))

    if not active_connectable_ports:
        return active_mode_availability

    requested_plotter_indices = plotter_indices or []
    try:
        if requested_plotter_indices:
            selected_ports = select_plotter_ports_by_index(
                detected_ports,
                active_connectable_ports,
                requested_plotter_indices,
            )
        else:
            target_count = len(active_connectable_ports) if count is None else count
            selected_ports = choose_target_ports(target_count, [], active_connectable_ports)
    except RuntimeError as exc:
        return {"status": "error", "message": str(exc)}

    if not reserve_specific_ports(selected_ports):
        return {
            "status": "busy",
            "message": "One or more requested plotters are currently busy.",
            "busy_ports": sorted(snapshot_busy_ports()),
            "requested_ports": selected_ports,
        }

    try:
        results = run_control_command(command_name, selected_ports)
    except Exception as exc:  # pylint: disable=broad-except
        traceback.print_exc()
        return {"status": "error", "message": f"Command failed: {exc}"}
    finally:
        release_busy_ports(selected_ports)

    return {
        "status": "done",
        "command": command_name,
        "message": control_command_message(command_name, len(results)),
        "results": results,
    }


def print_terminal_help() -> None:
    print("Terminal controls:")
    print("  b  -> on plotter 4, draw a rectangle in the guided kolam area, then flip and erase it")
    print("  d  -> pen down on all available plotters")
    print("  u  -> pen up on all available plotters")
    print("  e  -> draw 1 passive-mode kolam, then erase it by retracing on all available plotters")
    print("  e1 -> draw 1 passive-mode kolam, then erase it by retracing on all available plotters")
    print("  e2 -> draw 1 passive-mode kolam, then erase it with a sweep over the kolam bounds + 2 cm on all available plotters")
    print("  d1/d2/d3/d4 -> pen down on plotter 1 / 2 / 3 / 4")
    print("  u1/u2/u3/u4 -> pen up on plotter 1 / 2 / 3 / 4")
    print("  x  -> return home, then disable XY motors on all available plotters")
    print("  a  -> connect to Arduino")
    print("  r  -> toggle the configured plotter Arduino servos")
    print("  r1/r2/r3/r4 -> toggle the Arduino servo for plotter 1 / 2 / 3 / 4")
    print("  p  -> start passive mode on all available plotters")
    print("  p1/p2/p3/p4 -> start passive mode on 1 / 2 / 3 / 4 plotters")
    print("  pp1/pp2/pp3/pp4 -> start passive mode on logical plotter 1 / 2 / 3 / 4 only")
    print("  s  -> stop passive mode, then return home + disable XY motors")
    print("  m  -> reset packed draw/erase state (mode is always on)")
    print("  h  -> show this help")


def format_command_results(result: dict[str, Any]) -> str:
    message = str(result.get("message", ""))
    entries = result.get("results", [])
    if not entries:
        return message
    details = ", ".join(
        f"Plotter {entry.get('plotter_label', '?')}: {entry.get('action', '?')}"
        for entry in entries
        if isinstance(entry, dict)
    )
    return f"{message} {details}".strip()


def terminal_control_loop() -> None:
    command_map = {
        "d": "pen_down",
        "u": "pen_up",
        "e": "erase_trace",
        "x": "disable_motors",
    }
    unknown_command_message = (
        "Unknown command. Use b, d, u, e, e1, e2, x, d1/d2/d3/d4, u1/u2/u3/u4, "
        "r, r1/r2/r3/r4, a, p, p1/p2/p3/p4, pp1/pp2/pp3/pp4, s, m, or h."
    )
    print_terminal_help()

    while True:
        try:
            raw = input("> ").strip().lower()
        except EOFError:
            return
        except KeyboardInterrupt:
            print()
            return

        if not raw:
            continue
        if raw in {"h", "?"}:
            print_terminal_help()
            continue

        if raw == "p":
            print(format_command_results(start_passive_mode()))
            continue

        if len(raw) == 3 and raw[:2] == "pp" and raw[2].isdigit():
            target_plotter_index = int(raw[2])
            if target_plotter_index < 1 or target_plotter_index > MAX_SUPPORTED_PLOTTERS:
                print(f"Passive mode plotter index must be between 1 and {MAX_SUPPORTED_PLOTTERS}.")
                continue
            print(
                format_command_results(
                    start_passive_mode(plotter_indices=[target_plotter_index])
                )
            )
            continue

        if len(raw) == 2 and raw[0] == "p" and raw[1].isdigit():
            target_count = int(raw[1])
            if target_count < 1 or target_count > MAX_SUPPORTED_PLOTTERS:
                print(f"Passive mode plotter count must be between 1 and {MAX_SUPPORTED_PLOTTERS}.")
                continue
            print(format_command_results(start_passive_mode(count=target_count)))
            continue

        if raw == "s":
            print(format_command_results(stop_passive_mode()))
            continue

        if raw == "a":
            print(format_command_results(connect_arduino()))
            continue

        if raw == "r":
            print(format_command_results(toggle_arduino_servos()))
            continue

        if len(raw) == 2 and raw[0] == "r" and raw[1].isdigit():
            try:
                plotter_index = arduino_plotter_index(int(raw[1]))
            except RuntimeError as exc:
                print(str(exc))
                continue
            print(format_command_results(toggle_arduino_servos(plotter_indices=[plotter_index])))
            continue

        if raw == "m":
            print(format_command_results(toggle_active_guided_area_mode()))
            continue

        if raw == "b":
            print(
                format_command_results(
                    run_bridge_control_command(
                        "horizontal_sweep_area_test",
                        plotter_indices=[4],
                    )
                )
            )
            continue

        if raw == "e1":
            print(format_command_results(run_bridge_control_command("erase_trace", count=None)))
            continue

        if raw == "e2":
            print(format_command_results(run_bridge_control_command("erase_sweep_demo", count=None)))
            continue

        if len(raw) == 2 and raw[0] in {"d", "u"} and raw[1].isdigit():
            command_name = command_map.get(raw[0])
            if command_name is None:
                print(unknown_command_message)
                continue
            result = run_bridge_control_command(command_name, plotter_indices=[int(raw[1])])
            print(format_command_results(result))
            continue

        command_name = command_map.get(raw)
        if command_name is None:
            print(unknown_command_message)
            continue

        result = run_bridge_control_command(command_name, count=None)
        print(format_command_results(result))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def clone_element(element: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(element, encoding="unicode"))


def find_stroke_group(root: ET.Element) -> ET.Element | None:
    for child in root:
        if local_name(child.tag) == "g" and "stroke" in child.attrib:
            return child
    return None


def serialize_svg(root: ET.Element) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")


def split_svg_for_plotters(svg_text: str, plotter_count: int) -> list[str]:
    if plotter_count <= 1:
        return [svg_text]

    root = ET.fromstring(svg_text)
    stroke_group = find_stroke_group(root)
    if stroke_group is None:
        return [svg_text]

    stroke_elements = list(stroke_group)
    if not stroke_elements:
        return [svg_text]
    if len(stroke_elements) < plotter_count:
        return [svg_text]

    partitions: list[list[ET.Element]] = [[] for _ in range(plotter_count)]
    for idx, element in enumerate(stroke_elements):
        partitions[idx % plotter_count].append(clone_element(element))

    split_svgs: list[str] = []
    for partition in partitions:
        if not partition:
            return [svg_text]
        split_root = ET.fromstring(svg_text)
        split_stroke_group = find_stroke_group(split_root)
        if split_stroke_group is None:
            split_svgs.append(svg_text)
            continue

        for child in list(split_stroke_group):
            split_stroke_group.remove(child)
        for child in partition:
            split_stroke_group.append(child)

        split_svgs.append(serialize_svg(split_root))

    return split_svgs


def draw_svgs_on_plotters(svg_texts: list[str], ports: list[str]) -> None:
    if len(svg_texts) != len(ports):
        raise ValueError("SVG payload count must match port count.")
    if len(set(ports)) != len(ports):
        raise RuntimeError("Selected AxiDraw ports are not distinct.")

    plotters: list[tuple[str, axidraw.AxiDraw]] = []
    start_event = threading.Event()
    errors: list[str] = []

    try:
        for port, svg_text in zip(ports, svg_texts):
            plotters.append((port, build_plotter(svg_text, port)))

        def worker(label: str, ad: axidraw.AxiDraw) -> None:
            try:
                start_event.wait()
                ad.plot_run()
            except Exception as exc:  # pylint: disable=broad-except
                errors.append(f"{label}: {exc}")

        threads = [
            threading.Thread(target=worker, args=(port, ad), daemon=True)
            for port, ad in plotters
        ]
        for thread in threads:
            thread.start()

        start_event.set()

        for thread in threads:
            thread.join()

        if errors:
            raise RuntimeError("; ".join(errors))
    finally:
        for _, ad in plotters:
            try:
                ad.disconnect()
            except Exception:  # pylint: disable=broad-except
                pass


class PlotterBridgeHandler(BaseHTTPRequestHandler):
    server_version = "KolamPlotterBridge/0.1"

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status_code: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_file(self, file_path: Path) -> None:
        body = file_path.read_bytes()
        content_type, content_encoding = mimetypes.guess_type(str(file_path))
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_redirect(self, location: str, status_code: int = 302) -> None:
        self.send_response(status_code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _read_json_body(self, allow_empty: bool = False) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"status": "error", "message": "Invalid Content-Length."})
            return None

        if length <= 0:
            if allow_empty:
                return {}
            self._send_json(400, {"status": "error", "message": "Request body is empty."})
            return None
        if length > MAX_BODY_BYTES:
            self._send_json(413, {"status": "error", "message": "Request body too large."})
            return None

        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            self._send_json(400, {"status": "error", "message": "Malformed JSON body."})
            return None

        if not isinstance(body, dict):
            self._send_json(400, {"status": "error", "message": "JSON body must be an object."})
            return None
        return body

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json(200, {"status": "ok"})

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        refresh_passive_mode_state()

        if path == "/client":
            self._send_redirect("/client/")
            return

        if path == "/client/" or path.startswith("/client/"):
            relative_path = path[len("/client/") :] if path.startswith("/client/") else ""
            static_file = resolve_static_file(CLIENT_APP_ROOT_RESOLVED, relative_path)
            if static_file is None:
                self._send_json(404, {"status": "error", "message": "Client asset not found."})
                return
            self._send_file(static_file)
            return

        if path in {"/", "/dashboard"}:
            if not DASHBOARD_HTML.exists():
                self._send_json(
                    500,
                    {
                        "status": "error",
                        "message": f"Dashboard file not found: {DASHBOARD_HTML}",
                    },
                )
                return
            self._send_html(200, DASHBOARD_HTML.read_text(encoding="utf-8"))
            return

        if path == "/dashboard/state":
            self._send_json(200, snapshot_dashboard_state())
            return

        if path == "/health":
            ports = list_axidraw_ports()
            busy_ports = sorted(snapshot_busy_ports())
            idle_ports = [port for port in ports if port not in busy_ports]
            connectable_ports = sorted(list_connectable_axidraw_ports(idle_ports))
            usable_ports = sorted(set(connectable_ports) | set(busy_ports))
            self._send_json(
                200,
                {
                    "status": "ok",
                    "import_error": str(IMPORT_ERROR) if IMPORT_ERROR else None,
                    "ports": ports,
                    "detected_plotter_count": len(ports),
                    "connectable_ports": connectable_ports,
                    "busy_ports": busy_ports,
                    "plotter_count": len(usable_ports),
                    "scheduler_mode": (
                        "round_robin"
                        if should_round_robin_plotters(len(usable_ports))
                        else ("fixed" if len(usable_ports) > 1 else "single")
                    ),
                    "passive_mode": snapshot_passive_mode(),
                    "arduino": snapshot_arduino_state(),
                },
            )
            return

        if path == "/passive-mode/status":
            self._send_json(200, {"status": "ok", "passive_mode": snapshot_passive_mode()})
            return

        if path == "/arduino/status":
            self._send_json(200, {"status": "ok", "arduino": snapshot_arduino_state()})
            return

        if path == "/controller/status":
            self._send_json(200, {"status": "ok", "controller": snapshot_controller_state()})
            return

        self._send_json(404, {"status": "error", "message": "Not found."})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        refresh_passive_mode_state()
        control_routes = {
            "/horizontal-sweep-area-test": "horizontal_sweep_area_test",
            "/pen-down": "pen_down",
            "/pen-up": "pen_up",
            "/erase-area": "erase_area",
            "/erase-trace": "erase_trace",
            "/disable-motors": "disable_motors",
        }
        passive_routes = {"/passive-mode/start", "/passive-mode/stop"}
        dashboard_routes = {"/dashboard/passive-progress"}
        arduino_routes = {
            "/arduino/connect",
            "/arduino/disconnect",
            "/arduino/servo-toggle",
            "/arduino/servo-mode",
        }
        controller_routes = {
            "/controller/select-plotter",
            "/controller/pen-up",
            "/controller/pen-down",
            "/controller/servo-toggle",
        }
        if (
            path != "/plot-svg"
            and path not in control_routes
            and path not in passive_routes
            and path not in arduino_routes
            and path not in controller_routes
            and path not in dashboard_routes
        ):
            self._send_json(404, {"status": "error", "message": "Not found."})
            return

        if path == "/plot-svg" or path in control_routes or path in passive_routes or path in {
            "/controller/pen-up",
            "/controller/pen-down",
        }:
            if IMPORT_ERROR is not None:
                self._send_json(
                    500,
                    {
                        "status": "error",
                        "message": f"AxiDraw imports unavailable: {IMPORT_ERROR}",
                    },
                )
                return

        body = self._read_json_body(
            allow_empty=(
                path in control_routes
                or path in passive_routes
                or path in arduino_routes
                or path in controller_routes
            )
        )
        if body is None:
            return

        if path in dashboard_routes:
            port_value = body.get("port")
            if not isinstance(port_value, str) or not port_value.strip():
                self._send_json(400, {"status": "error", "message": "Field 'port' must be a non-empty string."})
                return

            patterns_drawn_value = body.get("patterns_drawn")
            if not isinstance(patterns_drawn_value, int):
                self._send_json(400, {"status": "error", "message": "Field 'patterns_drawn' must be an integer."})
                return

            optional_int_fields = {
                "pattern_seed": body.get("pattern_seed"),
                "size": body.get("size"),
                "slot_index": body.get("slot_index"),
                "slot_count": body.get("slot_count"),
                "cycles_completed": body.get("cycles_completed"),
            }
            for field_name, field_value in optional_int_fields.items():
                if field_value is not None and not isinstance(field_value, int):
                    self._send_json(400, {"status": "error", "message": f"Field '{field_name}' must be an integer."})
                    return

            try:
                canonical_port = update_passive_plotter_progress(
                    port_value=port_value,
                    patterns_drawn=patterns_drawn_value,
                    pattern_seed=optional_int_fields["pattern_seed"],
                    size=optional_int_fields["size"],
                    slot_index=optional_int_fields["slot_index"],
                    slot_count=optional_int_fields["slot_count"],
                    cycles_completed=optional_int_fields["cycles_completed"],
                )
            except Exception as exc:  # pylint: disable=broad-except
                self._send_json(500, {"status": "error", "message": str(exc)})
                return

            self._send_json(
                200,
                {
                    "status": "done",
                    "message": f"Updated passive progress for {canonical_port}.",
                    "port": canonical_port,
                },
            )
            return

        if path in arduino_routes:
            port_value = body.get("port")
            if port_value is not None and not isinstance(port_value, str):
                self._send_json(400, {"status": "error", "message": "Field 'port' must be a string."})
                return

            baud_rate_value = body.get("baud_rate")
            if baud_rate_value is not None and not isinstance(baud_rate_value, int):
                self._send_json(400, {"status": "error", "message": "Field 'baud_rate' must be an integer."})
                return

            reconnect_value = body.get("reconnect", False)
            if not isinstance(reconnect_value, bool):
                self._send_json(400, {"status": "error", "message": "Field 'reconnect' must be a boolean."})
                return

            force_value = body.get("force", False)
            if not isinstance(force_value, bool):
                self._send_json(400, {"status": "error", "message": "Field 'force' must be a boolean."})
                return

            plotter_index_value = body.get("plotter_index")
            if plotter_index_value is not None and not isinstance(plotter_index_value, int):
                self._send_json(400, {"status": "error", "message": "Field 'plotter_index' must be an integer."})
                return
            normalized_plotter_index: int | None = None
            if plotter_index_value is not None:
                try:
                    normalized_plotter_index = arduino_plotter_index(plotter_index_value)
                except RuntimeError as exc:
                    self._send_json(400, {"status": "error", "message": str(exc)})
                    return

            if path == "/arduino/connect":
                result = connect_arduino(
                    port=port_value,
                    baud_rate=baud_rate_value,
                    reconnect=reconnect_value,
                )
                status_code = 500 if result["status"] == "error" else 200
                self._send_json(status_code, result)
                return

            if path == "/arduino/disconnect":
                self._send_json(200, disconnect_arduino())
                return

            if path == "/arduino/servo-mode":
                mode_value = body.get("mode")
                if not isinstance(mode_value, str):
                    self._send_json(400, {"status": "error", "message": "Field 'mode' must be a string."})
                    return
                result = ensure_arduino_servo_mode(
                    mode_value,
                    plotter_index=normalized_plotter_index,
                    port=port_value,
                    baud_rate=baud_rate_value,
                    force=force_value,
                )
                status_code = 500 if result["status"] == "error" else 200
                self._send_json(status_code, result)
                return

            result = toggle_arduino_servos(
                plotter_indices=[normalized_plotter_index] if normalized_plotter_index is not None else None,
                port=port_value,
                baud_rate=baud_rate_value,
            )
            status_code = 500 if result["status"] == "error" else 200
            self._send_json(status_code, result)
            return

        if path in controller_routes:
            plotter_index_value = body.get("plotter_index")
            if plotter_index_value is not None and not isinstance(plotter_index_value, int):
                self._send_json(400, {"status": "error", "message": "Field 'plotter_index' must be an integer."})
                return

            source_value = body.get("source", "controller")
            if not isinstance(source_value, str):
                self._send_json(400, {"status": "error", "message": "Field 'source' must be a string."})
                return
            normalized_source = source_value.strip() or "controller"

            if path == "/controller/select-plotter":
                result = run_controller_command(
                    "select_plotter",
                    plotter_index=plotter_index_value,
                    source=normalized_source,
                )
            elif path == "/controller/pen-up":
                result = run_controller_command(
                    "pen_up",
                    plotter_index=plotter_index_value,
                    source=normalized_source,
                )
            elif path == "/controller/servo-toggle":
                result = run_controller_command(
                    "servo_toggle",
                    plotter_index=plotter_index_value,
                    source=normalized_source,
                )
            else:
                result = run_controller_command(
                    "pen_down",
                    plotter_index=plotter_index_value,
                    source=normalized_source,
                )

            status_code = 500 if result["status"] == "error" else 200
            self._send_json(status_code, result)
            return

        if IMPORT_ERROR is not None:
            self._send_json(
                500,
                {
                    "status": "error",
                    "message": f"AxiDraw imports unavailable: {IMPORT_ERROR}",
                },
            )
            return

        if path in passive_routes:
            count_value = body.get("count")
            if count_value is not None and not isinstance(count_value, int):
                self._send_json(400, {"status": "error", "message": "Field 'count' must be an integer."})
                return

            plotter_index_value = body.get("plotter_index")
            if plotter_index_value is not None and not isinstance(plotter_index_value, int):
                self._send_json(400, {"status": "error", "message": "Field 'plotter_index' must be an integer."})
                return

            requested_ports = body.get("ports", [])
            if requested_ports is None:
                requested_ports = []
            if not isinstance(requested_ports, list) or not all(isinstance(port, str) for port in requested_ports):
                self._send_json(400, {"status": "error", "message": "Field 'ports' must be a list of strings."})
                return

            if path == "/passive-mode/start":
                result = start_passive_mode(
                    count=count_value,
                    requested_ports=requested_ports,
                    plotter_indices=[plotter_index_value] if plotter_index_value is not None else None,
                )
                status_code = 200
                if result["status"] == "busy":
                    status_code = 409
                elif result["status"] == "error":
                    status_code = 500
                self._send_json(status_code, result)
                return

            result = stop_passive_mode()
            status_code = 500 if result["status"] == "error" else 200
            self._send_json(status_code, result)
            return

        if path in control_routes:
            command_name = control_routes[path]
            requested_ports = body.get("ports", [])
            if requested_ports is None:
                requested_ports = []
            if not isinstance(requested_ports, list) or not all(isinstance(port, str) for port in requested_ports):
                self._send_json(400, {"status": "error", "message": "Field 'ports' must be a list of strings."})
                return

            count_value = body.get("count")
            if count_value is not None and not isinstance(count_value, int):
                self._send_json(400, {"status": "error", "message": "Field 'count' must be an integer."})
                return

            plotter_index_value = body.get("plotter_index")
            if plotter_index_value is not None and not isinstance(plotter_index_value, int):
                self._send_json(400, {"status": "error", "message": "Field 'plotter_index' must be an integer."})
                return

            detected_ports = list_axidraw_ports()
            busy_ports = sorted(snapshot_busy_ports())
            idle_ports = [port for port in detected_ports if port not in busy_ports]
            connectable_ports = sorted(list_connectable_axidraw_ports(idle_ports))
            active_mode_availability = active_mode_availability_payload(
                detected_ports,
                connectable_ports,
                busy_ports,
            )
            active_connectable_ports = list(active_mode_availability.get("active_connectable_ports", []))

            if not active_connectable_ports:
                status_code = 409 if active_mode_availability["status"] == "busy" else 200
                self._send_json(status_code, active_mode_availability)
                return

            try:
                if plotter_index_value is not None:
                    selected_ports = select_plotter_ports_by_index(
                        detected_ports,
                        active_connectable_ports,
                        [plotter_index_value],
                    )
                else:
                    target_count = count_value if count_value is not None else (len(requested_ports) or len(active_connectable_ports))
                    selected_ports = choose_target_ports(target_count, requested_ports, active_connectable_ports)
            except RuntimeError as exc:
                self._send_json(400, {"status": "error", "message": str(exc)})
                return

            if not reserve_specific_ports(selected_ports):
                self._send_json(
                    409,
                    {
                        "status": "busy",
                        "message": "One or more requested plotters are currently busy.",
                        "busy_ports": sorted(snapshot_busy_ports()),
                        "requested_ports": selected_ports,
                    },
                )
                return

            try:
                results = run_control_command(command_name, selected_ports)
            except Exception as exc:  # pylint: disable=broad-except
                traceback.print_exc()
                self._send_json(500, {"status": "error", "message": f"Command failed: {exc}"})
                return
            finally:
                release_busy_ports(selected_ports)

            self._send_json(
                200,
                {
                    "status": "done",
                    "command": command_name,
                    "message": control_command_message(command_name, len(results)),
                    "results": results,
                },
            )
            return

        svg_text = body.get("svg")
        guided_kolam_payload = body.get("guidedKolam")
        if guided_kolam_payload is None:
            guided_kolam_payload = body.get("guided_kolam")
        client_request_id_value = body.get("clientRequestId")
        if client_request_id_value is None:
            client_request_id_value = body.get("client_request_id")
        if client_request_id_value is not None and not isinstance(client_request_id_value, str):
            self._send_json(400, {"status": "error", "message": "Field 'clientRequestId' must be a string."})
            return
        client_request_id = (
            client_request_id_value.strip()
            if isinstance(client_request_id_value, str) and client_request_id_value.strip()
            else None
        )
        guided_kolam: dict[str, Any] | None = None

        if guided_kolam_payload is not None:
            try:
                guided_kolam = normalize_guided_kolam_payload(guided_kolam_payload)
            except ValueError as exc:
                self._send_json(400, {"status": "error", "message": str(exc)})
                return

        if not isinstance(svg_text, str) or "<svg" not in svg_text:
            self._send_json(400, {"status": "error", "message": "Field 'svg' must contain SVG text."})
            return

        selected_port: str | None = None
        try:
            arduino_result_payload = ensure_arduino_ready_for_plotting("Could not prepare Arduino before plotting")
            if arduino_result_payload["status"] == "error":
                self._send_json(500, arduino_result_payload)
                return

            detected_ports = list_axidraw_ports()
            busy_ports = sorted(snapshot_busy_ports())
            idle_ports = [port for port in detected_ports if port not in busy_ports]
            connectable_ports = sorted(list_connectable_axidraw_ports(idle_ports))
            ports = active_mode_ports(connectable_ports, detected_ports)
            scheduler_ports = active_mode_ports(sorted(set(connectable_ports) | set(busy_ports)), detected_ports)
            active_mode_availability = active_mode_availability_payload(
                detected_ports,
                connectable_ports,
                busy_ports,
            )
            if not ports:
                status_code = 409 if active_mode_availability["status"] == "busy" else 200
                self._send_json(status_code, active_mode_availability)
                return
            if len(ports) > MAX_SUPPORTED_PLOTTERS:
                self._send_json(
                    200,
                    {
                        "status": "multiple_plotters",
                        "message": f"Up to {MAX_SUPPORTED_PLOTTERS} plotters are supported.",
                        "ports": ports,
                    },
                )
                return

            try:
                selection = choose_plotter_for_job(
                    scheduler_ports,
                    mapping_ports=detected_ports,
                )
                if selection is None:
                    self._send_json(
                        409,
                        {
                            "status": "busy",
                            "message": "All available plotters are currently busy.",
                            "busy_ports": sorted(snapshot_busy_ports()),
                            "detected_ports": detected_ports,
                        },
                    )
                    return

                selected_port, assigned_label, next_label, selected_plotter_index = selection
                mode = (
                    "round_robin"
                    if should_round_robin_plotters(len(scheduler_ports))
                    else ("fixed" if len(scheduler_ports) > 1 else "single")
                )
                set_active_guided_plot_request(
                    client_request_id,
                    selected_port=selected_port,
                    assigned_label=assigned_label,
                    next_label=next_label,
                    selected_plotter_index=selected_plotter_index,
                    mode=mode,
                    plot_mode="guided_kolam" if guided_kolam is not None else "svg",
                )
                active_guided_area_mode = snapshot_active_guided_area_mode()
                plot_message: str | None = None
                if guided_kolam is not None:
                    if active_guided_area_mode["enabled"]:
                        layout_result = draw_guided_kolam_in_active_area(
                            guided_kolam,
                            selected_port,
                            selected_plotter_index,
                        )
                        plot_message = str(layout_result["message"])
                    else:
                        draw_guided_kolam_on_plotter(
                            guided_kolam,
                            selected_port,
                            selected_plotter_index,
                        )
                else:
                    draw_svg_on_plotter(svg_text, selected_port, selected_plotter_index)
            except Exception as exc:  # pylint: disable=broad-except
                traceback.print_exc()
                self._send_json(500, {"status": "error", "message": f"Plot failed: {exc}"})
                return

            self._send_json(
                200,
                {
                    "status": "done",
                    "message": (
                        plot_message
                        if plot_message is not None
                        else (
                            f"Kolam drawn twice on Plotter {assigned_label} with an Arduino servo rotation between passes "
                            "and a return rotation afterward. "
                            f"Next kolam will go to Plotter {next_label}."
                            if mode == "round_robin"
                            else (
                                f"Kolam drawn twice on Plotter {assigned_label} "
                                "with an Arduino servo rotation between passes and a return rotation afterward."
                                if mode == "single"
                                else (
                                    f"Kolam drawn twice on Plotter {assigned_label} "
                                    "with an Arduino servo rotation between passes and a return rotation afterward. "
                                    "Plotter assignment is fixed; no alternating is applied."
                                )
                            )
                        )
                    ),
                    "plotter_count": len(scheduler_ports),
                    "ports": [selected_port],
                    "all_connectable_ports": ports,
                    "scheduler_ports": scheduler_ports,
                    "detected_ports": detected_ports,
                    "mode": mode,
                    "active_guided_area_mode": active_guided_area_mode,
                    "assigned_plotter_label": assigned_label,
                    "next_plotter_label": next_label,
                    "plot_mode": "guided_kolam" if guided_kolam is not None else "svg",
                },
            )
        finally:
            clear_active_guided_plot_request(client_request_id)
            release_busy_port(selected_port)

    def log_message(self, format_str: str, *args: Any) -> None:
        path = urlparse(getattr(self, "path", "")).path
        if path == "/dashboard/state":
            return
        message = "%s - - [%s] %s" % (
            self.address_string(),
            self.log_date_time_string(),
            format_str % args,
        )
        print(message)


def main() -> int:
    detected_ports = sorted(list_axidraw_ports())
    connectable_ports = sorted(list_connectable_axidraw_ports(detected_ports))
    plotter_indices = plotter_indices_by_port(detected_ports)
    arduino_state = snapshot_arduino_state()

    print(f"Plotter bridge listening on http://{HOST}:{PORT}")
    print("AxiDraw devices:")
    if not detected_ports:
        print("  none detected")
    else:
        for port in detected_ports:
            plotter_index = plotter_indices.get(port)
            label = plotter_label(plotter_index - 1) if plotter_index is not None else "?"
            status = "connectable" if port in connectable_ports else "detected_only"
            toggle_command = (
                arduino_plotter_toggle_command(plotter_index)
                if plotter_index is not None
                else ARDUINO_TOGGLE_COMMAND
            )
            print(f"  Plotter {label}: {port} ({status}, Arduino toggle: {toggle_command})")

    print("Arduino:")
    if arduino_state["connected"]:
        print(f"  connected on {arduino_state['port']} @ {arduino_state['baud_rate']} baud")
    else:
        candidate_ports = arduino_state.get("candidate_ports") or []
        if candidate_ports:
            print(f"  not connected; candidate ports: {', '.join(str(port) for port in candidate_ports)}")
        else:
            print("  not connected; no candidate ports detected")

    terminal_thread = threading.Thread(target=terminal_control_loop, daemon=True)
    terminal_thread.start()
    with ThreadingHTTPServer((HOST, PORT), PlotterBridgeHandler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
