"""
detection.py — YOLOv11-NCNN object detection.
=============================================
Owns the model and everything that touches it:
  • load_model()        — load the NCNN model once at startup
  • annotate_frame()    — run inference on a live BGR frame, draw boxes
                          in-place, emit detections (used by camera.py)
  • run_inference()     — run inference on raw JPEG bytes, return dicts
                          (used by the /api/infer phone-upload endpoint)
  • emit_detections()   — push detections (+ threat alerts) over SocketIO

Keeping all model access here means camera.py never has to know how
the model works — it just calls annotate_frame(). Easier to debug.
"""

import io
from datetime import datetime

from state import socketio
import state
import config

# Class names start from config and get replaced by the model's own
# names once it loads. Drawing/labelling reads this module-level list.
CLASS_NAMES = list(config.CLASS_NAMES)

yolo_model = None


def load_model():
    """Load the YOLOv11-NCNN model. Call once at startup."""
    global yolo_model, CLASS_NAMES
    try:
        from ultralytics import YOLO
        # Ultralytics auto-detects ncnn format from the folder / .param path.
        yolo_model = YOLO(config.DETECTION_MODEL, task="detect")
        CLASS_NAMES = list(yolo_model.names.values())
        print(f"[Detection] YOLOv11-NCNN loaded — {len(CLASS_NAMES)} classes")
        print(f"[Detection] Classes: {CLASS_NAMES}")
    except Exception as e:
        print(f"[Detection] Model load failed: {e}")
        yolo_model = None


def draw_detections(frame_bgr, results):
    """
    Draw bounding boxes + labels on `frame_bgr` (in-place).
    Returns a list of detection dicts for SocketIO emission.
    """
    import cv2
    h, w = frame_bgr.shape[:2]
    detections_list = []

    for r in results:
        for box in r.boxes:
            cls = int(box.cls[0])
            conf = float(box.conf[0])

            if conf < config.CONF_THRESHOLD:
                continue

            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            label = CLASS_NAMES[cls] if cls < len(CLASS_NAMES) else f"class_{cls}"
            color = config.BBOX_COLORS[cls % len(config.BBOX_COLORS)]

            # Bounding box
            cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), color, 2)

            # Label background + text
            tag = f"{label}: {int(conf * 100)}%"
            lsz, base = cv2.getTextSize(tag, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            ly = max(y1, lsz[1] + 10)
            cv2.rectangle(frame_bgr,
                          (x1, ly - lsz[1] - 10),
                          (x1 + lsz[0], ly + base - 10),
                          color, cv2.FILLED)
            cv2.putText(frame_bgr, tag, (x1, ly - 7),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)

            detections_list.append({
                "label": label,
                "confidence": round(conf, 3),
                # Normalised bbox [x, y, w, h] for the web UI
                "bbox": [
                    round(x1 / w, 4), round(y1 / h, 4),
                    round((x2 - x1) / w, 4), round((y2 - y1) / h, 4),
                ],
            })

    return detections_list


def annotate_frame(frame_bgr):
    """
    If detection is enabled and a model is loaded, run inference on the
    live frame, draw boxes in-place, emit results, and return the dets.
    Called once per frame by the camera loops. No-op otherwise.
    """
    if not (state.detection_enabled and yolo_model is not None):
        return []
    results = yolo_model(frame_bgr, conf=config.CONF_THRESHOLD, verbose=False)
    dets = draw_detections(frame_bgr, results)
    if dets:
        print(f"[Detection] {[(d['label'], d['confidence']) for d in dets]}")
    emit_detections(dets)
    return dets


def emit_detections(detections, source="camera"):
    """Emit detection events over SocketIO; fire alert for threat labels."""
    for det in detections:
        is_threat = det["label"] in config.THREAT_LABELS
        socketio.emit("detection", {
            **det,
            "threat": is_threat,
            "timestamp": datetime.now().isoformat(),
            "source": source,
        })
        if is_threat:
            socketio.emit("alert", {
                "message": f"THREAT: {det['label'].upper()}",
                **det,
                "timestamp": datetime.now().isoformat(),
                "source": source,
            })
            print(f"[ALERT]  {det['label']} @ {det['confidence']:.0%}")


def run_inference(jpeg_bytes):
    """
    Run YOLOv11-NCNN on a JPEG frame supplied as raw bytes.
    Used by the /api/infer endpoint (phone camera upload).
    Returns a list of detection dicts (no drawing — phone draws its own).
    """
    if yolo_model is None:
        return []
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        results = yolo_model(img, conf=config.CONF_THRESHOLD, verbose=False)
        dets = []
        for r in results:
            for box in r.boxes:
                cls = int(box.cls[0])
                conf = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxyn[0].tolist()   # normalised coords
                label = (CLASS_NAMES[cls]
                         if cls < len(CLASS_NAMES)
                         else f"class_{cls}")
                dets.append({
                    "label": label,
                    "confidence": round(conf, 3),
                    "bbox": [round(x1, 4), round(y1, 4),
                             round(x2 - x1, 4), round(y2 - y1, 4)],
                })
        return dets
    except Exception as e:
        print(f"[Detection] Inference error: {e}")
        return []
