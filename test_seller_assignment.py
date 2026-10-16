import os
import tempfile
import unittest
from io import BytesIO
from openpyxl import load_workbook

_data = tempfile.TemporaryDirectory()
os.environ.update(BUSINESSOS_DATA_DIR=_data.name, DATABASE_URL='sqlite:///:memory:')
import app as m
from seller_reports import load_owners, build_report


class SellerAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.context = m.app.app_context()
        self.context.push()
        m.db.drop_all()
        m.db.create_all()
        customer = m.Customer(name='Müşteri')
        self.order = m.Order(order_no='SS-TEST', customer=customer, order_type='Satış')
        self.order.items = [m.OrderItem(product_name='Koltuk', quantity=2, unit_price=100, vat_rate=10)]
        m.db.session.add(self.order)
        m.db.session.commit()
        self.client = m.app.test_client()

    def tearDown(self):
        m.db.session.remove()
        self.context.pop()

    def report(self, **filters):
        return build_report([self.order], load_owners(m.db), filters, str.casefold)

    def test_saved_assignment_updates_filter_totals_and_exports(self):
        created = self.order.created_at
        m.OrderSellerAssignment.__table__.drop(m.db.engine)
        self.assertEqual(self.report()['sales_rows'][0]['seller'], 'Bekir')
        response = self.client.post(f'/siparisler/{self.order.id}/satici', data={
            'assigned_seller': 'Ahmet', 'seller': 'Bekir', 'q': 'Müşteri', 'start_date': '2026-01-01'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('seller=Bekir', response.location)
        self.assertIn('#order-', response.location)
        self.assertEqual(self.order.created_at, created)
        self.assertEqual(self.report(seller='Ahmet')['sales_totals']['total'], self.order.total_amount)
        self.assertEqual(self.report(seller='Bekir')['sales_rows'], [])
        page = self.client.get('/siparisler/raporlar?report=seller&seller=Ahmet')
        self.assertIn(b'name="assigned_seller"', page.data)
        excel = self.client.get('/siparisler/raporlar?report=seller&seller=Ahmet&format=xlsx')
        workbook = load_workbook(BytesIO(excel.data))
        self.assertEqual(workbook['Sipariş Ayrıntıları']['C5'].value, 'Ahmet')
        pdf = self.client.get('/siparisler/raporlar?report=seller&seller=Ahmet&format=pdf')
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b'%PDF'))
        self.client.post(f'/siparisler/{self.order.id}/satici', data={'assigned_seller': 'Bekir'})
        self.assertEqual(self.report()['sales_rows'][0]['seller'], 'Bekir')
        self.assertEqual(m.OrderSellerAssignment.query.count(), 2)
        self.order.status = 'İptal Edildi'
        m.db.session.commit()
        self.assertEqual(self.report()['sales_rows'], [])

    def test_invalid_sellers_and_purchase_orders_are_rejected(self):
        endpoint = f'/siparisler/{self.order.id}/satici'
        self.assertEqual(self.client.post(endpoint, data={'assigned_seller': 'Başka'}).status_code, 400)
        self.order.order_type = 'Satın Alma'
        m.db.session.commit()
        self.assertEqual(self.client.post(endpoint, data={'assigned_seller': 'Ahmet'}).status_code, 400)
        self.assertEqual(m.OrderSellerAssignment.query.count(), 0)
