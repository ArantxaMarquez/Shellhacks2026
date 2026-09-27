"""Camera confidence check service.

Grabs frames from a camera in a background thread, scores how "clear" the
image is (0-100), and serves the result over HTTP on port 5050.

    python camera_service.py

Two ways to get frames (only this part differs; the scoring is the same):
  * picamera2   - Raspberry Pi camera (used automatically if it can be imported).
                  Two streams: a small "lores" one is scored, a bigger "main"
                  one is what /frame.jpg serves. Focus is locked at startup
                  (POST /refocus to redo it).
  * opencv      - laptop / USB webcams via cv2.VideoCapture (the fallback)

Optional phone alerts: when the status gets worse (clear -> degraded, clear ->
obstructed, degraded -> obstructed) a Retell voice agent calls the team numbers.
Set the RETELL_* values in pi-service/.env (see .env.example). RETELL_ENABLED=false
switches to the plain Twilio fallback. Without any config the service runs normally,
just without alerts.

Pick a different camera:  CAMERA_INDEX=1 python camera_service.py
Force a backend:          CAMERA_BACKEND=opencv python camera_service.py
                          (auto | picamera2 | opencv, default auto)
"""

import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape

import cv2
import numpy as np
from flask import Flask, jsonify, Response
from flask_cors import CORS

# =============================================================================
# TUNING KNOBS -- everything you are likely to touch under time pressure.
# =============================================================================

# --- Camera ------------------------------------------------------------------
CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))
# "auto": use picamera2 if it can be imported, otherwise OpenCV.
# "picamera2" / "opencv": force one, for debugging.
CAMERA_BACKEND = os.environ.get("CAMERA_BACKEND", "auto").strip().lower()
PICAMERA_LORES_SIZE = (640, 480)   # Pi camera stream that gets scored
PICAMERA_MAIN_SIZE = (1280, 720)   # Pi camera stream served by /frame.jpg
AF_TIMEOUT_S = 5.0                 # give up waiting for autofocus after this long
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

# --- Alerts ------------------------------------------------------------------
# One shared cooldown for the whole alert (not per number): after an alert goes
# out, nothing else fires for this long, even if the status keeps changing.
ALERT_COOLDOWN_S = 30
DIAL_ONLY_FIRST_NUMBER = False   # True: call just the first RETELL_TEAM_NUMBERS entry (testing)

# --- Server ------------------------------------------------------------------
PORT = 5050          # not 5000: macOS AirPlay Receiver squats on it
JPEG_QUALITY = 80

# =============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("camera_service")

if CAMERA_BACKEND not in ("auto", "picamera2", "opencv"):
    sys.exit("CAMERA_BACKEND must be auto, picamera2 or opencv (got %r)" % CAMERA_BACKEND)


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------

def to_analysis_gray(frame):
    """Shrink to ANALYSIS_WIDTH and convert to grayscale (frame may already be gray)."""
    h, w = frame.shape[:2]
    if w != ANALYSIS_WIDTH:
        frame = cv2.resize(frame, (ANALYSIS_WIDTH, int(h * ANALYSIS_WIDTH / w)),
                           interpolation=cv2.INTER_AREA)
    return frame if frame.ndim == 2 else cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


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

# Camera sources share one interface:
#   read()    -> (ok, analysis_frame, display_frame)   analysis = what gets scored,
#                                                      display = what /frame.jpg serves
#   release() -> free the camera
#   sensor_model, refocus() -- Pi camera only

class OpenCvSource:
    """Frames from a webcam via cv2.VideoCapture (BGR; one stream for both jobs)."""

    sensor_model = None

    def __init__(self, cap):
        self._cap = cap

    def read(self):
        ok, frame = self._cap.read()
        return ok, frame, frame

    def release(self):
        self._cap.release()


