import logging
import uvicorn
import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("cloud-gateway")

# Create Socket.IO server
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI(title="SENTRY Cloud Gateway", version="1.0.0")

# Wrap FastAPI with ASGI Socket.IO app
socket_app = socketio.ASGIApp(sio, app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Active connections tracking
robot_sid = None
operator_sids = set()

@app.get("/")
def read_root():
    return {
        "status": "online",
        "robot_connected": robot_sid is not None,
        "operators_count": len(operator_sids),
    }

# Socket.IO Event Handlers
@sio.event
async def connect(sid, environ):
    logger.info(f"Client connected: {sid}")

@sio.event
async def disconnect(sid):
    global robot_sid
    logger.info(f"Client disconnected: {sid}")
    
    if sid == robot_sid:
        logger.info("SENTRY Robot disconnected")
        robot_sid = None
        # Notify operators that robot is offline
        await sio.emit("robot_status", {"online": False}, room="operators")
    elif sid in operator_sids:
        logger.info(f"Operator disconnected: {sid}")
        operator_sids.remove(sid)

@sio.on("register")
async def handle_register(sid, role):
    global robot_sid
    logger.info(f"Registering client {sid} as {role}")
    
    if role == "robot":
        if robot_sid is not None and robot_sid != sid:
            logger.warning(f"New robot connecting. Disconnecting previous robot: {robot_sid}")
            await sio.disconnect(robot_sid)
        robot_sid = sid
        await sio.enter_room(sid, "robot")
        logger.info("SENTRY Robot successfully registered")
        # Notify operators that robot is online
        await sio.emit("robot_status", {"online": True}, room="operators")
        
    elif role == "operator":
        operator_sids.add(sid)
        await sio.enter_room(sid, "operators")
        logger.info(f"Operator registered: {sid}")
        # Send current robot status to the new operator
        await sio.emit("robot_status", {"online": robot_sid is not None}, to=sid)

# --- Commands: Operator -> Robot ---
@sio.on("control")
async def handle_control(sid, data):
    if sid in operator_sids:
        if robot_sid:
            logger.debug(f"Forwarding control command to robot: {data}")
            await sio.emit("control", data, to=robot_sid)
        else:
            logger.warning("Operator tried to send control, but robot is offline")

@sio.on("navigate")
async def handle_navigate(sid, data):
    if sid in operator_sids:
        if robot_sid:
            logger.info(f"Forwarding navigation target to robot: {data}")
            await sio.emit("navigate", data, to=robot_sid)
        else:
            logger.warning("Operator tried to send navigate target, but robot is offline")

# --- Telemetry & Events: Robot -> Operators ---
# Forward robot telemetry/scans/alerts to all operators
@sio.on("battery_update")
async def handle_battery(sid, data):
    if sid == robot_sid:
        await sio.emit("battery_update", data, room="operators")

@sio.on("status")
async def handle_status(sid, data):
    if sid == robot_sid:
        await sio.emit("status", data, room="operators")

@sio.on("detection")
async def handle_detection(sid, data):
    if sid == robot_sid:
        await sio.emit("detection", data, room="operators")

@sio.on("alert")
async def handle_alert(sid, data):
    if sid == robot_sid:
        await sio.emit("alert", data, room="operators")

@sio.on("obstacle")
async def handle_obstacle(sid, data):
    if sid == robot_sid:
        await sio.emit("obstacle", data, room="operators")

@sio.on("lidar_scan")
async def handle_lidar(sid, data):
    if sid == robot_sid:
        await sio.emit("lidar_scan", data, room="operators")

@sio.on("video_frame")
async def handle_video_frame(sid, data):
    if sid == robot_sid:
        await sio.emit("video_frame", data, room="operators")

# --- WebRTC Signaling Relay ---
@sio.on("webrtc_signal")
async def handle_webrtc_signal(sid, data):
    """
    Relays WebRTC signals between Operator and Robot.
    Data format:
    {
        "recipient": "robot" or "operator",
        "payload": { ...SDP/ICE details... },
        "operator_sid": "..." (optional, populated by gateway to route back to specific operator)
    }
    """
    if sid == robot_sid:
        # Relaying from robot to operator
        recipient_sid = data.get("operator_sid")
        if recipient_sid:
            logger.info(f"Relaying WebRTC response from Robot to Operator {recipient_sid}")
            await sio.emit("webrtc_signal", {
                "payload": data.get("payload")
            }, to=recipient_sid)
        else:
            # Broadcast to all operators if no specific operator is targeted
            logger.info("Relaying WebRTC response from Robot to all Operators")
            await sio.emit("webrtc_signal", {
                "payload": data.get("payload")
            }, room="operators")
    elif sid in operator_sids:
        # Relaying from operator to robot
        if robot_sid:
            logger.info(f"Relaying WebRTC signal from Operator {sid} to Robot")
            await sio.emit("webrtc_signal", {
                "operator_sid": sid,  # attach sender sid so robot knows who to reply to
                "payload": data.get("payload")
            }, to=robot_sid)
        else:
            logger.warning("Operator tried to signal WebRTC, but robot is offline")

if __name__ == "__main__":
    uvicorn.run("main:socket_app", host="0.0.0.0", port=8080, log_level="info")
