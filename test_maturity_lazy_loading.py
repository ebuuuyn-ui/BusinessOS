"""Lazy maturity details preserve totals, scope reads and escape references."""
import os
import tempfile
import unittest
from datetime import date,timedelta
from unittest.mock import patch
from sqlalchemy import event
from sqlalchemy.orm import Session
_data=tempfile.TemporaryDirectory(prefix='businessos-maturity-lazy-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,BUSINESSOS_BACKUP_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m
from invoice_maturity import build_maturity_groups

class MaturityLazyTests(unittest.TestCase):
    def setUp(self):
        self.ctx=m.app.app_context();self.ctx.push();m.db.drop_all();m.db.create_all()
        self.today=date.today();self.parties=[m.Customer(name='Test <Cari>',code='C1'),m.Customer(name='Other',code='C2')]
        self.product=m.Product(name='Test');m.db.session.add_all([*self.parties,self.product]);m.db.session.flush()
        for idx,c in enumerate(self.parties):
            for kind in ('Satış','Satın Alma'):
                i=m.Invoice(customer=c,invoice_no=f'{idx}-{kind}<script>x</script>',invoice_type=kind,invoice_date=self.today,due_date=self.today-timedelta(days=2))
                for number in range(8):
                    i.items.append(m.InvoiceItem(product=self.product,product_name='Test',quantity=number+1,unit_price=100+idx,vat_rate=20,vat_included=bool(number%2),discount_rate=10))
                m.db.session.add(i)
            m.db.session.add(m.AccountTransaction(customer=c,transaction_type='Devir',description='Test',reference_no=f'TX-{idx}',transaction_date=self.today,debit=500 if idx==0 else 0,credit=500 if idx else 0))
        m.db.session.commit();self.ids=[c.id for c in self.parties];self.client=m.app.test_client()
    def tearDown(self):m.db.session.remove();self.ctx.pop()
    def canonical(self,groups):
        return [dict(customer=g['customer'].id,remaining=g['remaining'],overdue=g['overdue_amount'],undated=g['undated_amount'],other=g['other_amount'],average=g['average_due_date'],entries=[(i['reference'],i['amount'],i['remaining'],i['collected'],i['state']) for i in g['entries']]) for g in groups]
    def test_both_sql_paths_match_legacy_ledger(self):
        for purchase in (False,True):
            expected=build_maturity_groups(m.Customer.query.all(),m.Invoice.query.all(),m.AccountTransaction.query.all(),self.today,purchase)
            self.assertEqual(self.canonical(m.load_invoice_maturity_groups(self.today,purchase)),self.canonical(expected))
            # Exercise the hosted aggregation branch against isolated SQL, not a live DB.
            with patch.object(m.db.engine.dialect,'name','postgresql'):
                actual=m.load_invoice_maturity_groups(self.today,purchase)
            self.assertEqual(self.canonical(actual),self.canonical(expected))
    def test_details_scoped_and_not_embedded(self):
        html=self.client.get('/tahsilat-takibi').get_data(as_text=True)
        self.assertNotIn('<th>Fatura / Hareket</th>',html)
        self.assertIn('data-detail-url=',html)
        for purchase,customer_id in [(False,self.ids[0]),(True,self.ids[1])]:
            statements=[]
            def sql(conn,cursor,statement,parameters,context,executemany): statements.append((statement,parameters))
            event.listen(m.db.engine,'before_cursor_execute',sql)
            try:r=self.client.get(f'/tahsilat-takibi/cari/{customer_id}/ayrinti?kind='+('purchase' if purchase else 'sales'))
            finally:event.remove(m.db.engine,'before_cursor_execute',sql)
            self.assertEqual(r.status_code,200);self.assertEqual(r.headers['X-BusinessOS-Fragment'],'maturity-detail')
            self.assertIn('no-store',r.headers['Cache-Control'])
            self.assertEqual(len(statements),3)
            self.assertTrue(all('WHERE' in sql for sql,_ in statements))
            body=r.get_data(as_text=True)
            self.assertNotIn('<script>x</script>',body)
            self.assertIn('&lt;script&gt;',body)
            if not purchase:self.assertNotIn('TX-1',body)
        self.assertEqual(self.client.get('/tahsilat-takibi/cari/99999/ayrinti').status_code,404)
        self.assertEqual(self.client.get('/tahsilat-takibi/cari/1/ayrinti?kind=bad').status_code,400)
    def test_initial_page_does_not_load_full_invoice_objects(self):
        m.db.session.remove();loaded=[]
        def load(session,obj):loaded.append(type(obj).__name__)
        event.listen(Session,'loaded_as_persistent',load)
        try:self.assertEqual(self.client.get('/tahsilat-takibi').status_code,200)
        finally:event.remove(Session,'loaded_as_persistent',load)
        self.assertEqual(loaded,[])
if __name__=='__main__':unittest.main()