class PiCameraSource:
    """Frames from a Raspberry Pi camera via picamera2, two streams."""

    # libcamera AfState values (what shows up in the metadata)
    AF_FOCUSED = 2
    AF_FAILED = 3

    def __init__(self, picam2, sensor_model):
        self._picam2 = picam2
        self.sensor_model = sensor_model
        self._af_lock = threading.Lock()

    def read(self):
        request = None
        try:
            # One request gives both streams from the same instant.
            request = self._picam2.capture_request()
            # "main" is RGB -> BGR for OpenCV/JPEG. "lores" is YUV420 and its first
            # plane is the brightness image, i.e. the grayscale we score.
            display = cv2.cvtColor(request.make_array("main"), cv2.COLOR_RGB2BGR)
            analysis = cv2.cvtColor(request.make_array("lores"), cv2.COLOR_YUV2GRAY_I420)
            return True, analysis, display
        except Exception as err:
            log.warning("picamera2 read failed: %s", err)
            return False, None, None
        finally:
            if request is not None:
                request.release()

    def release(self):
        try:
            self._picam2.stop()
            self._picam2.close()
        except Exception as err:
            log.warning("picamera2 release failed: %s", err)

    def refocus(self):
        """Run one autofocus cycle, then lock focus there.

        Returns {ok, lens_position, af_state} or {ok: False, error}. Only the
        Camera Module 3 (imx708) has autofocus; fixed-focus cameras skip this.
        """
        from libcamera import controls

        if "AfMode" not in self._picam2.camera_controls:
            return {"ok": False, "error": "this camera has no autofocus"}
        if not self._af_lock.acquire(blocking=False):
            return {"ok": False, "error": "a refocus is already running"}
        try:
            self._picam2.set_controls({"AfMode": controls.AfModeEnum.Auto,
                                       "AfTrigger": controls.AfTriggerEnum.Start})
            state, position = None, None
            deadline = time.time() + AF_TIMEOUT_S
            while time.time() < deadline:
                metadata = self._picam2.capture_metadata()
                state = metadata.get("AfState")
                position = metadata.get("LensPosition")
                if state in (self.AF_FOCUSED, self.AF_FAILED):
                    break

            # Lock the lens where it ended up so it stops hunting.
            controls_to_set = {"AfMode": controls.AfModeEnum.Manual}
            if position is not None:
                controls_to_set["LensPosition"] = position
            self._picam2.set_controls(controls_to_set)

            if state == self.AF_FOCUSED:
                log.info("Autofocus locked at lens position %.2f", position)
                return {"ok": True, "lens_position": position, "af_state": "focused"}
            reason = "failed" if state == self.AF_FAILED else "timed out"
            log.warning("Autofocus %s; lens locked at position %s anyway. "
                        "Aim at something with detail and POST /refocus.", reason, position)
            return {"ok": False, "lens_position": position, "af_state": reason,
                    "error": "autofocus " + reason}
        finally:
            self._af_lock.release()


def _open_opencv_once(index):
    # DirectShow is the reliable backend on Windows; elsewhere use the default.
    backend = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    if cap.isOpened():
        return OpenCvSource(cap)
    cap.release()
    return None


def _pi_sensor_model(Picamera2, index):
    """Sensor name (e.g. 'imx708') for the camera at this index, or None."""
    try:
        infos = Picamera2.global_camera_info()
        for info in infos:
            if info.get("Num") == index:
                return info.get("Model")
        return infos[index].get("Model")
    except Exception as err:
        log.warning("Could not read sensor model: %s", err)
        return None


def _open_picamera2_once(index):
    from picamera2 import Picamera2

    picam2 = Picamera2(camera_num=index)
    try:
        # picamera2 names formats by byte order in memory, so "BGR888" gives
        # arrays in R,G,B order (true RGB), which read() converts to BGR.
        # If the JPEG has red and blue swapped, this is the line to check.
        picam2.configure(picam2.create_video_configuration(
            main={"size": PICAMERA_MAIN_SIZE, "format": "BGR888"},
            lores={"size": PICAMERA_LORES_SIZE, "format": "YUV420"}))
        picam2.start()
        source = PiCameraSource(picam2, _pi_sensor_model(Picamera2, index))
        log.info("Sensor: %s", source.sensor_model)
        try:
            result = source.refocus()   # autofocus once, then lock
            if not result["ok"]:
                log.warning("Focus not locked: %s", result["error"])
        except Exception as err:        # a focus problem shouldn't stop the camera
            log.warning("Autofocus step failed: %s", err)
    except Exception:
        picam2.close()
        raise
    return source


def _picamera2_importable():
    try:
        import picamera2  # noqa: F401
        return True
    except ImportError:
        return False


