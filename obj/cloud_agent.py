#!/usr/bin/env python3
"""
cloud_agent.py — SENTRY Robot Cloud Agent.
==========================================
Connects the robot to the Cloud Gateway.
Handles all remote commands: drive, navigate, detection toggle,
map save/load, path recording/execution.

Optimizations:
  • WebRTC streams at 160x120 @ 8fps — minimal lag
  • JPEG fallback compressed to 40% quality at 160x120
  • LiDAR pushed at 4Hz instead of 8Hz
  • Grid pushed every 1s instead of 0.5s
"""

import sys, os, time, threading, logging, base64

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config, state, esp32, camera, lidar, battery

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] cloud-agent — %(message)s")
log = logging.getLogger("cloud-agent")

# ── Stream settings ───────────────────────────────────────────────────
STREAM_WIDTH  = 160
STREAM_HEIGHT = 120
STREAM_FPS    = 8
JPEG_QUALITY  = 40
LIDAR_RATE_HZ = 4
GRID_INTERVAL = 1.0

# ── Optional pathfinder ───────────────────────────────────────────────
try:
    import pathfinder as _pf
    PATHFINDER_AVAILABLE = True
except ImportError:
    PATHFINDER_AVAILABLE = False
    log.warning("pathfinder.py not found — navigation commands disabled")

# ── Optional WebRTC ───────────────────────────────────────────────────
WEBRTC_AVAILABLE = False
try:
    import asyncio
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    from av import VideoFrame
    import cv2, numpy as np
    WEBRTC_AVAILABLE = True
    log.info("WebRTC support loaded (aiortc)")
except ImportError:
    log.warning("aiortc not installed — using JPEG fallback stream")
    try:
        import cv2, numpy as np
        CV2_AVAILABLE = True
    except ImportError:
        CV2_AVAILABLE = False

sio        = None
is_running = False
local_loop = None
active_pc  = None

# ── WebRTC video track ────────────────────────────────────────────────

if WEBRTC_AVAILABLE:
    class CameraVideoTrack(VideoStreamTrack):
        def __init__(self):
            super().__init__()
            self._camera = camera.camera

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            jpeg_bytes = self._camera.get_frame()
            if not jpeg_bytes:
                img = np.zeros((STREAM_HEIGHT, STREAM_WIDTH, 3), dtype=np.uint8)
            else:
                nparr = np.frombuffer(jpeg_bytes, np.uint8)
                img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if img is None:
                    img = np.zeros((STREAM_HEIGHT, STREAM_WIDTH, 3), dtype=np.uint8)
                else:
                    img = cv2.resize(img, (STREAM_WIDTH, STREAM_HEIGHT),
                                     interpolation=cv2.INTER_LINEAR)
            frame_rgb       = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            frame           = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
            frame.pts       = pts
            frame.time_base = time_base
            await asyncio.sleep(1 / STREAM_FPS)
            return frame


# ── Socket.IO event handlers ──────────────────────────────────────────

