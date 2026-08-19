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


EXPECTED_TABLES = {"customer", "order", "order_item", "product"}
BACKUP_DIR_NAMES = {
    "businessos yedekleri",
    "bussinessos yedekleri",
    "business os yedekleri",
}


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


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    sqlite_report(destination)


def restore(app_dir: Path, backup: Path, check_only: bool) -> None:
    target = app_dir / "instance" / "business_os.db"
    report = sqlite_report(backup)
    report_path = verification_report(backup, report)
    backup_hash = sha256(backup)

    print(f"En yeni Google Drive yedeği: {backup.name}")
    print(f"Boyut: {backup.stat().st_size:,} bayt")
    print(f"SHA-256: {backup_hash}")
    print("SQLite bütünlük kontrolü: OK")
    print("Doğrulama raporu: " + (report_path.name if report_path else "yok (SQLite doğrulandı)"))
    counts = report["row_counts"]
    print(
        "Kayıtlar: "
        f"{counts.get('customer', 0)} cari, "
        f"{counts.get('order', 0)} sipariş, "
        f"{counts.get('product', 0)} ürün"
    )
    if check_only:
        print("Kontrol tamamlandı; veritabanı değiştirilmedi.")
        return

    stop_businessos(app_dir)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    safety_copy = (
        app_dir
        / "instance"
        / "backups"
        / f"business_os_before_drive_restore_{timestamp}.db"
    )
    if target.is_file():
        sqlite_backup(target, safety_copy)
        print(f"Mevcut Mac veritabanı korundu: {safety_copy.name}")

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
