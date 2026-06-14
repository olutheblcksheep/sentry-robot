#!/usr/bin/env python3
"""
server.py — Sentry LiDAR Dashboard & navigation server.
=======================================================
A single, self-contained FastAPI server that:
  • Renders a live 2D LiDAR map of the room
  • Lets the operator click a target destination on the map
  • ACTUALLY SENDS the resulting command to the ESP32 over serial
    (using the same JSON-over-serial protocol as the camera node)
  • Links back to the camera dashboard

This file is now standalone: the LiDAR data models and the CRC-validated
LD19 reader that used to live in sensor_hub.py are inlined below, so there
is no `import sensor_hub`. Everything the LiDAR map needs is in this one
file.

Run:
  python server.py                      # real hardware (default)
  python server.py --simulate           # fake room data for UI development
  python server.py --port 8000          # custom HTTP port
  python server.py --esp32-port /dev/ttyUSB0
  python server.py --no-esp32           # generate commands but don't open serial

Requires: fastapi, uvicorn, pyserial
  pip install fastapi "uvicorn[standard]" pyserial

──────────────────────────────────────────────────────────────────────
IMPORTANT — serial port sharing
──────────────────────────────────────────────────────────────────────
The camera node (obj/) and this server can BOTH try to open the same
ESP32 serial port. Two processes cannot hold one serial port at once.
Pick ONE of these:
  • Run only one of them at a time, OR
  • Give this server a different ESP32 port with --esp32-port, OR
  • Run this server with --no-esp32 and let the camera node own the
    ESP32 (this server then only displays the commands it would send).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

import serial
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("server")


# ╔═══════════════════════════════════════════════════════════════════╗
# ║         LIDAR DATA MODELS  (inlined from sensor_hub.py)          ║
# ╚═══════════════════════════════════════════════════════════════════╝

@dataclass
class LidarPoint:
    """Single measurement point from the LD19."""
    angle_deg: float      # Interpolated angle in degrees [0, 360)
    distance_mm: int      # Distance in millimetres (0 = invalid/no return)
    intensity: int        # Return signal intensity (0-255)


@dataclass
class LidarScan:
    """One full 360° sweep assembled from multiple packets."""
    points: List[LidarPoint] = field(default_factory=list)
    timestamp: float = 0.0          # time.monotonic() when sweep completed


# ╔═══════════════════════════════════════════════════════════════════╗
# ║       LD19 CRC-8 LOOKUP TABLE  (inlined from sensor_hub.py)      ║
# ╚═══════════════════════════════════════════════════════════════════╝
# The LD19 uses CRC-8 with polynomial 0x4D over the first 46 bytes of
# each 47-byte packet.  This table is pre-computed for speed.

_CRC_TABLE: list[int] = [
    0x00, 0x4D, 0x9A, 0xD7, 0x79, 0x34, 0xE3, 0xAE,
    0xF2, 0xBF, 0x68, 0x25, 0x8B, 0xC6, 0x11, 0x5C,
    0xA9, 0xE4, 0x33, 0x7E, 0xD0, 0x9D, 0x4A, 0x07,
    0x5B, 0x16, 0xC1, 0x8C, 0x22, 0x6F, 0xB8, 0xF5,
    0x1F, 0x52, 0x85, 0xC8, 0x66, 0x2B, 0xFC, 0xB1,
    0xED, 0xA0, 0x77, 0x3A, 0x94, 0xD9, 0x0E, 0x43,
    0xB6, 0xFB, 0x2C, 0x61, 0xCF, 0x82, 0x55, 0x18,
    0x44, 0x09, 0xDE, 0x93, 0x3D, 0x70, 0xA7, 0xEA,
    0x3E, 0x73, 0xA4, 0xE9, 0x47, 0x0A, 0xDD, 0x90,
    0xCC, 0x81, 0x56, 0x1B, 0xB5, 0xF8, 0x2F, 0x62,
    0x97, 0xDA, 0x0D, 0x40, 0xEE, 0xA3, 0x74, 0x39,
    0x65, 0x28, 0xFF, 0xB2, 0x1C, 0x51, 0x86, 0xCB,
    0x21, 0x6C, 0xBB, 0xF6, 0x58, 0x15, 0xC2, 0x8F,
    0xD3, 0x9E, 0x49, 0x04, 0xAA, 0xE7, 0x30, 0x7D,
    0x88, 0xC5, 0x12, 0x5F, 0xF1, 0xBC, 0x6B, 0x26,
    0x7A, 0x37, 0xE0, 0xAD, 0x03, 0x4E, 0x99, 0xD4,
    0x7C, 0x31, 0xE6, 0xAB, 0x05, 0x48, 0x9F, 0xD2,
    0x8E, 0xC3, 0x14, 0x59, 0xF7, 0xBA, 0x6D, 0x20,
    0xD5, 0x98, 0x4F, 0x02, 0xAC, 0xE1, 0x36, 0x7B,
    0x27, 0x6A, 0xBD, 0xF0, 0x5E, 0x13, 0xC4, 0x89,
    0x63, 0x2E, 0xF9, 0xB4, 0x1A, 0x57, 0x80, 0xCD,
    0x91, 0xDC, 0x0B, 0x46, 0xE8, 0xA5, 0x72, 0x3F,
    0xCA, 0x87, 0x50, 0x1D, 0xB3, 0xFE, 0x29, 0x64,
    0x38, 0x75, 0xA2, 0xEF, 0x41, 0x0C, 0xDB, 0x96,
    0x42, 0x0F, 0xD8, 0x95, 0x3B, 0x76, 0xA1, 0xEC,
    0xB0, 0xFD, 0x2A, 0x67, 0xC9, 0x84, 0x53, 0x1E,
    0xEB, 0xA6, 0x71, 0x3C, 0x92, 0xDF, 0x08, 0x45,
    0x19, 0x54, 0x83, 0xCE, 0x60, 0x2D, 0xFA, 0xB7,
    0x5D, 0x10, 0xC7, 0x8A, 0x24, 0x69, 0xBE, 0xF3,
    0xAF, 0xE2, 0x35, 0x78, 0xD6, 0x9B, 0x4C, 0x01,
    0xF4, 0xB9, 0x6E, 0x23, 0x8D, 0xC0, 0x17, 0x5A,
    0x06, 0x4B, 0x9C, 0xD1, 0x7F, 0x32, 0xE5, 0xA8,
]


def _crc8(data: bytes) -> int:
    """Compute CRC-8 over *data* using the LD19 polynomial table."""
    crc = 0
    for byte in data:
        crc = _CRC_TABLE[(crc ^ byte) & 0xFF]
    return crc


# ╔═══════════════════════════════════════════════════════════════════╗
# ║         LIDAR READER  (inlined from sensor_hub.py)              ║
# ╚═══════════════════════════════════════════════════════════════════╝

class LidarReader:
    """
    Threaded reader for the LD19 (D500) LiDAR.

    Packet anatomy (47 bytes, little-endian throughout):
      Header(1) VerLen(1) Speed(2) StartAngle(2) 12×(Dist2+Int1)=36
      EndAngle(2) Timestamp(2) CRC(1)

    Angle encoding : raw / 100.0 → degrees
    Distance       : raw uint16 → millimetres (0 = no return)
    The 12 points per packet are angle-interpolated between start & end.
    A full 360° sweep is published when the start angle wraps around.
    """

    HEADER = 0x54
    VERLEN = 0x2C
    PACKET_SIZE = 47
    POINTS_PER_PACKET = 12
    BAUD_RATE = 230400

    _PAYLOAD_FMT = "<HH" + "HB" * 12 + "HH"
    _PAYLOAD_SIZE = struct.calcsize(_PAYLOAD_FMT)  # 44 bytes

    def __init__(self, port: str = "/dev/serial0"):
        self._port = port
        self._serial: Optional[serial.Serial] = None

        self._lock = threading.Lock()
        self._latest_scan: Optional[LidarScan] = None

        self._sweep_buf: List[LidarPoint] = []
        self._prev_end_angle: float = 0.0

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._packets_ok: int = 0
        self._packets_crc_fail: int = 0

    @property
    def latest_scan(self) -> Optional[LidarScan]:
        with self._lock:
            return self._latest_scan

    @property
    def stats(self) -> dict:
        return {
            "packets_ok": self._packets_ok,
            "crc_failures": self._packets_crc_fail,
        }

    def start(self) -> None:
        log.info("LiDAR: opening %s @ %d baud", self._port, self.BAUD_RATE)
        self._serial = serial.Serial(
            port=self._port,
            baudrate=self.BAUD_RATE,
            timeout=1.0,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._reader_loop, name="lidar-reader", daemon=True,
        )
        self._thread.start()
        log.info("LiDAR: reader thread started.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        if self._serial and self._serial.is_open:
            self._serial.close()
        log.info("LiDAR: stopped.")

    def _reader_loop(self) -> None:
        assert self._serial is not None
        ser = self._serial

        while not self._stop_event.is_set():
            try:
                # Sync to header byte 0x54
                header_byte = ser.read(1)
                if len(header_byte) == 0:
                    continue
                if header_byte[0] != self.HEADER:
                    continue

                # Validate VerLen byte 0x2C
                verlen_byte = ser.read(1)
                if len(verlen_byte) == 0:
                    continue
                if verlen_byte[0] != self.VERLEN:
                    continue

                # Read remaining 45 bytes
                remaining = ser.read(self.PACKET_SIZE - 2)
                if len(remaining) < self.PACKET_SIZE - 2:
                    continue

                # CRC-8 over the first 46 bytes; 47th byte is the CRC
                full_packet = header_byte + verlen_byte + remaining
                computed_crc = _crc8(full_packet[:46])
                received_crc = full_packet[46]
                if computed_crc != received_crc:
                    self._packets_crc_fail += 1
                    continue

                self._packets_ok += 1

                # Unpack the 44-byte payload
                payload = full_packet[2:46]
                fields = struct.unpack(self._PAYLOAD_FMT, payload)

                speed_raw = fields[0]            # noqa: F841 (kept for clarity)
                start_raw = fields[1]
                end_raw = fields[26]
                timestamp_ms = fields[27]        # noqa: F841

                start_angle = start_raw / 100.0
                end_angle = end_raw / 100.0

                angle_span = end_angle - start_angle
                if angle_span < 0:
                    angle_span += 360.0
                angle_step = (angle_span / (self.POINTS_PER_PACKET - 1)
                              if angle_span > 0 else 0.0)

                packet_points: List[LidarPoint] = []
                for i in range(self.POINTS_PER_PACKET):
                    dist_mm = fields[2 + 2 * i]
                    intensity = fields[3 + 2 * i]
                    angle = (start_angle + i * angle_step) % 360.0
                    packet_points.append(LidarPoint(
                        angle_deg=round(angle, 2),
                        distance_mm=dist_mm,
                        intensity=intensity,
                    ))

                # Sweep detection & publication
                if (start_angle < self._prev_end_angle - 10.0
                        and len(self._sweep_buf) > 0):
                    completed = LidarScan(
                        points=self._sweep_buf.copy(),
                        timestamp=time.monotonic(),
                    )
                    with self._lock:
                        self._latest_scan = completed
                    self._sweep_buf.clear()

                self._sweep_buf.extend(packet_points)
                self._prev_end_angle = end_angle

            except serial.SerialException as exc:
                log.error("LiDAR serial error: %s — retrying in 1 s", exc)
                time.sleep(1.0)
            except Exception as exc:
                log.exception("LiDAR unexpected error: %s", exc)
                time.sleep(0.1)


# ╔═══════════════════════════════════════════════════════════════════╗
# ║                   ROOM SIMULATOR                                 ║
# ╚═══════════════════════════════════════════════════════════════════╝

class RoomSimulator:
    """
    Generates fake LiDAR scans that look like a rectangular room with
    furniture-like obstacles. Used for UI development with no hardware.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._latest_scan: Optional[LidarScan] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # Room dimensions in mm (4m × 3m)
        self._room_width = 4000.0
        self._room_height = 3000.0
        # Robot sits at the centre of the room
        self._robot_x = self._room_width / 2
        self._robot_y = self._room_height / 2

        # Furniture: list of (cx, cy, radius_mm) circles
        self._obstacles = [
            (800, 800, 250),     # chair
            (3200, 600, 350),    # table leg cluster
            (3400, 2400, 200),   # bin
            (600, 2200, 300),    # bookshelf corner
            (2000, 2800, 150),   # small object
        ]

    @property
    def latest_scan(self) -> Optional[LidarScan]:
        with self._lock:
            return self._latest_scan

    @property
    def stats(self) -> dict:
        return {"packets_ok": 0, "crc_failures": 0}

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="simulator")
        self._thread.start()
        log.info("Simulator: generating fake room scans.")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            points: List[LidarPoint] = []
            for i in range(360):
                angle_deg = float(i)
                angle_rad = math.radians(angle_deg)
                dx = math.cos(angle_rad)
                dy = math.sin(angle_rad)

                dist = self._cast_ray(dx, dy)
                dist += random.gauss(0, 15)
                dist = max(0, dist)

                points.append(LidarPoint(
                    angle_deg=angle_deg,
                    distance_mm=int(dist),
                    intensity=random.randint(180, 255) if dist < 5000 else random.randint(20, 80),
                ))

            with self._lock:
                self._latest_scan = LidarScan(points=points, timestamp=time.monotonic())

            time.sleep(0.1)  # ~10 Hz

    def _cast_ray(self, dx: float, dy: float) -> float:
        """Cast a ray from robot position and return distance to nearest hit."""
        min_dist = 9999.0

        # Walls (axis-aligned rectangle)
        if dx > 0:
            t = (self._room_width - self._robot_x) / dx
            min_dist = min(min_dist, t)
        if dx < 0:
            t = -self._robot_x / dx
            min_dist = min(min_dist, t)
        if dy > 0:
            t = (self._room_height - self._robot_y) / dy
            min_dist = min(min_dist, t)
        if dy < 0:
            t = -self._robot_y / dy
            min_dist = min(min_dist, t)

        # Obstacles (circles)
        for cx, cy, r in self._obstacles:
            ocx = cx - self._robot_x
            ocy = cy - self._robot_y
            a = dx * dx + dy * dy
            b = 2 * (dx * ocx + dy * ocy)
            c = ocx * ocx + ocy * ocy - r * r
            disc = b * b - 4 * a * c
            if disc >= 0:
                sqrt_disc = math.sqrt(disc)
                t1 = (-b - sqrt_disc) / (2 * a)
                if t1 > 10:
                    min_dist = min(min_dist, t1)

        return min_dist


