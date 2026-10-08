import os,tempfile,unittest
from datetime import date
from decimal import Decimal
from flask import template_rendered
from sqlalchemy import select
_data=tempfile.TemporaryDirectory(prefix='bos-fund-test-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,BUSINESSOS_BACKUP_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m
from treasury_accounts import TreasuryLocked

class FundTests(unittest.TestCase):
    def setUp(self):
        self.ctx=m.app.app_context();self.ctx.push();m.db.drop_all();m.db.create_all()
        self.client=m.app.test_client();self.ext=m.app.extensions['treasury_accounts'];self.accounts,self.postings,self.audit=self.ext['tables']
        self.buyer=m.Customer(name='Alıcı');self.ahmet=m.Customer(name='AHMET EMİN UYAN');self.supplier=m.Customer(name='Tedarikçi')
        m.db.session.add_all([self.buyer,self.ahmet,self.supplier]);m.db.session.commit()
        self.post(action='setup');self.ids={r['slug']:r['id'] for r in self.ext['rows']()}
    def tearDown(self):m.db.session.remove();self.ctx.pop()
    def post(self,**data):
        r=self.client.post('/kasa-cek/kasalar',data=data);self.assertEqual(r.status_code,302);return r
    def context(self):
        c={}
        def save(sender,template,context,**extra):c.update(context)
        template_rendered.connect(save,m.app)
        try:r=self.client.get('/kasa-cek/kasalar')
        finally:template_rendered.disconnect(save,m.app)
        self.assertEqual(r.status_code,200);return c
    def tx(self,method='Kredi Kartı',amount=80):
        t=m.AccountTransaction(customer=self.buyer,transaction_type='Tahsilat',payment_method=method,transaction_date=date.today(),description='Tahsilat',debit=0,credit=amount)
        m.db.session.add(t);m.db.session.commit();return t
    def link(self):self.post(action='link',account_id=self.ids['ahmet'],customer_id=self.ahmet.id)
    def count(self):return len(m.db.session.execute(select(self.postings)).all())
    def balances(self):return {a['slug']:a['balance'] for a in self.context()['accounts']}
    def test_setup_is_idempotent_and_does_not_link_or_backfill(self):
        self.tx();self.post(action='setup')
        self.assertEqual(len(self.ext['rows']()),5);self.assertEqual(self.count(),0)
        self.assertTrue(all(a['customer_id'] is None for a in self.ext['rows']()))
        self.assertTrue(all(v==0 for v in self.balances().values()))
    def test_ahmet_mirror_transfer_and_atomic_reversal(self):
        self.link();t=self.tx();key='tx:'+str(t.id)
        self.post(action='assign',source_key=key,account_id=self.ids['ahmet'])
        self.assertEqual(self.ahmet.balance,80);self.assertEqual(self.buyer.balance,-80)
        self.assertEqual(m.calculate_treasury()['cash_balance'],0)
        token=self.context()['token']
        data=dict(action='transfer',from_id=self.ids['ahmet'],to_id=self.ids['kuveyt'],amount='30.15',date=date.today().isoformat(),description='Banka aktarımı',token=token)
        self.post(**data);self.post(**data)
        self.assertEqual(self.count(),3)
        self.assertEqual(self.balances()['ahmet'],Decimal('49.85'));self.assertEqual(self.ahmet.balance,Decimal('49.85'))
        self.assertEqual(self.balances()['kuveyt'],Decimal('30.15'));self.assertEqual(m.calculate_treasury()['cash_balance'],Decimal('30.15'))
        transfer=m.db.session.execute(select(self.postings).where(self.postings.c.source_key.like('transfer:%'))).mappings().first()
        self.post(action='unassign',source_key=transfer['source_key']);self.assertEqual(self.count(),1);self.assertEqual(self.ahmet.balance,80)
        self.post(action='unassign',source_key=key);self.assertEqual(self.ahmet.balance,0);self.assertEqual(self.buyer.balance,-80);self.assertEqual(self.count(),0)
    def test_legacy_cash_method_can_be_assigned_to_bank_without_rewriting_source(self):
        t=self.tx();t.payment_method='Nakit';t.description='ENPARAYA GELEN';m.db.session.commit()
        before=m.calculate_treasury()['cash_balance']
        self.post(action='assign',source_key='tx:'+str(t.id),account_id=self.ids['enpara'])
        self.assertEqual(self.balances()['enpara'],Decimal('80'))
        self.assertEqual(t.payment_method,'Nakit')
        self.assertEqual(m.calculate_treasury()['cash_balance'],before)

    def test_cash_held_by_ahmet_is_receivable_not_liquid_and_reverses(self):
        self.link();t=self.tx(method='Nakit',amount=Decimal('62000'))
        t.transaction_date=date(2026,9,10);m.db.session.commit();key='tx:'+str(t.id)
        self.assertEqual(m.calculate_treasury()['cash_balance'],Decimal('62000'))
        self.post(action='assign',source_key=key,account_id=self.ids['ahmet'])
        self.assertEqual(self.ahmet.balance,Decimal('62000'))
        self.assertEqual(self.balances()['ahmet'],Decimal('62000'))
        self.assertEqual(t.payment_method,'Nakit')
        self.assertEqual(m.calculate_treasury()['cash_balance'],0)
        self.assertEqual(self.ext['liquid_transfer'](date(2026,9,9)),0)
        self.assertEqual(self.ext['liquid_transfer'](date(2026,9,10)),Decimal('-62000'))
        self.post(action='unassign',source_key=key)
        self.assertEqual(m.calculate_treasury()['cash_balance'],Decimal('62000'))
        self.assertEqual(self.ahmet.balance,0)

    def test_invalid_and_duplicate_assignment_preserve_money(self):
        self.link();t=self.tx();key='tx:'+str(t.id)
        self.post(action='assign',source_key=key,account_id=self.ids['kuveyt']);self.assertEqual(self.count(),0)
        self.post(action='assign',source_key=key,account_id=self.ids['ahmet']);self.post(action='assign',source_key=key,account_id=self.ids['ahmet'])
        self.assertEqual(self.count(),1);self.assertEqual(self.ahmet.balance,80)
        self.post(action='link',account_id=self.ids['ahmet'],customer_id='');self.assertEqual(next(a for a in self.ext['rows']() if a['slug']=='ahmet')['customer_id'],self.ahmet.id)
    def test_direct_pair_in_out_net_zero(self):
        a,b=m.add_direct_card_collection_pair(self.buyer,self.supplier,Decimal('95.05'),date.today(),'X','Doğrudan',{'card_installments':2});m.db.session.commit()
        self.post(action='assign',source_key='tx:'+str(a.id),account_id=self.ids['direct'])
        self.assertEqual(self.count(),2);self.assertEqual(self.balances()['direct'],0);self.assertEqual(m.calculate_treasury()['cash_balance'],0)
        self.post(action='unassign',source_key='tx:'+str(b.id));self.assertEqual(self.count(),0)
    def test_source_and_mirror_cannot_be_changed_independently(self):
        self.link();t=self.tx();self.post(action='assign',source_key='tx:'+str(t.id),account_id=self.ids['ahmet'])
        mirror=m.db.session.execute(select(self.postings.c.mirror_id)).scalar()
        r=self.client.post(f'/musteriler/{self.ahmet.id}/cari-hesap/hareket/{mirror}/sil');self.assertEqual(r.status_code,302)
        self.assertEqual(self.count(),1);self.assertEqual(self.ahmet.balance,80)
        t.credit=90
        with self.assertRaises(TreasuryLocked):m.db.session.commit()
        m.db.session.rollback();self.assertEqual(t.credit,80)
    def test_new_collection_assigns_and_bad_account_rolls_back_receipt(self):
        self.link();data=dict(customer_id=self.buyer.id,amount='100',payment_method='Kredi Kartı',transaction_date=date.today().isoformat(),card_installments='2',treasury_account_id=self.ids['ahmet'],description='Yeni')
        r=self.client.post('/tahsilat-girisi',data=data);self.assertEqual(r.status_code,302);self.assertEqual(self.count(),1);self.assertEqual(self.ahmet.balance,100)
        data['treasury_account_id']=self.ids['enpara'];self.client.post('/tahsilat-girisi',data=data)
        self.assertEqual(self.count(),1);self.assertEqual(self.buyer.balance,-100)
    def test_new_direct_collection_uses_transit_account(self):
        self.client.post('/tahsilat-girisi',data=dict(customer_id=self.buyer.id,amount='150',payment_method='Kredi Kartı',transaction_date=date.today().isoformat(),card_installments='1',direct_to_supplier='on',direct_supplier_id=self.supplier.id))
        self.assertEqual(self.count(),2);self.assertEqual(self.balances()['direct'],0)
        self.assertEqual(self.buyer.balance,-150);self.assertEqual(self.supplier.balance,150)
    def test_read_only_get(self):
        raw=m.db.engine.raw_connection();before='\n'.join(raw.iterdump());raw.close()
        self.context();self.client.get('/kasa-cek')
        raw=m.db.engine.raw_connection();after='\n'.join(raw.iterdump());raw.close();self.assertEqual(before,after)
    def test_bank_assignment_does_not_duplicate_existing_cash_balance(self):
        t=self.tx('Banka',120);self.post(action='assign',source_key='tx:'+str(t.id),account_id=self.ids['enpara'])
        self.assertEqual(m.calculate_treasury()['cash_balance'],120);self.assertEqual(self.balances()['enpara'],120)
        c=self.context();self.post(action='transfer',from_id=self.ids['enpara'],to_id=self.ids['kuveyt'],date=date.today().isoformat(),amount='20',description='Virman',token=c['token'])
        self.assertEqual(m.calculate_treasury()['cash_balance'],120)
    def test_invalid_transfers_no_partial_posting(self):
        for amount in ['NaN','-1','0','1.234']:
            self.post(action='transfer',from_id=self.ids['ahmet'],to_id=self.ids['enpara'],date=date.today().isoformat(),amount=amount,description='Test',token=self.context()['token'])
        self.assertEqual(self.count(),0)

if __name__=='__main__':unittest.main()
