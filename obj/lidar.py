"""
lidar.py — D500 LiDAR reader + reactive obstacle stop.
======================================================
This is the lightweight LiDAR layer that lives on the camera node. It
parses D500/LD06-style 47-byte packets into {angle, distance} dicts,
publishes them over SocketIO, and — when auto mode is on — stops the
robot if something enters the front cone.

(The full CRC-validated LD19 parser lives in the LiDAR dashboard,
server.py. This node keeps its own simpler reader so the two processes
stay independent.)
"""

import threading

from state import socketio
import state
import esp32
import config

# Latest scan as a list of {"angle", "distance"} dicts, guarded by a lock.
lidar_data = []
lidar_lock = threading.Lock()


def parse_d500_packet(packet):
    """Parse a 47-byte D500/LD06 packet → list of {angle, distance} dicts."""
    try:
        if packet[0] != 0x54:
            return []
        start_angle = int.from_bytes(packet[4:6], "little") / 100.0
        end_angle = int.from_bytes(packet[44:46], "little") / 100.0
        num_points = 12
        step = (end_angle - start_angle) / max(num_points - 1, 1)
        points = []
        for i in range(num_points):
            off = 6 + i * 3
            dist = int.from_bytes(packet[off:off + 2], "little")
            angle = (start_angle + i * step) % 360
            if dist > 0:
                points.append({"angle": round(angle, 1), "distance": dist})
        return points
    except Exception:
        return []


def check_obstacles(scan):
    """Stop the robot if an obstacle is within OBSTACLE_DIST in the front cone."""
    if not state.auto_mode:
        return
    threats = [
        p for p in scan
        if 0 < p["distance"] < config.OBSTACLE_DIST
        and (p["angle"] < config.FRONT_CONE or p["angle"] > 360 - config.FRONT_CONE)
    ]
    if threats:
        closest = min(threats, key=lambda p: p["distance"])
        esp32.move("stop")
        socketio.emit("obstacle", {
            "distance": closest["distance"],
            "angle": closest["angle"],
            "message": f"Obstacle {closest['distance']}mm ahead",
        })
        print(f"[LiDAR]  Obstacle at {closest['distance']}mm — stopped")


def lidar_loop():
    global lidar_data
    if not config.LIDAR_ENABLED or config.MOCK_MODE:
        print("[LiDAR]  Disabled or mock mode")
        return
    try:
        import serial as _serial
        ser = _serial.Serial(config.LIDAR_PORT, config.LIDAR_BAUD, timeout=1)
        print(f"[LiDAR]  D500 connected on {config.LIDAR_PORT}")
        buf = b""
        while True:
            buf += ser.read(ser.in_waiting or 1)
            while len(buf) >= 47:
                start = buf.find(b"\x54")
                if start == -1:
                    buf = b""
                    break
                if start > 0:
                    buf = buf[start:]
                if len(buf) < 47:
                    break
                packet = buf[:47]
                buf = buf[47:]
                scan = parse_d500_packet(packet)
                if scan:
                    with lidar_lock:
                        lidar_data = scan
                    socketio.emit("lidar_scan", {"points": scan})
                    check_obstacles(scan)
    except Exception as e:
        print(f"[LiDAR]  Error: {e}")


def start():
    """Spawn the LiDAR reader thread."""
    threading.Thread(target=lidar_loop, daemon=True).start()
