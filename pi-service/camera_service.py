"""Camera confidence check service.

Grabs frames from a camera in a background thread, scores how "clear" the
image is (0-100), and serves the result over HTTP on port 5050.

    python camera_service.py

Runs unchanged on a Raspberry Pi camera and a laptop webcam.
Pick a different camera with:  CAMERA_INDEX=1 python camera_service.py
"""

import logging
import os
import sys
import threading
import time

import cv2
import numpy as np
from flask import Flask, jsonify, Response
from flask_cors import CORS

# =============================================================================
# TUNING KNOBS -- everything you are likely to touch under time pressure.
# =============================================================================

# --- Camera ------------------------------------------------------------------
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
OPEN_RETRIES = 3            # attempts to open the camera before giving up
OPEN_RETRY_DELAY_S = 1.0    # pause between attempts
RECONNECT_DELAY_S = 5.0     # pause before the capture thread tries again
MAX_READ_FAILURES = 30      # consecutive failed reads before reconnecting

# --- Scoring -----------------------------------------------------------------
# Frames are shrunk to this width before scoring so results don't depend on
# camera resolution (Pi camera and laptop webcam score comparably).
ANALYSIS_WIDTH = 320

# 1) Fog / blur: variance of the Laplacian. Sharp image = high variance.
#    Variance >= LAPLACIAN_CLEAR gives a fog_score of 100; 0 gives 0.
#    Raise this if a good image scores low; lower it if a blurry one scores high.
LAPLACIAN_CLEAR = 150.0

# 2) Local obstruction: split the frame into a GRID_COLS x GRID_ROWS grid.
#    A cell counts as "covered" when it is BRIGHT and FLAT (something opaque
#    right on the lens: a finger, tape, a bright smudge), as opposed to a
#    uniform blur which affects the whole frame.
GRID_COLS = 8
GRID_ROWS = 6
COVERED_MIN_BRIGHTNESS = 160   # mean gray level (0-255) at or above this...
COVERED_MAX_STDDEV = 6.0       # ...and gray-level std-dev at or below this
COVERED_FRAME_FRACTION = 0.20  # this fraction of cells covered => score 0

# --- Combining ---------------------------------------------------------------
# confidence = min(fog_score, obstruction_score), then lightly smoothed so the
# number doesn't flicker frame to frame. SMOOTHING=1.0 disables smoothing.
SMOOTHING = 0.3

CLEAR_MIN = 70       # confidence >= 70          -> "clear"
DEGRADED_MIN = 40    # 40 <= confidence < 70     -> "degraded"
                     # confidence < 40           -> "obstructed"

# --- Server ------------------------------------------------------------------
PORT = 5050          # not 5000: macOS AirPlay Receiver squats on it
JPEG_QUALITY = 80

# =============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("camera_service")


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------

