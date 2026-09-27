#!/usr/bin/env bash
# Raspberry Pi setup (Raspberry Pi OS with a libcamera camera module).
#
#   bash pi-service/setup-pi.sh          # installs, starts the service, checks /health
#   bash pi-service/setup-pi.sh --venv   # same, but inside a venv (see below)
#
# Deliberately NOT a normal venv: picamera2 needs the system libcamera
# bindings, which pip can't provide. Everything comes from apt, plus
# flask-cors from pip. With --venv we create the venv with
# --system-site-packages so it can still see the apt-installed packages.
set -euo pipefail

cd "$(dirname "$0")"

USE_VENV=0
[ "${1:-}" = "--venv" ] && USE_VENV=1

sudo apt update
sudo apt install -y python3-picamera2 python3-opencv python3-flask python3-pip

if [ "$USE_VENV" = 1 ]; then
  python3 -m venv --system-site-packages venv
  source venv/bin/activate
  python -m pip install flask-cors twilio python-dotenv retell-sdk
else
  # Newer Raspberry Pi OS blocks system-wide pip unless told otherwise; older
  # pip doesn't know that flag, so fall back to a plain install.
  python3 -m pip install flask-cors twilio python-dotenv retell-sdk --break-system-packages \
    || python3 -m pip install flask-cors twilio python-dotenv retell-sdk
fi

echo
echo "Starting camera_service.py ..."
python3 camera_service.py &
SERVICE_PID=$!
trap 'kill "$SERVICE_PID" 2>/dev/null || true' EXIT INT TERM

# The service tries to open the camera before it starts listening, so give it a while.
HEALTH=""
for _ in $(seq 1 30); do
  if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
    echo "camera_service.py exited. See the error above." >&2
    exit 1
  fi
  if HEALTH=$(curl -fsS --max-time 2 localhost:5050/health 2>/dev/null); then
    break
  fi
  HEALTH=""
  sleep 1
done

if [ -z "$HEALTH" ]; then
  echo "Service did not answer on localhost:5050/health after 30s." >&2
  exit 1
fi

echo
echo "curl localhost:5050/health -> $HEALTH"
if echo "$HEALTH" | grep -Eq '"camera_connected": ?false'; then
  echo "Service is up but no camera was found. Check the ribbon cable / enable the camera, then look at the log above." >&2
fi

echo
echo "Service is running (Ctrl+C to stop). To start it again later:"
if [ "$USE_VENV" = 1 ]; then
  echo "  cd pi-service && source venv/bin/activate && python camera_service.py"
else
  echo "  cd pi-service && python3 camera_service.py"
fi
wait "$SERVICE_PID"
