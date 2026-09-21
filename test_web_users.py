"""Web account isolation against real app routes, using only in-memory test data."""
import os
import tempfile
import unittest
from unittest.mock import patch
from sqlalchemy import inspect, select
from werkzeug.security import check_password_hash

_data = tempfile.TemporaryDirectory(prefix="bos-web-users-tests-")
os.environ.update(BUSINESSOS_DATA_DIR=_data.name, BUSINESSOS_BACKUP_DIR=_data.name,
                  DATABASE_URL="sqlite:///:memory:")
import app as m
from web_users import users
from test_web_auth import ENV, BASE

STAFF_PASSWORD = "only-test-staff-password-123"
NEW_PASSWORD = "only-test-replacement-password-123"


class WebUserTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, ENV):
            self.app = m.create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
                                     "SECRET_KEY": ENV["SECRET_KEY"]})
        self.ctx = self.app.app_context()
        self.ctx.push()
        m.db.create_all()
        self.owner = self.app.test_client()
        self.staff = self.app.test_client()
        self.login(self.owner, ENV["BUSINESSOS_WEB_USERNAME"], ENV["BUSINESSOS_WEB_PASSWORD"])

    def tearDown(self):
        m.db.session.remove()
        m.db.engine.dispose()
        self.ctx.pop()

    def post(self, client, path, data=None):
        return client.post(path, base_url=BASE, headers={"Origin": BASE}, data=data or {})

    def get(self, client, path):
        return client.get(path, base_url=BASE)

    def login(self, client, username="staff", password=STAFF_PASSWORD):
        return self.post(client, "/giris", {"username": username, "password": password})

    def add_user(self):
        response = self.post(self.owner, "/kullanicilar", {"username": "staff",
            "password": STAFF_PASSWORD, "confirmation": STAFF_PASSWORD, "role": "owner"})
        self.assertEqual(response.status_code, 302)
        with m.db.engine.connect() as c:
            return c.execute(select(users.c.id)).scalar_one()

    def test_no_ddl_on_startup_owner_login_or_management_read(self):
        self.assertFalse(inspect(m.db.engine).has_table("web_user"))
        response = self.get(self.owner, "/kullanicilar")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Kullanıcı Oluştur", response.text)
        self.assertFalse(inspect(m.db.engine).has_table("web_user"))

    def test_account_creation_hashes_password_and_operator_menu(self):
        self.add_user()
        with m.db.engine.connect() as c:
            row = c.execute(select(users)).mappings().one()
        self.assertNotEqual(row["password_hash"], STAFF_PASSWORD)
        self.assertTrue(check_password_hash(row["password_hash"], STAFF_PASSWORD))
        self.assertEqual(self.login(self.staff).status_code, 302)
        page = self.get(self.staff, "/faturalar")
        self.assertEqual(page.status_code, 200)
        for forbidden in ("/sahsi-hesaplar", "/yedekler", "/kullanicilar", STAFF_PASSWORD, row["password_hash"]):
            self.assertNotIn(forbidden, page.text)
        owner_page = self.get(self.owner, "/faturalar")
        for link in ("/sahsi-hesaplar", "/yedekler", "/kullanicilar"):
            self.assertIn(link, owner_page.text)

    def test_all_private_routes_including_exports_and_admin_blocked(self):
        self.add_user()
        self.login(self.staff)
        import re
        rules = [r for r in self.app.url_map.iter_rules()
                 if r.rule.startswith(("/sahsi-hesaplar", "/yedekler", "/yonetim", "/kullanicilar"))]
        self.assertGreaterEqual(len(rules), 14)
        for rule in rules:
            path = re.sub(r"<int:[^>]+>", "1", rule.rule)
            path = re.sub(r"<path:[^>]+>", "test.db", path)
            for method in rule.methods - {"OPTIONS", "HEAD"}:
                r = self.staff.open(path, method=method, base_url=BASE, headers={"Origin": BASE})
                self.assertEqual(r.status_code, 403, (path, method))
        self.assertEqual(self.get(self.staff, "/%73ahsi-hesaplar?tab=ledger").status_code, 403)

    def test_can_create_and_edit_business_record(self):
        self.add_user()
        self.login(self.staff)
        # This is an actual business write, not just a navigation check.
        result = self.post(self.staff, "/musteriler", {"name": "Test company", "customer_type": "Müşteri"})
        self.assertEqual(result.status_code, 302)
        customer = m.Customer.query.filter_by(name="Test company").one()
        result = self.post(self.staff, f"/musteriler/{customer.id}/duzenle",
                           {"name": "Edited test company", "customer_type": "Müşteri"})
        self.assertEqual(result.status_code, 302)
        m.db.session.expire_all()
        self.assertEqual(m.db.session.get(m.Customer, customer.id).name, "Edited test company")

    def test_csrf_and_anonymous_cannot_create_account(self):
        payload = {"username": "staff", "password": STAFF_PASSWORD, "confirmation": STAFF_PASSWORD}
        self.assertEqual(self.post(self.staff, "/kullanicilar", payload).status_code, 401)
        self.assertEqual(self.owner.post("/kullanicilar", base_url=BASE, data=payload).status_code, 403)
        self.assertEqual(self.owner.post("/kullanicilar", base_url=BASE, data=payload,
            headers={"Origin": "https://other.test"}).status_code, 403)
        self.assertFalse(inspect(m.db.engine).has_table("web_user"))

    def test_duplicate_reserved_and_invalid_names_passwords(self):
        self.add_user()
        for name, password, confirmation in [
            ("STAFF", STAFF_PASSWORD, STAFF_PASSWORD),
            (ENV["BUSINESSOS_WEB_USERNAME"].upper(), STAFF_PASSWORD, STAFF_PASSWORD),
            ("../bad", STAFF_PASSWORD, STAFF_PASSWORD),
            ("staff2", "short", "short"),
            ("staff2", STAFF_PASSWORD, "different"),
        ]:
            r = self.post(self.owner, "/kullanicilar", {"username": name,
                "password": password, "confirmation": confirmation})
            self.assertEqual(r.status_code, 400, name)
            self.assertNotIn(STAFF_PASSWORD, r.text)
        with m.db.engine.connect() as c:
            self.assertEqual(len(c.execute(select(users.c.id)).all()), 1)

    def test_disable_revokes_existing_session_and_blocks_login(self):
        user_id = self.add_user()
        self.login(self.staff)
        cookie = self.staff.get_cookie("__Host-businessos", domain="example.test").value
        self.post(self.owner, f"/kullanicilar/{user_id}/durum", {"active": "0"})
        self.assertEqual(self.get(self.staff, "/faturalar").location, "/giris")
        self.assertEqual(self.login(self.staff).status_code, 401)
        self.post(self.owner, f"/kullanicilar/{user_id}/durum", {"active": "1"})
        self.staff.set_cookie("__Host-businessos", cookie, domain="example.test")
        self.assertEqual(self.get(self.staff, "/faturalar").location, "/giris")
        self.assertEqual(self.login(self.staff).status_code, 302)

    def test_password_change_revokes_sessions(self):
        user_id = self.add_user()
        self.login(self.staff)
        self.post(self.owner, f"/kullanicilar/{user_id}/sifre",
                  {"password": NEW_PASSWORD, "confirmation": NEW_PASSWORD})
        self.assertEqual(self.get(self.staff, "/faturalar").location, "/giris")
        self.assertEqual(self.login(self.staff).status_code, 401)
        self.assertEqual(self.login(self.staff, password=NEW_PASSWORD).status_code, 302)

    def test_five_bad_passwords_temporarily_lock_user(self):
        self.add_user()
        for _ in range(5):
            self.assertEqual(self.login(self.staff, password="wrong").status_code, 401)
        self.assertEqual(self.login(self.staff).status_code, 401)

    def test_operator_cannot_grant_access_or_change_other_users(self):
        user_id = self.add_user()
        self.login(self.staff)
        for path in ("/kullanicilar", f"/kullanicilar/{user_id}/durum", f"/kullanicilar/{user_id}/sifre"):
            self.assertEqual(self.post(self.staff, path, {"active": "1", "role": "owner"}).status_code, 403)
        self.assertEqual(self.get(self.staff, "/sahsi-hesaplar").status_code, 403)


if __name__ == "__main__":
    unittest.main()
