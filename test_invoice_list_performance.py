import unittest
from unittest.mock import patch
from sqlalchemy import event

from test_supplier_sheets_routes import SupplierRouteTests
import app as m
from test_web_auth import BASE
from invoice_order_allocation import linked_orders


class InvoiceListPerformanceTests(unittest.TestCase):
    setUp = SupplierRouteTests.setUp
    tearDown = SupplierRouteTests.tearDown

    def seed(self, count):
        second = m.Order(order_no='SA-SECOND', order_type='Satın Alma', customer_id=1339)
        second.items = [m.OrderItem(product_name='Chair', quantity=count, unit_price=100)]
        m.db.session.add(second)
        for i in range(count):
            invoice = m.Invoice(invoice_no=f'PERF-{i:03}', invoice_type='Satın Alma',
                                customer_id=1339, order_id=self.order_id if i % 2 else None)
            invoice.items = [m.InvoiceItem(product_name='Chair', quantity=2, unit_price=100,
                                          discount_rate=10, vat_rate=20, vat_included=bool(i % 2))]
            m.db.session.add(invoice)
            m.db.session.flush()
            m.db.session.add(m.InvoiceOrderAllocation(invoice=invoice, invoice_item=invoice.items[0],
                order=second, order_item=second.items[0], quantity=1))
        m.db.session.commit()

    def test_batched_register_preserves_html_and_does_not_write(self):
        self.seed(35)
        for path in ('/faturalar', '/faturalar?q=PERF-00', '/faturalar?customer_id=1339', '/faturalar?type=Satış'):
            m.db.session.remove()
            with patch('invoice_order_allocation.linked_orders_for_invoices',
                       side_effect=lambda invoices: {i.id: linked_orders(i) for i in invoices}):
                expected = self.client.get(path, base_url=BASE)
            m.db.session.remove()
            statements = []
            def record(conn, cursor, statement, parameters, context, executemany):
                statements.append(statement)
            event.listen(m.db.engine, 'before_cursor_execute', record)
            try:
                actual = self.client.get(path, base_url=BASE)
            finally:
                event.remove(m.db.engine, 'before_cursor_execute', record)
            self.assertEqual(actual.status_code, 200)
            self.assertEqual(actual.data, expected.data)
            self.assertLessEqual(len(statements), 10, '\n'.join(statements))
            self.assertFalse(any(s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for s in statements))


if __name__ == '__main__':
    unittest.main()
