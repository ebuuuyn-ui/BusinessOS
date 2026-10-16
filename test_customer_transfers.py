import os, tempfile, unittest, re, html
from decimal import Decimal
_data=tempfile.TemporaryDirectory()
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,BUSINESSOS_BACKUP_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m

class CustomerTransferTests(unittest.TestCase):
    def setUp(self):
        self.ctx=m.app.app_context();self.ctx.push();m.db.drop_all();m.db.create_all()
        self.a=m.Customer(name='ZDM');self.b=m.Customer(name='Diğer Firma')
        m.db.session.add_all([self.a,self.b]);m.db.session.flush()
        m.db.session.add(m.AccountTransaction(customer_id=self.a.id,transaction_type='Alacak Devir',description='Açılış',credit=1000,debit=0));m.db.session.commit()
        self.client=m.app.test_client()
    def tearDown(self):
        m.db.session.remove();self.ctx.pop()
    def preview(self,**changes):
        data=dict(source=self.a.id,target=self.b.id,amount='250,25',day='2026-10-16',kind='credit',note='Deneme')
        data.update(changes);return self.client.post('/musteriler/virman',data=data)
    def confirm(self,response):
        token=html.unescape(re.search(r'name="confirmation" value="([^"]+)"',response.text)[1])
        return self.client.post('/musteriler/virman',data={'confirmation':token}),token
    def test_credit_pair_cancel_and_replay(self):
        response=self.preview();self.assertEqual(response.status_code,200)
        self.assertEqual(m.AccountTransaction.query.count(),1)
        response,token=self.confirm(response);self.assertEqual(response.status_code,302)
        self.assertEqual(m.calculate_customer_balances(),{self.a.id:Decimal('-749.75'),self.b.id:Decimal('-250.25')})
        self.client.post('/musteriler/virman',data={'confirmation':token});self.assertEqual(m.AccountTransaction.query.count(),3)
        tx=m.AccountTransaction.query.filter_by(transaction_type='Cari Virman Çıkış').one()
        self.client.post(f'/musteriler/virman/{tx.id}/iptal')
        self.assertEqual(m.calculate_customer_balances(),{self.a.id:Decimal('-1000'),self.b.id:Decimal('0')})
        self.client.post('/musteriler/virman',data={'confirmation':token});self.assertEqual(m.AccountTransaction.query.count(),3)
    def test_debit_and_statement(self):
        tx=m.AccountTransaction.query.one();tx.debit=1000;tx.credit=0;m.db.session.commit()
        response,_=self.confirm(self.preview(kind='debit'))
        self.assertEqual(response.status_code,302)
        self.assertEqual(m.calculate_customer_balances()[self.b.id],Decimal('250.25'))
        self.assertIn('Cari Virman Giriş',self.client.get(f'/musteriler/{self.b.id}/cari-hesap').text)
    def test_invalid_and_insufficient(self):
        for changes in [dict(target=self.a.id),dict(amount='1001'),dict(amount='NaN'),dict(amount='0'),dict(amount='1,234'),dict(kind='debit'),dict(day='bad')]:
            response=self.preview(**changes);self.assertEqual(response.status_code,200);self.assertNotIn('name="confirmation"',response.text)
        self.assertEqual(m.AccountTransaction.query.count(),1)
    def test_confirmation_rechecks_balance(self):
        response=self.preview();tx=m.AccountTransaction.query.one();tx.credit=10;m.db.session.commit()
        response,_=self.confirm(response)
        self.assertEqual(response.status_code,200);self.assertEqual(m.AccountTransaction.query.count(),1)
    def test_forged_confirmation(self):
        response=self.client.post('/musteriler/virman',data={'confirmation':'fake'})
        self.assertEqual(response.status_code,200);self.assertEqual(m.AccountTransaction.query.count(),1)
