# SENTRY Cloud Integration: Step-by-Step Deployment Guide

This guide describes how to deploy the **Cloud Gateway**, publish the **Cloud Dashboard**, upload and configure the codebase on a **Raspberry Pi**, and connect them together.

---

## Architecture Components

1. **Cloud Gateway** (FastAPI/Socket.IO Backend) $\rightarrow$ Deployed to **Render**.
2. **Cloud Dashboard** (Vanilla HTML/CSS/JS Frontend) $\rightarrow$ Deployed to **Vercel** or **Render Static Sites**.
3. **SENTRY Robot Code** (Flask + Local Threads + Cloud Agent) $\rightarrow$ Uploaded to **Raspberry Pi**.

---

## Step 1: Deploying the Cloud Gateway (Render)

Render supports persistent stateful WebSockets/Socket.IO connections on its free tier.

1. Sign in to [Render](https://render.com/).
2. Click **New +** $\rightarrow$ **Web Service**.
3. Connect your Git Repository.
4. Fill in the following service details:
   * **Name:** `sentry-cloud-gateway` (or any custom name).
   * **Language:** `Python 3`
   * **Branch:** `main` (or your active branch).
   * **Root Directory:** `cloud-gateway` *(This ensures Render builds and runs out of the `cloud-gateway/` folder)*.
   * **Build Command:** `pip install -r requirements.txt`
   * **Start Command:** `uvicorn main:socket_app --host 0.0.0.0 --port $PORT`
5. Click **Create Web Service**.
6. Once deployed, copy your service's public URL (e.g., `https://sentry-cloud-gateway.onrender.com`).

---

## Step 2: Deploying the Cloud Dashboard (Vercel)

The operator frontend consists of static assets (`index.html`, `style.css`, `app.js`), making it ideal for free, fast CDN hosting on Vercel.

### Option A: Using Vercel Dashboard
1. Sign in to [Vercel](https://vercel.com/).
2. Click **Add New** $\rightarrow$ **Project**.
3. Import your Git Repository.
4. Under **Project Settings**:
   * **Framework Preset:** `Other` (or None).
   * **Root Directory:** `cloud-dashboard` *(Only deploy the frontend files)*.
5. Click **Deploy**.
6. Access your dashboard online (e.g., `https://sentry-portal.vercel.app`).

### Option B: Deploying Static Site on Render
If you prefer keeping all services on Render:
1. Click **New +** $\rightarrow$ **Static Site**.
2. Connect your Git repository.
3. Set **Publish Directory** to `cloud-dashboard`.
4. Click **Create Static Site**.

---

## Step 3: Uploading & Configuring the Raspberry Pi

### 1. Upload the files to the Pi
From your development machine, transfer the `obj/` folder and root `requirements.txt` to the Raspberry Pi using `scp` or `rsync`:

```bash
# Uploading via rsync (replace 'pi' and 'raspberrypi.local' with your Pi's credentials)
rsync -avz --exclude '.git' --exclude '__pycache__' \
  ~/Documents/sentry/obj/ \
  ~/Documents/sentry/requirements.txt \
  pi@raspberrypi.local:~/sentry/
```

### 2. Install System Dependencies on the Pi
Connect to the Pi via SSH (`ssh pi@raspberrypi.local`). Many Python packages (like OpenCV and optional WebRTC) require system binaries. Run:

```bash
sudo apt update
sudo apt install -y \
  python3-pip python3-venv \
  libglib2.0-0 libgthread-2.0-0 libgl1-mesa-glx \
  libatlas-base-dev \
  libavformat-dev libavdevice-dev libavfilter-dev \
  libopus-dev libvpx-dev pkg-config
```
*(The `libav*` and `libopus` packages are only needed if you wish to build WebRTC/`aiortc` support later. Skip them if you plan to stick with the fallback JPEG-over-WebSocket stream).*

### 3. Create a Python Virtual Environment
To isolate the dependencies:

```bash
cd ~/sentry
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Enable the Cloud Agent
Edit the configuration file on the Pi ([`obj/config.py`](file:///home/abdulbasit/Documents/sentry/obj/config.py)):

```python
# ── Cloud Integration settings ────────────────────────────────────────
CLOUD_AGENT_ENABLED = True
CLOUD_GATEWAY_URL   = "https://sentry-cloud-gateway.onrender.com"
```
*(Replace `https://sentry-cloud-gateway.onrender.com` with the real URL generated in **Step 1**).*

---

## Step 4: Running & Automating Startup

### Running Manually
To start SENTRY (both local Flask dashboard and Cloud connection client):
```bash
cd ~/sentry
source venv/bin/activate
python3 main.py
```

### Automating on Boot (Systemd)
To ensure the robot launches automatically whenever it is powered on:

1. Create a systemd service file:
   ```bash
   sudo nano /etc/systemd/system/sentry.service
   ```
2. Paste the following configuration:
   ```ini
   [Unit]
   Description=SENTRY Surveillance Robot Core
   After=network.target

   [Service]
   Type=simple
   User=pi
   WorkingDirectory=/home/pi/sentry/obj
   ExecStart=/home/pi/sentry/venv/bin/python3 main.py
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
3. Save the file (`Ctrl+O`, then `Ctrl+X`).
4. Reload systemd, enable, and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable sentry.service
   sudo systemctl start sentry.service
   ```
5. Check if it's running:
   ```bash
   sudo systemctl status sentry.service
   ```

---

## Step 5: Connecting and Operating

1. Power on the robot (or verify the service is running).
2. Open your deployed **Cloud Dashboard** in a web browser.
3. Input your Render Gateway URL (e.g. `https://sentry-cloud-gateway.onrender.com`) and click **CONNECT**.
4. The **ROBOT STATUS** indicator will glow green and read **ONLINE**.
5. Use the arrow keys / D-pad to drive, and click coordinates on the radar screen to navigate.