def _open_with_retries(name, open_once, index):
    """Call open_once(index) up to OPEN_RETRIES times. Returns a source or None."""
    for attempt in range(1, OPEN_RETRIES + 1):
        detail = ""
        try:
            source = open_once(index)
        except Exception as err:
            source, detail = None, ": %s" % err
        if source is not None:
            log.info("Camera opened via %s (device index %d)", name, index)
            return source
        log.warning("Could not open camera at device index %d (attempt %d/%d) [%s]%s",
                    index, attempt, OPEN_RETRIES, name, detail)
        if attempt < OPEN_RETRIES:
            time.sleep(OPEN_RETRY_DELAY_S)

    log.error("No camera found at device index %d via %s after %d attempts. "
              "Check the connection, or try another index: CAMERA_INDEX=1",
              index, name, OPEN_RETRIES)
    return None


def open_camera(index=CAMERA_INDEX):
    """Open a camera per CAMERA_BACKEND. Returns a source (has read/release) or None."""
    backends = []
    if CAMERA_BACKEND in ("auto", "picamera2"):
        if _picamera2_importable():
            backends.append(("picamera2", _open_picamera2_once))
        elif CAMERA_BACKEND == "picamera2":
            log.error("CAMERA_BACKEND=picamera2 but picamera2 can't be imported. "
                      "On a Raspberry Pi run: bash pi-service/setup-pi.sh")
            return None
    if CAMERA_BACKEND in ("auto", "opencv"):
        backends.append(("opencv", _open_opencv_once))

    for name, open_once in backends:
        source = _open_with_retries(name, open_once, index)
        if source is not None:
            return source
    return None


# ----------------------------------------------------------------------------
# Phone alerts. Primary channel: a Retell voice agent calls every team number.
# Fallback (RETELL_ENABLED=false): plain Twilio. Credentials live in pi-service/.env.
# Everything here is best-effort: it must never stop the camera or /status.
# ----------------------------------------------------------------------------

# Only these status changes fire an alert. Recoveries and "no change" never do.
ALERT_TRANSITIONS = {
    ("clear", "degraded"),
    ("clear", "obstructed"),
    ("degraded", "obstructed"),   # escalation: guidance goes from slow-with-hazards to full stop
}

RETELL_ENV_VARS = ("RETELL_API_KEY", "RETELL_AGENT_ID", "RETELL_FROM_NUMBER")
TWILIO_ENV_VARS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER")
E164 = re.compile(r"^\+[1-9]\d{6,14}$")


