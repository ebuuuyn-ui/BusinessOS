"""Native macOS shell for Business OS."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sys
import threading
import urllib.request
from werkzeug.serving import make_server
import webview

APP_NAME = "Business OS"
EXPORT_PATH = re.compile(r"^/siparisler/(?:\d+/(?:pdf|excel)|excel)$")

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
