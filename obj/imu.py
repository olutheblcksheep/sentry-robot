"""
imu.py — MPU6050 driver for the Raspberry Pi (direct I2C).
============================================================
Reads accelerometer + gyroscope data from an MPU6050 connected
directly to the Pi's I2C bus (no ESP32 involvement).

Provides:
  • Continuous heading tracking via gyro integration (yaw)
  • Tilt detection (pitch/roll) for safety / fall detection
  • Calibration routine to zero out gyro drift at startup

Wiring (Pi → MPU6050):
    Pi Pin 1  (3.3V)  → MPU6050 VCC
    Pi Pin 6  (GND)   → MPU6050 GND
    Pi Pin 3  (SDA)   → MPU6050 SDA
    Pi Pin 5  (SCL)   → MPU6050 SCL

Enable I2C first:
    sudo raspi-config → Interface Options → I2C → Enable
    sudo reboot

Install dependency:
    pip install smbus2

Verify the sensor is detected:
    sudo i2cdetect -y 1
    (should show address 0x68)
"""

from __future__ import annotations

import time
import threading
import math

try:
    import smbus2 as smbus
    SMBUS_AVAILABLE = True
except ImportError:
    SMBUS_AVAILABLE = False
    print("[IMU] smbus2 not installed — run: pip install smbus2")

# ── MPU6050 Registers ────────────────────────────────────────────────
MPU6050_ADDR     = 0x68
PWR_MGMT_1       = 0x6B
SMPLRT_DIV       = 0x19
CONFIG           = 0x1A
GYRO_CONFIG      = 0x1B
ACCEL_CONFIG     = 0x1C
ACCEL_XOUT_H     = 0x3B
GYRO_XOUT_H      = 0x43

# Sensitivity scale factors (depends on full-scale range config)
ACCEL_SCALE = 16384.0   # LSB/g at ±2g range
GYRO_SCALE  = 131.0     # LSB/(deg/s) at ±250 deg/s range

# ── Shared state ──────────────────────────────────────────────────────
bus               = None
imu_lock          = threading.Lock()
imu_available     = False

heading_deg       = 0.0    # integrated yaw, 0-360, 0 = startup orientation
pitch_deg         = 0.0
roll_deg          = 0.0
accel_x = accel_y = accel_z = 0.0
gyro_x  = gyro_y  = gyro_z  = 0.0

_gyro_bias_x = _gyro_bias_y = _gyro_bias_z = 0.0
_last_update_time = 0.0
_running = False


# ═══════════════════════════════════════════════════════════════
#  LOW-LEVEL I2C
# ═══════════════════════════════════════════════════════════════

def _read_word(reg: int) -> int:
    """Read a signed 16-bit word from two consecutive registers."""
    high = bus.read_byte_data(MPU6050_ADDR, reg)
    low  = bus.read_byte_data(MPU6050_ADDR, reg + 1)
    val  = (high << 8) + low
    if val >= 0x8000:
        val = -((65535 - val) + 1)
    return val


def _init_sensor() -> bool:
    """Wake the MPU6050 and configure sample rate / ranges."""
    global bus, imu_available
    if not SMBUS_AVAILABLE:
        return False
    try:
        bus = smbus.SMBus(1)   # I2C bus 1 on Raspberry Pi
        # Wake up the device (it starts in sleep mode)
        bus.write_byte_data(MPU6050_ADDR, PWR_MGMT_1, 0x00)
        time.sleep(0.1)
        # Sample rate divider: 1kHz / (1 + 7) = 125Hz
        bus.write_byte_data(MPU6050_ADDR, SMPLRT_DIV, 0x07)
        # Digital low pass filter
        bus.write_byte_data(MPU6050_ADDR, CONFIG, 0x06)
        # Gyro full scale range: ±250 deg/s
        bus.write_byte_data(MPU6050_ADDR, GYRO_CONFIG, 0x00)
        # Accel full scale range: ±2g
        bus.write_byte_data(MPU6050_ADDR, ACCEL_CONFIG, 0x00)
        time.sleep(0.1)
        imu_available = True
        print("[IMU] MPU6050 initialised on I2C bus 1, address 0x68")
        return True
    except Exception as e:
        print(f"[IMU] Init failed: {e} — running without IMU")
        imu_available = False
        return False