# ╔═══════════════════════════════════════════════════════════════════╗
# ║          ESP32 SERIAL LINK  (same protocol as the camera node)  ║
# ╚═══════════════════════════════════════════════════════════════════╝
# This mirrors the camera node's wire protocol: one-line JSON commands
# terminated by '\n', e.g.
#     {"cmd": "nav", "x": 1200, "y": -500, "speed": 60}\n
#     {"cmd": "stop"}\n
# When the serial link is down (or --no-esp32 is used) commands are
# logged instead of sent, so the dashboard still works for development.

_esp32_serial: Optional[serial.Serial] = None
_esp32_enabled: bool = True       # set False by --no-esp32
_esp32_port: str = "/dev/ttyUSB0"
_esp32_baud: int = 115200


def init_esp32() -> None:
    """Open the ESP32 serial port (no-op if --no-esp32)."""
    global _esp32_serial
    if not _esp32_enabled:
        log.info("ESP32: disabled (--no-esp32) — commands will be logged only.")
        _esp32_serial = None
        return
    try:
        _esp32_serial = serial.Serial(_esp32_port, _esp32_baud, timeout=1)
        # Native USB ESP32s drop connection briefly on open; let the
        # bootloader pass before we talk to it.
        time.sleep(2)
        _esp32_serial.reset_input_buffer()
        log.info("ESP32: connected on %s @ %d", _esp32_port, _esp32_baud)
    except Exception as e:
        log.warning("ESP32: connection failed — %s. Commands will be logged only.", e)
        _esp32_serial = None


