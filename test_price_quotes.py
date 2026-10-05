import unittest,json,io
from decimal import Decimal
from unittest.mock import patch
from flask import g
from PIL import Image
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
import app as m
from price_quotes import validate,contact
from quote_pdf import build_pdf

class QuoteTests(unittest.TestCase):
    setUp=SupplierRouteTests.setUp
    tearDown=SupplierRouteTests.tearDown
    def sample(self):
        p=m.Product(name='NESSA TEKLİ',code='O04401',unit_price=5100,active=True);m.db.session.add(p);m.db.session.commit();self.p=p
        return dict(date='2026-10-05',customer_name='Örnek Müşteri',customer_code='C01',address='İstanbul',recipient='Yetkili',phone='',conditions={'Ödeme':'Peşin'},lines=[dict(product_id=p.id,name=p.name,quantity=4,list_price='5100',mode='discount',discount='10',vat='10',description='Gri')])
    def post(self,data,uid=None,version=''):
        return self.client.post('/fiyat-teklifleri/'+(uid+'/duzenle' if uid else 'yeni'),base_url=BASE,headers={'Origin':BASE},data={'payload':json.dumps(data),'version':version})
    def test_save_edit_snapshot_and_no_accounting_effects(self):
        d=self.sample();r=self.post(d);self.assertEqual(r.status_code,302,r.text)
        q=m.PriceQuote.query.one();saved=json.loads(q.payload)
        self.assertEqual(saved['totals']['net'],'18360.00');self.assertEqual(saved['totals']['total'],'20196.00')
        self.assertEqual(saved['author']['email'],'bekir@abikamobilya.com')
        self.p.unit_price=999;m.db.session.commit();self.assertEqual(json.loads(q.payload)['lines'][0]['list_price'],'5100.00')
        self.assertEqual(m.Invoice.query.count(),0);self.assertEqual(m.StockMovement.query.count(),0);self.assertEqual(m.Order.query.count(),1)
        page=self.client.get('/fiyat-teklifleri/'+q.id,base_url=BASE);self.assertEqual(page.status_code,200);self.assertIn('PDF İndir',page.text)
        d['lines'][0].update(mode='price',price='4000');r=self.post(d,q.id,'1');self.assertEqual(r.status_code,302)
        m.db.session.expire_all();self.assertEqual(json.loads(q.payload)['totals']['total'],'17600.00')
        self.assertIn('başka bir oturumda',self.post(d,q.id,'1').text)
    def test_validation_and_auth(self):
        d=self.sample();d['lines'][0]['quantity']=1.5
        self.assertIn('pozitif tam sayı',self.post(d).text);self.assertEqual(m.PriceQuote.query.count(),0)
        d['lines'][0]['quantity']=1;d['lines'][0]['discount']=101;self.assertIn('100',self.post(d).text)
        self.assertEqual(self.app.test_client().get('/fiyat-teklifleri',base_url=BASE).status_code,302)
        self.assertEqual(self.client.post('/fiyat-teklifleri/yeni',base_url=BASE,data={}).status_code,403)
    def test_author_is_authenticated_and_preview_no_write(self):
        d=self.sample();d['author']={'email':'forged@example.com'}
        with self.app.test_request_context():
            g.web_username='ahmet';g.web_is_owner=False
            self.assertEqual(contact()['email'],'ahmet@abikamobilya.com')
        r=self.client.post('/fiyat-teklifleri/onizleme.pdf',base_url=BASE,headers={'Origin':BASE},data={'payload':json.dumps(d)})
        self.assertEqual(r.status_code,200);self.assertTrue(r.data.startswith(b'%PDF'));self.assertEqual(m.PriceQuote.query.count(),0)
    def test_image_and_pdf_many_rows(self):
        d=self.sample();im=Image.new('RGB',(20,20),'white');b=io.BytesIO();im.save(b,'PNG');b.seek(0)
        r=self.client.post(f'/fiyat-teklifleri/urun/{self.p.id}/gorsel',base_url=BASE,headers={'Origin':BASE},data={'image':(b,'chair.png')})
        self.assertEqual(r.status_code,200)
        self.post(d);q=m.PriceQuote.query.one();data=json.loads(q.payload);self.assertTrue(data['lines'][0]['image'])
        self.assertEqual(self.client.get(f'/fiyat-teklifleri/{q.id}/pdf',base_url=BASE).status_code,200)
        data['lines']*=20;blob=build_pdf(data,'TEST').getvalue();self.assertTrue(blob.startswith(b'%PDF'))
    def test_missing_tables_read_only_pages(self):
        m.PriceQuote.__table__.drop(m.db.engine);m.ProductQuoteImage.__table__.drop(m.db.engine)
        self.assertEqual(self.client.get('/fiyat-teklifleri',base_url=BASE).status_code,200)
        self.assertEqual(self.client.get('/fiyat-teklifleri/yeni',base_url=BASE).status_code,200)
