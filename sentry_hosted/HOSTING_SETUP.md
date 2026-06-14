# SENTRY — Free Hosting Setup Guide
## Access your robot from anywhere in the world

---

## How it works

```
Your Phone/Laptop
      ↓  opens
Cloud Dashboard (Vercel — free)
      ↓  connects to
Cloud Gateway (Render — free)
      ↑  connects to
Raspberry Pi (cloud_agent.py)
      ↑  controls
ESP32 + Camera + LiDAR
```

---

## STEP 1 — Push your code to GitHub (one time)

On your Pi or laptop:

```bash
cd ~/sentry
git init
git add .
git commit -m "SENTRY initial commit"
```

Go to https://github.com → New Repository → name it `sentry-robot` → Create.

Then:
```bash
git remote add origin https://github.com/YOUR_USERNAME/sentry-robot.git
git push -u origin main
```

---

## STEP 2 — Deploy Cloud Gateway to Render (free)

1. Go to https://render.com → Sign up free
2. Click **New +** → **Web Service**
3. Connect your GitHub → select `sentry-robot`
4. Fill in:
   - **Root Directory:** `cloud-gateway`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:socket_app --host 0.0.0.0 --port $PORT`
5. Click **Create Web Service**
6. Wait ~2 minutes for it to deploy
7. Copy your URL — it looks like:
   `https://sentry-robot-xxxx.onrender.com`

---

## STEP 3 — Deploy Cloud Dashboard to Vercel (free)

1. Go to https://vercel.com → Sign up free (use GitHub)
2. Click **Add New** → **Project**
3. Import your `sentry-robot` repo
4. Set **Root Directory** to `cloud-dashboard`
5. Click **Deploy**
6. Your dashboard URL will be something like:
   `https://sentry-robot.vercel.app`

---

## STEP 4 — Update your Pi config

```bash
nano ~/sentry/obj/config.py
```

Change these two lines:
```python
CLOUD_AGENT_ENABLED = True
CLOUD_GATEWAY_URL   = "https://sentry-robot-xxxx.onrender.com"  # your Render URL
```

Save with Ctrl+O → Enter → Ctrl+X

---

## STEP 5 — Run everything on the Pi

```bash
cd ~/sentry
source venv/bin/activate
./run.sh
```

---

## STEP 6 — Access from anywhere

Open your Vercel dashboard URL on any device:
`https://sentry-robot.vercel.app`

1. Paste your Render Gateway URL into the input box
2. Click **CONNECT**
3. Robot status turns **GREEN** when connected
4. You can now:
   - See live camera feed
   - See LiDAR radar
   - Drive the robot with arrow keys or D-pad
   - Click radar to navigate

---

## Troubleshooting

**Robot shows OFFLINE on dashboard**
→ Check `./run.sh` is running on the Pi
→ Check `CLOUD_GATEWAY_URL` in config.py is correct
→ Render free tier sleeps after 15 min — first connection takes ~30s to wake up

**Video feed not showing**
→ The dashboard falls back to JPEG stream automatically
→ WebRTC only works if both Pi and viewer are on non-firewalled connections

**Render URL not working**
→ Free tier can take 30-60 seconds to wake from sleep
→ Open the Render URL directly in browser first to wake it

---

## Local access (no internet needed)

You can always access locally too:
- Camera dashboard → http://<pi-ip>:5000
- LiDAR map → http://<pi-ip>:8000
