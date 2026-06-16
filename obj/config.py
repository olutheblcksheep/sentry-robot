"""
config.py — All tunable constants for the SENTRY camera/ESP32/YOLO node.
========================================================================
Edit CLOUD_GATEWAY_URL to point to your deployed Render gateway.
Ports are auto-detected at startup — no hardcoding needed.
"""

import glob, time, logging
log = logging.getLogger("config")

# ── Auto port detection ───────────────────────────────────────────────
def _detect_ports():
    import serial
    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not ports:
        return None, None
    lidar_port = None
    esp32_port = None
    remaining  = list(ports)
    for p in ports:
        try:
            with serial.Serial(p, 230400, timeout=1.0) as s:
                time.sleep(0.3)
                if 0x54 in s.read(100):
                    lidar_port = p
                    remaining.remove(p)
                    break
        except Exception:
            pass
    esp32_port = remaining[0] if remaining else None
    return esp32_port, lidar_port

print("[Config] Scanning serial ports...")
try:
    ESP32_PORT, LIDAR_PORT = _detect_ports()
except Exception:
    ESP32_PORT, LIDAR_PORT = None, None
print(f"[Config] ESP32 → {ESP32_PORT or 'NOT FOUND'}")
print(f"[Config] LiDAR → {LIDAR_PORT or 'NOT FOUND'}")

# ── Mode ──────────────────────────────────────────────────────────────
MOCK_MODE = False 


# ── Baud rates ────────────────────────────────────────────────────────
ESP32_BAUD      = 115200
LIDAR_BAUD      = 230400

# ── Camera ────────────────────────────────────────────────────────────
CAMERA_BACKEND  = "picamera2"
CAMERA_INDEX    = 0
CAMERA_WIDTH    = 320
CAMERA_HEIGHT   = 240
CAMERA_FPS      = 15

# ── LiDAR ────────────────────────────────────────────────────────────
LIDAR_ENABLED   = LIDAR_PORT is not None
OBSTACLE_DIST   = 500
FRONT_CONE      = 45

# ── YOLOv11-NCNN ─────────────────────────────────────────────────────
DETECTION_MODEL = "/home/oluu/sentry/best_ncnn_model"
CONF_THRESHOLD  = 0.35
CLASS_NAMES     = [
    "Axe", "Chainsaw", "Chisel", "Coin",
    "Drink", "Dumbbell", "Fork", "Hammer",
    "Knife", "Scissors", "Screwdriver", "Stapler",
]
THREAT_LABELS   = {"Knife", "Chainsaw", "Axe"}
BBOX_COLORS = [
    (87,120,164),(228,148,68),(209,97,93),(133,182,178),
    (106,159,88),(231,202,96),(168,124,159),(241,162,169),
    (150,118,98),(184,176,172),
]

# ── Web server ────────────────────────────────────────────────────────
HOST            = "0.0.0.0"
PORT            = 5000
MAP_SERVER_PORT = 8000

# ── Cloud Integration ─────────────────────────────────────────────────
# Set CLOUD_AGENT_ENABLED = True after you deploy the gateway to Render.
# Replace the URL with your actual Render service URL.
CLOUD_AGENT_ENABLED = True   # ← flip to True after Render deployment
CLOUD_GATEWAY_URL   = "https://sentry-robot.onrender.com" # ← replace this

