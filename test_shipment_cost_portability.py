"""Shipment costs work without SQLite's optional normalize_tr SQL function."""
import os
import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import event

_data = tempfile.TemporaryDirectory(prefix="businessos-shipment-cost-test-")
os.environ.update(BUSINESSOS_DATA_DIR=_data.name, BUSINESSOS_BACKUP_DIR=_data.name,
                  DATABASE_URL="sqlite:///:memory:")
import app as m


class ShipmentCostPortabilityTests(unittest.TestCase):
    def setUp(self):
        self.ctx = m.app.app_context()
        self.ctx.push()
        m.db.drop_all()
        m.db.create_all()
        self.customer = m.Customer(name="SSH test carisi")
        m.db.session.add(self.customer)
        m.db.session.flush()
        self.client = m.app.test_client()
        self.number = 0
        # Fail on the exact nonportable query even though SQLite registers it.
        # PostgreSQL production logs show UndefinedFunction for this expression.
        def reject_sqlite_function(conn, cursor, statement, parameters, context, many):
            if "normalize_tr(" in statement.lower():
                raise RuntimeError("normalize_tr SQL function is unavailable")
        self.reject_sqlite_function = reject_sqlite_function
        event.listen(m.db.engine, "before_cursor_execute", self.reject_sqlite_function)

    def tearDown(self):
        event.remove(m.db.engine, "before_cursor_execute", self.reject_sqlite_function)
        m.db.session.remove()
        self.ctx.pop()

    def order(self, kind="Satış", name="Cotto oturak minderi gri", price="0", cost="0",
              status="Bekliyor", realized=None, **item_values):
        self.number += 1
        order = m.Order(order_no=f"TEST-{self.number}", customer=self.customer,
                        order_type=kind, status=status)
        order.items.append(m.OrderItem(product_name=name, quantity=1,
            unit_price=Decimal(price), cost_unit_price=Decimal(cost), vat_rate=0,
            **item_values))
        if realized:
            order.history.append(m.OrderHistory(status=status, created_at=realized))
        m.db.session.add(order)
        m.db.session.commit()
        return order

    def ship(self, order, status="Sevk Edildi"):
        response = self.client.post(f"/siparisler/{order.id}/durum",
            data={"status": status, "return_to": "/siparisler"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/siparisler")
        m.db.session.expire_all()
        self.assertEqual(order.status, status)

    def test_free_ssh_without_purchase_can_ship_and_deliver(self):
        order = self.order()
        for status in ("Sevk Edildi", "Teslim Edildi"):
            self.ship(order, status)
            self.assertEqual(order.items[0].cost_unit_price, 0)
            self.assertEqual(order.total_amount, 0)
        self.assertEqual([h.status for h in reversed(order.history)],
                         ["Sevk Edildi", "Teslim Edildi"])
        self.ship(order, "Teslim Edildi")
        self.assertEqual(len(order.history), 2)

    def test_paid_sale_without_purchase_also_remains_valid(self):
        order = self.order(price="500")
        self.ship(order)
        self.assertEqual(order.items[0].cost_unit_price, 0)
        self.assertEqual(order.total_amount, 500)

    def test_latest_zero_purchase_is_not_replaced_by_older_cost(self):
        self.order(kind="Satın Alma", price="100", status="Teslim Edildi",
                   realized=datetime(2026, 9, 1))
        self.order(kind="Satın Alma", price="0", status="Teslim Edildi",
                   realized=datetime(2026, 9, 2))
        order = self.order()
        self.ship(order)
        self.assertEqual(order.items[0].cost_unit_price, 0)

    def test_turkish_name_matching_cutoff_and_vat_discount_are_preserved(self):
        purchase = self.order(kind="Satın Alma", name="ÇİZGİ MİNDERİ GRİ", price="110",
            status="Teslim Edildi", realized=datetime(2026, 9, 1), discount_rate=10,
            vat_included=True)
        purchase.items[0].vat_rate = 10
        m.db.session.commit()
        self.order(kind="Satın Alma", name="çizgi minderi gri", price="200",
                   status="Teslim Edildi", realized=datetime(2026, 9, 20))
        self.order(kind="Satın Alma", name="çizgi minderi gri", price="999")
        order = self.order(name="cizgi minderi gri")
        self.assertEqual(m.latest_delivered_purchase_cost(order.items[0], date(2026, 9, 10)), 90)
        self.ship(order)
        self.assertEqual(order.items[0].cost_unit_price, 200)

    def test_existing_cost_is_preserved(self):
        self.order(kind="Satın Alma", price="999", status="Teslim Edildi")
        order = self.order(cost="50")
        self.ship(order)
        self.assertEqual(order.items[0].cost_unit_price, 50)

    def test_linked_product_matches_by_id_not_name(self):
        p = m.Product(name="Test stok", code="TEST")
        m.db.session.add(p)
        m.db.session.commit()
        self.order(kind="Satın Alma", name="Eski ad", price="123", product_id=p.id,
                   status="Teslim Edildi")
        self.order(kind="Satın Alma", price="999", status="Teslim Edildi")
        order = self.order(product_id=p.id)
        self.ship(order)
        self.assertEqual(order.items[0].cost_unit_price, 123)


if __name__ == "__main__":
    unittest.main()
