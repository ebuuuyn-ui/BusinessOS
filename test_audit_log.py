"""Isolated audit tests. Optional PGLITE_MODULE runs generated SQL in real WASM PostgreSQL."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from flask import Flask, g, session
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects.postgresql import dialect
from audit_log import bind_actor, trigger_sql, SCHEMA_SQL


class AuditContextTests(unittest.TestCase):
    def setUp(self):
        self.callbacks = {}
        def listen(engine, name):
            def decorator(fn):
                self.callbacks[name] = fn
                return fn
            return decorator
        with patch("audit_log.event.listens_for", side_effect=listen):
            bind_actor(object())
        self.connection = SimpleNamespace(info={})
        self.cursor = Mock()
        self.context = SimpleNamespace(isinsert=True, isupdate=False, isdelete=False)
        self.app = Flask(__name__)
        self.app.secret_key = "test-only"

    def attach(self, sql="INSERT INTO customer VALUES (1)", context=None):
        self.callbacks["before_cursor_execute"](self.connection, self.cursor, sql, {},
                                               context or self.context, False)

    def test_actor_changes_between_requests_and_transactions(self):
        with self.app.test_request_context("/change", method="POST"):
            g.web_username = "test-owner"; g.web_is_owner = True
            self.attach()
            values = self.cursor.execute.call_args.args[1]
            self.assertEqual(values[:2], ("owner", "test-owner"))
            self.attach()
            self.assertEqual(self.cursor.execute.call_count, 1)
        self.callbacks["begin"](self.connection)
        with self.app.test_request_context("/change", method="POST"):
            g.web_username = "test-staff"; g.web_is_owner = False
            session["web_user_id"] = 7
            self.attach()
            values = self.cursor.execute.call_args.args[1]
            self.assertEqual(values[:2], ("user:7", "test-staff"))
            self.assertNotIn("password", str(values))
        self.callbacks["begin"](self.connection)
        self.attach()
        self.assertEqual(self.cursor.execute.call_args.args[1][0], "system")

    def test_savepoint_rollback_restores_context_on_next_write(self):
        with self.app.test_request_context("/change"):
            g.web_username = "owner"; g.web_is_owner = True
            self.attach()
            self.callbacks["rollback_savepoint"](self.connection, "test", None)
            self.attach()
        self.assertEqual(self.cursor.execute.call_count, 2)

    def test_reads_do_not_add_roundtrips(self):
        self.attach("SELECT * FROM customer", SimpleNamespace(isinsert=False, isupdate=False, isdelete=False))
        self.cursor.execute.assert_not_called()

    def test_file_and_credential_fields_not_projected(self):
        sql = trigger_sql("example", ["id", "name", "password_hash", "session_token", "content", "payload"], ["id"])
        for field in ("password_hash", "session_token", "content", "payload"):
            self.assertNotIn('."' + field + '"', sql)
        self.assertIn('NEW."name"', sql)

    @unittest.skipUnless(os.environ.get("PGLITE_MODULE"), "Optional test-only PostgreSQL WASM runtime not configured")
    def test_postgres_triggers(self):
        with tempfile.TemporaryDirectory(prefix="bos-audit-fixture-") as directory:
            with patch.dict(os.environ, {"BUSINESSOS_DATA_DIR": directory,
                    "BUSINESSOS_BACKUP_DIR": directory, "DATABASE_URL": "sqlite:///:memory:"}):
                import app as m
            from web_users import users
            tables = list(m.db.metadata.sorted_tables) + [users]
            fixture = {"schema": SCHEMA_SQL,
                "create": "\n".join(str(CreateTable(t).compile(dialect=dialect())) + ";" for t in tables),
                "triggers": [trigger_sql(t.name, [c.name for c in t.columns],
                                        [c.name for c in t.primary_key]) for t in tables]}
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps(fixture))
            result = subprocess.run(["node", str(Path(__file__).with_name("test_audit_postgres.mjs")), str(path)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
