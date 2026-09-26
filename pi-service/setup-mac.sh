#!/usr/bin/env bash
# Mac / Linux / Raspberry Pi setup: makes a venv and installs requirements.
# Run from anywhere:  bash pi-service/setup-mac.sh
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3 (https://www.python.org/downloads/) and re-run." >&2
  exit 1
fi

echo "Creating virtual environment in pi-service/venv ..."
python3 -m venv venv

# Activating here only affects this script; the install below uses that venv.
source venv/bin/activate

echo "Installing requirements ..."
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt

cat <<'EOF'

Setup done. Start the camera service with:

  cd pi-service
  source venv/bin/activate
  python camera_service.py

EOF
