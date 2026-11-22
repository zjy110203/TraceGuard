#!/usr/bin/env bash
# One-word entry for the stage-2 demo. Run:  bash run_demo.sh
set -euo pipefail
cd "$(dirname "$0")"
PYTHONPATH=. .venv/Scripts/python.exe -m scripts.demo
