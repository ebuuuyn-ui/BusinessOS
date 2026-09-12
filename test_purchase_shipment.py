"""Purchase shipment snapshots, tested without touching the live database."""
import os
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch

_data = tempfile.TemporaryDirectory(prefix='businessos-shipment-test-')
os.environ['BUSINESSOS_DATA_DIR'] = _data.name
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
from app import app, db, Customer, Order, OrderItem
from openpyxl import load_workbook


class PurchaseShipmentTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        self.customer = Customer(name='Teslimat Müşterisi', shipment_contact='ARDA ÇANCIĞLU', shipment_phone='0 532 295 43 82', shipment_city='İSTANBUL', shipment_address='KAVACIK MAH. ÖZGÜR CAD. NO:23\nBEYKOZ-İSTANBUL', shipment_note='=Kapı <B> & teslimat')
        self.supplier = Customer(name='Tedarikçi', shipment_address='Tedarikçinin farklı adresi')
        self.sale = Order(customer=self.customer, order_no='SS-TEST', order_type='Satış', status='Bekliyor')
        self.sale.items.append(OrderItem(product_name='Stoksuz hizmet', quantity=1, unit_price=100, vat_rate=10))
        db.session.add_all([self.sale, self.supplier])
        db.session.commit()
        self.client = app.test_client()
        self.fields = {name: getattr(self.customer, name) for name in ('shipment_contact', 'shipment_phone', 'shipment_address', 'shipment_note')}
        self.fields['delivery_city'] = self.customer.shipment_city

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def convert(self, shipment=None):
        data = {'customer_id': self.supplier.id, 'source_item_id[]': str(self.sale.items[0].id)}
        data.update(shipment or {})
        with patch('app.create_database_backup'):
            response = self.client.post(f'/siparisler/{self.sale.id}/satinalmaya-donustur', data=data)
        self.assertEqual(response.status_code, 302)
        return Order.query.filter_by(order_type='Satın Alma').one()

    def test_conversion_is_opt_in(self):
        html = self.client.get(f'/siparisler/{self.sale.id}/satinalmaya-donustur').get_data(as_text=True)
        self.assertIn('Sevkiyat Bilgilerini Ekle', html)
        self.assertIn(f'data-source-id="{self.customer.id}"', html)
        purchase = self.convert()
        self.assertFalse(purchase.shipment_address)
        self.assertFalse(purchase.shipment_contact)
        self.assertEqual(purchase.customer_id, self.supplier.id)

    def test_conversion_snapshot_details_and_exports(self):
        purchase = self.convert(self.fields)
        self.customer.shipment_address = 'Sonradan değişen cari adresi'
        db.session.commit()
        for key, value in self.fields.items():
            self.assertEqual(getattr(purchase, key), value)
        self.assertEqual(purchase.customer_id, self.supplier.id)
        html = self.client.get(f'/siparisler/{purchase.id}').get_data(as_text=True)
        self.assertIn('Sevkiyat Bilgileri', html)
        self.assertIn('BEYKOZ-İSTANBUL', html)
        self.assertIn('&lt;B&gt; &amp; teslimat', html)
        excel = self.client.get(f'/siparisler/{purchase.id}/excel')
        self.assertEqual(excel.status_code, 200)
        sheet = load_workbook(BytesIO(excel.data)).active
        values = [cell.value for row in sheet for cell in row]
        for value in self.fields.values():
            self.assertIn(value, values)
        self.assertEqual(sheet['G8'].data_type, 's')
        self.assertEqual(sheet['B11'].value, 'Stoksuz hizmet')
        pdf = self.client.get(f'/siparisler/{purchase.id}/pdf')
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.data.startswith(b'%PDF'))

    def test_new_and_edit_purchase(self):
        data = {'order_type': 'Satın Alma', 'customer_id': self.supplier.id, 'product_name[]': 'Hizmet', 'quantity[]': '1', 'unit_price[]': '100', **self.fields}
        response = self.client.post('/siparisler/yeni', data=data)
        self.assertEqual(response.status_code, 302)
        purchase = Order.query.filter_by(order_type='Satın Alma').one()
        self.assertEqual(purchase.shipment_address, self.fields['shipment_address'])
        edit = self.client.get(f'/siparisler/{purchase.id}/duzenle').get_data(as_text=True)
        self.assertIn('Sevkiyat Bilgilerini Ekle', edit)
        self.assertIn('BEYKOZ-İSTANBUL', edit)
        data['shipment_address'] = 'Siparişe özel yeni adres'
        with patch('app.create_database_backup'):
            response = self.client.post(f'/siparisler/{purchase.id}/duzenle', data=data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(purchase.shipment_address, 'Siparişe özel yeni adres')
        self.assertEqual(self.customer.shipment_address, self.fields['shipment_address'])


if __name__ == '__main__':
    unittest.main()
