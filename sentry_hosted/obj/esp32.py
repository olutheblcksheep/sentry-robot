"""
esp32.py — ESP32 serial link and motor commands.
================================================
Owns the serial connection to the ESP32 motor controller and the
helpers that send it JSON commands. This is the exact wire protocol the
LiDAR dashboard (server.py) was asked to reuse:

    {"cmd": "forward", "speed": 80}\n
    {"cmd": "stop"}\n

Each command is a one-line JSON object terminated by '\n'. The ESP32
optionally replies with a JSON acknowledgement on the same line.
"""

import json
import time

import serial

import config

# The live serial handle. None means "not connected" → commands are
# printed instead of sent (handy for bench testing without the ESP32).
serial_conn = None

MOTOR_CMDS = ["forward", "backward", "left", "right", "stop"]


def init_serial():
    """Open the ESP32 serial port. Safe to call once at startup."""
    global serial_conn
    try:
        serial_conn = serial.Serial(config.ESP32_PORT, config.ESP32_BAUD, timeout=1)
        # CRUCIAL: Native USB ESP32s drop connection briefly on open.
        # 2 seconds lets the bootloader pass safely before we talk.
        time.sleep(2)
        serial_conn.reset_input_buffer()  # clear out old boot remnants
        print(f"[ESP32] Connected on {config.ESP32_PORT} @ {config.ESP32_BAUD}")
    except Exception as e:
        print(f"[ESP32] Connection failed — {e}")
        serial_conn = None


def send_command(cmd: dict):
    """Serialise `cmd` as one JSON line and write it to the ESP32."""
    global serial_conn
    payload = json.dumps(cmd) + "\n"

    if serial_conn and serial_conn.is_open:
        try:
            serial_conn.write(payload.encode("utf-8"))
            serial_conn.flush()

            # OPTIONAL: read back the confirmation JSON from the ESP32
            if serial_conn.in_waiting:
                response = serial_conn.readline().decode("utf-8").strip()
                print(f"[ESP32 ACK] {response}")

        except Exception as e:
            print(f"[ESP32] Write error: {e}")
            # Handle a dropped cable by resetting connection state.
            try:
                serial_conn.close()
            except Exception:
                pass
            serial_conn = None
    else:
        print(f"[ESP32 MOCK] → {payload.strip()}")


def move(direction: str, speed: int = 80) -> bool:
    """Send a directional motor command. Returns False on bad direction."""
    if direction not in MOTOR_CMDS:
        print(f"[WARN] Invalid direction requested: {direction}")
        return False

    # Standardise 'stop' to ignore speed entirely.
    if direction == "stop":
        send_command({"cmd": "stop"})
    else:
        send_command({"cmd": direction, "speed": int(speed)})

    return True
