"""SQLite transfer packages are verified using isolated, disposable databases."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from sqlalchemy import Column, ForeignKey, Integer, LargeBinary, MetaData, Numeric, String, Table, create_engine

from database_export import PACKAGE_FORMAT, create_sqlite_transfer_package


class SQLiteTransferPackageTests(unittest.TestCase):
    def test_preserves_rows_ids_money_binary_files_and_relationships(self):
        source = create_engine("sqlite:///:memory:")
        metadata = MetaData()
        customer = Table("customer", metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String, nullable=False),
            Column("balance", Numeric(14, 2), nullable=False),
        )
        stored_file = Table("stored_file", metadata,
            Column("id", Integer, primary_key=True),
            Column("customer_id", Integer, ForeignKey("customer.id"), nullable=False),
            Column("storage_key", String, nullable=False),
            Column("content", LargeBinary, nullable=False),
            Column("sha256", String, nullable=False),
            Column("size_bytes", Integer, nullable=False),
        )
        metadata.create_all(source)
        file_content = b"Business OS test document\x00"
        with source.begin() as connection:
            connection.execute(customer.insert(), [{"id": 41, "name": "Test Cari", "balance": Decimal("1250.75")}])
            connection.execute(stored_file.insert(), [{
                "id": 73, "customer_id": 41, "storage_key": "order_documents/41/belge.pdf", "content": file_content,
                "sha256": hashlib.sha256(file_content).hexdigest(), "size_bytes": len(file_content),
            }])

        with tempfile.TemporaryDirectory(prefix="businessos-export-test-") as temporary:
            root = Path(temporary)
            document = root / "order_documents" / "41" / "makbuz.pdf"
            document.parent.mkdir(parents=True)
            document.write_bytes(b"external document")
            package = root / "transfer.zip"
            manifest = create_sqlite_transfer_package(source, package, root)

            self.assertEqual(manifest["format"], PACKAGE_FORMAT)
            self.assertEqual(manifest["database"]["tables"]["customer"]["rows"], 1)
            self.assertEqual(manifest["database"]["tables"]["stored_file"]["rows"], 1)
            self.assertEqual(manifest["database"]["tables"]["stored_file"]["financial_totals"], {})
            self.assertEqual(manifest["database"]["tables"]["customer"]["financial_totals"]["balance"], "1250.75")
            self.assertEqual(
                next(item["sha256"] for item in manifest["documents"] if item["path"] == "order_documents/41/makbuz.pdf"),
                hashlib.sha256(b"external document").hexdigest(),
            )

            with zipfile.ZipFile(package) as archive:
                self.assertEqual(set(archive.namelist()), {"business_os.db", "documents.zip", "manifest.json"})
                self.assertEqual(json.loads(archive.read("manifest.json"))["database"]["sha256"], manifest["database"]["sha256"])
                archive.extract("business_os.db", root)
                documents = zipfile.ZipFile(__import__("io").BytesIO(archive.read("documents.zip")))
                self.assertEqual(documents.read("order_documents/41/makbuz.pdf"), b"external document")
                self.assertEqual(documents.read("order_documents/41/belge.pdf"), file_content)

            target = sqlite3.connect(root / "business_os.db")
            try:
                self.assertEqual(target.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(target.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(target.execute("SELECT id, customer_id, content FROM stored_file").fetchone(), (73, 41, file_content))
                self.assertEqual(target.execute("SELECT balance FROM customer WHERE id = 41").fetchone()[0], 1250.75)
            finally:
                target.close()


if __name__ == "__main__":
    unittest.main()
