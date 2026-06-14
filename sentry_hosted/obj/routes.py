"""
routes.py — Flask HTTP routes + SocketIO event handlers.
========================================================
Importing this module registers every route and socket handler on the
shared `app` / `socketio` from state.py. main.py imports it once before
starting the server.

Includes the cross-link route `/map`, which redirects the operator from
this camera dashboard to the LiDAR map dashboard (server.py). The target
host is taken from the incoming request, so it works on any IP without
hard-coding the Pi's address — only the port (config.MAP_SERVER_PORT)
is fixed.
"""

from flask import Response, jsonify, request, send_from_directory, redirect
from flask_socketio import emit

from state import app, socketio
import state
import config
import esp32
import detection
import camera
import lidar


# ── HTTP routes ───────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/mobile")
def mobile():
    return send_from_directory("static", "mobile.html")


@app.route("/map")
def go_to_map():
    """Redirect to the LiDAR / navigation dashboard (server.py)."""
    host = request.host.split(":")[0]          # strip any :port
    return redirect(f"http://{host}:{config.MAP_SERVER_PORT}/")


@app.route("/video_feed")
def video_feed():
    return Response(camera.mjpeg_generator(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/control", methods=["POST"])
def control():
    data = request.get_json(force=True)
    cmd = data.get("cmd", "stop")
    speed = int(data.get("speed", 80))
    if esp32.move(cmd, speed):
        return jsonify({"ok": True, "cmd": cmd, "speed": speed})
    return jsonify({"ok": False, "error": "Unknown command"}), 400


@app.route("/api/detection", methods=["POST"])
def toggle_detection():
    state.detection_enabled = bool(request.get_json(force=True).get("enabled", False))
    print(f"[Detection] {'Enabled' if state.detection_enabled else 'Disabled'}")
    return jsonify({"ok": True, "detection_enabled": state.detection_enabled})


@app.route("/api/auto", methods=["POST"])
def toggle_auto():
    state.auto_mode = bool(request.get_json(force=True).get("enabled", False))
    print(f"[LiDAR]  Auto mode {'ON' if state.auto_mode else 'OFF'}")
    return jsonify({"ok": True, "auto_mode": state.auto_mode})


@app.route("/api/lidar")
def lidar_snapshot():
    with lidar.lidar_lock:
        return jsonify({"points": lidar.lidar_data.copy()})


@app.route("/api/status")
def status():
    return jsonify({
        "battery": round(state.battery_level, 1),
        "detection_enabled": state.detection_enabled,
        "auto_mode": state.auto_mode,
        "serial_connected": esp32.serial_conn is not None and esp32.serial_conn.is_open,
        "mock_mode": config.MOCK_MODE,
        "model_loaded": detection.yolo_model is not None,
        "classes": detection.CLASS_NAMES,
        "lidar_enabled": config.LIDAR_ENABLED,
    })


@app.route("/api/infer", methods=["POST"])
def infer_frame():
    """Accept a JPEG frame from the mobile app, run YOLOv11-NCNN, emit results."""
    try:
        f = request.files.get("frame")
        if not f:
            return jsonify({"ok": False, "error": "No frame"}), 400
        dets = detection.run_inference(f.read())
        detection.emit_detections(dets, source="phone")
        return jsonify({"ok": True, "detections": dets})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── SocketIO events ───────────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    emit("battery_update", {"level": round(state.battery_level, 1)})
    emit("status", {
        "serial_connected": esp32.serial_conn is not None,
        "detection_enabled": state.detection_enabled,
        "auto_mode": state.auto_mode,
        "mock_mode": config.MOCK_MODE,
        "model_loaded": detection.yolo_model is not None,
    })


@socketio.on("control")
def on_control(data):
    esp32.move(data.get("cmd", "stop"), int(data.get("speed", 80)))


@socketio.on("toggle_detection")
def on_toggle_detection(data):
    state.detection_enabled = bool(data.get("enabled", False))


@socketio.on("toggle_auto")
def on_toggle_auto(data):
    state.auto_mode = bool(data.get("enabled", False))
