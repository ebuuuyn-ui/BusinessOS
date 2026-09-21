"""Idle connection recovery, using an isolated file database, never live data."""
import os
import tempfile
import unittest
from pathlib import Path
from sqlalchemy import text

_data = tempfile.TemporaryDirectory(prefix="businessos-connection-test-")
os.environ.update(BUSINESSOS_DATA_DIR=_data.name, BUSINESSOS_BACKUP_DIR=_data.name,
                  DATABASE_URL="sqlite:///:memory:")
import app as m


class ConnectionRecoveryTests(unittest.TestCase):
    def test_closed_idle_connection_is_replaced_before_business_query(self):
        with tempfile.TemporaryDirectory(prefix="businessos-dead-connection-") as folder:
            app = m.create_app({"TESTING": True, "WEB_AUTH_ENABLED": False,
                "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(Path(folder) / "test.db")})
            with app.app_context():
                engine = m.db.engine
                with engine.begin() as connection:
                    connection.execute(text("CREATE TABLE recovery_probe (value INTEGER)"))
                    connection.execute(text("INSERT INTO recovery_probe VALUES (42)"))
                with engine.connect() as connection:
                    original = connection.connection.driver_connection
                    self.assertEqual(connection.scalar(text("SELECT value FROM recovery_probe")), 42)
                # Simulate the database closing an idle pooled connection.
                original.close()
                with engine.connect() as connection:
                    self.assertIsNot(connection.connection.driver_connection, original)
                    self.assertEqual(connection.scalar(text("SELECT value FROM recovery_probe")), 42)
                m.db.session.remove()
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
