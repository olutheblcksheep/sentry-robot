"""
routes.py — Flask HTTP routes + SocketIO event handlers.
=========================================================
Includes:
  - Camera, detection, ESP32, LiDAR routes (original)
  - Pathfinder / map / navigate routes
  - Servo control route
  - Timed path recording and replay
"""

from flask import Response, jsonify, request, send_from_directory, redirect
from flask_socketio import emit
import time as _time
import json
import os
import threading

from state import app, socketio
import state
import config
import esp32
import detection
import camera
import lidar


# ═══════════════════════════════════════════════════════════════
#  STANDARD ROUTES
# ═══════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/mobile")
def mobile():
    return send_from_directory("static", "mobile.html")


@app.route("/map")
def go_to_map():
    host = request.host.split(":")[0]
    return redirect(f"http://{host}:{config.MAP_SERVER_PORT}/")


@app.route("/video_feed")
def video_feed():
    return Response(camera.mjpeg_generator(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/detection", methods=["POST"])
def toggle_detection():
    state.detection_enabled = bool(request.get_json(force=True).get("enabled", False))
    print(f"[Detection] {'Enabled' if state.detection_enabled else 'Disabled'}")
    return jsonify({"ok": True, "detection_enabled": state.detection_enabled})


@app.route("/api/auto", methods=["POST"])
def toggle_auto():
    state.auto_mode = bool(request.get_json(force=True).get("enabled", False))
    print(f"[Auto] {'ON' if state.auto_mode else 'OFF'}")
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
        "recording":         _recording,
        "replay_running":    _replay_running,
    })


@app.route("/api/infer", methods=["POST"])
def infer_frame():
    try:
        f = request.files.get("frame")
        if not f:
            return jsonify({"ok": False, "error": "No frame"}), 400
        dets = detection.run_inference(f.read())
        detection.emit_detections(dets, source="phone")
        return jsonify({"ok": True, "detections": dets})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════
#  CONTROL ROUTE (with recording hook)
# ═══════════════════════════════════════════════════════════════

@app.route("/api/control", methods=["POST"])
def control():
    global _last_cmd, _last_cmd_time, _recorded_cmds

    data  = request.get_json(force=True)
    cmd   = data.get("cmd", "stop")
    speed = int(data.get("speed", 80))

    # ── Record timed commands ─────────────────────────────────────
    if _recording:
        now = _time.time()
        if _last_cmd and _last_cmd != cmd:
            duration = now - _last_cmd_time
            if duration > 0.05:
                _recorded_cmds.append({
                    "cmd":      _last_cmd,
                    "speed":    speed,
                    "duration": round(duration, 3),
                })
        _last_cmd      = cmd
        _last_cmd_time = now

    # ── Handle servo commands ─────────────────────────────────────
    if cmd == "servo":
        servo_id = data.get("id", "")
        angle    = int(data.get("angle", 90))
        esp32.send_command({"cmd": "servo", "id": servo_id, "angle": angle})
        return jsonify({"ok": True, "cmd": cmd, "id": servo_id, "angle": angle})

    if cmd == "servo_center":
        esp32.send_command({"cmd": "servo_center"})
        return jsonify({"ok": True, "cmd": cmd})

    # ── Motor commands ────────────────────────────────────────────
    if esp32.move(cmd, speed):
        return jsonify({"ok": True, "cmd": cmd, "speed": speed})
    return jsonify({"ok": False, "error": "Unknown command"}), 400


# ═══════════════════════════════════════════════════════════════
#  TIMED PATH RECORDING
# ═══════════════════════════════════════════════════════════════

_recording      = False
_recorded_cmds  = []
_last_cmd       = None
_last_cmd_time  = 0
_replay_running = False

PATHS_DIR = os.path.expanduser("~/sentry/paths")
os.makedirs(PATHS_DIR, exist_ok=True)


@app.route("/api/record/start", methods=["POST"])
def record_start():
    global _recording, _recorded_cmds, _last_cmd, _last_cmd_time
    _recording     = True
    _recorded_cmds = []
    _last_cmd      = None
    _last_cmd_time = _time.time()
    socketio.emit("record_status", {"recording": True, "message": "Recording started"})
    return jsonify({"ok": True, "message": "Recording started"})


@app.route("/api/record/stop", methods=["POST"])
def record_stop():
    global _recording, _last_cmd, _last_cmd_time
    # Save the last command
    if _last_cmd and _last_cmd != "stop":
        duration = _time.time() - _last_cmd_time
        if duration > 0.05:
            _recorded_cmds.append({
                "cmd":      _last_cmd,
                "speed":    80,
                "duration": round(duration, 3),
            })
    _recording = False
    esp32.move("stop", 0)
    socketio.emit("record_status", {"recording": False, "message": f"Stopped — {len(_recorded_cmds)} steps"})
    return jsonify({"ok": True, "steps": len(_recorded_cmds)})


