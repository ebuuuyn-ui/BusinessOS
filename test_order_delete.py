"""Deletion checks use only an isolated in-memory database."""
import re
import unittest
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
import app as m


class OrderDeleteTests(unittest.TestCase):
    setUp = SupplierRouteTests.setUp
    tearDown = SupplierRouteTests.tearDown

    def preview(self):
        self.path = f'/siparisler/{self.order_id}/sil'
        return self.client.get(self.path, base_url=BASE)

    def submit(self, token):
        return self.client.post(self.path, base_url=BASE, headers={'Origin': BASE},
                                data={'confirmation': token, 'ack': 'yes'})

    def test_detail_button_and_confirmed_deletion(self):
        detail = self.client.get(f'/siparisler/{self.order_id}', base_url=BASE)
        self.assertIn('Siparişi Sil', detail.text)
        page = self.preview()
        self.assertIsNotNone(m.db.session.get(m.Order, self.order_id))
        token = re.search(r'name="confirmation" value="([^"]+)"', page.text)[1]
        self.assertEqual(self.submit(token).status_code, 302)
        self.assertIsNone(m.db.session.get(m.Order, self.order_id))
        self.assertEqual(m.OrderItem.query.count(), 0)

    def test_invoice_added_after_preview_blocks_deletion(self):
        page = self.preview()
        token = re.search(r'name="confirmation" value="([^"]+)"', page.text)[1]
        m.db.session.add(m.Invoice(invoice_no='TEST', invoice_type='Alış', customer_id=1339, order_id=self.order_id))
        m.db.session.commit()
        result = self.submit(token)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(m.Invoice.query.count(), 1)
        self.assertIsNotNone(m.db.session.get(m.Order, self.order_id))

    def test_linked_purchase_blocks_deletion(self):
        m.db.session.add(m.Order(order_no='CHILD', order_type='Satın Alma', customer_id=1339, source_order_id=self.order_id))
        m.db.session.commit()
        self.assertIn('satın alma siparişi var', self.preview().text)
        self.assertNotIn('Evet, Siparişi Sil', self.preview().text)

    def test_missing_confirmation_and_anonymous_access(self):
        self.preview()
        self.assertEqual(self.submit('invalid').status_code, 400)
        self.assertEqual(self.app.test_client().post(self.path, base_url=BASE).status_code, 401)
        self.assertIsNotNone(m.db.session.get(m.Order, self.order_id))


if __name__ == '__main__':
    unittest.main()
