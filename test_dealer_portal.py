"""Isolated SQLite fixtures and mocked HTTP. No live credentials or business data."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
import time
import hmac
from datetime import date,datetime,timezone
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

    def machine(self,method,path,payload=None,stamp=None,headers=None):
        stamp=stamp or str(int(time.time()));body=d.encoded(payload) if payload is not None else b''
        message=f'{method}\n{path}\n{stamp}\n{hashlib.sha256(body).hexdigest()}'.encode()
        auth={'X-BOS-Source':'portal-fixture','X-BOS-Timestamp':stamp,'X-BOS-Signature':hmac.new(b'a'*64,message,hashlib.sha256).hexdigest(),**(headers or {})}
        return self.app.test_client().open(path,method=method,base_url=BASE,data=body,content_type='application/json',headers=auth)

    def test_automatic_delivery_is_signed_idempotent_and_does_not_create_sales(self):
        self.init();path='/api/dealer-portal/v1/orders';p=packet()
        self.assertEqual(self.app.test_client().post(path,base_url=BASE,json=p).status_code,401)
        self.assertEqual(self.machine('POST',path,p,headers={'Origin':BASE}).status_code,403)
        self.assertEqual(self.machine('POST',path,p,stamp=str(int(time.time())-121)).status_code,401)
        self.assertEqual(self.machine('POST',path,p,headers={'X-BOS-Source':'other'}).status_code,401)
        for _ in range(2):self.assertEqual(self.machine('POST',path,p).status_code,200)
        self.assertEqual(m.db.session.execute(select(func.count()).select_from(d.inbox)).scalar(),1)
        self.assertEqual(m.Order.query.count(),0)
        p['payload']['customer']['company']='Changed';p['payloadHash']=hashlib.sha256(d.encoded(p['payload'])).hexdigest()
        self.assertEqual(self.machine('POST',path,p).status_code,409)

    def test_portal_activity_is_owner_only_read_only_and_escaped(self):
        self.init()
        values={'company':'<script>TEST</script>','contactName':'Yetkili','email':'test@example.invalid','phone':'05550000000','companyAddress':'TEST adres'}
        event={'id':4,'userId':7,'kind':'profile_change','createdAt':1792584000,'username':'testdealer',
               'before':{**values,'company':'Eski firma'},'after':values}
        result={'sourceId':'portal-fixture','events':[event],'nextBefore':4}
        with patch('dealer_portal.remote',return_value=result) as call:
            page=self.client.get(self.base+'?section=changes',base_url=BASE)
            self.assertEqual(page.status_code,200)
            self.assertIn('Bayi Portalı',page.text);self.assertNotIn('Bayi Portalı İstekleri',page.text)
            self.assertIn('&lt;script&gt;TEST&lt;/script&gt;',page.text)
            self.assertNotIn('<script>TEST</script>',page.text)
            self.assertIn('Eski firma',page.text);self.assertIn('Daha eski bildirimler',page.text)
            self.assertIn('Türkiye saati',page.text)
            self.assertEqual(call.call_args.args[2],'/api/bos/v1/activity?kind=profile_change&before=0')
        self.assertEqual(self.customer.name,'TEST BOS müşteri')
        self.assertEqual(m.Order.query.count(),0)
        self.assertEqual(m.db.session.execute(select(func.count()).select_from(d.links)).scalar(),0)
        with patch('dealer_portal.remote') as call:
            anonymous=self.app.test_client().get(self.base+'?section=members',base_url=BASE)
            self.assertEqual(anonymous.status_code,302);call.assert_not_called()
        for invalid in ({**result,'sourceId':'wrong'},{**result,'nextBefore':99},
                        {**result,'events':[{**event,'after':{**values,'password':'secret'}}]}):
            with patch('dealer_portal.remote',return_value=invalid):
                page=self.client.get(self.base+'?section=changes',base_url=BASE)
                self.assertEqual(page.status_code,200);self.assertIn('doğrulanamadı',page.text)
                self.assertNotIn('secret',page.text)
        with patch('dealer_portal.remote',side_effect=d.PortalError('Bağlantı kurulamadı')):
            page=self.client.get(self.base+'?section=members',base_url=BASE)
            self.assertIn('Kayıt bulunmadığı anlamına gelmez',page.text)
        self.assertEqual(self.client.get(self.base+'?section=wrong',base_url=BASE).status_code,400)

    def test_feedback_is_owner_only_paginated_and_escaped(self):
        self.init()
        message={'id':8,'userId':7,'createdAt':1792584000,'username':'testdealer','company':'TEST Bayi',
                 'contactName':'Test Yetkili','email':'test@example.invalid','phone':'05550000000',
                 'category':'product','type':'complaint','subject':'<script>title</script>',
                 'product':'M01801','message':'TEST mesaj <img src=x onerror=alert(1)>'}
        result={'sourceId':'portal-fixture','messages':[message],'nextBefore':8}
        with patch('dealer_portal.remote',return_value=result) as call:
            page=self.client.get(self.base+'?section=feedback',base_url=BASE)
            self.assertEqual(page.status_code,200)
            self.assertIn('Öneri ve şikâyetler',page.text)
            self.assertIn('&lt;script&gt;title&lt;/script&gt;',page.text)
            self.assertIn('&lt;img src=x onerror=alert(1)&gt;',page.text)
            self.assertNotIn('<script>title</script>',page.text)
            self.assertIn('Şikâyet',page.text);self.assertIn('Daha eski mesajlar',page.text)
            self.assertEqual(call.call_args.args[2],'/api/bos/v1/feedback?before=0')
        with patch('dealer_portal.remote') as call:
            self.assertEqual(self.app.test_client().get(self.base+'?section=feedback',base_url=BASE).status_code,302)
            call.assert_not_called()
        for invalid in ({**result,'sourceId':'wrong'},{**result,'nextBefore':4},
                        {**result,'messages':[{**message,'type':['bad']}]},
                        {**result,'messages':[{**message,'message':'x'*4001}]},
                        {**result,'messages':[{**message,'password':'private'}]}):
            with patch('dealer_portal.remote',return_value=invalid):
                page=self.client.get(self.base+'?section=feedback',base_url=BASE)
                self.assertIn('doğrulanamadı',page.text)
                self.assertNotIn('private',page.text)
        self.assertEqual(m.Order.query.count(),0)
        self.assertEqual(self.customer.name,'TEST BOS müşteri')

    def test_account_isolation_period_balance_history_and_trello(self):
        self.init();row=self.pull();self.link(row)
        with patch('dealer_portal.remote',return_value={'acknowledged':True,'portalOrderId':1}):self.approve(row)
        other=m.Customer(name='OTHER PRIVATE',code='OTHER');m.db.session.add(other);m.db.session.flush()
        m.db.session.add_all([
            m.AccountTransaction(customer_id=self.customer.id,transaction_date=date(2026,9,1),transaction_type='Açılış',description='Açılış',debit=100,credit=0),
            m.AccountTransaction(customer_id=self.customer.id,transaction_date=date(2026,10,1),transaction_type='Tahsilat',description='Tahsilat',debit=0,credit=25),
            m.AccountTransaction(customer_id=other.id,transaction_date=date(2026,10,1),transaction_type='Açılış',description='SECRET OTHER',debit=99999,credit=0)])
        m.db.session.commit();item=m.Order.query.one().items[0]
        with patch('dealer_account.trello_states',return_value=({item.id:'Üretimde'},{'connected':True,'checkedAt':'now'})):
            response=self.machine('GET','/api/dealer-portal/v1/account?userId=7&start=2026-10-01&end=2026-10-31')
        self.assertEqual(response.status_code,200);body=response.json
        self.assertEqual((body['currentCents'],body['openingCents'],body['creditCents']),(7500,10000,2500))
        self.assertEqual(body['orders'][0]['status'],'Üretimde')
        self.assertEqual(body['orders'][0]['portalOrderId'],1)
        price=body['orders'][0]['items'][0]['pricing']
        self.assertEqual(price['listUnitCents'],1248500)
        self.assertEqual(price['discountRate'],45)
        self.assertEqual(price['discountCents'],1123650)
        self.assertEqual(price['netCents'],1373350)
        self.assertEqual(price['totalCents'],1648020)
        self.assertNotIn('SECRET OTHER',response.text);self.assertNotIn('cost_unit_price',response.text)
        unlinked=self.machine('GET','/api/dealer-portal/v1/account?userId=8').json
        self.assertFalse(unlinked['linked']);self.assertNotIn('currentCents',unlinked)
        self.assertEqual(self.machine('GET','/api/dealer-portal/v1/account?userId=7&start=bad').status_code,400)

    def test_trello_uses_exact_purchase_line_mapping(self):
        import base64
        from cryptography.fernet import Fernet
        from supplier_trello import meta,connection,deliveries
        from dealer_account import trello_states
        self.init();row=self.pull();self.link(row)
        with patch('dealer_portal.remote',return_value={'acknowledged':True,'portalOrderId':1}):self.approve(row)
        sale=m.Order.query.one();line=sale.items[0]
        purchase=m.Order(order_no='TEST-PURCHASE',order_type='Satın Alma',customer_id=self.customer.id,order_date=date.today(),source_order_id=sale.id,status='Onaylandı')
        purchase.items.append(m.OrderItem(product_id=self.product.id,product_name='SONIC',quantity=2,unit_price=1,source_order_item_id=line.id))
        m.db.session.add(purchase);m.db.session.commit();meta.create_all(m.db.engine)
        cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256(('bos-trello-v1:'+self.app.secret_key).encode()).digest()))
        with m.db.engine.begin() as conn:
            conn.execute(connection.insert().values(id=1,config=cipher.encrypt(b'{"key":"test","token":"test"}').decode()))
            conn.execute(deliveries.insert().values(key=f'bos:order:{purchase.id}:item:{purchase.items[0].id}',card_id='owned-card'))
        def trello_call(_self,method,path,**kwargs):
            self.assertEqual(method,'GET')
            return [{'id':'list1','name':'Sevkiyata hazır'}] if path.endswith('/lists') else [{'id':'owned-card','idList':'list1','closed':False},{'id':'other-card','idList':'list1','closed':False}]
        with patch('supplier_trello.Client.call',trello_call):
            states,health=trello_states(self.app,m.db,m.Order,m.OrderItem,[sale])
        self.assertEqual(states,{line.id:'Sevkiyata hazır'});self.assertTrue(health['connected'])

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