@app.route("/api/record/save", methods=["POST"])
def record_save():
    d    = request.get_json(force=True)
    name = d.get("name", "path_001")
    fp   = os.path.join(PATHS_DIR, f"{name}.json")
    with open(fp, "w") as f:
        json.dump({"steps": _recorded_cmds, "saved_at": _time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    return jsonify({"ok": True, "steps": len(_recorded_cmds), "file": fp})


@app.route("/api/record/list")
def record_list():
    paths = [f.replace(".json", "") for f in os.listdir(PATHS_DIR) if f.endswith(".json")]
    return jsonify({"paths": sorted(paths)})


@app.route("/api/record/replay", methods=["POST"])
def record_replay():
    d    = request.get_json(force=True)
    name = d.get("name", "path_001")
    fp   = os.path.join(PATHS_DIR, f"{name}.json")
    if not os.path.exists(fp):
        return jsonify({"ok": False, "message": "Path not found"}), 404
    with open(fp) as f:
        data = json.load(f)
    steps = data.get("steps", [])
    if not steps:
        return jsonify({"ok": False, "message": "No steps in path"}), 400
    threading.Thread(target=_run_replay, args=(steps,), daemon=True).start()
    return jsonify({"ok": True, "steps": len(steps)})


@app.route("/api/record/replay/stop", methods=["POST"])
def replay_stop():
    global _replay_running
    _replay_running = False
    esp32.move("stop", 0)
    return jsonify({"ok": True})


def _run_replay(steps):
    global _replay_running
    _replay_running = True
    socketio.emit("replay_status", {"status": "started", "message": f"Replaying {len(steps)} steps"})
    for i, step in enumerate(steps):
        if not _replay_running:
            break
        esp32.move(step["cmd"], step.get("speed", 80))
        socketio.emit("replay_status", {
            "status":  "running",
            "message": f"Step {i+1}/{len(steps)}: {step['cmd']} for {step['duration']}s",
        })
        _time.sleep(step["duration"])
    esp32.move("stop", 0)
    _replay_running = False
    socketio.emit("replay_status", {"status": "done", "message": "Replay complete"})


# ═══════════════════════════════════════════════════════════════
#  PATHFINDER ROUTES
# ═══════════════════════════════════════════════════════════════

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
        d = request.get_json(force=True)
        return jsonify({"ok": True, "file": _pf.save_map(d.get("name", "map_001"))})

    @app.route("/api/map/load", methods=["POST"])
    def map_load():
        d = request.get_json(force=True)
        return jsonify({"ok": _pf.load_map(d.get("name", "map_001"))})

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
        return jsonify({"ok": _pf.go_home()})

    @app.route("/api/path/waypoint", methods=["POST"])
    def path_waypoint():
        d = request.get_json(force=True)
        _pf.record_waypoint(float(d.get("x_mm", 0)), float(d.get("y_mm", 0)))
        return jsonify({"ok": True, "count": len(_pf._recorded_waypoints)})

    @app.route("/api/path/save", methods=["POST"])
    def path_save():
        d = request.get_json(force=True)
        return jsonify({"ok": True, "file": _pf.save_path(d.get("name", "path_001"))})

    @app.route("/api/path/run", methods=["POST"])
    def path_run():
        d = request.get_json(force=True)
        return jsonify({"ok": _pf.load_and_run_path(d.get("name", "path_001"))})

    @app.route("/api/path/clear", methods=["POST"])
    def path_clear():
        _pf.clear_waypoints()
        return jsonify({"ok": True})

except ImportError:
    print("[Routes] pathfinder.py not found — navigation routes disabled")


# ═══════════════════════════════════════════════════════════════
#  SOCKETIO EVENTS
# ═══════════════════════════════════════════════════════════════

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
    cmd   = data.get("cmd", "stop")
    speed = int(data.get("speed", 80))

    # Record if active
    global _last_cmd, _last_cmd_time, _recorded_cmds
    if _recording:
        now = _time.time()
        if _last_cmd and _last_cmd != cmd:
            duration = now - _last_cmd_time
            if duration > 0.05:
                _recorded_cmds.append({
                    "cmd":      _last_cmd,
                    "speed":    speed,
                    "duration": round(duration, 3),
                })
        _last_cmd      = cmd
        _last_cmd_time = now

    if cmd == "servo":
        esp32.send_command({"cmd": "servo", "id": data.get("id"), "angle": data.get("angle", 90)})
    elif cmd == "servo_center":
        esp32.send_command({"cmd": "servo_center"})
    else:
        esp32.move(cmd, speed)


@socketio.on("toggle_detection")
def on_toggle_detection(data):
    state.detection_enabled = bool(data.get("enabled", False))


@socketio.on("toggle_auto")
def on_toggle_auto(data):
    state.auto_mode = bool(data.get("enabled", False))
