#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-build.txt
python build_mac.py
printf '\nBuild complete. See dist-dmg/.\n'
read -n 1 -s -r -p "Press any key to close..."
