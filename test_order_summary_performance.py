"""Order summary totals match model math without loading off-page orders."""
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.dialects import postgresql
_data=tempfile.TemporaryDirectory(prefix='businessos-order-summary-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,BUSINESSOS_BACKUP_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m

class OrderSummaryPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.ctx=m.app.app_context();self.ctx.push();m.db.drop_all();m.db.create_all()
        self.customer=m.Customer(name='Test');m.db.session.add(self.customer);m.db.session.flush()
    def tearDown(self):
        m.db.session.remove();self.ctx.pop()
    def seed(self,n=100):
        for i in range(n):
            o=m.Order(customer=self.customer,order_no=f'T-{i}',order_type=m.ORDER_TYPES[i%2],status=m.ORDER_STATUSES[i%len(m.ORDER_STATUSES)])
            if i%7:
                o.items.append(m.OrderItem(product_name='Test',quantity=i%5+1,unit_price=Decimal('123.45'),vat_rate=Decimal('20'),discount_rate=[Decimal('-5'),Decimal('12.34'),Decimal('110')][i%3],vat_included=bool(i%2)))
                o.items.append(m.OrderItem(product_name='Test 2',quantity=2,unit_price=Decimal('0.05'),vat_rate=10,discount_rate=0))
            m.db.session.add(o)
        m.db.session.commit()
    def legacy(self):
        summary={k:{s:dict(count=0,total=Decimal('0')) for s in m.ORDER_STATUSES} for k in m.ORDER_TYPES}
        counts={}
        for order in m.Order.query.all():
            key=(order.order_type,order.status);counts[key]=counts.get(key,0)+1
            if order.order_type in summary and order.status in summary[order.order_type]:
                g=summary[order.order_type][order.status];g['count']+=1;g['total']+=order.total_amount
        return summary,counts
    def test_summary_math_and_postgresql_query(self):
        self.seed()
        expected,counts=self.legacy()
        self.assertEqual(m.order_status_summaries(),(expected,counts))
        query=m.order_status_summary_query()
        sql=str(query.statement.compile(dialect=postgresql.dialect()))
        self.assertIn('LEFT OUTER JOIN',sql);self.assertIn('count(distinct',sql.lower())
        self.assertIn('NUMERIC(38, 10)',sql)
        for kind,status,count,total in query.all():
            self.assertEqual(count,counts[kind,status])
            self.assertEqual(total,expected[kind][status]['total'])
    def test_filtered_pages_match_legacy_summary_and_are_read_only(self):
        self.seed(45)
        def dump():
            raw=m.db.engine.raw_connection()
            try:return '\n'.join(raw.iterdump())
            finally:raw.close()
        before=dump();client=m.app.test_client()
        for path in ['/siparisler','/siparisler?page=2','/siparisler?active=1','/siparisler?type=Satış&q=T-1','/siparisler?delivery_pending=1']:
            with patch('app.order_status_summaries',self.legacy):expected=client.get(path)
            m.db.session.remove();actual=client.get(path)
            self.assertEqual(actual.status_code,200)
            self.assertEqual(actual.data,expected.data)
        self.assertEqual(before,dump())
    def test_only_page_orders_materialized(self):
        self.seed(100);m.db.session.remove();loaded=[]
        def load(session,obj):loaded.append(type(obj).__name__)
        event.listen(Session,'loaded_as_persistent',load)
        try:response=m.app.test_client().get('/siparisler')
        finally:event.remove(Session,'loaded_as_persistent',load)
        self.assertEqual(response.status_code,200)
        self.assertEqual(loaded.count('Order'),50)
        self.assertLessEqual(loaded.count('OrderItem'),100)
    def test_empty(self):
        self.assertEqual(m.order_status_summaries()[1],{})
        self.assertEqual(m.app.test_client().get('/siparisler').status_code,200)
if __name__=='__main__':unittest.main()
