#!/usr/bin/env python3
"""
cloud_agent.py — SENTRY Robot Cloud Agent.
==========================================
Connects the local SENTRY robot to the remote Cloud Gateway using Socket.IO.
Aggregates local camera, LiDAR, and battery telemetry, and forwards incoming
commands to the local motor controller.

Includes WebRTC low-latency video streaming (using aiortc if installed)
and falls back to JPEG-over-WebSocket streaming if aiortc is unavailable.
"""

import sys
import os
import argparse
import time
import threading
import logging
import json

# Add parent directory to path so we can import from obj/ even if run from inside/outside
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import state
import esp32
import camera
import lidar
import battery

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] cloud-agent — %(message)s")
log = logging.getLogger("cloud-agent")

# Attempt WebRTC dependencies imports
WEBRTC_AVAILABLE = False
try:
    import asyncio
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    from aiortc.mediastreams import MediaStreamError
    from av import VideoFrame
    import cv2
    import numpy as np
    WEBRTC_AVAILABLE = True
    log.info("WebRTC support loaded successfully (aiortc).")
except ImportError:
    log.warning("aiortc or av not installed. Streaming will fall back to JPEG-over-WebSocket.")

# Global socket client
sio = None
is_running = False
local_loop = None

# Custom Video Stream Track for WebRTC using our CameraStream
if WEBRTC_AVAILABLE:
    class CameraVideoTrack(VideoStreamTrack):
        def __init__(self):
            super().__init__()
            self._camera = camera.camera
            
        async def recv(self):
            pts, time_base = await self.next_timestamp()
            
            # Get latest JPEG frame from CameraStream
            jpeg_bytes = self._camera.get_frame()
            if not jpeg_bytes:
                # generate dummy blank frame if no camera frame yet
                img = np.zeros((config.CAMERA_HEIGHT, config.CAMERA_WIDTH, 3), dtype=np.uint8)
            else:
                # Decode JPEG bytes back to BGR numpy array for WebRTC encoding
                nparr = np.frombuffer(jpeg_bytes, np.uint8)
                img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                if img is None:
                    img = np.zeros((config.CAMERA_HEIGHT, config.CAMERA_WIDTH, 3), dtype=np.uint8)

            # Convert BGR to RGB (aiortc expects YUV420p or RGB)
            frame_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            frame = VideoFrame.from_ndarray(frame_rgb, format="rgb24")
            frame.pts = pts
            frame.time_base = time_base
            
            # Match FPS rate limit
            await asyncio.sleep(1 / config.CAMERA_FPS)
            return frame

# --- Socket.IO Event Handlers ---
def setup_socket_events(client):
    @client.event
    def connect():
        log.info("Successfully connected to SENTRY Cloud Gateway!")
        client.emit("register", "robot")

    @client.event
    def disconnect():
        log.warning("Disconnected from SENTRY Cloud Gateway.")

    @client.on("robot_status")
    def on_robot_status(data):
        log.info(f"Gateway robot status report: {data}")

    # Manual movement controls from operator
    @client.on("control")
    def on_control(data):
        cmd = data.get("cmd")
        speed = data.get("speed", 80)
        log.info(f"Received remote control: {cmd} at speed {speed}%")
        esp32.move(cmd, speed)

    # Coordinate navigation from operator
    @client.on("navigate")
    def on_navigate(data):
        x = data.get("x_mm")
        y = data.get("y_mm")
        speed = data.get("speed_pct", 60)
        log.info(f"Received remote navigation coordinates: X={x} Y={y} Speed={speed}%")
        # Forward to ESP32 using the raw serial command writer
        esp32.send_command({
            "cmd": "nav",
            "x": x,
            "y": y,
            "speed": speed
        })

    # WebRTC Signaling handler
    @client.on("webrtc_signal")
    def on_webrtc_signal(data):
        if not WEBRTC_AVAILABLE:
            log.warning("Received WebRTC signal but WebRTC is unavailable. Ignoring.")
            return
            
        payload = data.get("payload")
        operator_sid = data.get("operator_sid")
        
        # We run the WebRTC handling in the main thread's asyncio loop
        if local_loop:
            asyncio.run_coroutine_threadsafe(
                handle_webrtc_signal_async(payload, operator_sid),
                local_loop
            )

# --- WebRTC Asynchronous Negotiation ---
active_pc = None

if WEBRTC_AVAILABLE:
    async def handle_webrtc_signal_async(payload, operator_sid):
        global active_pc
        
        sig_type = payload.get("type")
        if sig_type == "answer":
            if active_pc:
                log.info("WebRTC: setting remote description (answer)")
                await active_pc.setRemoteDescription(RTCSessionDescription(
                    sdp=payload.get("sdp"),
                    type="answer"
                ))
        elif sig_type == "candidate":
            # SENTRY is the offerer, so it generally doesn't receive candidates first,
            # but we support it just in case
            pass
        elif sig_type == "offer":
            # If operator initiates (though usually SENTRY initiates once requested)
            pass

    async def initiate_webrtc_call(operator_sid):
        global active_pc
        log.info(f"WebRTC: Initiating Peer Connection to Operator: {operator_sid}")
        
        if active_pc:
            await active_pc.close()
            
        pc = RTCPeerConnection()
        active_pc = pc
        
        # Add our custom camera track
        video_track = CameraVideoTrack()
        pc.addTrack(video_track)
        
        # Create Offer
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        
        # Send Offer to Operator via Gateway
        sio.emit("webrtc_signal", {
            "operator_sid": operator_sid,
            "payload": {
                "type": "offer",
                "sdp": pc.localDescription.sdp
            }
        })
        
        # Set up local candidate sender
        @pc.on("icecandidate")
        def on_icecandidate(event):
            if event.candidate:
                sio.emit("webrtc_signal", {
                    "operator_sid": operator_sid,
                    "payload": {
                        "type": "candidate",
                        "candidate": event.candidate
                    }
                })

