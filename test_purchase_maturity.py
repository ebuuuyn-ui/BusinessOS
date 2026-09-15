"""Purchase maturity regressions using isolated data, never the live database."""
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from openpyxl import load_workbook

_data = tempfile.TemporaryDirectory(prefix='businessos-purchase-maturity-')
os.environ['BUSINESSOS_DATA_DIR'] = _data.name
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['BUSINESSOS_BACKUP_DIR'] = _data.name
from app import app, db, Customer, Order, OrderItem, OrderHistory, AccountTransaction, delivered_purchase_payment_tracking, delivered_sales_collection_tracking


class PurchaseMaturityTests(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context(); self.ctx.push()
        db.drop_all(); db.create_all()
        self.today = date.today()
        self.party = Customer(name='Örnek Tedarikçi', code='320.TEST')
        db.session.add(self.party); db.session.flush()
        self.first = self.order('SA-1', 'Satın Alma', -50, 100)
        self.second = self.order('SA-2', 'Satın Alma', -25, 300)
        self.sale = self.order('SS-1', 'Satış', -50, 500)
        self.order('SA-WAIT', 'Satın Alma', -50, 900, 'Bekliyor')
        self.order('SA-CANCEL', 'Satın Alma', -50, 900, 'İptal Edildi')
        self.payment('Ödeme', 150, 0)
        self.payment('Tahsilat', 0, 40)
        self.payment('Ödeme', 999, 0, 1)
        db.session.commit(); self.client = app.test_client()

    def order(self, number, kind, days, amount, status='Teslim Edildi'):
        delivered = self.today + timedelta(days=days)
        order = Order(customer=self.party, order_no=number, order_type=kind, status=status, delivery_date=delivered)
        order.items.append(OrderItem(product_name='Ürün', quantity=1, unit_price=amount, vat_rate=0))
        if status == 'Teslim Edildi':
            order.history.append(OrderHistory(status=status, created_at=datetime.combine(delivered, time(12))))
        db.session.add(order); db.session.flush(); return order

    def payment(self, kind, debit, credit, days=0):
        db.session.add(AccountTransaction(customer=self.party, transaction_type=kind, debit=debit, credit=credit,
            description='İzole test hareketi', payment_method='Kredi Kartı', transaction_date=self.today+timedelta(days=days)))

    def tearDown(self):
        db.session.remove(); self.ctx.pop()

    def test_fifo_and_sales_purchase_separation(self):
        items = delivered_purchase_payment_tracking(self.today)
        self.assertEqual([i['order'].order_no for i in items], ['SA-1', 'SA-2'])
        self.assertEqual([i['remaining'] for i in items], [0,250])
        self.assertEqual([i['collected'] for i in items], [100,50])
        self.assertEqual(items[1]['due_date'], self.today+timedelta(days=5))
        self.assertEqual(items[0]['state_label'], 'Ödendi')
        self.assertEqual(items[1]['state'], 'due_soon')
        self.assertEqual(delivered_sales_collection_tracking(self.today)[0]['remaining'],460)

    def test_debit_adjustments_overpayment_and_date_fallback(self):
        self.payment('Borç Dekontu', 25, 0); self.payment('Borç Devir', 25, 0)
        self.first.history.clear(); db.session.commit()
        self.assertEqual(delivered_purchase_payment_tracking(self.today)[1]['remaining'],200)
        self.payment('Ödeme', 500, 0); db.session.commit()
        self.assertTrue(all(i['remaining']==0 for i in delivered_purchase_payment_tracking(self.today)))


    def test_overdue_not_hidden_by_weighted_future_average(self):
        AccountTransaction.query.filter_by(transaction_type='Ödeme').delete()
        self.second.history[0].created_at = datetime.combine(self.today-timedelta(days=10), time(12))
        db.session.commit()
        from collection_tracking import customer_summaries
        group=customer_summaries(delivered_purchase_payment_tracking(self.today),self.today)[0]
        self.assertGreater(group['average_days'],0)
        self.assertEqual(group['overdue_amount'],100)
        self.assertEqual(group['oldest_due_date'],self.today-timedelta(days=20))
        self.assertEqual(group['remaining'],400)

if __name__=='__main__': unittest.main()