def setup_socket_events(client):

    @client.event
    def connect():
        log.info("Connected to Cloud Gateway")
        client.emit("register", "robot")

    @client.event
    def disconnect():
        log.warning("Disconnected from Cloud Gateway")

    @client.on("control")
    def on_control(data):
        esp32.move(data.get("cmd", "stop"), data.get("speed", 80))

    @client.on("toggle_detection")
    def on_toggle_detection(data):
        state.detection_enabled = bool(data.get("enabled", False))
        log.info(f"Detection {'ON' if state.detection_enabled else 'OFF'}")
        client.emit("status", {
            "detection_enabled": state.detection_enabled,
            "auto_mode": state.auto_mode,
        })

    @client.on("toggle_auto")
    def on_toggle_auto(data):
        state.auto_mode = bool(data.get("enabled", False))

    @client.on("navigate")
    def on_navigate(data):
        if PATHFINDER_AVAILABLE:
            _pf.navigate_to(float(data.get("x_mm", 0)), float(data.get("y_mm", 0)))
        else:
            esp32.send_command({"cmd": "nav", "x": data.get("x_mm"), "y": data.get("y_mm")})

    @client.on("nav_stop")
    def on_nav_stop(data):
        if PATHFINDER_AVAILABLE: _pf.stop_navigation()
        else: esp32.move("stop")

    @client.on("map_save")
    def on_map_save(data):
        if not PATHFINDER_AVAILABLE: return
        fp = _pf.save_map(data.get("name", "map_001"))
        client.emit("map_status", {"message": f"Map saved: {data.get('name','map_001')}"})

    @client.on("map_load")
    def on_map_load(data):
        if not PATHFINDER_AVAILABLE: return
        name = data.get("name", "map_001")
        ok = _pf.load_map(name)
        client.emit("map_loaded", {"name": name, "ok": ok, "home": _pf.home_pos})

    @client.on("map_clear")
    def on_map_clear(data):
        if not PATHFINDER_AVAILABLE: return
        _pf.clear_grid()
        client.emit("map_status", {"message": "Map cleared"})

    @client.on("home_set")
    def on_home_set(data):
        if not PATHFINDER_AVAILABLE: return
        _pf.set_home(float(data.get("x_mm", 0)), float(data.get("y_mm", 0)))
        client.emit("home_set_confirm", {"home": _pf.home_pos})

    @client.on("home_go")
    def on_home_go(data):
        if PATHFINDER_AVAILABLE: _pf.go_home()

    @client.on("path_waypoint")
    def on_path_waypoint(data):
        if not PATHFINDER_AVAILABLE: return
        _pf.record_waypoint(float(data.get("x_mm", 0)), float(data.get("y_mm", 0)))
        client.emit("waypoint_added", {
            "index": len(_pf._recorded_waypoints),
            "x_mm": data.get("x_mm"), "y_mm": data.get("y_mm"),
        })

    @client.on("path_save")
    def on_path_save(data):
        if not PATHFINDER_AVAILABLE: return
        _pf.save_path(data.get("name", "path_001"))
        client.emit("map_status", {"message": f"Path saved: {data.get('name','path_001')}"})

    @client.on("path_run")
    def on_path_run(data):
        if PATHFINDER_AVAILABLE: _pf.load_and_run_path(data.get("name", "path_001"))

    @client.on("path_clear")
    def on_path_clear(data):
        if PATHFINDER_AVAILABLE: _pf.clear_waypoints()

    # ── Servo commands ────────────────────────────────────────────────
    @client.on("servo_cmd")
    def on_servo_cmd(data):
        if data.get("cmd") == "servo_center":
            esp32.send_command({"cmd": "servo_center"})
        else:
            esp32.send_command({
                "cmd":   "servo",
                "id":    data.get("id", "head"),
                "angle": int(data.get("angle", 90)),
            })

    # ── Timed path recorder ───────────────────────────────────────────
    @client.on("record_start")
    def on_record_start(data):
        try:
            import requests
            requests.post("http://localhost:5000/api/record/start", timeout=2)
            client.emit("record_status", {"recording": True, "message": "Recording started"})
        except Exception as e:
            log.error(f"Record start: {e}")

    @client.on("record_stop")
    def on_record_stop(data):
        try:
            import requests
            name = data.get("name", "path_001")
            requests.post("http://localhost:5000/api/record/stop", timeout=2)
            requests.post("http://localhost:5000/api/record/save",
                          json={"name": name}, timeout=2)
            client.emit("record_status", {"recording": False, "message": f"Saved: {name}"})
        except Exception as e:
            log.error(f"Record stop: {e}")

    @client.on("record_replay")
    def on_record_replay(data):
        try:
            import requests
            name = data.get("name", "path_001")
            r = requests.post("http://localhost:5000/api/record/replay",
                              json={"name": name}, timeout=2)
            d = r.json()
            client.emit("replay_status", {
                "status":  "started" if d.get("ok") else "error",
                "message": f"Replaying {d.get('steps',0)} steps" if d.get("ok") else "Path not found",
            })
        except Exception as e:
            log.error(f"Replay: {e}")

    @client.on("record_replay_stop")
    def on_record_replay_stop(data):
        try:
            import requests
            requests.post("http://localhost:5000/api/record/replay/stop", timeout=2)
            client.emit("replay_status", {"status": "done", "message": "Replay stopped"})
        except Exception as e:
            log.error(f"Replay stop: {e}")

    @client.on("record_list")
    def on_record_list(data):
        try:
            import requests
            r = requests.get("http://localhost:5000/api/record/list", timeout=2)
            client.emit("record_list", r.json())
        except Exception as e:
            log.error(f"Record list: {e}")

    @client.on("webrtc_signal")
    def on_webrtc_signal(data):
        if not WEBRTC_AVAILABLE or not local_loop: return
        asyncio.run_coroutine_threadsafe(
            handle_webrtc_signal_async(data.get("payload"), data.get("operator_sid")),
            local_loop
        )


# ── WebRTC async ──────────────────────────────────────────────────────

