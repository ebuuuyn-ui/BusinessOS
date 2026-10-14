import re
import unittest
from decimal import Decimal
from unittest.mock import patch

import app as m
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
from invoice_order_allocation import summaries


class GRTests(unittest.TestCase):
    setUp = SupplierRouteTests.setUp
    tearDown = SupplierRouteTests.tearDown

    def order(self, kind='Satış'):
        order = m.db.session.get(m.Order, self.order_id)
        order.order_type = kind
        product = m.Product(name='Koltuk', code='GR-STOCK')
        m.db.session.add(product)
        order.items = [m.OrderItem(product=product, product_name='Koltuk', quantity=2,
            unit_price=Decimal('110'), discount_rate=10, vat_rate=10, vat_included=True)]
        m.db.session.commit()
        self.path = f'/siparisler/{order.id}/gr'
        return order

    def form(self):
        page = self.client.get(self.path, base_url=BASE)
        self.assertEqual(page.status_code, 200)
        return dict(preview=re.search(r'name="preview" value="([^"]+)"', page.text)[1],
                    invoice_date='2026-10-14', due_date='2026-11-14')

    def post(self, form):
        return self.client.post(self.path, data=form, base_url=BASE, headers={'Origin': BASE})

    def test_sale_once_automatic_code_ledger_stock_and_no_uyumsoft(self):
        order = self.order()
        with patch('uyumsoft.Client') as client:
            form = self.form()
            self.assertEqual(m.Invoice.query.count(), 0)
            self.assertEqual(self.post(form).status_code, 302)
            self.assertEqual(self.post(form).status_code, 302)
            client.assert_not_called()
        inv = m.Invoice.query.one()
        self.assertEqual(inv.invoice_no, 'GR1001')
        self.assertTrue(inv.is_gr)
        self.assertEqual(inv.total_amount, Decimal('198'))
        self.assertEqual(order.customer.balance, Decimal('198'))
        self.assertEqual(m.load_invoice_ledger(customer_id=order.customer_id)[1][0].total_amount, Decimal('198'))
        self.assertEqual(m.AccountTransaction.query.count(), 0)
        movement = m.StockMovement.query.one()
        self.assertEqual((movement.movement_type, movement.quantity), ('Stok Çıkışı', 2))
        self.assertEqual(summaries([order])[order.id]['state'], 'Faturalandı')
        page = self.client.get(f'/faturalar/{inv.id}', base_url=BASE).text
        self.assertIn('GR · İç Kayıt', page)
        self.assertNotIn('Uyumsoft Fatura Taslağı', page)
        self.assertEqual(self.client.get(f'/faturalar/{inv.id}/uyumsoft', base_url=BASE).status_code, 403)
        self.assertEqual(self.client.post(f'/faturalar/{inv.id}/sil', base_url=BASE, headers={'Origin':BASE}).status_code,302)
        self.assertEqual(order.customer.balance, 0)
        self.assertEqual(m.StockMovement.query.count(), 0)

    def test_purchase_credit_and_unique_codes(self):
        order = self.order('Satın Alma')
        self.post(self.form())
        self.assertEqual(order.customer.balance, Decimal('-198'))
        self.assertEqual(m.StockMovement.query.one().movement_type, 'Stok Girişi')
        other = m.Order(order_no='SS-OTHER', customer_id=order.customer_id, order_type='Satış')
        other.items=[m.OrderItem(product_id=order.items[0].product_id, product_name='Koltuk', quantity=1, unit_price=100)]
        m.db.session.add(other);m.db.session.commit()
        self.path=f'/siparisler/{other.id}/gr'
        self.post(self.form())
        self.assertEqual({i.invoice_no for i in m.Invoice.query.all()},{'GR1001','GR1002'})

    def test_stale_missing_invalid_date_and_cancelled_no_effect(self):
        order = self.order()
        form = self.form()
        order.items[0].quantity=3;m.db.session.commit()
        self.assertEqual(self.post(form).status_code,400)
        self.assertEqual(self.post({}).status_code,400)
        form=self.form();form['invoice_date']='bad'
        self.assertEqual(self.post(form).status_code,400)
        order.status='İptal Edildi';m.db.session.commit()
        self.assertEqual(self.post(self.form()).status_code,400)
        self.assertEqual(m.Invoice.query.count(),0)
        self.assertEqual(m.StockMovement.query.count(),0)

    def test_auth_and_cross_origin(self):
        self.order()
        self.assertEqual(self.app.test_client().get(self.path,base_url=BASE).status_code,302)
        self.assertEqual(self.client.post(self.path,base_url=BASE,headers={'Origin':'https://evil.test'}).status_code,403)

    def test_partial_or_uyumsoft_attempt_blocked(self):
        from uyumsoft import order_attempts
        order=self.order();form=self.form()
        order_attempts.create(m.db.engine, checkfirst=True)
        with m.db.engine.begin() as conn:
            conn.execute(order_attempts.insert().values(order_id=order.id,uuid='draft-test',state='Draft',snapshot='{}',account='test'))
        self.assertEqual(self.post(form).status_code,400)
        self.assertEqual(m.Invoice.query.count(),0)
        with m.db.engine.begin() as conn:conn.execute(order_attempts.delete())
        invoice=m.Invoice(invoice_no='PARTIAL',invoice_type='Satış',customer_id=order.customer_id)
        invoice.items=[m.InvoiceItem(product_id=order.items[0].product_id,product_name='Koltuk',quantity=1)]
        m.db.session.add(invoice);m.db.session.flush()
        m.db.session.add(m.InvoiceOrderAllocation(invoice_id=invoice.id,invoice_item_id=invoice.items[0].id,order_id=order.id,order_item_id=order.items[0].id,quantity=1))
        m.db.session.commit()
        self.assertEqual(self.post(form).status_code,400)
        self.assertEqual(m.Invoice.query.count(),1)

    def test_deleted_number_not_reused_and_invalid_request_does_not_advance(self):
        self.order()
        form=self.form();bad=dict(form,invoice_date='invalid')
        self.assertEqual(self.post(bad).status_code,400)
        self.post(form)
        first=m.Invoice.query.one()
        self.assertEqual(first.invoice_no,'GR1001')
        self.client.post(f'/faturalar/{first.id}/sil',base_url=BASE,headers={'Origin':BASE})
        self.post(self.form())
        self.assertEqual(m.Invoice.query.one().invoice_no,'GR1002')

    def test_existing_database_without_counter_is_upgraded_on_post_only(self):
        self.order()
        m.GRSequence.__table__.drop(m.db.engine)
        form=self.form()
        from sqlalchemy import inspect
        self.assertFalse(inspect(m.db.engine).has_table(m.GRSequence.__tablename__))
        self.assertEqual(self.post(form).status_code,302)
        self.assertEqual(m.Invoice.query.one().invoice_no,'GR1001')
