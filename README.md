# Camera Confidence Check

Scores how clear a camera image is (0-100) and shows it in a web page.

- `pi-service/` – Python Flask service. Reads the camera, scores frames, serves JSON + a JPEG.
- `web/` – plain HTML/CSS/JS frontend. No build step, no framework: open `index.html` or serve the folder.

The same script runs on a Raspberry Pi camera and on a laptop webcam.

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

## Troubleshooting

**Camera not found** (log says `Could not open camera at device index 0`)
- Try index 1. In [`pi-service/camera_service.py`](pi-service/camera_service.py) change the `0` to `1` in the `CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", 0))` line, then restart. Or without editing anything: `CAMERA_INDEX=1 python camera_service.py` (Mac/Linux), or `set CAMERA_INDEX=1` then `python camera_service.py` (Windows).
- Close anything else using the camera (Zoom, Teams, browser tabs).
- Mac: System Settings → Privacy & Security → Camera → allow your terminal app, then restart the service.
- Raspberry Pi ribbon-cable camera: `ls /dev/video*`. If nothing is there, OpenCV can't see it (recent Pi OS uses libcamera). A USB webcam is the easy fix.

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
| `GET /health` | `{ok: true, camera_connected: bool}` |

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
