import logging
import uvicorn
import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cloud-gateway")

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI(title="SENTRY Cloud Gateway", version="1.0.0")
socket_app = socketio.ASGIApp(sio, app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

robot_sid = None
operator_sids = set()


@app.get("/")
def read_root():
    return {
        "status": "online",
        "robot_connected": robot_sid is not None,
        "operators_count": len(operator_sids),
    }


@sio.event
async def connect(sid, environ):
    logger.info(f"Client connected: {sid}")


@sio.event
async def disconnect(sid):
    global robot_sid
    logger.info(f"Client disconnected: {sid}")
    if sid == robot_sid:
        robot_sid = None
        await sio.emit("robot_status", {"online": False}, room="operators")
    elif sid in operator_sids:
        operator_sids.remove(sid)


@sio.on("register")
async def handle_register(sid, role):
    global robot_sid
    if role == "robot":
        if robot_sid and robot_sid != sid:
            await sio.disconnect(robot_sid)
        robot_sid = sid
        await sio.enter_room(sid, "robot")
        logger.info("SENTRY Robot registered")
        await sio.emit("robot_status", {"online": True}, room="operators")
    elif role == "operator":
        operator_sids.add(sid)
        await sio.enter_room(sid, "operators")
        await sio.emit("robot_status", {"online": robot_sid is not None}, to=sid)


# ── Operator → Robot commands ─────────────────────────────────────────

async def _to_robot(event, data, sid):
    if sid in operator_sids and robot_sid:
        await sio.emit(event, data, to=robot_sid)

@sio.on("control")
async def handle_control(sid, data):
    await _to_robot("control", data, sid)

@sio.on("navigate")
async def handle_navigate(sid, data):
    await _to_robot("navigate", data, sid)

@sio.on("toggle_detection")
async def handle_toggle_detection(sid, data):
    await _to_robot("toggle_detection", data, sid)

@sio.on("toggle_auto")
async def handle_toggle_auto(sid, data):
    await _to_robot("toggle_auto", data, sid)

# Map commands
@sio.on("map_save")
async def handle_map_save(sid, data):
    await _to_robot("map_save", data, sid)

@sio.on("map_load")
async def handle_map_load(sid, data):
    await _to_robot("map_load", data, sid)

@sio.on("map_clear")
async def handle_map_clear(sid, data):
    await _to_robot("map_clear", data, sid)

@sio.on("home_set")
async def handle_home_set(sid, data):
    await _to_robot("home_set", data, sid)

@sio.on("home_go")
async def handle_home_go(sid, data):
    await _to_robot("home_go", data, sid)

@sio.on("path_waypoint")
async def handle_path_waypoint(sid, data):
    await _to_robot("path_waypoint", data, sid)

@sio.on("path_save")
async def handle_path_save(sid, data):
    await _to_robot("path_save", data, sid)

@sio.on("path_run")
async def handle_path_run(sid, data):
    await _to_robot("path_run", data, sid)

@sio.on("path_clear")
async def handle_path_clear(sid, data):
    await _to_robot("path_clear", data, sid)

@sio.on("nav_stop")
async def handle_nav_stop(sid, data):
    await _to_robot("nav_stop", data, sid)


# ── Robot → Operators telemetry ───────────────────────────────────────

async def _to_operators(event, data, sid):
    if sid == robot_sid:
        await sio.emit(event, data, room="operators")

@sio.on("battery_update")
async def handle_battery(sid, data):
    await _to_operators("battery_update", data, sid)

@sio.on("status")
async def handle_status(sid, data):
    await _to_operators("status", data, sid)

@sio.on("detection")
async def handle_detection(sid, data):
    await _to_operators("detection", data, sid)

@sio.on("alert")
async def handle_alert(sid, data):
    await _to_operators("alert", data, sid)

@sio.on("obstacle")
async def handle_obstacle(sid, data):
    await _to_operators("obstacle", data, sid)

@sio.on("lidar_scan")
async def handle_lidar(sid, data):
    await _to_operators("lidar_scan", data, sid)

@sio.on("video_frame")
async def handle_video_frame(sid, data):
    await _to_operators("video_frame", data, sid)

@sio.on("path_status")
async def handle_path_status(sid, data):
    await _to_operators("path_status", data, sid)

@sio.on("path_update")
async def handle_path_update(sid, data):
    await _to_operators("path_update", data, sid)

@sio.on("map_status")
async def handle_map_status(sid, data):
    await _to_operators("map_status", data, sid)

@sio.on("map_loaded")
async def handle_map_loaded(sid, data):
    await _to_operators("map_loaded", data, sid)

@sio.on("home_set_confirm")
async def handle_home_set_confirm(sid, data):
    await _to_operators("home_set_confirm", data, sid)

@sio.on("waypoint_added")
async def handle_waypoint_added(sid, data):
    await _to_operators("waypoint_added", data, sid)

@sio.on("grid_snapshot")
async def handle_grid_snapshot(sid, data):
    await _to_operators("grid_snapshot", data, sid)

# WebRTC relay
@sio.on("webrtc_signal")
async def handle_webrtc_signal(sid, data):
    if sid == robot_sid:
        recipient_sid = data.get("operator_sid")
        if recipient_sid:
            await sio.emit("webrtc_signal", {"payload": data.get("payload")}, to=recipient_sid)
        else:
            await sio.emit("webrtc_signal", {"payload": data.get("payload")}, room="operators")
    elif sid in operator_sids and robot_sid:
        await sio.emit("webrtc_signal", {
            "operator_sid": sid,
            "payload": data.get("payload")
        }, to=robot_sid)

# Ping
@sio.on("ping_req")
async def handle_ping(sid, data=None):
    await sio.emit("pong_res", {}, to=sid)


if __name__ == "__main__":
    uvicorn.run("main:socket_app", host="0.0.0.0", port=8080, log_level="info")