def esp32_send(cmd: dict) -> dict:
    """
    Serialise `cmd` as one JSON line and write it to the ESP32.
    Returns a status dict describing what happened (for the API response).
    """
    global _esp32_serial
    payload = json.dumps(cmd) + "\n"

    if _esp32_serial and _esp32_serial.is_open:
        try:
            _esp32_serial.write(payload.encode("utf-8"))
            _esp32_serial.flush()
            ack = None
            if _esp32_serial.in_waiting:
                ack = _esp32_serial.readline().decode("utf-8", "replace").strip()
                log.info("ESP32 ACK: %s", ack)
            return {"link": "live", "transmitted": payload.strip(), "ack": ack}
        except Exception as e:
            log.error("ESP32 write error: %s", e)
            try:
                _esp32_serial.close()
            except Exception:
                pass
            _esp32_serial = None
            return {"link": "error", "transmitted": payload.strip(), "error": str(e)}
    else:
        log.info("ESP32 MOCK → %s", payload.strip())
        return {"link": "mock", "transmitted": payload.strip(), "ack": None}


# ╔═══════════════════════════════════════════════════════════════════╗
# ║                   SERIAL COMMAND PROTOCOL (display formats)     ║
# ╚═══════════════════════════════════════════════════════════════════╝
# In addition to the JSON command actually sent above, the UI still
# shows these two illustrative formats for educational purposes.

