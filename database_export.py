"""Read-only database exports for the web edition of Business OS.

The web application may use PostgreSQL while the desktop application uses
SQLite.  A PostgreSQL dump cannot be opened by the desktop application, so an
export is deliberately rebuilt as SQLite and checked before it is offered for
download.  This module never writes to the source connection.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

from sqlalchemy import MetaData, Numeric, LargeBinary, create_engine, inspect, select, text


PACKAGE_FORMAT = "business-os-sqlite-transfer-v1"
DOCUMENT_FOLDERS = (
    "order_documents",
    "customer_tax_documents",
    "earchive_inbox",
    "telegram_inbox",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value):
    """Stable, non-sensitive representation used only for verification hashes."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"sha256": hashlib.sha256(bytes(value)).hexdigest(), "bytes": len(value)}
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _digest(values) -> str:
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_canonical).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _table_metrics(connection, metadata: MetaData) -> dict:
    """Return row, identity, monetary and binary checks without retaining data."""
    metrics = {}
    for table in metadata.sorted_tables:
        rows = connection.execute(select(table)).mappings().all()
        primary_keys = [column.name for column in table.primary_key.columns]
        numeric_columns = [column.name for column in table.columns if isinstance(column.type, Numeric)]
        binary_columns = [column.name for column in table.columns if isinstance(column.type, LargeBinary)]
        identities = [[_canonical(row[column]) for column in primary_keys] for row in rows] if primary_keys else []
        financial = {}
        for column in numeric_columns:
            total = sum((Decimal(str(row[column])) for row in rows if row[column] is not None), Decimal("0"))
            financial[column] = format(total, "f")
        binary = {}
        for column in binary_columns:
            binary[column] = _digest([_canonical(row[column]) for row in rows if row[column] is not None])
        metrics[table.name] = {
            "rows": len(rows),
            "identity_sha256": _digest(identities),
            "financial_totals": financial,
            "binary_sha256": binary,
        }
    return metrics


def _copy_table_rows(source, destination, metadata: MetaData) -> None:
    for table in metadata.sorted_tables:
        result = source.execute(select(table)).mappings()
        while True:
            batch = result.fetchmany(500)
            if not batch:
                break
            destination.execute(table.insert(), [dict(row) for row in batch])


def _document_entries(instance_path: str | None):
    if not instance_path:
        return []
    root = Path(instance_path).resolve()
    files = []
    for folder in DOCUMENT_FOLDERS:
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(root)
                files.append((path, relative.as_posix()))
    return sorted(files, key=lambda item: item[1])


def _stored_file_entries(connection, metadata: MetaData):
    """Turn web-only StoredFile rows back into desktop document files."""
    table = metadata.tables.get("stored_file")
    required = {"storage_key", "content", "sha256", "size_bytes"}
    if table is None or not required.issubset(table.columns.keys()):
        return []
    entries = []
    for row in connection.execute(select(table)).mappings():
        key = str(row["storage_key"] or "").replace("\\", "/").strip("/")
        parts = key.split("/")
        if not key or parts[0] not in DOCUMENT_FOLDERS or any(part in {"", ".", ".."} for part in parts):
            continue
        content = bytes(row["content"])
        digest = hashlib.sha256(content).hexdigest()
        if digest != row["sha256"] or len(content) != row["size_bytes"]:
            raise RuntimeError("Aktarım doğrulaması başarısız oldu: saklanan belge özeti eşleşmiyor.")
        entries.append((key, content))
    return sorted(entries, key=lambda item: item[0])


@contextmanager
def _read_only_source(engine):
    """Open a source transaction that PostgreSQL itself enforces as read-only."""
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            if engine.dialect.name == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
            elif engine.dialect.name == "sqlite":
                connection.execute(text("PRAGMA query_only = ON"))
            yield connection
        finally:
            transaction.rollback()


def create_sqlite_transfer_package(source_engine, output_path, instance_path: str | None = None, metadata: MetaData | None = None) -> dict:
    """Create one verified ZIP package from a source engine without modifying it.

    The running application's metadata can be supplied to avoid PostgreSQL
    reflection differences. It includes binary StoredFile-style tables.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="businessos-sqlite-export-") as temporary:
        temporary_path = Path(temporary)
        sqlite_path = temporary_path / "business_os.db"
        documents_zip = temporary_path / "documents.zip"
        with _read_only_source(source_engine) as source:
            transfer_metadata = metadata or MetaData()
            if metadata is None:
                transfer_metadata.reflect(bind=source)
            if not transfer_metadata.tables:
                raise RuntimeError("Aktarılacak veritabanı tablosu bulunamadı.")
            source_metrics = _table_metrics(source, transfer_metadata)
            stored_file_entries = _stored_file_entries(source, transfer_metadata)
            target_engine = create_engine(f"sqlite:///{sqlite_path}")
            try:
                transfer_metadata.create_all(target_engine)
                with target_engine.begin() as destination:
                    destination.execute(text("PRAGMA foreign_keys = OFF"))
                    _copy_table_rows(source, destination, transfer_metadata)
                target_engine.dispose()
            finally:
                target_engine.dispose()

        target_engine = create_engine(f"sqlite:///{sqlite_path}")
        try:
            with target_engine.connect() as target:
                target_metrics = _table_metrics(target, transfer_metadata)
                foreign_key_errors = target.execute(text("PRAGMA foreign_key_check")).fetchall()
        finally:
            target_engine.dispose()
        if source_metrics != target_metrics:
            raise RuntimeError("Aktarım doğrulaması başarısız oldu: tablo verileri eşleşmiyor.")
        if foreign_key_errors:
            raise RuntimeError("Aktarım doğrulaması başarısız oldu: ilişki denetimi geçmedi.")
        integrity = sqlite3.connect(sqlite_path)
        try:
            if integrity.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Aktarım doğrulaması başarısız oldu: SQLite bütünlük denetimi geçmedi.")
        finally:
            integrity.close()

        document_manifest = []
        with zipfile.ZipFile(documents_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            added_documents = set()
            for relative_name, content in stored_file_entries:
                archive.writestr(relative_name, content)
                added_documents.add(relative_name)
                document_manifest.append({"path": relative_name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()})
            for path, relative_name in _document_entries(instance_path):
                if relative_name in added_documents:
                    continue
                archive.write(path, relative_name)
                document_manifest.append({"path": relative_name, "size": path.stat().st_size, "sha256": sha256_file(path)})
        manifest = {
            "format": PACKAGE_FORMAT,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "database": {"name": "business_os.db", "sha256": sha256_file(sqlite_path), "tables": source_metrics},
            "documents": document_manifest,
            "documents_archive_sha256": sha256_file(documents_zip),
        }
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(sqlite_path, "business_os.db")
            archive.write(documents_zip, "documents.zip")
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return manifest