# --- Background Telemetry & Streaming Loops ---
def telemetry_loop():
    """Periodically emits battery levels and system parameters to the cloud."""
    while is_running:
        if sio and sio.connected:
            try:
                sio.emit("battery_update", {"level": round(state.battery_level, 1)})
                sio.emit("status", {
                    "auto_mode": state.auto_mode,
                    "detection_enabled": state.detection_enabled,
                    "mock_mode": config.MOCK_MODE
                })
            except Exception as e:
                log.error(f"Error emitting telemetry: {e}")
        time.sleep(5.0)

def lidar_loop():
    """Periodically emits LiDAR scans to the cloud."""
    last_sent_time = 0
    while is_running:
        # Rate limit LiDAR scans to ~8 Hz to preserve bandwidth
        now = time.time()
        if now - last_sent_time >= 0.125:
            if sio and sio.connected:
                try:
                    with lidar.lidar_lock:
                        points = lidar.lidar_data.copy()
                    if points:
                        sio.emit("lidar_scan", {"points": points})
                        last_sent_time = now
                except Exception as e:
                    log.error(f"Error emitting LiDAR scan: {e}")
        time.sleep(0.05)

def jpeg_stream_loop():
    """Fallback loop that emits JPEG frames over Socket.IO if WebRTC is offline/unsupported."""
    log.info("Starting JPEG-over-WebSocket fallback stream thread.")
    while is_running:
        # If WebRTC is active and connected, we don't need to push JPEGs
        if WEBRTC_AVAILABLE and active_pc and active_pc.connectionState == "connected":
            time.sleep(1.0)
            continue
            
        if sio and sio.connected:
            try:
                frame_bytes = camera.camera.get_frame()
                if frame_bytes:
                    # Emit raw binary payload (Socket.IO client handles bytes automatically)
                    sio.emit("video_frame", frame_bytes)
            except Exception as e:
                log.error(f"Error emitting JPEG video frame: {e}")
                
        # Maintain ~10 FPS target
        time.sleep(1 / config.CAMERA_FPS)

# --- Async Loop Wrapper for WebRTC ---
def start_async_loop():
    global local_loop
    local_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(local_loop)
    local_loop.run_forever()

# --- Main Entry Point ---
def start(gateway_url):
    global sio, is_running
    import socketio

    is_running = True
    sio = socketio.Client()
    setup_socket_events(sio)

    # Start SocketIO outbound connection thread
    def connection_thread():
        while is_running:
            if not sio.connected:
                try:
                    log.info(f"Connecting to Cloud Gateway: {gateway_url}...")
                    sio.connect(gateway_url, transports=["websocket"])
                    sio.wait()
                except Exception as e:
                    log.error(f"Gateway connection error: {e}. Retrying in 5s...")
            time.sleep(5.0)

    threading.Thread(target=connection_thread, daemon=True).start()

    # Start telemetry loops
    threading.Thread(target=telemetry_loop, daemon=True).start()
    threading.Thread(target=lidar_loop, daemon=True).start()
    threading.Thread(target=jpeg_stream_loop, daemon=True).start()

    # Start async thread for aiortc tasks
    if WEBRTC_AVAILABLE:
        threading.Thread(target=start_async_loop, daemon=True).start()
        
        # When an operator registers, ask the gateway to let us initiate WebRTC
        @sio.on("robot_status")
        def on_operator_status_webrtc(data):
            # Check if gateway sent us a request to initiate WebRTC with an operator
            operator_sid = data.get("operator_sid")
            if operator_sid and local_loop:
                asyncio.run_coroutine_threadsafe(
                    initiate_webrtc_call(operator_sid),
                    local_loop
                )

def stop():
    global is_running, sio
    is_running = False
    if sio and sio.connected:
        sio.disconnect()
    if WEBRTC_AVAILABLE and local_loop:
        local_loop.call_soon_threadsafe(local_loop.stop)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SENTRY Robot Cloud Agent Client")
    parser.add_argument("--gateway", default="http://localhost:8080",
                        help="Cloud gateway server address (default: http://localhost:8080)")
    parser.add_argument("--simulate", action="store_true",
                        help="Enable local hardware simulator modules")
    args = parser.parse_args()

    log.info("SENTRY Cloud Agent starting...")

    # If running standalone, we spin up the hardware simulation loops or real loops
    if args.simulate:
        config.MOCK_MODE = True
        log.info("Running standalone with simulated hardware.")
    else:
        # Initialize serial link
        esp32.init_serial()

    # Start mock/real background threads
    camera.camera.start()
    battery.start()
    
    # Standalone LiDAR launch (mimicking main.py)
    if config.MOCK_MODE:
        log.info("LiDAR: mock mode active (simulating radar data)")
    else:
        lidar.start()

    # Start cloud client
    start(args.gateway)

    try:
        # Keep main thread alive
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Terminating Cloud Agent...")
        stop()