class CallAlerter:
    def __init__(self):
        self._prev_status = None      # status on the previous frame
        self._last_alert_at = None    # time.monotonic() of the last alert (shared cooldown)
        self._client = None
        self._from = None
        self._agent_id = None
        self.numbers = []             # who gets called
        self.channel = None           # "retell" or "twilio-fallback"
        self.enabled = self._setup()

    # -- setup -------------------------------------------------------------------

    def _setup(self):
        """Read .env and build the client. Returns False (and warns once) if we can't."""
        try:
            from dotenv import load_dotenv
            load_dotenv(Path(__file__).with_name(".env"))
        except ImportError:
            log.warning("python-dotenv is not installed, so .env can't be read "
                        "(pip install -r requirements.txt). Only real environment variables count.")

        self.numbers = self._parse_numbers(os.environ.get("RETELL_TEAM_NUMBERS", ""))
        if not self.numbers:
            log.warning("Alerts are OFF: RETELL_TEAM_NUMBERS in pi-service/.env has no valid "
                        "E.164 numbers (like +15551234567, comma-separated). Camera scoring works as normal.")
            return False
        if DIAL_ONLY_FIRST_NUMBER:
            self.numbers = self.numbers[:1]

        retell_on = os.environ.get("RETELL_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")
        ok = self._setup_retell() if retell_on else self._setup_twilio_fallback()
        if ok:
            log.info("Alerts are ON via %s. Will dial: %s (cooldown %ss)",
                     self.channel, ", ".join(self.numbers), ALERT_COOLDOWN_S)
        return ok

    @staticmethod
    def _parse_numbers(raw):
        numbers, bad = [], []
        for item in raw.split(","):
            item = item.strip()
            if not item:
                continue
            if not E164.match(item):
                bad.append(item)
            elif item not in numbers:
                numbers.append(item)
        if bad:
            log.warning("Ignoring RETELL_TEAM_NUMBERS entries that aren't E.164: %s", ", ".join(bad))
        return numbers

    @staticmethod
    def _missing(names):
        return [n for n in names if not os.environ.get(n, "").strip()]

    def _setup_retell(self):
        missing = self._missing(RETELL_ENV_VARS)
        if missing:
            log.warning("Alerts are OFF: %s not set in pi-service/.env. "
                        "(RETELL_ENABLED=false switches to the Twilio fallback.)", ", ".join(missing))
            return False
        try:
            from retell import Retell
            self._client = Retell(api_key=os.environ["RETELL_API_KEY"].strip())
        except Exception as err:  # package missing, or a bad key format
            log.warning("Alerts are OFF: could not set up Retell (%s: %s). "
                        "(RETELL_ENABLED=false switches to the Twilio fallback.)", type(err).__name__, err)
            return False
        self._agent_id = os.environ["RETELL_AGENT_ID"].strip()
        self._from = os.environ["RETELL_FROM_NUMBER"].strip()
        self.channel = "retell"
        return True

    def _setup_twilio_fallback(self):
        missing = self._missing(TWILIO_ENV_VARS)
        if missing:
            log.warning("Alerts are OFF: RETELL_ENABLED=false, but %s not set in pi-service/.env.",
                        ", ".join(missing))
            return False
        try:
            from twilio.rest import Client
            self._client = Client(os.environ["TWILIO_ACCOUNT_SID"].strip(),
                                  os.environ["TWILIO_AUTH_TOKEN"].strip())
        except Exception as err:
            log.warning("Alerts are OFF: could not set up Twilio (%s: %s)", type(err).__name__, err)
            return False
        self._from = os.environ["TWILIO_FROM_NUMBER"].strip()
        self.channel = "twilio-fallback"
        return True

    # -- trigger -----------------------------------------------------------------

    def update(self, status, confidence):
        """Call once per scored frame. Fires only on the transitions in ALERT_TRANSITIONS."""
        previous, self._prev_status = self._prev_status, status
        if not self.enabled or (previous, status) not in ALERT_TRANSITIONS:
            return

        now = time.monotonic()
        if self._last_alert_at is not None and now - self._last_alert_at < ALERT_COOLDOWN_S:
            log.info("%s -> %s, but an alert went out %.0fs ago; skipping (shared cooldown %ss)",
                     previous, status, now - self._last_alert_at, ALERT_COOLDOWN_S)
            return

        self._last_alert_at = now  # counts even if every call fails, so we don't hammer the API
        # Dial from a separate thread: slow or dead networks must not stall
        # frame capture or the /status endpoint.
        threading.Thread(target=self._dispatch, args=(previous, status, confidence),
                         name="alert", daemon=True).start()

    def _dispatch(self, previous, status, confidence):
        log.warning("ALERT %s -> %s (confidence %d) via %s. Dialing %d number(s): %s",
                    previous, status, confidence, self.channel, len(self.numbers), ", ".join(self.numbers))
        try:
            if self.channel == "retell":
                self._send_retell_calls(confidence, status)
            else:
                self.send_fallback_call(confidence, status)
        except Exception:
            log.exception("Alert dispatch failed")

    # -- channels ------------------------------------------------------------------

    def _send_retell_calls(self, confidence, status):
        """One Retell call per team number. Each has its own try/except so one bad
        number doesn't stop the rest."""
        placed, failed = [], []
        for number in self.numbers:
            try:
                call = self._client.call.create_phone_call(
                    from_number=self._from,
                    to_number=number,
                    override_agent_id=self._agent_id,
                    retell_llm_dynamic_variables={"confidence": str(confidence), "status": status},
                )
                log.info("Retell call placed to %s (call_id %s)", number, getattr(call, "call_id", "?"))
                placed.append(number)
            except Exception as err:
                log.error("Retell call to %s FAILED: %s: %s", number, type(err).__name__, err)
                failed.append(number)
        self._log_summary("Retell", placed, failed)

    def send_fallback_call(self, confidence, status):
        """Plain-Twilio fallback, used when RETELL_ENABLED=false. Not called automatically.

        Places a normal Twilio voice call to each team number that reads the alert aloud
        (twice). Same per-number try/except as the Retell path.
        """
        speech = ("Camera alert. The camera is now %s. Confidence is %d out of 100. "
                  "Please check the camera." % (status, confidence))
        twiml = '<Response><Say loop="2">%s</Say></Response>' % escape(speech)
        placed, failed = [], []
        for number in self.numbers:
            try:
                call = self._client.calls.create(twiml=twiml, from_=self._from, to=number)
                log.info("Twilio fallback call placed to %s (sid %s)", number, call.sid)
                placed.append(number)
            except Exception as err:
                log.error("Twilio fallback call to %s FAILED: %s: %s", number, type(err).__name__, err)
                failed.append(number)
        self._log_summary("Twilio fallback", placed, failed)

    @staticmethod
    def _log_summary(channel, placed, failed):
        line = "%s alert done: %d/%d reached" % (channel, len(placed), len(placed) + len(failed))
        if failed:
            log.error("%s. Failed: %s", line, ", ".join(failed))
        else:
            log.warning("%s (%s)", line, ", ".join(placed))


