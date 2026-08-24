#!/bin/zsh
set -e
APP_DIR="${0:A:h}"
cd "$APP_DIR"
"$APP_DIR/.venv/bin/python" -m PyInstaller --noconfirm --clean --windowed --name "Business OS" --add-data "templates:templates" --add-data "static:static" desktop.py
