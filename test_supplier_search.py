"""Supplier selection uses the existing IDs and validates against an isolated DB."""
import os
import tempfile
import unittest
from unittest.mock import patch

_data = tempfile.TemporaryDirectory(prefix='businessos-supplier-test-')
os.environ['BUSINESSOS_DATA_DIR'] = _data.name
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
from app import app, db, Customer, AccountTransaction


class SupplierSearchTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        self.customer = Customer(name='Tahsilat Müşterisi', code='120.TEST')
        self.supplier = Customer(name='ABİKA MOBİLYA', code='320.TEST')
        db.session.add_all([self.customer, self.supplier])
        db.session.commit()
        self.client = app.test_client()
        self.fields = dict(customer_id=self.customer.id, amount='100', payment_method='Kredi Kartı',
                           card_installments='1', direct_to_supplier='on', direct_supplier_id=self.supplier.id)

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def test_both_forms_load_search_and_keep_native_select(self):
        for path in ('/tahsilat-girisi', f'/musteriler/{self.customer.id}/cari-hesap'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('name="direct_supplier_id"', html)
            self.assertIn('ABİKA MOBİLYA', html)
            self.assertIn('320.TEST', html)
            self.assertIn('supplier-search.js', html)
            self.assertIn('supplier-search.css', html)
        for asset in ('supplier-search.js', 'supplier-search.css'):
            with self.client.get('/static/' + asset) as response:
                self.assertEqual(response.status_code, 200)

    def test_selected_supplier_receives_linked_payment(self):
        with patch('app.create_database_backup'):
            response = self.client.post('/tahsilat-girisi', data=self.fields)
        self.assertEqual(response.status_code, 302)
        collection = AccountTransaction.query.filter_by(customer_id=self.customer.id).one()
        payment = AccountTransaction.query.filter_by(customer_id=self.supplier.id).one()
        self.assertEqual(collection.credit, 100)
        self.assertEqual(payment.debit, 100)
        self.assertEqual(collection.linked_transaction_id, payment.id)
        self.assertEqual(payment.linked_transaction_id, collection.id)

    def test_missing_invalid_and_self_supplier_write_nothing(self):
        for supplier_id in ('', 'ABİKA MOBİLYA', '9999999', self.customer.id):
            with patch('app.create_database_backup') as backup:
                response = self.client.post('/tahsilat-girisi', data=dict(self.fields, direct_supplier_id=supplier_id))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(AccountTransaction.query.count(), 0)
            self.assertNotIn('before_quick_collection', [call.args[1] for call in backup.call_args_list])

    def test_cash_collection_does_not_require_supplier(self):
        fields = dict(self.fields, payment_method='Nakit', direct_supplier_id='')
        fields.pop('direct_to_supplier')
        with patch('app.create_database_backup'):
            response = self.client.post('/tahsilat-girisi', data=fields)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AccountTransaction.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
