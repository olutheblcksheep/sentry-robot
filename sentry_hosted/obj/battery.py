"""
battery.py — Battery level monitor.
===================================
Periodically broadcasts the battery level over SocketIO. In MOCK_MODE it
slowly drains a fake value so the UI has something to show; on real
hardware, replace the body of battery_loop() with a real ADC/fuel-gauge
read into state.battery_level.
"""

import random
import threading
import time
from datetime import datetime

from state import socketio
import state
import config


def battery_loop():
    while True:
        if config.MOCK_MODE:
            state.battery_level = max(0, state.battery_level - random.uniform(0, 0.03))
        socketio.emit("battery_update", {
            "level": round(state.battery_level, 1),
            "timestamp": datetime.now().isoformat(),
        })
        time.sleep(10)


def start():
    """Spawn the battery monitor thread."""
    threading.Thread(target=battery_loop, daemon=True).start()
