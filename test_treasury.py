"""All payment channels share one read-only treasury ledger."""
import os
import tempfile
import unittest
from datetime import date, timedelta
from flask import template_rendered

_data = tempfile.TemporaryDirectory(prefix='bos-treasury-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name, BUSINESSOS_BACKUP_DIR=_data.name, DATABASE_URL='sqlite:///:memory:')
import app as m

class TreasuryTests(unittest.TestCase):
    def setUp(self):
        self.ctx=m.app.app_context();self.ctx.push();m.db.drop_all();m.db.create_all()
        self.today=date.today()
        self.customer=m.Customer(name='Müşteri');self.supplier=m.Customer(name='Tedarikçi')
        m.db.session.add_all([self.customer,self.supplier]);m.db.session.flush()
        for method,kind,amount in [('Nakit','Tahsilat',100),('Banka','Ödeme',20),('Kredi Kartı','Tahsilat',300),('Kredi Kartı','Ödeme',40),('Çek','Tahsilat',500)]:
            m.db.session.add(m.AccountTransaction(customer=self.customer,transaction_date=self.today,transaction_type=kind,payment_method=method,description=method+' işlem',credit=amount if kind=='Tahsilat' else 0,debit=amount if kind=='Ödeme' else 0,check_status='Bekliyor' if method=='Çek' else None,check_no='CHK1' if method=='Çek' else None,check_due_date=self.today+timedelta(days=5) if method=='Çek' else None))
        m.db.session.add_all([m.Expense(expense_date=self.today,category='Diğer',description='Kart masrafı',payment_method='Kredi Kartı',amount=10),m.Expense(expense_date=self.today,category='Diğer',description='Nakit masrafı',payment_method='Nakit',amount=5),m.CashMovement(movement_date=self.today,movement_type='Giriş',description='Açılış',amount=50)])
        m.add_direct_card_collection_pair(self.customer,self.supplier,70,self.today,'DIRECT','Bağlı kart',{'card_installments':3})
        m.db.session.commit()

    def tearDown(self):
        m.db.session.remove();self.ctx.pop()

    def render(self,query=''):
        saved={}
        def capture(sender,template,context,**extra):saved.update(context)
        template_rendered.connect(capture,m.app)
        try:r=m.app.test_client().get('/kasa-cek'+query)
        finally:template_rendered.disconnect(capture,m.app)
        self.assertEqual(r.status_code,200)
        return r.get_data(as_text=True),saved

    def test_all_channels_once_and_cash_not_inflated_by_cards(self):
        html,c=self.render()
        self.assertEqual(len(c['movements']),10)
        self.assertEqual(c['filtered_in'],1020);self.assertEqual(c['filtered_out'],145)
        self.assertEqual(c['summary']['cash_balance'],125)
        self.assertEqual(c['summary']['incoming_checks'],500)
        self.assertEqual({x['kind'] for x in c['movements']},{'Kasa','Banka','Kredi Kartı','Çek'})
        self.assertEqual(html.count('<table'),1)
        self.assertNotIn('Tüm Nakit Akışları',html)
        self.assertIn('Doğrudan tedarikçiye aktarım',html)
        self.assertIn('çek durumu',html)

    def test_filters_and_summary_scope(self):
        _,c=self.render('?movement=card&direction=out')
        self.assertEqual(len(c['movements']),3);self.assertEqual(c['filtered_out'],120)
        self.assertEqual(c['summary']['cash_balance'],125)
        _,c=self.render('?movement=bank');self.assertEqual(len(c['movements']),1)
        _,c=self.render('?movement=check&status=Bekliyor&q=CHK1');self.assertEqual(len(c['movements']),1)
        _,c=self.render('?movement=card&q=3+taksit');self.assertEqual(len(c['movements']),2)
        _,c=self.render('?start_date='+(self.today+timedelta(days=1)).isoformat());self.assertEqual(c['movements'],[])

    def test_read_only(self):
        raw=m.db.engine.raw_connection()
        before='\n'.join(raw.iterdump());raw.close()
        self.render('?movement=card')
        raw=m.db.engine.raw_connection()
        after='\n'.join(raw.iterdump());raw.close()
        self.assertEqual(before,after)

if __name__=='__main__':unittest.main()
