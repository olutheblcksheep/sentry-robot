"""
routes.py — Flask HTTP routes + SocketIO event handlers.
========================================================
Includes all original routes (camera, detection, ESP32, LiDAR)
plus pathfinder routes (navigate, grid, map save/load, path save/run).
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
    host = request.host.split(":")[0]
    return redirect(f"http://{host}:{config.MAP_SERVER_PORT}/")


@app.route("/video_feed")
def video_feed():
    return Response(camera.mjpeg_generator(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/control", methods=["POST"])
def control():
    data  = request.get_json(force=True)
    cmd   = data.get("cmd", "stop")
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
        "battery":           round(state.battery_level, 1),
        "detection_enabled": state.detection_enabled,
        "auto_mode":         state.auto_mode,
        "serial_connected":  esp32.serial_conn is not None and esp32.serial_conn.is_open,
        "mock_mode":         config.MOCK_MODE,
        "model_loaded":      detection.yolo_model is not None,
        "classes":           detection.CLASS_NAMES,
        "lidar_enabled":     config.LIDAR_ENABLED,
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


# ── Pathfinder / Map / Navigate routes ───────────────────────────────

try:
    import pathfinder as _pf

    @app.route("/api/navigate", methods=["POST"])
    def navigate():
        d  = request.get_json(force=True)
        ok = _pf.navigate_to(float(d.get("x", 0)), float(d.get("y", 0)))
        return jsonify({"ok": ok, "message": "Navigating" if ok else "No path found"})

    @app.route("/api/navigate/stop", methods=["POST"])
    def navigate_stop():
        _pf.stop_navigation()
        return jsonify({"ok": True})

    @app.route("/api/grid")
    def get_grid():
        return jsonify(_pf.get_grid_snapshot())

    @app.route("/api/map/save", methods=["POST"])
    def map_save():
        d    = request.get_json(force=True)
        name = d.get("name", "map_001")
        fp   = _pf.save_map(name)
        return jsonify({"ok": True, "file": fp})

    @app.route("/api/map/load", methods=["POST"])
    def map_load():
        d    = request.get_json(force=True)
        name = d.get("name", "map_001")
        ok   = _pf.load_map(name)
        return jsonify({"ok": ok})

    @app.route("/api/map/clear", methods=["POST"])
    def map_clear():
        _pf.clear_grid()
        return jsonify({"ok": True})

    @app.route("/api/map/list")
    def map_list():
        return jsonify({"maps": _pf.list_maps(), "paths": _pf.list_paths()})

    @app.route("/api/home/set", methods=["POST"])
    def home_set():
        d = request.get_json(force=True)
        _pf.set_home(float(d.get("x_mm", 0)), float(d.get("y_mm", 0)))
        return jsonify({"ok": True, "home": _pf.home_pos})

    @app.route("/api/home/go", methods=["POST"])
    def home_go():
        ok = _pf.go_home()
        return jsonify({"ok": ok})

    @app.route("/api/path/waypoint", methods=["POST"])
    def path_waypoint():
        d = request.get_json(force=True)
        _pf.record_waypoint(float(d.get("x_mm", 0)), float(d.get("y_mm", 0)))
        return jsonify({"ok": True, "count": len(_pf._recorded_waypoints)})

    @app.route("/api/path/save", methods=["POST"])
    def path_save():
        d    = request.get_json(force=True)
        name = d.get("name", "path_001")
        fp   = _pf.save_path(name)
        return jsonify({"ok": True, "file": fp})

    @app.route("/api/path/run", methods=["POST"])
    def path_run():
        d    = request.get_json(force=True)
        name = d.get("name", "path_001")
        ok   = _pf.load_and_run_path(name)
        return jsonify({"ok": ok})

    @app.route("/api/path/clear", methods=["POST"])
    def path_clear():
        _pf.clear_waypoints()
        return jsonify({"ok": True})

except ImportError:
    print("[Routes] pathfinder.py not found — navigation routes disabled")


# ── SocketIO events ───────────────────────────────────────────────────

@socketio.on("connect")
def on_connect():
    emit("battery_update", {"level": round(state.battery_level, 1)})
    emit("status", {
        "serial_connected":  esp32.serial_conn is not None,
        "detection_enabled": state.detection_enabled,
        "auto_mode":         state.auto_mode,
        "mock_mode":         config.MOCK_MODE,
        "model_loaded":      detection.yolo_model is not None,
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
