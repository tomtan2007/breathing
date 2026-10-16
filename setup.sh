#!/bin/bash
# One-time setup: creates the Python environment and downloads the face model.
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
mkdir -p models
curl -sSfL -o models/face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
echo "Setup done. Start the app with ./run.sh"
