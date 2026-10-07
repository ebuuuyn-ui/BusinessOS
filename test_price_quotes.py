import unittest,json,io
from pathlib import Path
import hashlib
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
    def test_usd_discount_save_pdf_and_tl_order(self):
        from pypdf import PdfReader
        d=self.sample();d.update(currency='USD',exchange_rate='40')
        self.assertEqual(self.post(d).status_code,302)
        q=m.PriceQuote.query.one();saved=json.loads(q.payload)
        row=saved['lines'][0]
        self.assertEqual(row['source_list_price'],'5100.00')
        self.assertEqual(row['list_price'],'127.50')
        self.assertEqual(row['price'],'114.75')
        self.assertEqual(saved['totals']['total'],'504.90')
        pdf=self.client.get(f'/fiyat-teklifleri/{q.id}/pdf',base_url=BASE)
        text=' '.join(page.extract_text() for page in PdfReader(io.BytesIO(pdf.data)).pages)
        self.assertIn('114,75 USD',text);self.assertIn('1 USD = 40 TL',text)
        detail=self.client.get(f'/fiyat-teklifleri/{q.id}',base_url=BASE)
        self.assertIn('USD',detail.text)
        path=f'/fiyat-teklifleri/{q.id}/siparise-aktar'
        preview=self.client.get(path,base_url=BASE)
        self.assertIn('4.590,00',preview.text)
        self.assertEqual(self.transfer_post(path).status_code,302)
        order=m.Order.query.filter_by(order_type='Satış').order_by(m.Order.id.desc()).first()
        self.assertEqual(order.items[0].net_amount,Decimal('18360'))
        self.assertIn('1 USD = 40 TL',order.notes)

    def test_direct_usd_price_and_bad_rate(self):
        d=self.sample();d.update(currency='USD',exchange_rate='40.25')
        d['lines'][0].update(mode='usd',usd_price='100')
        clean=validate(d,{str(self.p.id):self.p},lambda p,r:None)
        self.assertEqual(clean['lines'][0]['price'],'100.00')
        self.assertEqual(clean['lines'][0]['source_price'],'4025.00')
        self.assertEqual(clean['totals']['total'],'440.00')
        for bad in ('0','-1','NaN','Infinity',''):
            d['exchange_rate']=bad
            with self.assertRaises(ValueError):validate(d,{str(self.p.id):self.p},lambda p,r:None)
        self.assertEqual(m.PriceQuote.query.count(),0)

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

    def prepare_transfer(self):
        data=self.sample();data['customer_name']='ABİKA TEST'
        data['lines'][0]['description']='Gri kumaş'
        self.post(data)
        quote=m.PriceQuote.query.one()
        return quote,f'/fiyat-teklifleri/{quote.id}/siparise-aktar'

    def transfer_post(self,path,version='1',**values):
        data=dict(version=version,customer_id='1339',order_date='2026-10-06',delivery_date='2026-10-20',payment_method='Banka Havalesi')
        data.update(values)
        return self.client.post(path,base_url=BASE,headers={'Origin':BASE},data=data)

    def test_transfer_preview_commit_and_duplicate_guard(self):
        quote,path=self.prepare_transfer()
        self.assertEqual(self.client.get(path,base_url=BASE).status_code,200)
        self.assertEqual(m.Order.query.count(),1)
        response=self.transfer_post(path)
        self.assertEqual(response.status_code,302)
        link=m.QuoteOrderTransfer.query.one();order=m.db.session.get(m.Order,link.order_id)
        self.assertEqual(order.order_type,'Satış');self.assertEqual(order.customer_id,1339)
        self.assertEqual(order.items[0].quantity,4)
        self.assertEqual(order.items[0].discount_rate,Decimal('10'))
        self.assertEqual(order.items[0].description,'Gri kumaş')
        self.assertEqual(order.total_amount,Decimal('20196'))
        self.assertIn(quote.number,order.notes);self.assertIn('Ödeme: Peşin',order.notes)
        self.assertEqual(self.transfer_post(path).location,response.location)
        self.assertEqual(m.Order.query.count(),2)
        self.assertEqual(m.Invoice.query.count(),0);self.assertEqual(m.StockMovement.query.count(),0)
        m.db.session.delete(order);m.db.session.commit()
        self.transfer_post(path)
        self.assertEqual(m.Order.query.count(),1)
        self.assertEqual(m.QuoteOrderTransfer.query.count(),1)

    def test_transfer_rejects_stale_missing_fields_inactive_and_cross_origin(self):
        quote,path=self.prepare_transfer()
        for fields in [dict(version='0'),dict(customer_id='99999'),dict(payment_method=''),dict(delivery_date='bad')]:
            response=self.transfer_post(path,**fields)
            self.assertEqual(response.status_code,200)
            self.assertEqual(m.Order.query.count(),1)
        self.p.active=False;m.db.session.commit()
        self.assertIn('pasif',self.transfer_post(path).text)
        self.assertEqual(m.Order.query.count(),1)
        self.assertEqual(self.app.test_client().get(path,base_url=BASE).status_code,302)
        self.assertEqual(self.client.post(path,base_url=BASE,headers={'Origin':'https://wrong.test'}).status_code,403)

    def test_transfer_direct_price_and_rounding_preserve_agreed_price(self):
        data=self.sample();data['lines'][0].update(list_price='12.29',discount='13.33',quantity=4)
        self.post(data);quote=m.PriceQuote.query.one()
        self.transfer_post(f'/fiyat-teklifleri/{quote.id}/siparise-aktar')
        link=m.QuoteOrderTransfer.query.one();item=m.db.session.get(m.Order,link.order_id).items[0]
        saved=json.loads(quote.payload)['lines'][0]
        self.assertEqual(item.unit_price,Decimal(saved['price']));self.assertEqual(item.discount_rate,0)
        self.assertEqual(item.net_amount,Decimal(saved['total']))
        self.assertIn('13.33',item.note)

    def test_catalog_images_exact_codes_and_manual_override(self):
        root=Path(__file__).parent
        manifest=json.loads((root/'assets/abika-product-images.json').read_text())['products']
        self.assertEqual(len(manifest),143)
        for code,entry in manifest.items():
            p=m.Product(name=entry['name'],code=code,unit_price=1,active=True)
            m.db.session.add(p);m.db.session.commit()
            response=self.client.get(f'/fiyat-teklifleri/urun/{p.id}/gorsel',base_url=BASE)
            self.assertEqual(response.status_code,200,code)
            self.assertEqual(hashlib.sha256(response.data).hexdigest(),entry['sha256'],code)
        self.assertEqual(m.ProductQuoteImage.query.count(),0)
        p=m.Product.query.filter_by(code='M00502').one()
        m.db.session.add(m.ProductQuoteImage(product_id=p.id,content=b'manual-image'))
        m.db.session.commit()
        self.assertEqual(self.client.get(f'/fiyat-teklifleri/urun/{p.id}/gorsel',base_url=BASE).data,b'manual-image')
        p=m.Product(name='Unmatched',code='O04502',unit_price=1,active=True)
        m.db.session.add(p);m.db.session.commit()
        self.assertEqual(self.client.get(f'/fiyat-teklifleri/urun/{p.id}/gorsel',base_url=BASE).status_code,404)
