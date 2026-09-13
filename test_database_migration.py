import tempfile
import unittest
import sqlite3
import hashlib
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine, func, select

from app import db
from database_migration import MigrationError, migrate_sqlite_database


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="businessos-migration-test-")
        self.source_path = Path(self.folder.name) / "source.db"
        self.target_path = Path(self.folder.name) / "target.db"
        self.source = create_engine(f"sqlite:///{self.source_path}")
        self.target = create_engine(f"sqlite:///{self.target_path}")
        db.metadata.create_all(self.source)
        db.metadata.create_all(self.target)
        customer = db.metadata.tables["customer"]
        product = db.metadata.tables["product"]
        order = db.metadata.tables["order"]
        item = db.metadata.tables["order_item"]
        with self.source.begin() as connection:
            connection.execute(customer.insert(), [{"id": 7, "name": "Aktarım Müşterisi", "created_at": datetime(2026, 9, 13, 10, 0)}])
            connection.execute(product.insert(), [{"id": 11, "name": "Ürün", "unit": "Adet", "unit_price": 12.5, "purchase_price": 8, "include_in_catalog": False, "include_in_price_list": False, "active": True, "created_at": datetime(2026, 9, 13, 10, 0)}])
            connection.execute(order.insert(), [{"id": 13, "order_no": "TEST-13", "order_type": "Satış", "customer_id": 7, "order_date": date(2026, 9, 13), "status": "Bekliyor", "created_at": datetime(2026, 9, 13, 10, 0), "updated_at": datetime(2026, 9, 13, 10, 0)}])
            connection.execute(item.insert(), [{"id": 17, "order_id": 13, "product_id": 11, "product_name": "Ürün", "quantity": 2, "unit": "Adet", "unit_price": 12.5, "vat_included": False, "discount_rate": 0, "vat_rate": 0}])

    def tearDown(self):
        self.source.dispose()
        self.target.dispose()
        self.folder.cleanup()

    def test_atomic_empty_target_migration_and_duplicate_guard(self):
        document = Path(self.folder.name) / "order_documents" / "13" / "document.pdf"
        document.parent.mkdir(parents=True)
        document.write_bytes(b"test document")
        with sqlite3.connect(self.source_path) as source:
            source.execute("CREATE TABLE retired_table (id INTEGER PRIMARY KEY, value TEXT)")
            source.execute("INSERT INTO retired_table VALUES (1, 'preserve me')")
        result = migrate_sqlite_database(self.source_path, self.target, db.metadata, file_root=self.folder.name)
        self.assertEqual(result["target_counts"]["customer"], 1)
        self.assertEqual(result["target_counts"]["order_item"], 1)
        self.assertEqual(result["source_counts"], result["target_counts"])
        self.assertEqual(result["archived_legacy_rows"], 1)
        self.assertEqual(result["file_count"], 1)
        with self.target.connect() as connection:
            stored = connection.execute(select(db.metadata.tables["stored_file"])).mappings().one()
            self.assertEqual(stored["storage_key"], "order_documents/13/document.pdf")
            self.assertEqual(stored["sha256"], hashlib.sha256(b"test document").hexdigest())
            legacy = connection.execute(select(db.metadata.tables["migration_legacy_archive"])).mappings().one()
            self.assertEqual(legacy["payload"]["value"], "preserve me")
        with self.assertRaisesRegex(MigrationError, "boş değil"):
            migrate_sqlite_database(self.source_path, self.target, db.metadata)

    def test_corrupt_source_writes_nothing(self):
        bad = Path(self.folder.name) / "bad.db"
        bad.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            migrate_sqlite_database(bad, self.target, db.metadata)
        with self.target.connect() as connection:
            customer = db.metadata.tables["customer"]
            self.assertEqual(connection.execute(select(func.count()).select_from(customer)).scalar_one(), 0)


if __name__ == "__main__":
    unittest.main()
