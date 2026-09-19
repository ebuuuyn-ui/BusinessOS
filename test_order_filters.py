"""Order text filters work in SQLite and compile for the hosted PostgreSQL app."""
import os
import tempfile
import unittest
from sqlalchemy.dialects import postgresql

_data = tempfile.TemporaryDirectory(prefix="businessos-order-filter-test-")
os.environ["BUSINESSOS_DATA_DIR"] = _data.name
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import Customer, Order, OrderItem, app, apply_order_text_filters, db


class OrderFilterTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        customer = Customer(name="Örnek Cari", code="CR-100")
        order = Order(customer=customer, order_no="SA-2026-00052", order_type="Satın Alma")
        order.items.append(OrderItem(product_name="Deneme ürünü", quantity=1, unit_price=10, vat_rate=10))
        db.session.add(order)
        db.session.commit()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def test_order_number_filter_and_exports_return_successfully(self):
        response = self.client.get("/siparisler?q=00052")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SA-2026-00052", response.data)
        for path in ("/siparisler/excel?q=00052", "/siparisler/excel/ayrintili?q=00052"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.data.startswith(b"PK"))

    def test_hosted_database_filter_does_not_call_sqlite_only_function(self):
        records = apply_order_text_filters(
            Order.query.join(Customer), query="00052", customer_query="ornek", dialect_name="postgresql"
        )
        statement = str(records.statement.compile(dialect=postgresql.dialect()))
        self.assertIn("ILIKE", statement)
        self.assertNotIn("normalize_tr", statement)

    def test_orders_page_lists_fifty_records_before_paginating(self):
        customer = Customer.query.one()
        for index in range(50):
            db.session.add(Order(
                customer=customer,
                order_no=f"SA-2026-PAGE-{index:02d}",
                order_type="Satış",
            ))
        db.session.commit()

        first_page = self.client.get("/siparisler")
        second_page = self.client.get("/siparisler?page=2")

        self.assertEqual(first_page.data.count(b'class="order-summary-row"'), 50)
        self.assertIn("51 kayıt · 1 / 2".encode(), first_page.data)
        self.assertEqual(second_page.data.count(b'class="order-summary-row"'), 1)


if __name__ == "__main__":
    unittest.main()
