#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  SENTRY — Unified launcher
#  Starts all services from ~/sentry in one command.
#
#  Usage:
#    chmod +x run.sh
#    ./run.sh
#
#  Services started:
#    • Flask camera + detection dashboard  (port 5000)
#    • FastAPI LiDAR map dashboard         (port 8000)
#    • Cloud agent (if enabled in config)
# ═══════════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")"

# ── Activate venv ─────────────────────────────────────────────────────
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
    echo "[SENTRY] venv activated"
else
    echo "[SENTRY] No venv found — using system Python"
fi

# ── Get Pi IP for banner ──────────────────────────────────────────────
PI_IP=$(hostname -I | awk '{print $1}')

echo ""
echo "════════════════════════════════════════════════════"
echo "  SENTRY — Starting all services"
echo "════════════════════════════════════════════════════"
echo "  Camera dashboard  → http://${PI_IP}:5000"
echo "  LiDAR map         → http://${PI_IP}:8000"
echo "════════════════════════════════════════════════════"
echo ""

# ── Start LiDAR map server in background ──────────────────────────────
echo "[SENTRY] Starting LiDAR map server (port 8000)..."
python3 server.py &
SERVER_PID=$!
echo "[SENTRY] LiDAR server PID: $SERVER_PID"

sleep 2

# ── Start camera + detection dashboard ────────────────────────────────
echo "[SENTRY] Starting camera dashboard (port 5000)..."
python3 obj/main.py &
MAIN_PID=$!
echo "[SENTRY] Camera dashboard PID: $MAIN_PID"

# ── Trap Ctrl+C to kill both ──────────────────────────────────────────
trap "echo ''; echo '[SENTRY] Shutting down...'; kill $SERVER_PID $MAIN_PID 2>/dev/null; exit 0" SIGINT SIGTERM

# ── Keep script alive ─────────────────────────────────────────────────
echo ""
echo "[SENTRY] All services running. Press Ctrl+C to stop."
echo ""
wait
