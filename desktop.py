"""Native macOS shell for Business OS."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from werkzeug.serving import make_server
import webview

APP_NAME = "Business OS"
EXPORT_PATH = re.compile(r"^/(?:siparisler/(?:\d+/(?:pdf|excel)|excel(?:/ayrintili)?)|siparis-belgeleri/\d+/indir|telegram-belgeler/\d+/indir|e-arsiv-belgeleri/\d+/indir|tahsilat-takibi/(?:excel|pdf)|musteriler/bakiyeler/(?:excel|pdf)|musteriler/toplam-hareket-bakiyeleri/excel|musteriler/\d+/cari-hesap/ekstre/(?:ozet|ayrintili)/(?:excel|pdf))$")
PDF_SHARE_PATH = re.compile(r"^/siparisler/\d+/pdf$")

def resource_directory() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

def user_data_directory() -> Path:
    if sys.platform == "darwin": return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME

def migrate_legacy_data(destination: Path) -> None:
    if (destination / "business_os.db").exists(): return
    for legacy in (Path.home() / "Documents" / "BusinessOS" / "instance", Path.home() / "Documents" / "Business OS" / "instance"):
        database = legacy / "business_os.db"
        if not database.is_file(): continue
        destination.mkdir(parents=True, exist_ok=True); shutil.copy2(database, destination / "business_os.db")
        for extra in ("ozon_api_settings.json",):
            if (legacy / extra).is_file(): shutil.copy2(legacy / extra, destination / extra)
        if (legacy / "backups").is_dir(): shutil.copytree(legacy / "backups", destination / "backups", dirs_exist_ok=True)
        if (legacy / "order_documents").is_dir(): shutil.copytree(legacy / "order_documents", destination / "order_documents", dirs_exist_ok=True)
        return

class DesktopApi:
    def __init__(self, base_url: str): self.base_url = base_url
    def save_export(self, path: str, fallback_name: str) -> dict:
        if not EXPORT_PATH.fullmatch(path.partition("?")[0]): return {"ok": False, "error": "Bu dosya dışa aktarma yolu geçerli değil."}
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=60) as response:
                content = response.read(); disposition = response.headers.get("Content-Disposition", "")
            found = re.search(r'filename="?([^";]+)', disposition)
            filename = Path(found.group(1) if found else fallback_name).name or "Business-OS-dosya"
            downloads = Path.home() / "Downloads"; downloads.mkdir(parents=True, exist_ok=True)
            target = downloads / filename; index = 2
            while target.exists(): target = downloads / f"{Path(filename).stem} ({index}){Path(filename).suffix}"; index += 1
            target.write_bytes(content)
            return {"ok": True, "path": str(target), "name": target.name}
        except Exception as error: return {"ok": False, "error": f"Dosya indirilemedi: {error}"}

    def share_export(self, path: str, fallback_name: str) -> dict:
        """PDF'i kaydeder ve macOS'un yerleşik paylaşım penceresini açar."""
        if not PDF_SHARE_PATH.fullmatch(path.partition("?")[0]):
            return {"ok": False, "error": "Bu paylaşım yolu geçerli değil."}
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=60) as response:
                content = response.read(); disposition = response.headers.get("Content-Disposition", "")
            found = re.search(r'filename="?([^";]+)', disposition)
            filename = Path(found.group(1) if found else fallback_name).name or "Business-OS-Siparis.pdf"
            downloads = Path.home() / "Downloads"; downloads.mkdir(parents=True, exist_ok=True)
            target = downloads / filename; index = 2
            while target.exists():
                target = downloads / f"{Path(filename).stem} ({index}){Path(filename).suffix}"; index += 1
            target.write_bytes(content)
            if sys.platform != "darwin":
                return {"ok": True, "name": target.name, "shared": False}

            from AppKit import NSApplication, NSMakeRect, NSMinYEdge, NSSharingServicePicker
            from Foundation import NSURL
            from PyObjCTools import AppHelper

            def show_share_sheet() -> None:
                app = NSApplication.sharedApplication()
                window = app.keyWindow() or app.mainWindow()
                if window is None:
                    return
                picker = NSSharingServicePicker.alloc().initWithItems_([NSURL.fileURLWithPath_(str(target))])
                self._active_share_picker = picker
                picker.showRelativeToRect_ofView_preferredEdge_(NSMakeRect(0, 0, 1, 1), window.contentView(), NSMinYEdge)

            AppHelper.callAfter(show_share_sheet)
            return {"ok": True, "name": target.name, "shared": True}
        except Exception as error:
            return {"ok": False, "error": f"PDF hazırlanamadı: {error}"}

    def whatsapp_export(self, path: str, fallback_name: str) -> dict:
        """PDF'i panoya dosya olarak koyar ve WhatsApp'ı açar.

        WhatsApp macOS paylaşım uzantısı sağlamadığı için kullanıcı yalnızca
        hedef sohbeti seçip Command-V ile hazır dosyayı ekler.
        """
        if not PDF_SHARE_PATH.fullmatch(path.partition("?")[0]):
            return {"ok": False, "error": "Bu paylaşım yolu geçerli değil."}
        try:
            with urllib.request.urlopen(self.base_url + path, timeout=60) as response:
                content = response.read(); disposition = response.headers.get("Content-Disposition", "")
            found = re.search(r'filename="?([^";]+)', disposition)
            filename = Path(found.group(1) if found else fallback_name).name or "Business-OS-Siparis.pdf"
            downloads = Path.home() / "Downloads"; downloads.mkdir(parents=True, exist_ok=True)
            target = downloads / filename; index = 2
            while target.exists():
                target = downloads / f"{Path(filename).stem} ({index}){Path(filename).suffix}"; index += 1
            target.write_bytes(content)
            if sys.platform != "darwin":
                return {"ok": True, "name": target.name, "opened": False}

            from AppKit import NSPasteboard
            from Foundation import NSURL
            pasteboard = NSPasteboard.generalPasteboard()
            pasteboard.clearContents()
            if not pasteboard.writeObjects_([NSURL.fileURLWithPath_(str(target))]):
                return {"ok": False, "error": "PDF panoya kopyalanamadı."}
            result = subprocess.run(["open", "-a", "WhatsApp"], capture_output=True, text=True, check=False)
            if result.returncode != 0:
                return {"ok": False, "error": "WhatsApp uygulaması açılamadı. Mac'e WhatsApp'ı kurun veya Paylaş düğmesini kullanın."}
            return {"ok": True, "name": target.name, "opened": True}
        except Exception as error:
            return {"ok": False, "error": f"WhatsApp paylaşımı hazırlanamadı: {error}"}

def main() -> None:
    data_directory = user_data_directory(); migrate_legacy_data(data_directory)
    os.environ["BUSINESSOS_DATA_DIR"] = str(data_directory); os.chdir(resource_directory())
    from app import app
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    window = webview.create_window(APP_NAME, base_url, js_api=DesktopApi(base_url), width=1440, height=920, min_size=(1100, 700), background_color="#f5f5f7")
    window.events.closed += lambda: server.shutdown()
    webview.start(gui="cocoa" if sys.platform == "darwin" else None)

if __name__ == "__main__": main()
