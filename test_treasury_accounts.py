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
    def test_edit_description_preserves_assigned_pair_and_balances(self):
        a,b=m.add_direct_card_collection_pair(self.buyer,self.supplier,Decimal('113850'),date.today(),'','Abikadan çekildi',{'card_installments':2});m.db.session.commit()
        self.post(action='assign',source_key='tx:'+str(a.id),account_id=self.ids['direct'])
        before=self.balances();count=m.AccountTransaction.query.count()
        route=f'/musteriler/{a.customer_id}/cari-hesap/hareket/{a.id}/duzenle'
        self.assertEqual(self.client.get(route).status_code,200)
        r=self.client.post(route,data=dict(original_description=a.description,description="Decofis’ten çekildi"))
        self.assertEqual(r.status_code,302)
        self.assertEqual(a.description,"Decofis’ten çekildi")
        self.assertTrue(b.description.endswith(' · Decofis’ten çekildi'))
        self.assertEqual(a.credit,Decimal('113850'));self.assertEqual(b.debit,Decimal('113850'))
        self.assertEqual(a.linked_transaction_id,b.id);self.assertEqual(b.linked_transaction_id,a.id)
        self.assertEqual(m.AccountTransaction.query.count(),count);self.assertEqual(self.balances(),before)
        self.assertTrue(all('Decofis’ten çekildi' in p['description'] for p in m.db.session.execute(select(self.postings)).mappings()))
        self.client.post(route,data=dict(original_description='stale',description='Wrong'))
        self.assertEqual(a.description,"Decofis’ten çekildi")

    def test_edit_description_updates_mirror_without_balance_change(self):
        self.link();t=self.tx();self.post(action='assign',source_key='tx:'+str(t.id),account_id=self.ids['ahmet'])
        p=m.db.session.execute(select(self.postings)).mappings().first();mirror=m.db.session.get(m.AccountTransaction,p['mirror_id'])
        route=f'/musteriler/{t.customer_id}/cari-hesap/hareket/{t.id}/duzenle'
        self.client.post(route,data=dict(original_description=t.description,description='Yeni not'))
        self.assertTrue(mirror.description.endswith('Yeni not'));self.assertEqual(self.ahmet.balance,80)
        self.assertEqual(self.client.get(f'/musteriler/{mirror.customer_id}/cari-hesap/hareket/{mirror.id}/duzenle').status_code,404)
        self.client.post(route,data=dict(original_description=t.description,description=''))
        self.assertEqual(t.description,'Yeni not')

    def test_edit_account_reassigns_atomically_and_preserves_amount(self):
        t=self.tx(method='Nakit');self.post(action='assign',source_key='tx:'+str(t.id),account_id=self.ids['cash'])
        route=f'/musteriler/{t.customer_id}/cari-hesap/hareket/{t.id}/duzenle'
        data=dict(original_description=t.description,original_account_id=str(self.ids['cash']),description=t.description,treasury_account_id=str(self.ids['enpara']))
        self.assertEqual(self.client.post(route,data=data).status_code,302)
        self.assertEqual(t.payment_method,'Banka');self.assertEqual(t.credit,80)
        self.assertEqual(self.balances()['cash'],0);self.assertEqual(self.balances()['enpara'],80);self.assertEqual(self.count(),1)
        data['treasury_account_id']=str(self.ids['kuveyt'])
        self.client.post(route,data=data)
        self.assertEqual(self.balances()['enpara'],80);self.assertEqual(self.balances()['kuveyt'],0)
        data.update(original_account_id=str(self.ids['enpara']),treasury_account_id='999999',description='Wrong')
        self.client.post(route,data=data);self.assertEqual(t.description,'Tahsilat');self.assertEqual(self.balances()['enpara'],80)
        data.update(treasury_account_id='',description=t.description)
        self.client.post(route,data=data);self.assertEqual(self.count(),0);self.assertEqual(t.credit,80)

    def test_edit_account_moves_ahmet_mirror_without_duplicate(self):
        self.link();t=self.tx(method='Nakit');self.post(action='assign',source_key='tx:'+str(t.id),account_id=self.ids['ahmet'])
        route=f'/musteriler/{t.customer_id}/cari-hesap/hareket/{t.id}/duzenle'
        self.client.post(route,data=dict(original_description=t.description,original_account_id=str(self.ids['ahmet']),description=t.description,treasury_account_id=str(self.ids['enpara'])))
        self.assertEqual(self.ahmet.balance,0);self.assertEqual(self.balances()['enpara'],80);self.assertEqual(self.count(),1)
        self.assertEqual(m.AccountTransaction.query.count(),1)

    def test_edit_returns_to_filtered_source_and_rejects_external_return(self):
        t=self.tx();route=f'/musteriler/{t.customer_id}/cari-hesap/hareket/{t.id}/duzenle'
        data=dict(original_description=t.description,description=t.description,return_to='/kasa-cek?account=unassigned&q=abc&page=2')
        r=self.client.post(route,data=data);self.assertEqual(r.location,data['return_to'])
        for unsafe in ['https://example.com','//example.com','/%2fexample.com','/\\example.com']:
            data['return_to']=unsafe;r=self.client.post(route,data=data)
            self.assertEqual(r.location,f'/musteriler/{t.customer_id}/cari-hesap')

    def test_total_balance_includes_intermediary_and_negative_cards_once(self):
        self.link();a=self.tx(method='Nakit',amount=100);b=self.tx(amount=80)
        self.post(action='assign',source_key='tx:'+str(a.id),account_id=self.ids['cash'])
        self.post(action='assign',source_key='tx:'+str(b.id),account_id=self.ids['ahmet'])
        self.tx(amount=999) # Unassigned money is not an account balance.
        self.post(action='setup_cards')
        card=next(x for x in self.ext['rows']() if x['slug']=='card-maximum')
        t=m.AccountTransaction(customer=self.supplier,transaction_type='Ödeme',payment_method='Kredi Kartı',card_owner_type='Kendi Kartımız',transaction_date=date.today(),description='Kart ödeme',debit=30,credit=0)
        m.db.session.add(t);m.db.session.commit();self.post(action='assign',source_key='tx:'+str(t.id),account_id=card['id'])
        self.assertEqual(self.ext['total_balance'](),Decimal('150'))
        self.post(action='transfer',from_id=self.ids['cash'],to_id=self.ids['enpara'],amount='25',date=date.today().isoformat(),description='Virman',token=self.context()['token'])
        self.assertEqual(self.ext['total_balance'](),Decimal('150'))
        self.assertEqual(sum(self.balances().values()),Decimal('150'))
        self.assertEqual(self.ext['total_balance'](self.ids['cash']),Decimal('75'))
        html=self.client.get('/kasa-cek?account='+str(self.ids['cash'])+'&q=no-match').get_data(as_text=True)
        self.assertIn('Seçili Hesap Bakiyesi',html);self.assertIn('₺75,00',html)
        self.assertNotIn('Seçili Hesap Bakiyesi',self.client.get('/kasa-cek?account=unassigned').get_data(as_text=True))
        self.assertIn('Toplam Hesap Bakiyesi',self.client.get('/kasa-cek').get_data(as_text=True))

    def test_merge_cards_preserves_postings_and_is_idempotent(self):
        self.post(action='setup_cards')
        m.db.session.execute(self.accounts.insert().values(slug='card-bonus',name='Bonus Kredi Kartı',kind='Kredi Kartı',created_at=m.datetime.utcnow()));m.db.session.commit()
        cards={a['slug']:a for a in self.ext['rows']()}
        for slug,amount in [('card-bonus',34),('card-garanti',200)]:
            tx=m.AccountTransaction(customer=self.supplier,transaction_type='Ödeme',payment_method='Kredi Kartı',card_owner_type='Kendi Kartımız',transaction_date=date.today(),description='Kart',debit=amount,credit=0)
            m.db.session.add(tx);m.db.session.commit();self.post(action='assign',source_key='tx:'+str(tx.id),account_id=cards[slug]['id'])
        before=self.ext['total_balance']();ids=set(m.db.session.execute(select(self.postings.c.id)).scalars())
        self.post(action='merge_garanti_bonus');self.post(action='merge_garanti_bonus');self.post(action='setup_cards')
        self.assertEqual(self.ext['total_balance'](),before)
        self.assertEqual(set(m.db.session.execute(select(self.postings.c.id)).scalars()),ids)
        self.assertNotIn('card-bonus',[a['slug'] for a in self.ext['rows']()])
        self.assertEqual(self.ext['total_balance'](cards['card-garanti']['id']),Decimal('-234'))
        self.assertEqual(m.AccountTransaction.query.count(),2)

    def test_ahmet_card_move_creates_one_credit_and_future_payments_reduce_receivable(self):
        self.link();receipt=self.tx(amount=100);self.post(action='assign',source_key='tx:'+str(receipt.id),account_id=self.ids['ahmet'])
        m.db.session.execute(self.accounts.insert().values(slug='card-ahmet-enpara',name='Ahmet Enpara',kind='Kredi Kartı',created_at=m.datetime.utcnow()));m.db.session.commit()
        old=next(a['id'] for a in self.ext['rows']() if a['slug']=='card-ahmet-enpara')
        tx=m.AccountTransaction(customer=self.supplier,transaction_type='Ödeme',payment_method='Kredi Kartı',card_owner_type='Kendi Kartımız',transaction_date=date.today(),description='Ahmet ödeme',debit=30,credit=0)
        m.db.session.add(tx);m.db.session.commit();self.post(action='assign',source_key='tx:'+str(tx.id),account_id=old)
        self.post(action='move_ahmet_card');self.post(action='move_ahmet_card');self.post(action='setup_cards')
        self.assertEqual(self.ahmet.balance,70);self.assertEqual(self.ext['total_balance'](),70);self.assertEqual(self.count(),2)
        self.assertNotIn('card-ahmet-enpara',[a['slug'] for a in self.ext['rows']()]);self.assertEqual(tx.debit,30)
        self.client.post('/odeme-girisi',data=dict(customer_id=self.supplier.id,amount='10',payment_method='Kredi Kartı',transaction_date=date.today().isoformat(),card_installments='1',card_owner_type='Kendi Kartımız',description='Ahmet yeni ödeme',treasury_account_id=self.ids['ahmet']))
        self.assertEqual(self.ahmet.balance,60);self.assertEqual(self.count(),3)

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

    def test_own_card_payment_and_bank_virman_reverse_without_second_expense(self):
        self.post(action='setup_cards');self.post(action='setup_cards')
        cards=[a for a in self.ext['rows']() if a['kind']=='Kredi Kartı'];self.assertEqual(len(cards),2)
        card=cards[0]['id']
        t=m.AccountTransaction(customer=self.supplier,transaction_type='Ödeme',payment_method='Kredi Kartı',card_owner_type='Kendi Kartımız',transaction_date=date.today(),description='Maximum ödeme',debit=100,credit=0)
        m.db.session.add(t);m.db.session.commit()
        self.post(action='assign',source_key='tx:'+str(t.id),account_id=self.ids['ahmet']);self.assertEqual(self.count(),0)
        self.post(action='assign',source_key='tx:'+str(t.id),account_id=card)
        self.assertEqual(self.balances()['card-maximum'],-100);self.assertEqual(m.calculate_treasury()['cash_balance'],0)
        before=m.AccountTransaction.query.count()
        self.post(action='transfer',from_id=self.ids['enpara'],to_id=card,amount='40',date=date.today().isoformat(),description='Ekstre ödemesi',token=self.context()['token'])
        self.assertEqual(self.balances()['card-maximum'],-60);self.assertEqual(self.balances()['enpara'],-40)
        self.assertEqual(m.calculate_treasury()['cash_balance'],-40);self.assertEqual(m.AccountTransaction.query.count(),before)
        transfer=m.db.session.execute(select(self.postings).where(self.postings.c.source_key.like('transfer:%'))).mappings().first()
        self.post(action='unassign',source_key=transfer['source_key']);self.assertEqual(self.balances()['card-maximum'],-100);self.assertEqual(m.calculate_treasury()['cash_balance'],0)
        incoming=self.tx();self.post(action='assign',source_key='tx:'+str(incoming.id),account_id=card);self.assertEqual(self.count(),1)

    def test_new_own_card_payment_requires_card_and_posts_once(self):
        self.post(action='setup_cards');card=next(a['id'] for a in self.ext['rows']() if a['slug']=='card-maximum')
        data=dict(customer_id=self.supplier.id,amount='100',payment_method='Kredi Kartı',transaction_date=date.today().isoformat(),card_installments='1',card_owner_type='Kendi Kartımız',description='Kart ödeme')
        self.client.post('/odeme-girisi',data=data)
        self.assertEqual(m.AccountTransaction.query.count(),0)
        data['treasury_account_id']=card;self.client.post('/odeme-girisi',data=data)
        self.assertEqual(m.AccountTransaction.query.count(),1);self.assertEqual(self.balances()['card-maximum'],-100)

    def test_expense_create_edit_reassign_clear_and_invalid_rollback(self):
        self.post(action='setup_cards');card=next(a['id'] for a in self.ext['rows']() if a['slug']=='card-maximum')
        data=dict(category='Kira',payment_method='Banka',amount='100',description='Masraf',expense_date=date.today().isoformat(),treasury_account_id=self.ids['enpara'])
        r=self.client.post('/masraflar',data=data);self.assertEqual(r.status_code,302)
        expense=m.Expense.query.one();self.assertEqual(expense.payment_method,'Banka');self.assertEqual(self.balances()['enpara'],-100);self.assertEqual(m.calculate_treasury()['cash_balance'],-100)
        url=f'/masraflar/{expense.id}/duzenle'
        page=self.client.get(url);self.assertEqual(page.status_code,200);self.assertIn(b'data-kind="Banka" selected',page.data)
        data.update(amount='150',treasury_account_id=card)
        self.client.post(url,data=data);self.assertEqual(self.balances()['enpara'],0);self.assertEqual(self.balances()['card-maximum'],-150);self.assertEqual(m.calculate_treasury()['cash_balance'],0)
        data.update(amount='200',treasury_account_id=self.ids['ahmet'])
        self.client.post(url,data=data);m.db.session.expire_all();self.assertEqual(expense.amount,150);self.assertEqual(self.balances()['card-maximum'],-150)
        data.update(amount='150',payment_method='Kredi Kartı',treasury_account_id='')
        self.client.post(url,data=data);self.assertEqual(self.count(),0);self.assertEqual(m.Expense.query.count(),1);self.assertEqual(self.balances()['card-maximum'],0)

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