def to_analysis_gray(frame):
    """Shrink to ANALYSIS_WIDTH and convert to grayscale."""
    h, w = frame.shape[:2]
    if w != ANALYSIS_WIDTH:
        frame = cv2.resize(frame, (ANALYSIS_WIDTH, int(h * ANALYSIS_WIDTH / w)),
                           interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def fog_score(gray):
    """Global blur/fog score, 0-100 (100 = sharp). Variance of the Laplacian."""
    variance = cv2.Laplacian(gray, cv2.CV_64F).var()
    return float(np.clip(variance / LAPLACIAN_CLEAR, 0.0, 1.0) * 100.0)


def obstruction_score(gray):
    """Local obstruction score, 0-100 (100 = nothing covering the lens).

    Counts grid cells that are bright with almost no internal variation.
    """
    h, w = gray.shape
    cell_h, cell_w = h // GRID_ROWS, w // GRID_COLS
    # Crop so the frame divides evenly, then reshape into (row, y, col, x).
    cells = gray[: cell_h * GRID_ROWS, : cell_w * GRID_COLS].astype(np.float32)
    cells = cells.reshape(GRID_ROWS, cell_h, GRID_COLS, cell_w)

    mean = cells.mean(axis=(1, 3))
    std = cells.std(axis=(1, 3))

    covered = (mean >= COVERED_MIN_BRIGHTNESS) & (std <= COVERED_MAX_STDDEV)
    covered_fraction = covered.mean()
    return float((1.0 - min(1.0, covered_fraction / COVERED_FRAME_FRACTION)) * 100.0)


def status_for(confidence):
    if confidence >= CLEAR_MIN:
        return "clear"
    if confidence >= DEGRADED_MIN:
        return "degraded"
    return "obstructed"


# ----------------------------------------------------------------------------
# Camera + background capture thread
# ----------------------------------------------------------------------------

def open_camera(index=CAMERA_INDEX):
    """Open the camera, retrying a few times. Returns a VideoCapture or None."""
    # DirectShow is the reliable backend on Windows; elsewhere use the default.
    backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY

    for attempt in range(1, OPEN_RETRIES + 1):
        cap = cv2.VideoCapture(index, backend)
        if cap.isOpened():
            log.info("Camera opened (device index %d)", index)
            return cap
        cap.release()
        log.warning("Could not open camera at device index %d (attempt %d/%d)",
                    index, attempt, OPEN_RETRIES)
        if attempt < OPEN_RETRIES:
            time.sleep(OPEN_RETRY_DELAY_S)

    log.error("No camera found at device index %d after %d attempts. "
              "Check the connection, or try another index: CAMERA_INDEX=1",
              index, OPEN_RETRIES)
    return None


class CameraMonitor:
    """Captures frames in a background thread and keeps the latest result."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None      # latest BGR frame
        self._result = None     # latest {confidence, status, ...} dict
        self._connected = False
        self._smoothed = None

    def start(self):
        # Open the first camera here, on the calling (main) thread: macOS only
        # shows the camera-permission prompt from the main thread. Reconnects
        # after that happen on the capture thread.
        cap = open_camera()
        threading.Thread(target=self._run, args=(cap,), name="capture", daemon=True).start()

    # -- read side (called from Flask threads) --------------------------------

    @property
    def connected(self):
        with self._lock:
            return self._connected

    def latest_result(self):
        with self._lock:
            return dict(self._result) if self._result else None

    def latest_frame(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    # -- capture side ----------------------------------------------------------

    def _run(self, cap):
        while True:
            if cap is None:
                self._set_connected(False)
                time.sleep(RECONNECT_DELAY_S)
                cap = open_camera()
                continue

            self._set_connected(True)
            failures = 0
            while failures < MAX_READ_FAILURES:
                ok, frame = cap.read()
                if not ok or frame is None:
                    failures += 1
                    time.sleep(0.05)
                    continue
                failures = 0
                self._process(frame)

            log.error("Lost camera at device index %d (%d failed reads); reconnecting",
                      CAMERA_INDEX, MAX_READ_FAILURES)
            cap.release()
            cap = None
            self._set_connected(False)

    def _set_connected(self, value):
        with self._lock:
            self._connected = value

    def _process(self, frame):
        gray = to_analysis_gray(frame)
        fog = fog_score(gray)
        obstruction = obstruction_score(gray)

        raw = min(fog, obstruction)
        self._smoothed = raw if self._smoothed is None else (
            SMOOTHING * raw + (1 - SMOOTHING) * self._smoothed)
        confidence = round(self._smoothed)

        result = {
            "confidence": confidence,
            "status": status_for(confidence),
            "fog_score": round(fog, 1),
            "obstruction_score": round(obstruction, 1),
            "timestamp": time.time(),  # unix seconds
        }
        with self._lock:
            self._frame = frame
            self._result = result


# ----------------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)
monitor = CameraMonitor()


@app.get("/status")
def status():
    result = monitor.latest_result()
    if result is None:
        return jsonify(error="no frame captured yet", camera_connected=monitor.connected), 503
    return jsonify(result)


@app.get("/frame.jpg")
def frame_jpg():
    frame = monitor.latest_frame()
    if frame is None:
        return jsonify(error="no frame captured yet"), 503
    ok, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return jsonify(error="jpeg encoding failed"), 500
    resp = Response(jpeg.tobytes(), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/health")
def health():
    return jsonify(ok=True, camera_connected=monitor.connected)


if __name__ == "__main__":
    monitor.start()
    # debug/reloader stay off: the reloader would start a second capture thread
    # and fight over the camera.
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
