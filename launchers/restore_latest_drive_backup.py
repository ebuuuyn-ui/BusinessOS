#!/usr/bin/env python3
"""Restore the newest verified BusinessOS SQLite backup from Google Drive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import tempfile
import zipfile


EXPECTED_TABLES = {"customer", "order", "order_item", "product"}
BACKUP_DIR_NAMES = {
    "businessos yedekleri",
    "bussinessos yedekleri",
    "business os yedekleri",
}


def active_data_directory(app_dir: Path) -> Path:
    """Use the native app's persistent data location on both platforms."""
    configured = os.environ.get("BUSINESSOS_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Business OS"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "Business OS"
    return app_dir / "instance"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sqlite_report(path: Path) -> dict:
    uri = f"file:{path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing = EXPECTED_TABLES - tables
        if missing:
            raise RuntimeError(
                "BusinessOS tabloları eksik: " + ", ".join(sorted(missing))
            )
        counts = {}
        for table in sorted(tables):
            safe_table = table.replace('"', '""')
            counts[table] = connection.execute(
                f'SELECT COUNT(*) FROM "{safe_table}"'
            ).fetchone()[0]
    if quick_check != "ok" or integrity_check != "ok":
        raise RuntimeError(
            f"SQLite bütünlük kontrolü başarısız: {quick_check} / {integrity_check}"
        )
    if foreign_keys:
        raise RuntimeError(f"Yabancı anahtar hatası bulundu: {len(foreign_keys)}")
    return {
        "quick_check": quick_check,
        "integrity_check": integrity_check,
        "foreign_key_violations": len(foreign_keys),
        "row_counts": counts,
    }


def find_backup_folders() -> list[Path]:
    cloud_root = Path.home() / "Library" / "CloudStorage"
    candidates: list[Path] = []
    for drive in cloud_root.glob("GoogleDrive-*"):
        for root_name in ("Drive'ım", "My Drive"):
            root = drive / root_name
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir() and child.name.casefold() in BACKUP_DIR_NAMES:
                    candidates.append(child)
    return candidates


def newest_backup(folders: list[Path]) -> Path:
    backups = []
    for folder in folders:
        backups.extend(
            path
            for path in folder.glob("business_os_*.db")
            if path.is_file() and not path.name.endswith(".db.incoming")
        )
    if not backups:
        raise RuntimeError(
            "Google Drive içinde business_os_....db biçiminde yedek bulunamadı."
        )
    return max(backups, key=lambda path: (path.stat().st_mtime, path.name))


def verification_report(backup: Path, actual: dict) -> Path | None:
    report_path = backup.with_suffix(".verification.json")
    if not report_path.is_file():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    expected = report.get("backup", report.get("source", {}))
    expected_size = expected.get("size")
    expected_hash = str(expected.get("sha256", "")).lower()
    if expected_size is not None and int(expected_size) != backup.stat().st_size:
        raise RuntimeError("Doğrulama raporundaki dosya boyutu yedekle eşleşmiyor.")
    if expected_hash and expected_hash != sha256(backup):
        raise RuntimeError("Doğrulama raporundaki SHA-256 yedekle eşleşmiyor.")
    expected_counts = expected.get("row_counts", {})
    for table, expected_count in expected_counts.items():
        if table in actual["row_counts"] and actual["row_counts"][table] != expected_count:
            raise RuntimeError(f"{table} kayıt sayısı doğrulama raporuyla eşleşmiyor.")
    return report_path


def document_archive_for(backup: Path) -> Path:
    return backup.with_name(f"{backup.stem}.documents.zip")


def verification_manifest(report_path: Path | None) -> dict:
    if not report_path:
        return {}
    return json.loads(report_path.read_text(encoding="utf-8-sig"))


def validate_document_archive(archive_path: Path, expected: dict) -> dict:
    if not archive_path.is_file():
        raise RuntimeError("Belge arşivi bulunamadı; geri yükleme güvenlik nedeniyle durduruldu.")
    if expected.get("sha256") and sha256(archive_path) != expected["sha256"]:
        raise RuntimeError("Belge arşivinin SHA-256 doğrulaması başarısız oldu.")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            for member in members:
                relative = Path(member.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise RuntimeError("Belge arşivinde güvenli olmayan dosya yolu var.")
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Belge arşivi okunamadı.") from exc
    if expected.get("file_count") is not None and len(members) != int(expected["file_count"]):
        raise RuntimeError("Belge arşivindeki dosya sayısı doğrulama raporuyla eşleşmiyor.")
    return {"file_count": len(members), "sha256": sha256(archive_path)}


def extract_document_archive(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            relative = Path(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("Belge arşivinde güvenli olmayan dosya yolu var.")
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, output.open("wb") as target:
                shutil.copyfileobj(source, target)


def archive_existing_documents(source: Path, destination: Path) -> None:
    if not source.is_dir():
        return
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


def businessos_pids(app_dir: Path) -> list[int]:
    result = subprocess.run(
        ["lsof", "-tiTCP:5000", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = []
    for value in result.stdout.split():
        if not value.isdigit():
            continue
        pid = int(value)
        cwd_result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            check=False,
        )
        cwd_values = [line[1:] for line in cwd_result.stdout.splitlines() if line.startswith("n")]
        if any(Path(value).resolve() == app_dir.resolve() for value in cwd_values):
            pids.append(pid)
    return pids


def stop_businessos(app_dir: Path) -> None:
    pids = businessos_pids(app_dir)
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 8
    while pids and time.time() < deadline:
        remaining = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                remaining.append(pid)
            except ProcessLookupError:
                pass
        pids = remaining
        if pids:
            time.sleep(0.25)
    if pids:
        raise RuntimeError("BusinessOS güvenli biçimde kapatılamadı. Önce uygulamayı kapatın.")

    native = subprocess.run(["pgrep", "-x", "Business OS"], capture_output=True, text=True, check=False)
    for value in native.stdout.split():
        if value.isdigit():
            os.kill(int(value), signal.SIGTERM)


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    sqlite_report(destination)


def restore(app_dir: Path, backup: Path, check_only: bool) -> None:
    data_dir = active_data_directory(app_dir)
    target = data_dir / "business_os.db"
    report = sqlite_report(backup)
    report_path = verification_report(backup, report)
    manifest = verification_manifest(report_path)
    documents_archive = document_archive_for(backup)
    documents_expected = manifest.get("documents", {})
    has_documents_archive = documents_archive.is_file()
    document_report = None
    if has_documents_archive:
        document_report = validate_document_archive(documents_archive, documents_expected)
    backup_hash = sha256(backup)

    print(f"En yeni Google Drive yedeği: {backup.name}")
    print(f"Boyut: {backup.stat().st_size:,} bayt")
    print(f"SHA-256: {backup_hash}")
    print("SQLite bütünlük kontrolü: OK")
    print("Doğrulama raporu: " + (report_path.name if report_path else "yok (SQLite doğrulandı)"))
    if has_documents_archive:
        print(f"Belge arşivi doğrulandı: {document_report['file_count']} dosya")
    else:
        print("Belge arşivi: bu eski yedekte yok; Mac'teki mevcut belgeler korunacak.")
    counts = report["row_counts"]
    print(
        "Kayıtlar: "
        f"{counts.get('customer', 0)} cari, "
        f"{counts.get('order', 0)} sipariş, "
        f"{counts.get('product', 0)} ürün"
    )
    if check_only:
        print("Kontrol tamamlandı; veritabanı ve belgeler değiştirilmedi.")
        return

    extracted_documents = None
    if has_documents_archive:
        data_dir.mkdir(parents=True, exist_ok=True)
        extracted_documents = Path(tempfile.mkdtemp(prefix="order-documents-incoming-", dir=data_dir))
        try:
            extract_document_archive(documents_archive, extracted_documents)
        except Exception:
            shutil.rmtree(extracted_documents, ignore_errors=True)
            raise

    stop_businessos(app_dir)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    safety_copy = (
        data_dir
        / "backups"
        / f"business_os_before_drive_restore_{timestamp}.db"
    )
    if target.is_file():
        sqlite_backup(target, safety_copy)
        print(f"Mevcut Mac veritabanı korundu: {safety_copy.name}")

    documents_target = data_dir / "order_documents"
    if has_documents_archive and documents_target.is_dir():
        documents_safety_copy = (
            data_dir / "backups" / f"order_documents_before_drive_restore_{timestamp}.zip"
        )
        documents_safety_copy.parent.mkdir(parents=True, exist_ok=True)
        archive_existing_documents(documents_target, documents_safety_copy)
        print(f"Mevcut Mac belgeleri korundu: {documents_safety_copy.name}")

    incoming = target.with_suffix(".db.incoming")
    shutil.copy2(backup, incoming)
    incoming_report = sqlite_report(incoming)
    if sha256(incoming) != backup_hash or incoming_report != report:
        incoming.unlink(missing_ok=True)
        raise RuntimeError("Kopyalama sonrası doğrulama başarısız oldu.")
    os.replace(incoming, target)
    final_report = sqlite_report(target)
    if sha256(target) != backup_hash or final_report != report:
        raise RuntimeError("Geri yüklenen veritabanı son kontrolden geçemedi.")
    if has_documents_archive and extracted_documents:
        previous_documents = data_dir / f"order_documents_before_restore_{timestamp}"
        if documents_target.exists():
            os.replace(documents_target, previous_documents)
        os.replace(extracted_documents, documents_target)
        print(f"Belge arşivi geri yüklendi: {document_report['file_count']} dosya")
    print("Yedek başarıyla geri yüklendi.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    app_dir = Path(os.environ.get("BUSINESSOS_APP_DIR", Path.home() / "Documents" / "BusinessOS"))
    if not (app_dir / "app.py").is_file():
        raise RuntimeError(f"BusinessOS uygulama klasörü bulunamadı: {app_dir}")
    folders = find_backup_folders()
    if not folders:
        raise RuntimeError(
            "Google Drive eşitleme klasörü bulunamadı. Google Drive uygulamasını açıp eşitlemenin tamamlanmasını bekleyin."
        )
    restore(app_dir, newest_backup(folders), args.check_only)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nHATA: {exc}", file=sys.stderr)
        raise SystemExit(1)