class CameraMonitor:
    """Captures frames in a background thread and keeps the latest result."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None      # latest BGR frame
        self._result = None     # latest {confidence, status, ...} dict
        self._connected = False
        self._source = None     # the open camera source, if any
        self._smoothed = None

    def start(self):
        # Open the first camera here, on the calling (main) thread: macOS only
        # shows the camera-permission prompt from the main thread. Reconnects
        # after that happen on the capture thread.
        log.info("Camera backend setting: %s", CAMERA_BACKEND)
        source = open_camera()
        threading.Thread(target=self._run, args=(source,), name="capture", daemon=True).start()

    # -- read side (called from Flask threads) --------------------------------

    @property
    def connected(self):
        with self._lock:
            return self._connected

    @property
    def sensor_model(self):
        with self._lock:
            return self._source.sensor_model if self._source else None

    def refocus(self):
        """Redo autofocus + lock. None if there's no autofocus-capable Pi camera."""
        with self._lock:
            source = self._source
        if source is None or not hasattr(source, "refocus"):
            return None
        return source.refocus()

    def latest_result(self):
        with self._lock:
            return dict(self._result) if self._result else None

    def latest_frame(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    # -- capture side ----------------------------------------------------------

    def _run(self, source):
        while True:
            if source is None:
                self._set_connected(False)
                time.sleep(RECONNECT_DELAY_S)
                source = open_camera()
                continue

            self._set_connected(True, source)
            failures = 0
            while failures < MAX_READ_FAILURES:
                ok, analysis, display = source.read()
                if not ok or analysis is None:
                    failures += 1
                    time.sleep(0.05)
                    continue
                failures = 0
                self._process(analysis, display)

            log.error("Lost camera at device index %d (%d failed reads); reconnecting",
                      CAMERA_INDEX, MAX_READ_FAILURES)
            self._set_connected(False)
            source.release()
            source = None

    def _set_connected(self, value, source=None):
        with self._lock:
            self._connected = value
            self._source = source if value else None

    def _process(self, analysis_frame, display_frame):
        gray = to_analysis_gray(analysis_frame)
        fog = fog_score(gray)
        obstruction = obstruction_score(gray)

        raw = min(fog, obstruction)
        self._smoothed = raw if self._smoothed is None else (
            SMOOTHING * raw + (1 - SMOOTHING) * self._smoothed)
        confidence = round(self._smoothed)
        status = status_for(confidence)
        try:
            alerter.update(status, confidence)
        except Exception:
            log.exception("Alert check failed; ignoring")

        result = {
            "confidence": confidence,
            "status": status,
            "fog_score": round(fog, 1),
            "obstruction_score": round(obstruction, 1),
            "timestamp": time.time(),  # unix seconds
        }
        with self._lock:
            self._frame = display_frame
            self._result = result


# ----------------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------------

app = Flask(__name__)
CORS(app)
alerter = CallAlerter()
monitor = CameraMonitor()


@app.route("/status")
def status():
    result = monitor.latest_result()
    if result is None:
        return jsonify(error="no frame captured yet", camera_connected=monitor.connected), 503
    return jsonify(result)


@app.route("/frame.jpg")
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


@app.route("/health")
def health():
    # sensor_model is e.g. "imx708" on a Pi camera, null on the OpenCV/webcam path.
    return jsonify(ok=True, camera_connected=monitor.connected,
                   sensor_model=monitor.sensor_model)


@app.route("/refocus", methods=["POST"])
def refocus():
    result = monitor.refocus()
    if result is None:
        return jsonify(ok=False, error="no Pi camera connected (refocus needs the picamera2 backend)"), 409
    if result["ok"]:
        return jsonify(result)
    # 500 = autofocus ran but failed; 409 = it couldn't run at all (no AF, or busy)
    return jsonify(result), (500 if "af_state" in result else 409)


if __name__ == "__main__":
    monitor.start()
    # debug/reloader stay off: the reloader would start a second capture thread
    # and fight over the camera.
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
