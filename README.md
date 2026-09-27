# Freeze: Autonomous Camera for Winter Safety

A safety monitor for a vehicle-mounted (or robot-mounted) camera. A Raspberry Pi camera — or a laptop webcam while developing — feeds a small Python service that scores, frame by frame, how much the camera's view can be trusted: is the lens clear, is it fogged or blurry, or is something physically blocking it? That score drives a live dashboard, and if the view genuinely degrades and stays that way, the service places an outbound phone call to the team so nobody has to be watching a screen to find out.

It exists because a camera can silently go bad — condensation, a smudge, snow buildup, a knock that shifts the lens — and whatever depends on that camera (a driver, an operator, an automated system) has no way to know unless something is actively checking. This project is that check.

## How it works

1. **Capture.** [`pi-service/camera_service.py`](pi-service/camera_service.py) opens the camera in a background thread and reads frames continuously — a Raspberry Pi Camera Module via `picamera2`, or any laptop/USB webcam via OpenCV, with the same code path either way.
2. **Score.** Every frame gets two independent 0–100 scores: a **fog/blur score** (variance of the Laplacian — a sharp image has high variance, a blurry or foggy one doesn't) and an **obstruction score** (the frame is split into a grid, and cells that are bright *and* unusually flat suggest something opaque is sitting on the lens, as opposed to blur affecting the whole image evenly). The lower of the two becomes the overall **confidence**, lightly smoothed so it doesn't flicker, and mapped to a status: `clear`, `degraded`, or `obstructed`.
3. **Serve.** A Flask API exposes the current confidence, status, and the live frame as a JPEG, over the local network — see [Endpoints](#endpoints).
4. **Display.** [`web/`](web) is a dependency-free HTML/CSS/JS dashboard that polls that API once a second: a live camera thumbnail, a numeric confidence readout, a color-coded bar and status badge, and an animated road scene that visibly slows down and fogs over as confidence drops — a way to feel what "degraded camera confidence" means, not just read a number.
5. **Alert.** If the status genuinely gets worse (`clear → degraded`, `clear → obstructed`, or `degraded → obstructed`) and holds for several seconds — not just a one-frame blip — the service places outbound calls through a [Retell AI](https://www.retellai.com) voice agent to a configured team, reading out the live confidence score, with a Twilio voice-call fallback if Retell is unavailable. See [Phone alerts](#phone-alerts-optional).

The `tensorflow/` folder at the repo root holds an in-progress 3-class weather classifier (blizzard / clear / snowy), trained with transfer learning on top of MobileNetV2 and exported to TensorFlow Lite — a further signal, alongside the classical scoring, for what the camera is actually seeing. It isn't wired into the live service on this branch yet.

## Tech stack

**Backend / camera service** — Python, Flask + `flask-cors`, OpenCV (`opencv-python-headless`), NumPy, `picamera2` (Raspberry Pi camera access), `python-dotenv` (config from `.env`).

**Phone alerts** — [Retell AI](https://www.retellai.com) (`retell-sdk`) for the primary voice-agent call-out, [Twilio](https://www.twilio.com) as a fallback voice channel.

**On-device ML (in progress)** — TensorFlow / Keras for training (`tensorflow/`, MobileNetV2 transfer learning), exported to TensorFlow Lite for on-device inference.

**Frontend** — plain HTML, CSS and JavaScript. No framework, no build step, no `npm install`: open the file or serve the folder. A handful of visual effects (a cursor-tracking spotlight, shimmering and gradient text, an animated glow border) are ported from [React Bits](https://reactbits.dev) into vanilla CSS/JS. Type is self-hosted Barlow / Barlow Condensed.

**Hardware target** — Raspberry Pi + Camera Module (autofocus support for Camera Module 3), with a laptop webcam as the development stand-in.

## Project layout

- [`pi-service/`](pi-service) — the Flask service: camera capture, scoring, the HTTP API, and phone alerting.
- [`web/`](web) — the dashboard frontend.
- [`tensorflow/`](tensorflow) — training script, dataset-building tooling, and the current weather-classifier model/labels.

## Contributors

- Arantxa Marquez
- Naila Desgrottes
- Lenny Wandeto

## Quick start

Needs Python 3 and git. Run these in order. Step 3 keeps running, so do steps 4-6 in a **second terminal opened in the `Shellhacks2026` folder**.

<table>
<tr><th>Mac / Linux / Raspberry Pi</th><th>Windows (Command Prompt)</th></tr>
<tr><td>

**1. Clone**
```bash
git clone https://github.com/ArantxaMarquez/Shellhacks2026.git
cd Shellhacks2026
```

**2. Set up (once)**
```bash
bash pi-service/setup-mac.sh
```

**3. Start the camera service**
```bash
cd pi-service
source venv/bin/activate
python camera_service.py
```

**4. Find this machine's IP**
```bash
ipconfig getifaddr en0   # Mac Wi-Fi
hostname -I              # Linux / Pi
```

</td><td>

**1. Clone**
```bat
git clone https://github.com/ArantxaMarquez/Shellhacks2026.git
cd Shellhacks2026
```

**2. Set up (once)**
```bat
pi-service\setup-windows.bat
```

**3. Start the camera service**
```bat
cd pi-service
venv\Scripts\activate
python camera_service.py
```

**4. Find this machine's IP**
```bat
ipconfig
```
Look for `IPv4 Address`.

</td></tr>
</table>

**5. Point the dashboard at the service** (both systems). Open [`web/app.js`](web/app.js) and edit line 1:

```js
const SERVICE_URL = 'http://192.168.1.50:5050';   // use the IP from step 4
```

Skip this step if the dashboard and the camera service are on the **same machine**. The default `http://localhost:5050` already works. You only need the IP when the dashboard opens on a different machine (e.g. the service runs on a Pi).

Test the service alone: open `http://<that IP>:5050/health`. It should show `"camera_connected": true`.

The service listens on port **5050** (not 5000, which macOS AirPlay Receiver uses). On macOS, the first run asks for camera permission for your terminal app.

<table>
<tr><th>Mac / Linux / Raspberry Pi</th><th>Windows (Command Prompt)</th></tr>
<tr><td>

**6. Open the dashboard**
```bash
cd web
python3 -m http.server 8080
```
Then visit http://localhost:8080
(or just open `web/index.html`).

</td><td>

**6. Open the dashboard**
```bat
cd web
python -m http.server 8080
```
Then visit http://localhost:8080
(or just open `web\index.html`).

</td></tr>
</table>

## Raspberry Pi

On a Pi with a camera module (Pi OS Bookworm or Bullseye), use its own setup script instead of `setup-mac.sh`:

```bash
bash pi-service/setup-pi.sh
```

It installs `python3-picamera2`, `python3-opencv`, `python3-flask` and `python3-pip` with apt, installs `flask-cors` with pip, starts the service and runs `curl localhost:5050/health`. Ctrl+C stops the service. To run it again later: `cd pi-service && python3 camera_service.py`.

It does not create a venv, because picamera2 needs the system libcamera bindings, which pip can't install. If you want a venv anyway, run `bash pi-service/setup-pi.sh --venv`. That creates it with `--system-site-packages` so it can still see the apt packages.

**Two streams, locked focus:** the Pi backend captures a 640×480 "lores" stream that is the only thing scored, and a 1280×720 "main" stream that `/frame.jpg` serves. At startup it runs one autofocus cycle, then locks the lens at the result and logs it (`Autofocus locked at lens position ...`). If the lighting or distance changes and the image goes soft, re-lock without restarting:

```bash
curl -X POST localhost:5050/refocus
```

Point the camera at something with detail first. Confirm you're on the real camera with `curl localhost:5050/health` (expect `"sensor_model": "imx708"`). Fixed-focus cameras (Camera Module 2) skip autofocus.

**Which camera code runs:** `CAMERA_BACKEND` is `auto` by default: it uses `picamera2` if it can be imported, then falls back to OpenCV (`cv2.VideoCapture`) if the Pi camera can't be opened. So a USB webcam on a Pi still works. Set `CAMERA_BACKEND=picamera2` or `CAMERA_BACKEND=opencv` to force one. Mac and Windows have no `picamera2`, so they use OpenCV automatically.

## Phone alerts (optional)

When the camera gets worse, a Retell voice agent calls the team. Without setup the service runs normally and logs one warning: `Alerts are OFF`.

```bash
cd pi-service
cp .env.example .env      # Windows: copy .env.example .env
```

Fill in `pi-service/.env` (git-ignored), then restart `camera_service.py`. The log should say `Alerts are ON via retell. Will dial: ...`.

```
RETELL_TEAM_NUMBERS=+1555...,+1555...,+1555...   # three E.164 numbers, comma-separated
RETELL_ENABLED=true                              # false = use the Twilio fallback
RETELL_API_KEY=...
RETELL_AGENT_ID=...                              # the Lavender agent
RETELL_FROM_NUMBER=+1...
```

- **When it fires:** only on `clear` to `degraded`, `clear` to `obstructed`, and `degraded` to `obstructed`. Recoveries, no-change frames and the first frame after startup never fire.
- **Debounced:** a status only counts once it has held steady for 5 seconds (`ALERT_SUSTAIN_S` in `camera_service.py`). A hand passing by or a moment of motion blur won't fire anything; the lens has to actually stay obstructed.
- **One event, one round of calls:** every number is called once, one after another, with `confidence` and `status` passed to the agent. A bad or unverified number logs an error and the others are still called.
- **One shared cooldown, live-adjustable:** after an alert, nothing else fires for a while, so a flickering status can't spam the team. It defaults to 5 minutes (`DEFAULT_ALERT_COOLDOWN_S`), set it at startup with `ALERT_COOLDOWN_S` in `.env`, or change it on the fly (no restart) with `GET`/`PUT /alert-config` — the dashboard reads and can update this. Bounded between 10 seconds and 1 hour.
- **Test with one number:** set `DIAL_ONLY_FIRST_NUMBER = True` in `camera_service.py`.
- **Retell misbehaving during the demo?** Set `RETELL_ENABLED=false`, fill in the three `TWILIO_*` lines, and restart. The same team numbers get a plain Twilio **voice call** instead (`send_fallback_call()` in `camera_service.py`) that reads the alert aloud twice: "Camera alert. The camera is now obstructed. Confidence is 12 out of 100. Please check the camera." It is not automatic; you flip it yourself. (A Twilio trial account can only call numbers you've verified in its console.)
- **Reading the log:** each alert prints the transition, the channel, the numbers being dialed, one line per number (with the Retell `call_id`), and a summary like `Retell alert done: 2/3 reached. Failed: +1...`.
- **Failures are safe:** calls are placed from a background thread. Bad keys, no internet or a rate limit only log errors; frame capture and `/status` are unaffected.
- Run `pip install -r requirements.txt` again on an existing install (the Pi script installs `retell-sdk`, `twilio` and `python-dotenv` for you).

## Troubleshooting

**Camera not found** (log says `Could not open camera at device index 0`)
- Try index 1. In [`pi-service/camera_service.py`](pi-service/camera_service.py) change the `0` to `1` in the `CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))` line, then restart. Or without editing anything: `CAMERA_INDEX=1 python camera_service.py` (Mac/Linux), or `set CAMERA_INDEX=1` then `python camera_service.py` (Windows).
- Close anything else using the camera (Zoom, Teams, browser tabs).
- Mac: System Settings → Privacy & Security → Camera → allow your terminal app, then restart the service.
- Raspberry Pi camera module: use `setup-pi.sh` (see "Raspberry Pi" below). It installs `picamera2`, which is how the service talks to libcamera cameras. Check `libcamera-hello` (or `rpicam-hello`) shows a preview; if not, it's the cable/config, not this code.
- Image is blurry on the Pi: `curl -X POST localhost:5050/refocus` with the camera aimed at something detailed. The service logs the locked lens position. If it says autofocus failed, improve the lighting and try again.
- Wrong backend picked? Force one: `CAMERA_BACKEND=opencv` or `CAMERA_BACKEND=picamera2` before `python camera_service.py`. The log line "Camera opened via ..." says which one is in use.
- Pi image has red and blue swapped: look at the `"format": "BGR888"` line in `_open_picamera2_once` in `camera_service.py`.

**CORS error in the browser console**
- The service uses `flask-cors`. Check it's installed: activate the venv and run `pip show flask-cors`. If missing, run `pip install -r requirements.txt`.
- Restart `camera_service.py` (Ctrl+C, then start it again). Changes don't apply until you do.
- Check `SERVICE_URL` in `web/app.js` matches the address the service is on, including `:5050`.

**"Address already in use" / port 5050 is in use**

Something is already on 5050, usually an old copy of this service still running. Find and stop it:

```bash
lsof -i :5050          # Mac/Linux: note the PID, then: kill <PID>
```
```bat
netstat -ano | findstr :5050
taskkill /PID <PID> /F
```

If you change `PORT` in `camera_service.py` instead, change `SERVICE_URL` in `web/app.js` to match. If port 8080 is taken for the dashboard, use another, like `8081`.

**Dashboard says "Sensor offline"**
- Is the service running, and does `http://<IP>:5050/health` load in the browser?
- Windows may show a firewall prompt the first time Python listens. Click **Allow**, or other machines can't reach it.
- Both machines must be on the same Wi-Fi/network.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /status` | `{confidence, status, fog_score, obstruction_score, timestamp}` (`timestamp` = unix seconds). `503` until the first frame arrives. |
| `GET /frame.jpg` | Most recent frame as JPEG. `503` until the first frame arrives. |
| `GET /health` | `{ok: true, camera_connected: bool, sensor_model}`. `sensor_model` is e.g. `"imx708"` on a Pi camera and `null` on a webcam, so you can tell the OpenCV fallback isn't silently in use. |
| `POST /refocus` | Pi camera only: runs one autofocus cycle and locks the lens there. `200 {ok, lens_position, af_state}`. `500` if autofocus failed or timed out, `409` if there is no autofocus-capable Pi camera. |
| `GET /alert-config` | `{cooldown_s, min_cooldown_s, max_cooldown_s}` — the live alert cooldown and its bounds. |
| `PUT /alert-config` | Body `{"cooldown_s": 300}`. Changes the alert cooldown immediately, no restart. `400` if out of bounds (10–3600). |

`status` is `clear` (confidence ≥ 70), `degraded` (40–69) or `obstructed` (< 40).

## How the score works

Two independent scores, both 0-100 (100 = good), computed on a frame shrunk to 320 px wide so different cameras score comparably:

1. **`fog_score`** – variance of the Laplacian of the grayscale image. Low = blurry/foggy overall.
2. **`obstruction_score`** – frame split into an 8×6 grid; a cell is "covered" if it is bright *and* nearly flat (something opaque on the lens). Score drops as more cells are covered. A uniform blur doesn't trigger this; a bright patch does. Dark patches are not detected by design.

`confidence = min(fog_score, obstruction_score)`, lightly smoothed so it doesn't flicker.

## Tuning

All knobs are constants in the block at the top of [`pi-service/camera_service.py`](pi-service/camera_service.py):

| Symptom | Change |
|---|---|
| A good image scores low | Lower `LAPLACIAN_CLEAR` |
| A blurry image still scores high | Raise `LAPLACIAN_CLEAR` |
| Covering the lens isn't caught | Lower `COVERED_MIN_BRIGHTNESS`, raise `COVERED_MAX_STDDEV`, or lower `COVERED_FRAME_FRACTION` |
| Bright blank walls flagged as obstruction | Raise `COVERED_MIN_BRIGHTNESS`, lower `COVERED_MAX_STDDEV`, or raise `COVERED_FRAME_FRACTION` |
| Score too twitchy / too laggy | Lower / raise `SMOOTHING` (1.0 = off) |
| Status cutoffs | `CLEAR_MIN`, `DEGRADED_MIN` |

## Credits

Some UI effects in `web/` are plain-JS/CSS ports of [React Bits](https://reactbits.dev) components: SpotlightCard, ShinyText, GradientText, CountUp and StarBorder. The background glow and film grain are lightweight CSS versions inspired by React Bits' Aurora and Noise. Check React Bits' license before reusing this outside the hackathon.

Fonts are Barlow and Barlow Condensed (SIL Open Font License, see `web/fonts/OFL.txt`), self-hosted in `web/fonts/` so the dashboard looks the same offline.
