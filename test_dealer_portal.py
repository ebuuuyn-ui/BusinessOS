"""Isolated SQLite fixtures and mocked HTTP. No live credentials or business data."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch,MagicMock
from decimal import Decimal
from sqlalchemy import select,func,inspect

_data=tempfile.TemporaryDirectory(prefix='bos-portal-fixture-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m
import dealer_portal as d
from test_web_auth import BASE,ENV,login

CONFIG={**ENV,'BUSINESSOS_DATA_DIR':_data.name,'DATABASE_URL':'sqlite:///:memory:',
        'BOS_PORTAL_ENABLED':'1','BOS_PORTAL_URL':'https://portal.test','BOS_PORTAL_SOURCE_ID':'portal-fixture',
        'BOS_PORTAL_SIGNING_KEY':'a'*64}


def packet(method='kart-taksit',identity=1):
    rate=d.PAYMENTS[method][1];subtotal=2_497_000
    p={'schemaVersion':1,'sourceId':'portal-fixture','portalOrderId':identity,'portalUserId':7,
       'portalOrderNo':'BAY-20261020-123456789A','orderDate':'2026-10-20',
       'customer':{'company':'TEST Bayi','name':'Test Yetkili','phone':'00000000000','email':'test@example.invalid',
                   'address':'TEST sevkiyat yok','note':'<script>test</script>','paymentMethod':method},
       'pricing':{'methodId':method,'label':'Kredi kartı · 3 taksit','rate':rate,'subtotalCents':subtotal,
                  'discountCents':subtotal*rate//100,'totalCents':subtotal-subtotal*rate//100},
       'items':[{'code':'M00901','name':'SONIC','label':'Misafir','detail':'Sabit kolçak','quantity':2,
                 'unitCents':1_248_500,'colorLabel':'Gri','note':'TEST ürün talebi'}]}
    return {'payload':p,'payloadHash':hashlib.sha256(d.encoded(p)).hexdigest()}


class DealerPortalTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,CONFIG,clear=True);self.env.start();self.addCleanup(self.env.stop)
        self.app=m.create_app({'TESTING':True});self.context=self.app.app_context();self.context.push()
        m.db.create_all()
        self.customer=m.Customer(name='TEST BOS müşteri',code='TEST-C001')
        self.product=m.Product(name='SONIC',code='M00901',active=True,unit_price=999,purchase_price=10)
        m.db.session.add_all([self.customer,self.product]);m.db.session.commit()
        self.client=self.app.test_client();self.assertEqual(login(self.client).status_code,302)
        self.base='/yonetim/bayi-portali'
    def tearDown(self):
        m.db.session.remove();self.context.pop()
    def post(self,path,data=None):return self.client.post(path,base_url=BASE,headers={'Origin':BASE},data=data or {})
    def init(self):self.assertEqual(self.post(self.base+'/kur',{'confirm':'yes'}).status_code,302)
    def pull(self,records=None):
        records=[packet()] if records is None else records
        with patch('dealer_portal.remote',return_value={'sourceId':'portal-fixture','orders':records,'after':1}):
            response=self.post(self.base+'/esitle')
        self.assertEqual(response.status_code,302)
        return m.db.session.execute(select(d.inbox)).mappings().first()
    def link(self,row):return self.post(f"{self.base}/{row['id']}/eslestir",{'confirm':'yes','customer_code':'TEST-C001'})
    def approve(self,row,**changes):
        data={'confirm':'yes','payload_hash':row['payload_hash'],'vat_rate':'20'};data.update(changes)
        return self.post(f"{self.base}/{row['id']}/onayla",data)

    def test_startup_get_and_disabled_configuration_never_create_bridge_tables(self):
        self.assertNotIn(d.inbox.name,inspect(m.db.engine).get_table_names())
        self.assertEqual(self.client.get(self.base,base_url=BASE).status_code,200)
        self.assertNotIn(d.inbox.name,inspect(m.db.engine).get_table_names())
        with patch.dict(os.environ,{'BOS_PORTAL_ENABLED':'0'}):
            self.assertIn('Bağlantı kapalı',self.client.get(self.base,base_url=BASE).text)
            self.assertEqual(self.post(self.base+'/kur',{'confirm':'yes'}).status_code,404)

    def test_auth_csrf_and_owner_only(self):
        other=self.app.test_client()
        self.assertEqual(other.get(self.base,base_url=BASE).status_code,302)
        self.assertEqual(other.post(self.base+'/kur',base_url=BASE).status_code,401)
        self.assertEqual(self.client.post(self.base+'/kur',base_url=BASE,data={'confirm':'yes'}).status_code,403)
        from web_users import users
        with m.db.engine.begin() as conn:
            users.create(conn,checkfirst=True)
            identity=conn.execute(users.insert().values(username='test-restricted',password_hash='unused-test',active=True,
                session_token='test-session-token',failed_attempts=0)).inserted_primary_key[0]
        with other.session_transaction(base_url=BASE) as sess:
            sess['web_user_id']=identity;sess['web_user_token']='test-session-token'
        self.assertEqual(other.get(self.base,base_url=BASE).status_code,403)
        self.assertEqual(other.post(self.base+'/kur',base_url=BASE,headers={'Origin':BASE},data={'confirm':'yes'}).status_code,403)

    def test_pull_has_no_business_effect_and_requires_explicit_customer_mapping(self):
        self.init();row=self.pull()
        self.assertIsNotNone(row);self.assertEqual(m.Order.query.count(),0)
        self.assertIn('Son bağlantı kontrolü başarılı.',self.client.get(self.base,base_url=BASE).text)
        for model in (m.Invoice,m.StockMovement,m.AccountTransaction):self.assertEqual(model.query.count(),0)
        response=self.client.get(f"{self.base}/{row['id']}",base_url=BASE)
        self.assertEqual(response.status_code,200)
        self.assertIn('Kredi Kartı 3 Taksit',response.text);self.assertIn('Sabit kolçak',response.text)
        self.assertNotIn('<script>test</script>',response.text)
        self.assertEqual(self.approve(row).status_code,409)
        self.assertEqual(self.link(row).status_code,302)
        self.assertEqual(m.Order.query.count(),0)
        self.assertEqual(self.approve(row,vat_rate='').status_code,400)

    def test_approval_retry_is_idempotent_and_ack_failure_is_recoverable(self):
        self.init();row=self.pull();self.link(row)
        with patch('dealer_portal.remote',side_effect=d.PortalError('TEST transport outage')):
            self.assertEqual(self.approve(row).status_code,302)
        self.assertEqual(m.Order.query.count(),1)
        order=m.Order.query.one()
        self.assertEqual(order.payment_method,'Kredi Kartı 3 Taksit')
        self.assertEqual(order.status,'Onaylandı');self.assertEqual(order.net_amount,Decimal('13733.5'))
        self.assertEqual(order.items[0].unit_price,Decimal('12485')) # not the changed BOS list price 999
        self.assertEqual(order.items[0].discount_rate,45);self.assertEqual(order.items[0].vat_rate,20)
        self.assertIn('TEST sevkiyat yok',order.notes);self.assertEqual(order.items[0].variant,'Gri')
        self.assertTrue(m.db.session.execute(select(d.inbox.c.ack_pending)).scalar())
        with patch('dealer_portal.remote',return_value={'acknowledged':True,'portalOrderId':1}) as transport:
            self.assertEqual(self.approve(row).status_code,302)
            self.assertEqual(self.post(f"{self.base}/{row['id']}/bildir").status_code,302)
            self.assertEqual(transport.call_count,2)
        self.assertFalse(m.db.session.execute(select(d.inbox.c.ack_pending)).scalar())
        self.assertEqual(m.Order.query.count(),1)
        for model in (m.Invoice,m.StockMovement,m.AccountTransaction):self.assertEqual(model.query.count(),0)
        self.assertEqual(self.client.get(f"{self.base}/{row['id']}",base_url=BASE).status_code,200)

    def test_unknown_or_inactive_product_stops_approval(self):
        self.init();row=self.pull();self.link(row)
        self.product.active=False;m.db.session.commit()
        self.assertEqual(self.approve(row).status_code,409);self.assertEqual(m.Order.query.count(),0)

    def test_all_four_payment_mappings_preserve_rates_and_cheque_maturity(self):
        self.init()
        for identity,method in enumerate(d.PAYMENTS,1):
            with self.subTest(method=method):
                self.pull([packet(method,identity)])
                row=m.db.session.execute(select(d.inbox).where(d.inbox.c.portal_order_id==identity)).mappings().one()
                self.link(row)
                with patch('dealer_portal.remote',return_value={'acknowledged':True,'portalOrderId':identity}):
                    self.assertEqual(self.approve(row).status_code,302)
                number='BAYI-'+hashlib.sha256(b'portal-fixture').hexdigest()[:6].upper()+'-'+str(identity)
                order=m.Order.query.filter_by(order_no=number).one()
                self.assertEqual(order.payment_method,d.PAYMENTS[method][0])
                self.assertEqual(order.items[0].discount_rate,d.PAYMENTS[method][1])
                if method=='cek-90-gun':self.assertIn('90 gün vadeli çek',order.notes)
        self.assertEqual(m.Order.query.count(),4)
        for model in (m.Invoice,m.StockMovement,m.AccountTransaction):self.assertEqual(model.query.count(),0)

    def test_rounding_difference_blocks_instead_of_changing_customer_total(self):
        self.init();record=packet('havale')
        record['payload']['items'][0].update(quantity=1,unitCents=1)
        record['payload']['pricing'].update(subtotalCents=1,discountCents=1,totalCents=0)
        record['payloadHash']=hashlib.sha256(d.encoded(record['payload'])).hexdigest()
        row=self.pull([record]);self.link(row)
        self.assertEqual(self.approve(row).status_code,409)
        self.assertEqual(m.Order.query.count(),0)

    def test_changed_packet_and_mismatched_price_are_rejected(self):
        self.init();bad=packet();bad['payload']['items'][0]['unitCents']=1
        self.assertIsNone(self.pull([bad]));self.assertEqual(m.Order.query.count(),0)
        bad['payloadHash']=hashlib.sha256(d.encoded(bad['payload'])).hexdigest()
        with self.assertRaises(d.PortalError):d.validate(bad,'portal-fixture')
        with self.assertRaises(d.PortalError):d.validate(packet(),'wrong-source')

    def test_customer_link_cannot_be_silently_reassigned(self):
        self.init();row=self.pull();self.link(row)
        m.db.session.add(m.Customer(name='Another TEST',code='TEST-C002'));m.db.session.commit()
        response=self.post(f"{self.base}/{row['id']}/eslestir",{'confirm':'yes','customer_code':'TEST-C002'})
        self.assertEqual(response.status_code,409)
        self.assertEqual(m.db.session.execute(select(d.links.c.customer_id)).scalar(),self.customer.id)

    def test_vercel_transport_requires_short_lived_deployment_identity(self):
        config={**d.settings(),'origin':'https://portal-fixture.vercel.app'}
        with patch('dealer_portal.get_vercel_oidc_token_sync',side_effect=d.VercelOidcTokenError('fixture')), patch('dealer_portal.requests.request') as transport:
            with self.assertRaises(d.PortalError):d.remote(config,'GET','/api/bos/v1/orders?after=0')
            transport.assert_not_called()
        response=MagicMock(status_code=200);response.iter_content.return_value=[b'{"ok":true}'];response.__enter__.return_value=response
        with patch('dealer_portal.get_vercel_oidc_token_sync',return_value='fixture-short-lived-token'), patch('dealer_portal.requests.request',return_value=response) as transport:
            self.assertEqual(d.remote(config,'GET','/api/bos/v1/orders?after=0'),{'ok':True})
        headers=transport.call_args.kwargs['headers']
        self.assertEqual(headers['x-vercel-trusted-oidc-idp-token'],'fixture-short-lived-token')
        self.assertNotIn('x-vercel-protection-bypass',headers)

    def test_server_transport_is_signed_and_does_not_follow_redirects(self):
        response=MagicMock(status_code=200)
        response.iter_content.return_value=[b'{"ok":true}']
        response.__enter__.return_value=response
        with patch('dealer_portal.requests.request',return_value=response) as transport:
            self.assertEqual(d.remote(d.settings(),'GET','/api/bos/v1/orders?after=0'),{'ok':True})
        args=transport.call_args
        self.assertFalse(args.kwargs['allow_redirects']);self.assertEqual(args.kwargs['timeout'],(5,15))
        headers=args.kwargs['headers'];self.assertEqual(headers['X-BOS-Source'],'portal-fixture')
        message=f"GET\n/api/bos/v1/orders?after=0\n{headers['X-BOS-Timestamp']}\n{hashlib.sha256(b'').hexdigest()}".encode()
        import hmac
        self.assertEqual(headers['X-BOS-Signature'],hmac.new(('a'*64).encode(),message,hashlib.sha256).hexdigest())


if __name__=='__main__':unittest.main()
