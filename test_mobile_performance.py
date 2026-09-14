import unittest

from app import Customer, create_app, db


class MobilePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WEB_AUTH_ENABLED": False,
        })
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        db.session.add_all(Customer(name=f"Cari {index:04d}") for index in range(125))
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def test_customer_cards_are_paginated(self):
        response = self.client.get("/musteriler?page=2")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("125 kayıt · 2/3. sayfa", body)
        self.assertEqual(body.count('class="record-card"'), 50)
        self.assertIn("Cari 0050", body)
        self.assertNotIn("Cari 0000", body)

    def test_customer_search_still_scans_all_records(self):
        response = self.client.get("/musteriler?q=Cari+0124")
        body = response.get_data(as_text=True)
        self.assertIn("1 kayıt · 1/1. sayfa", body)
        self.assertIn("Cari 0124", body)

    def test_mobile_view_skips_duplicate_workspace_iframe(self):
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        mobile_guard = body.index("window.matchMedia('(max-width: 900px)').matches")
        workspace_open = body.index("open(window.location.href,pageTitle())")
        self.assertLess(mobile_guard, workspace_open)


if __name__ == "__main__":
    unittest.main()