if WEBRTC_AVAILABLE:
    async def handle_webrtc_signal_async(payload, operator_sid):
        global active_pc
        if not payload: return
        if payload.get("type") == "answer" and active_pc:
            await active_pc.setRemoteDescription(
                RTCSessionDescription(sdp=payload["sdp"], type="answer"))

    async def initiate_webrtc_call(operator_sid):
        global active_pc
        if active_pc: await active_pc.close()
        pc = RTCPeerConnection()
        active_pc = pc
        pc.addTrack(CameraVideoTrack())
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        sio.emit("webrtc_signal", {
            "operator_sid": operator_sid,
            "payload": {"type": "offer", "sdp": pc.localDescription.sdp},
        })

        @pc.on("icecandidate")
        def on_ice(event):
            if event.candidate:
                sio.emit("webrtc_signal", {
                    "operator_sid": operator_sid,
                    "payload": {"type": "candidate", "candidate": event.candidate},
                })


# ── Background loops ──────────────────────────────────────────────────

def telemetry_loop():
    while is_running:
        if sio and sio.connected:
            try:
                sio.emit("battery_update", {"level": round(state.battery_level, 1)})
                sio.emit("status", {
                    "auto_mode":         state.auto_mode,
                    "detection_enabled": state.detection_enabled,
                    "mock_mode":         config.MOCK_MODE,
                    "model_loaded":      True,
                })
            except Exception as e:
                log.error(f"Telemetry error: {e}")
        time.sleep(5.0)


def lidar_stream_loop():
    interval = 1.0 / LIDAR_RATE_HZ
    last = 0
    while is_running:
        now = time.time()
        if now - last >= interval and sio and sio.connected:
            try:
                with lidar.lidar_lock:
                    points = lidar.lidar_data.copy()
                if points:
                    sio.emit("lidar_scan", {"points": points})
                    last = now
            except Exception as e:
                log.error(f"LiDAR stream error: {e}")
        time.sleep(0.05)


def jpeg_stream_loop():
    log.info("JPEG fallback stream thread started")
    interval = 1.0 / STREAM_FPS
    while is_running:
        if WEBRTC_AVAILABLE and active_pc and \
                getattr(active_pc, "connectionState", None) == "connected":
            time.sleep(1.0)
            continue
        if sio and sio.connected:
            try:
                frame_bytes = camera.camera.get_frame()
                if frame_bytes:
                    try:
                        nparr = np.frombuffer(frame_bytes, np.uint8)
                        img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                        if img is not None:
                            img = cv2.resize(img, (STREAM_WIDTH, STREAM_HEIGHT),
                                             interpolation=cv2.INTER_LINEAR)
                            _, buf = cv2.imencode(".jpg", img,
                                                  [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                            frame_bytes = buf.tobytes()
                    except Exception:
                        pass
                    b64 = base64.b64encode(frame_bytes).decode("utf-8")
                    sio.emit("video_frame", {"jpeg": b64})
            except Exception as e:
                log.error(f"JPEG stream error: {e}")
        time.sleep(interval)


def grid_push_loop():
    while is_running:
        if sio and sio.connected and PATHFINDER_AVAILABLE:
            try:
                sio.emit("grid_snapshot", _pf.get_grid_snapshot())
            except Exception as e:
                log.error(f"Grid push error: {e}")
        time.sleep(GRID_INTERVAL)


def async_loop_thread():
    global local_loop
    local_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(local_loop)
    local_loop.run_forever()


# ── Main entry ────────────────────────────────────────────────────────

def start(gateway_url: str):
    global sio, is_running
    import socketio as _sio
    is_running = True
    sio = _sio.Client(reconnection=True, reconnection_attempts=0)
    setup_socket_events(sio)

    def connection_thread():
        while is_running:
            if not sio.connected:
                try:
                    log.info(f"Connecting to {gateway_url}...")
                    sio.connect(gateway_url, transports=["websocket", "polling"])
                    sio.wait()
                except Exception as e:
                    log.error(f"Connection error: {e}. Retrying in 5s...")
            time.sleep(5.0)

    threading.Thread(target=connection_thread, daemon=True).start()
    threading.Thread(target=telemetry_loop,    daemon=True).start()
    threading.Thread(target=lidar_stream_loop, daemon=True).start()
    threading.Thread(target=jpeg_stream_loop,  daemon=True).start()
    threading.Thread(target=grid_push_loop,    daemon=True).start()

    if WEBRTC_AVAILABLE:
        threading.Thread(target=async_loop_thread, daemon=True).start()
        log.info(f"Stream: WebRTC {STREAM_WIDTH}x{STREAM_HEIGHT} @ {STREAM_FPS}fps")
    else:
        log.info(f"Stream: JPEG {STREAM_WIDTH}x{STREAM_HEIGHT} @ {STREAM_FPS}fps q={JPEG_QUALITY}%")


def stop():
    global is_running, sio
    is_running = False
    if sio and sio.connected:
        sio.disconnect()