CMD_NAV = 0x01
CMD_STOP = 0x02
CMD_ROTATE = 0x03


def make_ascii_command(x_mm: int, y_mm: int, speed_pct: int = 60) -> str:
    """Build an NMEA-style ASCII navigation command string."""
    body = f"NAV,{x_mm},{y_mm},{speed_pct}"
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}\n"


def make_binary_command(x_mm: int, y_mm: int, speed_pct: int = 60) -> bytes:
    """Build a compact binary navigation command packet (9 bytes)."""
    payload = struct.pack("<BBBhhB", 0xAA, 0x55, CMD_NAV, x_mm, y_mm, speed_pct)
    crc = 0
    for b in payload:
        crc ^= b
    return payload + bytes([crc])


def format_binary_hex(data: bytes) -> str:
    """Pretty-print bytes as hex for display."""
    return " ".join(f"0x{b:02X}" for b in data)


# ╔═══════════════════════════════════════════════════════════════════╗
# ║                   PYDANTIC MODELS                                ║
# ╚═══════════════════════════════════════════════════════════════════╝

class NavigateRequest(BaseModel):
    x_mm: int            # target X in mm (relative to robot = 0,0)
    y_mm: int            # target Y in mm
    speed_pct: int = 60  # 0-100


class MoveRequest(BaseModel):
    cmd: str             # forward | backward | left | right | stop
    speed_pct: int = 60


