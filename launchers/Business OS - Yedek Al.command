#!/bin/zsh

APP_DIR="${BUSINESSOS_APP_DIR:-$HOME/Documents/BusinessOS}"
PYTHON="$APP_DIR/.venv/bin/python"
BACKUP_SCRIPT="$APP_DIR/launchers/backup_to_google_drive.py"

clear
echo "BusinessOS - Google Drive'a yedek al"
echo "======================================"

if [[ ! -x "$PYTHON" || ! -f "$BACKUP_SCRIPT" ]]; then
  echo "HATA: BusinessOS kurulumu veya yedekleme aracı bulunamadı."
  echo "Beklenen klasör: $APP_DIR"
  echo
  read "?Kapatmak için Enter tuşuna basın..."
  exit 1
fi

"$PYTHON" "$BACKUP_SCRIPT"
STATUS=$?
echo
if [[ $STATUS -ne 0 ]]; then
  echo "Yedek alınamadı. Mevcut veritabanınız değiştirilmedi."
  read "?Kapatmak için Enter tuşuna basın..."
  exit $STATUS
fi

echo "Yedek doğrulandı ve Google Drive klasörüne kaydedildi."
echo "Windows'a geçmeden önce Google Drive eşitleme işaretinin tamamlanmasını bekleyin."
echo
read "?Kapatmak için Enter tuşuna basın..."
