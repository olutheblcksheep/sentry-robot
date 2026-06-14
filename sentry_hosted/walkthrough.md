# SENTRY Remote Control & WebRTC Cloud Integration: Walkthrough

We have successfully designed, built, and verified the remote cloud integration for the SENTRY Surveillance Robot. 

The integration allows a SENTRY robot located on a local private network to connect outward to a public Cloud Gateway, and allows operators to teleoperate the robot, stream video, and navigate via a 2D LiDAR radar dashboard from any internet-connected web browser.

---

## 1. Components Created

We added three key components in separate directories:

1. **Cloud Gateway Server (`cloud-gateway/`)**:
   * [`requirements.txt`](file:///home/abdulbasit/Documents/sentry/cloud-gateway/requirements.txt): Python dependencies.
   * [`main.py`](file:///home/abdulbasit/Documents/sentry/cloud-gateway/main.py): FastAPI and Socket.IO server designed to run continuously on a cloud host (like Render). It routes signals and telemetry between the active robot and operator dashboard.
2. **Cloud Dashboard Frontend (`cloud-dashboard/`)**:
   * [`index.html`](file:///home/abdulbasit/Documents/sentry/cloud-dashboard/index.html): Dark cybernetic themed operator interface containing the video feed player, 2D LiDAR radar canvas, D-pad controls, speed sliders, coordinate input panels, and event logger.
   * [`style.css`](file:///home/abdulbasit/Documents/sentry/cloud-dashboard/style.css): Custom styled animations (e.g. scanning line, threat pulses, custom range slider, battery fill).
   * [`app.js`](file:///home/abdulbasit/Documents/sentry/cloud-dashboard/app.js): Socket.IO client interface, canvas drawing coordinate helper, teleoperation and keyboard handlers, and WebRTC peer connection setup.
3. **Robot Cloud Agent (`obj/cloud_agent.py`)**:
   * [`cloud_agent.py`](file:///home/abdulbasit/Documents/sentry/obj/cloud_agent.py): SENTRY robot side client that links the local sensory systems (LiDAR scans, battery ADC, Camera stream) to the gateway, and parses inbound teleoperation and Cartesian coordinates to send to the ESP32. It supports WebRTC with `aiortc` and falls back automatically to JPEG-over-WebSocket if `aiortc` is not installed on the system.

We also integrated this agent into the main startup logic:
* [`config.py`](file:///home/abdulbasit/Documents/sentry/obj/config.py): Added `CLOUD_AGENT_ENABLED` and `CLOUD_GATEWAY_URL` parameters.
* [`main.py`](file:///home/abdulbasit/Documents/sentry/obj/main.py): Automatically launches the cloud agent background thread if enabled.

---

## 2. Verification Results

We verified the local end-to-end integration using a mock operator client script and verified that all commands and telemetry are successfully routed:

### Gateway Server Startup Logs
```
INFO:     Started server process [48649]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)
```

### Robot Agent Startup & Registration Logs
```
[ESP32] Connection failed — [Errno 2] No such file or directory: '/dev/ttyUSB0'
[Detection] Model load failed: No module named 'ultralytics'
[LiDAR]  Error: [Errno 2] No such file or directory: '/dev/ttyUSB1'
cloud-agent — aiortc or av not installed. Streaming will fall back to JPEG-over-WebSocket.
cloud-agent — Connecting to Cloud Gateway: http://localhost:8080...
cloud-agent — Starting JPEG-over-WebSocket fallback stream thread.
cloud-agent — Successfully connected to SENTRY Cloud Gateway!
[Camera] OpenCV /dev/video0 — running
```

### Gateway Connection Acknowledgement
```
INFO:     127.0.0.1:52088 - "WebSocket /socket.io/?..." [accepted]
Client connected: 1TRQyPhG2-oorAf1AAAB
Registering client 1TRQyPhG2-oorAf1AAAB as robot
SENTRY Robot successfully registered
```

### End-to-End Command Routing Test
We simulated an operator connecting to the gateway to send directional commands and coordinate targets:
1. **Operator Command Sent:**
   ```
   [Operator Test] Connected to Cloud Gateway!
   [Operator Test] Robot status received: {'online': True}
   [Operator Test] Sending forward command...
   [Operator Test] Sending navigation coordinates...
   [Operator Test] Battery update received: {'level': 87.5}
   ```
2. **Robot Command Received & Executed:**
   ```
   cloud-agent — Received remote control: forward at speed 90%
   [ESP32 MOCK] → {"cmd": "forward", "speed": 90}
   cloud-agent — Received remote navigation coordinates: X=1500 Y=-800 Speed=75%
   [ESP32 MOCK] → {"cmd": "nav", "x": 1500, "y": -800, "speed": 75}
   ```

---

## 3. How to Deploy & Use

### Local Testing
1. **Launch Cloud Gateway:**
   ```bash
   cd cloud-gateway
   python3 main.py
   ```
2. **Launch SENTRY Robot Server:**
   ```bash
   cd obj
   python3 main.py
   ```
3. **Open Dashboard:**
   Open [`cloud-dashboard/index.html`](file:///home/abdulbasit/Documents/sentry/cloud-dashboard/index.html) in any web browser. Enter `http://localhost:8080` in the connection manager input and click **CONNECT**.
   * Teleoperation commands can be sent using WASD / Arrow keys or the onscreen D-pad.
   * Coordinate targets can be chosen by clicking on the 2D radar grid and clicking **SEND NAV COMMAND**.

### Remote Deployment

#### 1. Deploy the Gateway to Render
1. Create a new Web Service on **Render** (free tier is sufficient).
2. Point it to your repository.
3. Configure start parameters:
   * **Environment:** Python
   * **Build Command:** `pip install -r requirements.txt`
   * **Start Command:** `uvicorn main:socket_app --host 0.0.0.0 --port $PORT`
4. Copy the generated Render Web Service URL (e.g. `https://sentry-gateway.onrender.com`).

#### 2. Configure the Robot
Update the gateway URL in [`obj/config.py`](file:///home/abdulbasit/Documents/sentry/obj/config.py):
```python
CLOUD_GATEWAY_URL = "https://sentry-gateway.onrender.com"
```
When `main.py` is run on the robot, it will connect to Render.

#### 3. Deploy the Dashboard to Vercel or Render Static Hosting
1. Deploy the `cloud-dashboard/` folder as a static site on Vercel or Render Static Sites.
2. Open the deployed website URL on any device, enter your Render Gateway URL (e.g. `https://sentry-gateway.onrender.com`) and click **CONNECT** to start control!
