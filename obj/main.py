#!/usr/bin/env python3
"""
main.py — SENTRY camera/ESP32/YOLO node entry point.
====================================================
This is the one file you run. It imports the section modules, starts the
hardware threads in a clear, debuggable order, and launches the web
server.

Run (from inside this folder):
    python main.py

Module map (each is a small, independently-debuggable file):
    config.py     — all tunable constants
    state.py      — Flask app, SocketIO, shared runtime flags
    esp32.py      — ESP32 serial link + motor commands
    detection.py  — YOLOv11-NCNN model, drawing, inference
    camera.py     — camera capture + MJPEG stream
    lidar.py      — D500 reader + reactive obstacle stop
    battery.py    — battery monitor
    routes.py     — Flask routes + SocketIO events (imported for side effects)

Note on startup: in the original single-file version the camera/lidar/
battery threads were started and the model was loaded at import time.
Here those side effects are deferred to this file so importing any single
module (e.g. for testing) does NOT spin up hardware. Runtime behaviour
when launched via `python main.py` is unchanged.

Folder layout expected on the Pi:
    obj/
    ├── main.py            ← run this
    ├── config.py
    ├── state.py
    ├── esp32.py
    ├── detection.py
    ├── camera.py
    ├── lidar.py
    ├── battery.py
    ├── routes.py
    └── static/
        ├── index.html
        └── mobile.html
"""

import config
from state import app, socketio   # noqa: F401  (app used by routes)
import esp32
import detection
import camera
import lidar
import battery
import routes                      # noqa: F401  (import registers routes)

try:
    import imu
    IMU_AVAILABLE = True
except ImportError:
    IMU_AVAILABLE = False

try:
    import gps
    GPS_AVAILABLE = True
except ImportError:
    GPS_AVAILABLE = False


def main():
    # 1. Serial link to the ESP32 motor controller.
    esp32.init_serial()

    # 2. Load the detection model (sets detection.yolo_model).
    detection.load_model()

    # 3. Start the background hardware threads.
    camera.camera.start()
    battery.start()
    lidar.start()

    # 3b. Start the IMU — calibrates gyro bias, must run with robot still.
    imu_started = False
    if IMU_AVAILABLE:
        print("[IMU] Starting MPU6050 — keep robot stationary for calibration...")
        imu_started = imu.start()

    # 3c. Start GPS — for outdoor patrol. Indoors this will simply report
    #     no fix and the robot falls back to the LiDAR/IMU pathfinder.
    gps_started = False
    if GPS_AVAILABLE:
        gps_started = gps.start()
        if gps_started:
            print("[GPS] NEO-6M started — waiting for satellite fix...")

    # Start Cloud Integration Agent if enabled
    if config.CLOUD_AGENT_ENABLED:
        import cloud_agent
        cloud_agent.start(config.CLOUD_GATEWAY_URL)

    # 4. Banner.
    print(f"\n{'='*48}")
    print(f"  SENTRY Surveillance Robot")
    print(f"{'='*48}")
    print(f"  Dashboard  → http://<pi-ip>:{config.PORT}")
    print(f"  Mobile     → http://<pi-ip>:{config.PORT}/mobile")
    print(f"  LiDAR map  → http://<pi-ip>:{config.PORT}/map "
          f"(redirects to :{config.MAP_SERVER_PORT})")
    print(f"  Mock mode    {config.MOCK_MODE}")
    print(f"  Camera       {config.CAMERA_BACKEND}")
    print(f"  ESP32        {config.ESP32_PORT}")
    print(f"  LiDAR        {config.LIDAR_PORT} (enabled={config.LIDAR_ENABLED})")
    print(f"  IMU          {'MPU6050 active' if imu_started else 'NOT AVAILABLE'}")
    print(f"  GPS          {'NEO-6M active (awaiting fix)' if gps_started else 'NOT AVAILABLE'}")
    print(f"  Model        {'loaded' if detection.yolo_model else 'FAILED'}")
    print(f"  Classes      {detection.CLASS_NAMES}")
    print(f"{'='*48}\n")

    # 5. Run the web server (blocking).
    socketio.run(app, host=config.HOST, port=config.PORT,
                 debug=False, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
