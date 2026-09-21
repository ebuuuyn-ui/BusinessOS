"""SSH lines survive invoice creation/edit/delete without touching stock."""
import os
import tempfile
import unittest
from decimal import Decimal
from werkzeug.datastructures import MultiDict

_data = tempfile.TemporaryDirectory(prefix='bos-ssh-invoice-tests-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name, BUSINESSOS_BACKUP_DIR=_data.name,
                  DATABASE_URL='sqlite:///:memory:')
import app as m
from invoice_ssh import allows_nonstock_rows, is_ssh_order_item, prepare_nonstock_rows


class InvoiceSSHTests(unittest.TestCase):
    def setUp(self):
        self.ctx=m.app.app_context(); self.ctx.push()
        m.db.drop_all(); m.db.create_all()
        self.customer=m.Customer(name='Test cari')
        self.product=m.Product(name='Koltuk',code='P-1',unit='Adet',active=True)
        self.order=m.Order(order_no='SS-TEST',customer=self.customer,order_type='Satış',status='Sevk Edildi')
        self.order.items=[m.OrderItem(product=self.product,product_name='Koltuk',quantity=3,
                                     unit_price=7000,discount_rate=45,vat_rate=10),
                          m.OrderItem(product_name='SSH gri minder',quantity=3,unit_price=0,vat_rate=10,unit='Adet')]
        m.db.session.add_all([self.product,self.order]);m.db.session.commit()
        self.client=m.app.test_client()

    def tearDown(self):
        m.db.session.remove();self.ctx.pop()

    def payload(self):
        return MultiDict([('invoice_no','TEST-SSH'),('invoice_type','Satış'),
            ('customer_ref',self.customer.name),('order_ref',self.order.order_no),
            ('invoice_date','2026-09-21'),('due_date','2026-10-21'),
            ('product_ref[]','P-1 · Koltuk'),('product_ref[]','SSH gri minder'),
            ('row_kind[]','stock'),('row_kind[]','ssh'),('row_unit[]','Adet'),('row_unit[]','Adet'),
            ('quantity[]','3'),('quantity[]','3'),('unit_price[]','7000'),('unit_price[]','0'),
            ('discount_rate[]','45'),('discount_rate[]','0'),('vat_rate[]','10'),('vat_rate[]','10'),
            ('vat_included[]','0'),('vat_included[]','0')])

    def test_transfer_create_edit_delete_and_financial_stock_effects(self):
        response=self.client.get(f'/faturalar?order_id={self.order.id}&entry=1')
        self.assertEqual(response.status_code,200)
        body=response.get_data(as_text=True)
        self.assertIn('SSH gri minder',body)
        self.assertIn('name="row_kind[]" value="ssh"',body)
        unrelated=m.StockMovement(product=self.product,movement_type='Açılış Stoğu',quantity=20)
        m.db.session.add(unrelated);m.db.session.commit();unrelated_id=unrelated.id
        r=self.client.post('/faturalar',data=self.payload());self.assertEqual(r.status_code,302)
        invoice=m.Invoice.query.filter_by(invoice_no='TEST-SSH').one();invoice_id=invoice.id
        self.assertEqual(invoice.total_amount,Decimal('12705'))
        self.assertEqual(len(invoice.items),2)
        self.assertEqual(m.calculate_customer_balances([self.customer.id])[self.customer.id], 12705)
        ssh=invoice.items[1]
        self.assertIsNone(ssh.product_id);self.assertIsNone(ssh.stock_movement_id)
        self.assertEqual(ssh.quantity,3);self.assertEqual(ssh.total_amount,0)
        self.assertEqual(m.StockMovement.query.count(),2)
        for path in [f'/faturalar/{invoice_id}',f'/faturalar/{invoice_id}/duzenle',f'/musteriler/{self.customer.id}/cari-hesap']:
            r=self.client.get(path)
            self.assertEqual(r.status_code,200)
            self.assertIn('TEST-SSH' if '/cari-hesap' in path else 'SSH gri minder',r.get_data(as_text=True))
        r=self.client.post(f'/faturalar/{invoice_id}/duzenle',data=self.payload());self.assertEqual(r.status_code,302)
        m.db.session.expire_all();invoice=m.db.session.get(m.Invoice,invoice_id)
        self.assertEqual(invoice.total_amount,12705);self.assertEqual(len(invoice.items),2)
        self.assertEqual(m.StockMovement.query.count(),2)
        r=self.client.post(f'/faturalar/{invoice_id}/sil');self.assertEqual(r.status_code,302)
        self.assertIsNone(m.db.session.get(m.Invoice,invoice_id))
        self.assertEqual(m.InvoiceItem.query.count(),0)
        self.assertEqual(m.calculate_customer_balances([self.customer.id]).get(self.customer.id, 0), 0)
        self.assertEqual(m.StockMovement.query.count(),1)
        self.assertIsNotNone(m.db.session.get(m.StockMovement,unrelated_id))
        self.assertEqual(len(self.order.items),2)

    def test_ssh_cannot_have_nonzero_price(self):
        data=self.payload();data.setlist('unit_price[]',['7000','1'])
        self.assertEqual(self.client.post('/faturalar',data=data).status_code,200)
        self.assertEqual(m.Invoice.query.count(),0);self.assertEqual(m.StockMovement.query.count(),0)

    def test_unknown_paid_rows_and_inactive_stock_still_blocked(self):
        data=self.payload();data.setlist('row_kind[]',['stock','stock'])
        self.client.post('/faturalar',data=data)
        self.assertEqual(m.Invoice.query.count(),0)
        self.product.active=False;m.db.session.commit()
        self.assertEqual(self.client.get(f'/faturalar?order_id={self.order.id}').status_code,302)

    def test_nonzero_unlinked_order_is_not_silently_dropped(self):
        self.order.items[1].unit_price=1;m.db.session.commit()
        self.assertEqual(self.client.get(f'/faturalar?order_id={self.order.id}').status_code,302)
        self.assertEqual(m.Invoice.query.count(),0)

    def test_all_zero_invoice_is_valid_and_has_no_stock_movement(self):
        data=self.payload()
        for field in ['product_ref[]','row_kind[]','row_unit[]','quantity[]','unit_price[]','discount_rate[]','vat_rate[]','vat_included[]']:
            data.setlist(field,[data.getlist(field)[1]])
        self.assertEqual(self.client.post('/faturalar',data=data).status_code,302)
        self.assertEqual(m.Invoice.query.one().total_amount,0)
        self.assertEqual(m.StockMovement.query.count(),0)

    def test_new_schema_and_no_sqlite_schema_rebuild(self):
        self.assertTrue(allows_nonstock_rows(m.db.session.connection()))
        with self.assertRaises(ValueError):prepare_nonstock_rows(m.db.engine)
        self.assertEqual(self.client.get('/yonetim/ssh-fatura-uyumlulugu').status_code,404)


if __name__=='__main__':unittest.main()
