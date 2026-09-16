"""Dashboard summaries retain their accounting rules without duplicate loads."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch
from flask import template_rendered
from sqlalchemy import event
from sqlalchemy.orm import Session
_data=tempfile.TemporaryDirectory(prefix='businessos-dashboard-test-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name, BUSINESSOS_BACKUP_DIR=_data.name, DATABASE_URL='sqlite:///:memory:')
import app as m

class DashboardPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.ctx=m.app.app_context();self.ctx.push();m.db.drop_all();m.db.create_all()
        self.today=date.today();self.customer=m.Customer(name='Test cari');self.product=m.Product(name='Test')
        m.db.session.add_all([self.customer,self.product]);m.db.session.flush()
        self.orders=[]
        for index,status in enumerate(['Bekliyor','Sevk Edildi','Teslim Edildi','İptal Edildi']*3):
            stamp=datetime.combine(self.today-timedelta(days=index),datetime.min.time())
            o=m.Order(customer=self.customer,order_no=f'T-{index}',order_type='Satış' if index%2==0 else 'Satın Alma',status=status,order_date=self.today,delivery_date=self.today-timedelta(days=index%2),created_at=stamp,updated_at=stamp)
            o.items.append(m.OrderItem(product=self.product,product_name='Test',quantity=2,unit_price=120,discount_rate=10,vat_rate=20,vat_included=True))
            if index!=2:
                o.history.append(m.OrderHistory(status='Sevk Edildi',created_at=stamp))
                o.history.append(m.OrderHistory(status='Teslim Edildi',created_at=datetime.combine(self.today,datetime.min.time())))
                o.history.append(m.OrderHistory(status='Teslim Edildi',created_at=datetime.combine(self.today,datetime.min.time())))
            self.orders.append(o);m.db.session.add(o)
        inv=m.Invoice(customer=self.customer,invoice_no='I',invoice_type='Satış',invoice_date=self.today)
        inv.items.append(m.InvoiceItem(product=self.product,product_name='Test',quantity=1,unit_price=500,vat_rate=20))
        m.db.session.add(inv)
        m.db.session.add(m.AccountTransaction(customer=self.customer,transaction_type='Tahsilat',description='Test',payment_method='Nakit',transaction_date=self.today,debit=0,credit=100))
        m.db.session.commit()

    def tearDown(self):
        m.db.session.remove();self.ctx.pop()

    def render(self):
        context={}
        def capture(sender,template,context:dict,**extra): saved.update(context)
        saved={}
        template_rendered.connect(capture,m.app)
        try: response=m.app.test_client().get('/')
        finally: template_rendered.disconnect(capture,m.app)
        self.assertEqual(response.status_code,200)
        return response,saved

    def test_summaries_and_history_fallback(self):
        response,c=self.render()
        self.assertEqual(c['active_count'],6)
        self.assertEqual(c['completed_count'],3)
        self.assertEqual(c['delivered_today'],11) # duplicate events count once, cancelled orders included
        self.assertEqual(c['due_today_count'],3)
        self.assertEqual(c['overdue_count'],3)
        self.assertNotIn('recent_orders',c)
        self.assertNotIn('Son Siparişler',response.get_data(as_text=True))
        self.assertEqual(c['total_debit_balance'],500)
        self.assertEqual(c['today_sales_count'],6)
        self.assertEqual(c['today_purchase_count'],3)
        self.assertEqual(sum(row['total'] for row in c['week_activity']),4)

    def test_one_balance_calculation_no_history_objects_and_no_writes(self):
        def dump():
            raw=m.db.engine.raw_connection()
            try:return '\n'.join(raw.iterdump())
            finally:raw.close()
        before=dump();m.db.session.remove();loaded=[];statements=[]
        def load(session,obj):loaded.append(type(obj).__name__)
        def sql(conn,cursor,statement,parameters,context,executemany):statements.append(statement)
        event.listen(Session,'loaded_as_persistent',load);event.listen(m.db.engine,'before_cursor_execute',sql)
        try:
            with patch('app.calculate_customer_balances',wraps=m.calculate_customer_balances) as balances:
                self.render();self.assertEqual(balances.call_count,1)
        finally:
            event.remove(Session,'loaded_as_persistent',load);event.remove(m.db.engine,'before_cursor_execute',sql)
        self.assertNotIn('OrderHistory',loaded)
        self.assertEqual(loaded.count('Invoice'),0)
        self.assertEqual(loaded.count('InvoiceItem'),0)
        self.assertFalse(any('LIMIT' in s and 'ORDER BY' in s and 'created_at DESC' in s for s in statements))
        self.assertEqual(before,dump())

    def test_invoice_due_cards_match_tracking_page(self):
        invoice=m.Invoice.query.one()
        invoice.due_date=self.today-timedelta(days=1)
        m.db.session.commit()
        response,c=self.render()
        groups=m.load_invoice_maturity_groups(self.today)
        self.assertEqual(c['overdue_collection_amount'],500)
        self.assertEqual(c['overdue_collection_amount'],sum(g['overdue_amount'] for g in groups))
        self.assertEqual(c['overdue_collection_count'],1)
        self.assertEqual(c['due_soon_collection_amount'],0)
        self.assertIn('1 fatura',response.get_data(as_text=True))
        invoice.due_date=self.today+timedelta(days=7)
        m.db.session.commit()
        _,c=self.render()
        self.assertEqual(c['overdue_collection_amount'],0)
        self.assertEqual(c['due_soon_collection_amount'],500)
        self.assertEqual(c['due_soon_collection_count'],1)

    def test_ledger_balances_match_original_object_arithmetic(self):
        from decimal import Decimal
        other=m.Customer(name='Other');m.db.session.add(other);m.db.session.flush()
        invoice=m.Invoice(customer=other,invoice_no='BUY',invoice_type='Satın Alma',invoice_date=self.today)
        invoice.items.append(m.InvoiceItem(product=self.product,product_name='Test',quantity=3,unit_price=Decimal('123.45'),discount_rate=Decimal('12.34'),vat_rate=20,vat_included=False))
        m.db.session.add(invoice);m.db.session.commit()
        expected={c.id:Decimal('0') for c in m.Customer.query.all()}
        for inv in m.Invoice.query.all():
            expected[inv.customer_id]+=inv.total_amount if inv.invoice_type=='Satış' else -inv.total_amount
        for tx in m.AccountTransaction.query.all():expected[tx.customer_id]+=tx.debit-tx.credit
        self.assertEqual(m.calculate_customer_balances(),expected)
        self.assertEqual(m.calculate_customer_balances([other.id]),{other.id:expected[other.id]})
        self.assertEqual(m.calculate_customer_balances([]),{})

    def test_treasury_aggregation_preserves_amounts_and_checks(self):
        m.db.session.add_all([
            m.AccountTransaction(customer=self.customer,transaction_type='Ödeme',description='Test',payment_method='Nakit',transaction_date=self.today,debit=25,credit=0),
            m.AccountTransaction(customer=self.customer,transaction_type='Tahsilat',description='Test',payment_method='Çek',check_status='Bekliyor',check_due_date=self.today,transaction_date=self.today,debit=0,credit=80),
            m.Expense(expense_date=self.today,category='Test',description='Test',payment_method='Nakit',amount=12),
            m.CashMovement(movement_date=self.today,movement_type='Giriş',description='Test',amount=20),
            m.CashMovement(movement_date=self.today,movement_type='Çıkış',description='Test',amount=3),
        ])
        m.db.session.commit()
        result=m.calculate_treasury(self.today)
        self.assertEqual(result['cash_balance'],80)
        self.assertEqual(result['incoming_checks'],80)
        self.assertEqual(len(result['due_soon']),1)
        self.assertEqual(len(result['overdue_checks']),0)

    def test_pending_balances_argument_matches_default(self):
        orders=m.Order.query.filter(m.Order.status!='İptal Edildi').all()
        self.assertEqual(m.calculate_pending_delivery_amounts(orders),m.calculate_pending_delivery_amounts(orders,balances=m.calculate_customer_balances()))

if __name__=='__main__':unittest.main()
