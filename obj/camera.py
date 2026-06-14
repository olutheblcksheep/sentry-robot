"""
camera.py — Camera capture + MJPEG streaming.
=============================================
Owns the CameraStream class (picamera2 / OpenCV / mock backends) and the
MJPEG generator that feeds the /video_feed route.

Inference is delegated to detection.annotate_frame(), so this file never
touches the YOLO model directly — it just hands each frame over to be
annotated before JPEG-encoding it.
"""

import threading
import time

import detection
import config


class CameraStream:
    def __init__(self):
        self.frame = b""
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        if config.MOCK_MODE:
            self._mock_camera()
        elif config.CAMERA_BACKEND == "picamera2":
            self._picamera2()
        else:
            self._opencv()

    # ------------------------------------------------------------------
    #  picamera2 (CSI port — OV5647 / IMX219 / IMX477 …)
    # ------------------------------------------------------------------
    def _picamera2(self):
        try:
            import cv2
            from picamera2 import Picamera2

            cam = Picamera2()
            cam.configure(cam.create_video_configuration(
                main={"size": (config.CAMERA_WIDTH, config.CAMERA_HEIGHT),
                      "format": "RGB888"}
            ))
            cam.start()
            time.sleep(2)
            print("[Camera] CSI camera via picamera2 — running")

            while not self._stop.is_set():
                # capture_array() returns an RGB numpy array
                frame_rgb = cam.capture_array()
                frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)

                # Inline NCNN inference (draws boxes + emits, if enabled)
                detection.annotate_frame(frame_bgr)

                _, buf = cv2.imencode(".jpg", frame_bgr,
                                      [cv2.IMWRITE_JPEG_QUALITY, 75])
                with self._lock:
                    self.frame = buf.tobytes()

                time.sleep(1 / config.CAMERA_FPS)

            cam.close()

        except Exception as e:
            print(f"[Camera] picamera2 failed: {e} — trying OpenCV")
            self._opencv()

    # ------------------------------------------------------------------
    #  OpenCV fallback (USB camera or V4L2 CSI device)
    # ------------------------------------------------------------------
    def _opencv(self):
        try:
            import cv2
            cap = cv2.VideoCapture(config.CAMERA_INDEX)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.CAMERA_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.CAMERA_HEIGHT)
            cap.set(cv2.CAP_PROP_FPS, config.CAMERA_FPS)
            if not cap.isOpened():
                print("[Camera] OpenCV failed — using mock")
                self._mock_camera()
                return
            print(f"[Camera] OpenCV /dev/video{config.CAMERA_INDEX} — running")

            while not self._stop.is_set():
                ret, frame_bgr = cap.read()
                if not ret:
                    time.sleep(0.1)
                    continue

                # Inline NCNN inference (draws boxes + emits, if enabled)
                detection.annotate_frame(frame_bgr)

                _, buf = cv2.imencode(".jpg", frame_bgr,
                                      [cv2.IMWRITE_JPEG_QUALITY, 75])
                with self._lock:
                    self.frame = buf.tobytes()

                time.sleep(1 / config.CAMERA_FPS)

            cap.release()

        except Exception as e:
            print(f"[Camera] OpenCV failed: {e} — using mock")
            self._mock_camera()

    # ------------------------------------------------------------------
    #  Mock camera (animated placeholder — no real hardware needed)
    # ------------------------------------------------------------------
    def _mock_camera(self):
        import struct
        import zlib
        print("[Camera] Mock mode — animated placeholder")
        w, h, n = config.CAMERA_WIDTH, config.CAMERA_HEIGHT, 0
        while not self._stop.is_set():
            rows = []
            for y in range(h):
                row = bytearray([0])
                for x in range(w):
                    wave = (x + n * 2) % w / w
                    row += bytes([
                        int(10 + 8 * abs(wave - 0.5)),
                        int(80 + 40 * abs((y / h) - 0.5)),
                        int(30 + 20 * abs((x / w + y / h) / 2 - 0.5)),
                    ])
                rows.append(bytes(row))
            raw = b"".join(rows)

            def chunk(tag, data):
                return (struct.pack(">I", len(data)) + tag + data +
                        struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

            ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
            png = (b"\x89PNG\r\n\x1a\n"
                   + chunk(b"IHDR", ihdr)
                   + chunk(b"IDAT", zlib.compress(raw, 1))
                   + chunk(b"IEND", b""))
            with self._lock:
                self.frame = png
            n += 1
            time.sleep(1 / config.CAMERA_FPS)

    def get_frame(self):
        with self._lock:
            return self.frame


# Single shared instance. Created at import (cheap, no hardware touched);
# the capture thread only starts when main.py calls camera.start().
camera = CameraStream()


def mjpeg_generator():
    while True:
        frame = camera.get_frame()
        if frame:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        time.sleep(1 / config.CAMERA_FPS)
