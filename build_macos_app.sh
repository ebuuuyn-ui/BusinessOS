#!/bin/zsh
set -e
APP_DIR="${0:A:h}"
cd "$APP_DIR"
ICONSET_DIR="$APP_DIR/assets/BusinessOS.iconset"
ICON_FILE="$APP_DIR/assets/BusinessOS.icns"
ICON_SOURCE="$APP_DIR/assets/business-os-mark.png"
ICON_MASTER="$APP_DIR/assets/business-os-icon-1024.png"
mkdir -p "$ICONSET_DIR"
sips -s format png --padToHeightWidth 1024 1024 --padColor FFFFFF "$ICON_SOURCE" --out "$ICON_MASTER" >/dev/null
for size in 16 32 128 256 512; do
  sips -s format png -z "$size" "$size" "$ICON_MASTER" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
  double_size=$((size * 2))
  sips -s format png -z "$double_size" "$double_size" "$ICON_MASTER" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET_DIR" -o "$ICON_FILE"
"$APP_DIR/.venv/bin/python" -m PyInstaller --noconfirm --clean --windowed --name "Business OS" --icon "$ICON_FILE" --add-data "templates:templates" --add-data "static:static" desktop.py
