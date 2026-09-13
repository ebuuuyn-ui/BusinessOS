"""One-way, empty-target SQLite to SQLAlchemy database migration."""
import json
import hashlib
import mimetypes
import os
import sqlite3
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, JSON, Numeric, func, select, text


TARGET_ONLY_TABLES = {"stored_file", "migration_legacy_archive"}
FILE_ROOTS = {"order_documents", "customer_tax_documents", "telegram_inbox", "earchive_inbox"}


class MigrationError(RuntimeError):
    pass


def _source_snapshot(path, tables):
    source = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        checks = [row[0] for row in source.execute("PRAGMA integrity_check")]
    except Exception:
        source.close()
        raise
    if checks != ["ok"]:
        source.close()
        raise MigrationError("Yüklenen SQLite yedeğinin bütünlük kontrolü başarısız.")
    source_tables = {
        row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    counts, extra_columns = {}, {}
    for table in tables:
        if table.name not in source_tables:
            source.close()
            raise MigrationError(f"Kaynak yedekte gerekli tablo eksik: {table.name}")
        columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table.name}")')]
        missing = [column.name for column in table.columns if column.name not in columns]
        if missing:
            source.close()
            raise MigrationError(f"{table.name} tablosunda gerekli alanlar eksik: {', '.join(missing)}")
        extra = [column for column in columns if column not in table.columns]
        if extra:
            extra_columns[table.name] = extra
        counts[table.name] = source.execute(f'SELECT COUNT(*) FROM "{table.name}"').fetchone()[0]
    ignored_tables = {}
    known = {table.name for table in tables}
    for name in sorted(source_tables - known):
        count = source.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        if count:
            ignored_tables[name] = count
    return source, counts, extra_columns, ignored_tables


def _adapt(value, column):
    if value is None:
        return None
    kind = column.type
    if isinstance(kind, Boolean):
        return bool(value)
    if isinstance(kind, DateTime):
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if isinstance(kind, Date):
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    if isinstance(kind, Numeric):
        return value if isinstance(value, Decimal) else Decimal(str(value))
    if isinstance(kind, JSON) and isinstance(value, str):
        return json.loads(value)
    return value


def target_row_counts(connection, tables):
    return {
        table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
        for table in tables
    }


def _json_value(value):
    if isinstance(value, bytes):
        return {"encoding": "hex", "value": value.hex()}
    return value


def _legacy_rows(source, ignored_tables, extra_columns):
    rows = []
    for table_name in ignored_tables:
        cursor = source.execute(f'SELECT * FROM "{table_name}"')
        for index, row in enumerate(cursor, start=1):
            payload = {key: _json_value(row[key]) for key in row.keys()}
            rows.append({
                "source_table": table_name,
                "source_key": str(payload.get("id", index)),
                "payload": payload,
            })
    for table_name, column_names in extra_columns.items():
        columns = ["id", *column_names]
        quoted = ", ".join(f'"{name}"' for name in columns)
        condition = " OR ".join(f'"{name}" IS NOT NULL' for name in column_names)
        for row in source.execute(f'SELECT {quoted} FROM "{table_name}" WHERE {condition}'):
            payload = {name: _json_value(row[name]) for name in column_names}
            rows.append({
                "source_table": f"{table_name}.__extra_columns__",
                "source_key": str(row["id"]),
                "payload": payload,
            })
    return rows


def _file_rows(file_root):
    if not file_root:
        return []
    rows = []
    for root_name in sorted(FILE_ROOTS):
        directory = os.path.join(file_root, root_name)
        if not os.path.isdir(directory):
            continue
        for folder, _directories, filenames in os.walk(directory):
            for filename in sorted(filenames):
                path = os.path.join(folder, filename)
                with open(path, "rb") as handle:
                    content = handle.read()
                relative = os.path.relpath(path, file_root).replace(os.sep, "/")
                rows.append({
                    "storage_key": relative,
                    "content": content,
                    "mime_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                })
    return rows


def migrate_sqlite_database(source_path, engine, metadata, file_root=None):
    tables = [table for table in metadata.sorted_tables if table.name not in TARGET_ONLY_TABLES]
    source, source_counts, extra_columns, ignored_tables = _source_snapshot(source_path, tables)
    legacy_rows = _legacy_rows(source, ignored_tables, extra_columns)
    file_rows = _file_rows(file_root)
    try:
        with engine.begin() as target:
            if engine.dialect.name == "postgresql":
                target.execute(text("SELECT pg_advisory_xact_lock(7240920260913)"))
            before = target_row_counts(target, tables)
            occupied = {name: count for name, count in before.items() if count}
            if occupied:
                details = ", ".join(f"{name}={count}" for name, count in occupied.items())
                raise MigrationError(f"Neon veritabanı boş değil; aktarım durduruldu: {details}")

            for table in tables:
                cursor = source.execute(f'SELECT * FROM "{table.name}"')
                column_names = [column.name for column in table.columns]
                while True:
                    batch = cursor.fetchmany(250)
                    if not batch:
                        break
                    values = [
                        {column.name: _adapt(row[column.name], column) for column in table.columns}
                        for row in batch
                    ]
                    target.execute(table.insert(), values)

                primary_keys = list(table.primary_key.columns)
                if engine.dialect.name == "postgresql" and len(primary_keys) == 1:
                    key = primary_keys[0]
                    if getattr(key.type, "python_type", None) is int:
                        sequence = target.execute(
                            text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                            {"table_name": f'"{table.name}"', "column_name": key.name},
                        ).scalar()
                        if sequence:
                            maximum = source.execute(
                                f'SELECT MAX("{key.name}") FROM "{table.name}"'
                            ).fetchone()[0]
                            target.execute(
                                text("SELECT setval(CAST(:sequence AS regclass), :value, :called)"),
                                {"sequence": sequence, "value": maximum or 1, "called": maximum is not None},
                            )

            if legacy_rows:
                target.execute(metadata.tables["migration_legacy_archive"].insert(), legacy_rows)
            if file_rows:
                target.execute(metadata.tables["stored_file"].insert(), file_rows)

            after = target_row_counts(target, tables)
            mismatches = {
                name: {"source": source_counts[name], "target": after[name]}
                for name in source_counts if source_counts[name] != after[name]
            }
            if mismatches:
                raise MigrationError(f"Kayıt sayıları eşleşmedi; işlem geri alındı: {mismatches}")
            return {
                "source_counts": source_counts,
                "target_counts": after,
                "total_rows": sum(after.values()),
                "extra_columns": extra_columns,
                "ignored_tables": ignored_tables,
                "archived_legacy_rows": len(legacy_rows),
                "file_count": len(file_rows),
            }
    finally:
        source.close()
