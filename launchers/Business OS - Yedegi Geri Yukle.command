#!/bin/zsh

APP_DIR="${BUSINESSOS_APP_DIR:-$HOME/Documents/BusinessOS}"
PYTHON="$APP_DIR/.venv/bin/python"
RESTORE_SCRIPT="$APP_DIR/launchers/restore_latest_drive_backup.py"

clear
echo "BusinessOS - Google Drive yedeğini geri yükle"
echo "================================================"

if [[ ! -x "$PYTHON" || ! -f "$RESTORE_SCRIPT" ]]; then
  echo "HATA: BusinessOS kurulumu veya geri yükleme aracı bulunamadı."
  echo "Beklenen klasör: $APP_DIR"
  echo
  read "?Kapatmak için Enter tuşuna basın..."
  exit 1
fi

"$PYTHON" "$RESTORE_SCRIPT"
STATUS=$?
if [[ $STATUS -ne 0 ]]; then
  echo
  echo "Yedek yüklenmedi; mevcut veritabanınız değiştirilmedi."
  read "?Kapatmak için Enter tuşuna basın..."
  exit $STATUS
fi

echo
echo "BusinessOS en güncel yedekle başlatılıyor..."
sleep 2
exec "$HOME/.local/bin/businessos"