# ╔═══════════════════════════════════════════════════════════════════╗
# ║                   FASTAPI APP                                    ║
# ╚═══════════════════════════════════════════════════════════════════╝

app = FastAPI(
    title="Sentry LiDAR Dashboard",
    description="Live 2D LiDAR mapping and navigation command interface",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Assigned in main() based on --simulate flag.
_lidar_source = None          # LidarReader or RoomSimulator
_simulator: Optional[RoomSimulator] = None

# Port of the camera dashboard (obj/) for the "back to camera" link.
_camera_server_port: int = 5000


@app.get("/api/scan")
def get_scan():
    """Return the latest 360° LiDAR scan as JSON."""
    scan = _lidar_source.latest_scan if _lidar_source else None
    if not scan:
        return {"points": [], "count": 0, "timestamp": 0}
    return {
        "points": [
            {"angle": p.angle_deg, "distance": p.distance_mm, "intensity": p.intensity}
            for p in scan.points
        ],
        "count": len(scan.points),
        "timestamp": scan.timestamp,
    }


@app.post("/api/navigate")
def navigate(req: NavigateRequest):
    """
    Accept a navigation target, SEND it to the ESP32, and return both the
    transmitted command and the illustrative ASCII/binary encodings.
    """
    ascii_cmd = make_ascii_command(req.x_mm, req.y_mm, req.speed_pct)
    binary_cmd = make_binary_command(req.x_mm, req.y_mm, req.speed_pct)

    distance = math.sqrt(req.x_mm ** 2 + req.y_mm ** 2)
    bearing = math.degrees(math.atan2(req.y_mm, req.x_mm)) % 360

    # Actually transmit to the ESP32 (same JSON protocol as camera node).
    tx = esp32_send({
        "cmd": "nav",
        "x": req.x_mm,
        "y": req.y_mm,
        "speed": req.speed_pct,
    })

    return {
        "status": "command_sent" if tx["link"] == "live" else "command_generated",
        "esp32": tx,
        "target": {"x_mm": req.x_mm, "y_mm": req.y_mm, "speed_pct": req.speed_pct},
        "distance_mm": round(distance),
        "bearing_deg": round(bearing, 1),
        "serial_commands": {
            "json_sent": {
                "description": "JSON line actually written to the ESP32",
                "command": tx["transmitted"],
            },
            "ascii": {
                "description": "NMEA-style ASCII (human-readable, debug-friendly)",
                "format": "$NAV,<x_mm>,<y_mm>,<speed_pct>*<XOR checksum>\n",
                "command": ascii_cmd.strip(),
                "bytes": len(ascii_cmd),
            },
            "binary": {
                "description": "Compact binary (fast MCU parsing)",
                "format": "[0xAA][0x55][CMD][X_lo][X_hi][Y_lo][Y_hi][SPD][CRC]",
                "command_hex": format_binary_hex(binary_cmd),
                "bytes": len(binary_cmd),
                "raw_bytes": list(binary_cmd),
            },
        },
    }


@app.post("/api/move")
def move(req: MoveRequest):
    """Send a simple directional command (forward/backward/left/right/stop)."""
    valid = {"forward", "backward", "left", "right", "stop"}
    if req.cmd not in valid:
        return {"status": "error", "error": f"Unknown command '{req.cmd}'"}
    if req.cmd == "stop":
        tx = esp32_send({"cmd": "stop"})
    else:
        tx = esp32_send({"cmd": req.cmd, "speed": req.speed_pct})
    return {"status": "command_sent" if tx["link"] == "live" else "command_generated",
            "esp32": tx}


@app.post("/api/stop")
def stop():
    """Emergency stop — always available."""
    tx = esp32_send({"cmd": "stop"})
    return {"status": "stopped", "esp32": tx}


@app.get("/api/status")
def get_status():
    """Sensor health and connection info."""
    esp32_state = (
        "connected" if (_esp32_serial and _esp32_serial.is_open)
        else ("disabled" if not _esp32_enabled else "disconnected")
    )
    if _simulator:
        return {"mode": "simulate", "lidar": "simulated", "esp32": esp32_state}
    if _lidar_source:
        return {"mode": "live", "lidar": _lidar_source.stats, "esp32": esp32_state}
    return {"mode": "unknown", "esp32": esp32_state}


# ── Cross-link back to the camera dashboard (obj/) ────────────────────
@app.get("/camera")
def go_to_camera(request: Request):
    """Redirect to the camera dashboard. Host is taken from the request,
    so it works on any IP without hard-coding the Pi's address."""
    host = request.url.hostname or "localhost"
    return RedirectResponse(url=f"http://{host}:{_camera_server_port}/")


# Serve the frontend
@app.get("/")
def serve_index():
    return FileResponse("static/index.html")


# Mount static assets AFTER explicit routes so they don't shadow /api
app.mount("/static", StaticFiles(directory="static"), name="static")


# ╔═══════════════════════════════════════════════════════════════════╗
# ║                      ENTRY POINT                                 ║
# ╚═══════════════════════════════════════════════════════════════════╝

def parse_args():
    p = argparse.ArgumentParser(description="Sentry LiDAR Dashboard Server")
    p.add_argument("--simulate", action="store_true",
                   help="Use simulated room data (no hardware needed)")
    p.add_argument("--host", default="0.0.0.0",
                   help="Bind address (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=8000,
                   help="HTTP port (default: 8000)")
    p.add_argument("--lidar-port", default="/dev/serial0",
                   help="Serial port for LD19 (default: /dev/serial0)")
    p.add_argument("--esp32-port", default="/dev/ttyUSB0",
                   help="Serial port for the ESP32 (default: /dev/ttyUSB0)")
    p.add_argument("--esp32-baud", type=int, default=115200,
                   help="ESP32 baud rate (default: 115200)")
    p.add_argument("--no-esp32", action="store_true",
                   help="Do not open the ESP32 serial port (log commands only)")
    p.add_argument("--camera-port", type=int, default=5000,
                   help="Port of the camera dashboard for the /camera link")
    return p.parse_args()


def main():
    global _lidar_source, _simulator
    global _esp32_enabled, _esp32_port, _esp32_baud, _camera_server_port
    args = parse_args()

    # ── ESP32 link ────────────────────────────────────────────────────
    _esp32_enabled = not args.no_esp32
    _esp32_port = args.esp32_port
    _esp32_baud = args.esp32_baud
    _camera_server_port = args.camera_port
    init_esp32()

    # ── LiDAR source ──────────────────────────────────────────────────
    if args.simulate:
        log.info("Starting in SIMULATION mode (no LiDAR hardware).")
        _simulator = RoomSimulator()
        _simulator.start()
        _lidar_source = _simulator
    else:
        log.info("Starting in LIVE mode — connecting to the LD19 LiDAR.")
        _lidar_source = LidarReader(port=args.lidar_port)
        _lidar_source.start()

    log.info("Dashboard: http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
