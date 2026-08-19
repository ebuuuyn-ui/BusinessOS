#!/usr/bin/env python3
"""Create and verify a BusinessOS SQLite backup in the local Google Drive folder."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import sys
import tempfile


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


def database_report(path: Path) -> dict:
    path = path.resolve()
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        missing = EXPECTED_TABLES - set(tables)
        if missing:
            raise RuntimeError(
                "BusinessOS tabloları eksik: " + ", ".join(sorted(missing))
            )
        counts = {}
        for table in tables:
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
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": sha256(path),
        "quick_check": quick_check,
        "integrity_check": integrity_check,
        "foreign_key_violations": len(foreign_keys),
        "row_counts": counts,
    }


def find_backup_folder() -> Path:
    cloud_root = Path.home() / "Library" / "CloudStorage"
    matches: list[Path] = []
    for drive in cloud_root.glob("GoogleDrive-*"):
        for root_name in ("Drive'ım", "My Drive"):
            root = drive / root_name
            if not root.is_dir():
                continue
            for child in root.iterdir():
                if child.is_dir() and child.name.casefold() in BACKUP_DIR_NAMES:
                    matches.append(child)
    if not matches:
        raise RuntimeError(
            "Google Drive yedek klasörü bulunamadı. Google Drive uygulamasını açıp eşitlemenin tamamlanmasını bekleyin."
        )
    return max(matches, key=lambda path: path.stat().st_mtime)


def sqlite_backup(source: Path, destination: Path) -> None:
    with sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True, timeout=30) as src:
        with sqlite3.connect(destination, timeout=30) as dst:
            src.backup(dst)


def safe_device_name() -> str:
    value = platform.node() or "MAC"
    value = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")
    return value.upper() or "MAC"


def create_backup(app_dir: Path, check_only: bool) -> tuple[Path | None, dict]:
    source = app_dir / "instance" / "business_os.db"
    if not source.is_file():
        raise RuntimeError(f"BusinessOS veritabanı bulunamadı: {source}")
    source_report = database_report(source)
    destination = find_backup_folder()

    print(f"Aktif veritabanı: {source}")
    print(f"Google Drive hedefi: {destination}")
    print("Kaynak SQLite bütünlük kontrolü: OK")
    counts = source_report["row_counts"]
    print(
        "Kayıtlar: "
        f"{counts.get('customer', 0)} cari, "
        f"{counts.get('order', 0)} sipariş, "
        f"{counts.get('product', 0)} ürün"
    )
    if check_only:
        print("Kontrol tamamlandı; Google Drive'a dosya yazılmadı.")
        return None, source_report

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    name = f"business_os_{safe_device_name()}_{stamp}.db"
    target = destination / name

    with tempfile.TemporaryDirectory(prefix="businessos-backup-") as temp_dir:
        temp_db = Path(temp_dir) / name
        sqlite_backup(source, temp_db)
        backup_report = database_report(temp_db)
        if source_report["row_counts"] != backup_report["row_counts"]:
            raise RuntimeError("Yedek kayıt sayıları kaynak veritabanıyla eşleşmiyor.")

        manifest = {
            "operation": "backup",
            "created_at": datetime.now().astimezone().isoformat(),
            "verified": True,
            "source": source_report,
            "backup": backup_report,
        }
        temp_json = temp_db.with_suffix(".verification.json")
        temp_json.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        incoming_db = destination / f".{name}.incoming"
        incoming_json = destination / f".{target.stem}.verification.json.incoming"
        try:
            shutil.copy2(temp_db, incoming_db)
            shutil.copy2(temp_json, incoming_json)
            installed_report = database_report(incoming_db)
            if installed_report["sha256"] != backup_report["sha256"]:
                raise RuntimeError("Google Drive'a kopyalama sırasında dosya özeti değişti.")
            os.replace(incoming_db, target)
            os.replace(incoming_json, target.with_suffix(".verification.json"))
        except Exception:
            incoming_db.unlink(missing_ok=True)
            incoming_json.unlink(missing_ok=True)
            raise

    final_report = database_report(target)
    if final_report["sha256"] != backup_report["sha256"]:
        raise RuntimeError("Google Drive yedeği son doğrulamadan geçemedi.")
    print(f"YEDEK_HAZIR={target}")
    print(f"SHA-256: {final_report['sha256']}")
    print("Google Drive eşitlemesi için yedek güvenle hazırlandı.")
    return target, final_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    app_dir = Path(
        os.environ.get("BUSINESSOS_APP_DIR", Path.home() / "Documents" / "BusinessOS")
    )
    if not (app_dir / "app.py").is_file():
        raise RuntimeError(f"BusinessOS uygulama klasörü bulunamadı: {app_dir}")
    create_backup(app_dir, args.check_only)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nHATA: {exc}", file=sys.stderr)
        raise SystemExit(1)