def _read_raw():
    """Read raw accel + gyro values, return as scaled physical units."""
    ax = _read_word(ACCEL_XOUT_H)     / ACCEL_SCALE
    ay = _read_word(ACCEL_XOUT_H + 2) / ACCEL_SCALE
    az = _read_word(ACCEL_XOUT_H + 4) / ACCEL_SCALE
    gx = _read_word(GYRO_XOUT_H)      / GYRO_SCALE
    gy = _read_word(GYRO_XOUT_H + 2)  / GYRO_SCALE
    gz = _read_word(GYRO_XOUT_H + 4)  / GYRO_SCALE
    return ax, ay, az, gx, gy, gz


# ═══════════════════════════════════════════════════════════════
#  CALIBRATION
# ═══════════════════════════════════════════════════════════════

def calibrate(samples: int = 200) -> None:
    """
    Measure gyro bias while the robot is stationary.
    MUST be called at startup with the robot perfectly still,
    otherwise heading will drift even when not moving.
    """
    global _gyro_bias_x, _gyro_bias_y, _gyro_bias_z
    if not imu_available:
        return

    print(f"[IMU] Calibrating gyro bias ({samples} samples) — keep robot still...")
    sum_x = sum_y = sum_z = 0.0
    good_samples = 0

    for _ in range(samples):
        try:
            with imu_lock:
                _, _, _, gx, gy, gz = _read_raw()
            sum_x += gx
            sum_y += gy
            sum_z += gz
            good_samples += 1
        except Exception:
            pass
        time.sleep(0.005)

    if good_samples > 0:
        _gyro_bias_x = sum_x / good_samples
        _gyro_bias_y = sum_y / good_samples
        _gyro_bias_z = sum_z / good_samples
        print(f"[IMU] Calibration done. Gyro bias: "
              f"x={_gyro_bias_x:.3f} y={_gyro_bias_y:.3f} z={_gyro_bias_z:.3f} deg/s")
    else:
        print("[IMU] Calibration failed — no samples read")


# ═══════════════════════════════════════════════════════════════
#  CONTINUOUS UPDATE LOOP
# ═══════════════════════════════════════════════════════════════

def _update_loop():
    """
    Background thread: integrates gyro readings into heading,
    and computes pitch/roll from accelerometer for tilt detection.
    Runs at roughly 100Hz.
    """
    global heading_deg, pitch_deg, roll_deg
    global accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z
    global _last_update_time, _running

    _last_update_time = time.monotonic()

    while _running:
        try:
            with imu_lock:
                ax, ay, az, gx, gy, gz = _read_raw()

            now = time.monotonic()
            dt  = now - _last_update_time
            _last_update_time = now

            # Remove calibrated bias
            gx -= _gyro_bias_x
            gy -= _gyro_bias_y
            gz -= _gyro_bias_z

            # Integrate yaw rate (gz) into heading — this is the key value
            # used for navigation since the robot turns in the XY plane
            heading_deg = (heading_deg + gz * dt) % 360

            # Pitch/roll from accelerometer (for tilt / fall detection)
            pitch_deg = math.degrees(math.atan2(ay, math.sqrt(ax**2 + az**2)))
            roll_deg  = math.degrees(math.atan2(-ax, az))

            accel_x, accel_y, accel_z = ax, ay, az
            gyro_x,  gyro_y,  gyro_z  = gx, gy, gz

        except Exception as e:
            print(f"[IMU] Read error: {e}")

        time.sleep(0.01)   # ~100Hz


def start() -> bool:
    """Initialise the sensor, calibrate, and start the background thread."""
    global _running
    if not _init_sensor():
        return False
    calibrate()
    _running = True
    threading.Thread(target=_update_loop, daemon=True).start()
    return True


def stop() -> None:
    global _running
    _running = False


def reset_heading(new_heading: float = 0.0) -> None:
    """
    Manually reset the integrated heading to a known value.
    Useful when the robot returns to a known reference orientation
    (e.g. facing the same way as when the map was created).
    """
    global heading_deg
    heading_deg = new_heading % 360
    print(f"[IMU] Heading reset to {new_heading}°")


def get_status() -> dict:
    """Return current IMU readings for the web dashboard / debugging."""
    return {
        "available":  imu_available,
        "heading":    round(heading_deg, 1),
        "pitch":      round(pitch_deg, 1),
        "roll":       round(roll_deg, 1),
        "gyro":       {"x": round(gyro_x, 2),  "y": round(gyro_y, 2),  "z": round(gyro_z, 2)},
        "accel":      {"x": round(accel_x, 2), "y": round(accel_y, 2), "z": round(accel_z, 2)},
    }


def is_tilted(threshold_deg: float = 25.0) -> bool:
    """Safety check — returns True if the robot has tipped beyond threshold."""
    return abs(pitch_deg) > threshold_deg or abs(roll_deg) > threshold_deg
