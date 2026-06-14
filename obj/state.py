"""
state.py — Shared application objects and runtime flags.
========================================================
This is the single place that owns the Flask `app` and the SocketIO
`socketio` object, plus the mutable runtime flags that more than one
module needs to read or write (detection on/off, auto mode, battery).

Why this exists
---------------
In the original single-file program these were module-level globals
mutated with the `global` keyword. Spread across several files that
becomes a tangle. Instead, every module does:

    from state import socketio
    import state
    ...
    state.detection_enabled = True      # write
    if state.auto_mode: ...             # read

Reading/writing attributes on this module is shared across the whole
program, so there is exactly one source of truth and no circular
imports.
"""

import threading

from flask import Flask
from flask_socketio import SocketIO

import config

# ── The Flask app + SocketIO server (created once, imported everywhere)
app = Flask(__name__, static_folder="static")
app.config["SECRET_KEY"] = "sentry-robot-2024"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Mutable runtime flags (shared across modules) ─────────────────────
detection_enabled: bool = False     # YOLO inference drawing on/off
auto_mode: bool = False             # LiDAR auto obstacle-stop on/off
battery_level: float = 87.5         # percent

# A lock you can reuse if you ever need to guard the flags above.
flag_lock = threading.Lock()
