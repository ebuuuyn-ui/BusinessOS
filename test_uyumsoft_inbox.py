"""Incoming invoices: read-only review, explicit mapping, atomic booking guards."""
import copy
import re
import unittest
from unittest.mock import patch
from decimal import Decimal
import test_uyumsoft as fixture
import app as m
from test_web_auth import BASE
from uyumsoft import snapshot
from uyumsoft_client import invoice_xml, A, B, T, ET, Client, UyumError
from uyumsoft_inbox import parse_inbox, matches, details
from uyumsoft_import import imports

class InboxTests(unittest.TestCase):
    setUp=fixture.UyumTests.setUp
    tearDown=fixture.UyumTests.tearDown
    post=fixture.UyumTests.post
    connect=fixture.UyumTests.connect
    make_invoice=fixture.UyumTests.make_invoice
    def fixture(self):
        inv=self.make_invoice()
        data=snapshot(inv,fixture.DEFAULT_SELLER,'Test')
        data['seller'],data['buyer']=data['buyer'],data['seller']
        m.db.session.delete(inv);m.db.session.commit()
        self.connect()
        self.uid='22222222-3333-4444-5555-666666666666'
        self.root=invoice_xml(data,self.uid,True)
        self.root.find('{'+B+'}ID').text='INB2026000000001'
        self.product=m.Product(name='BOS Çalışma Koltuğu',code='BOS-01',unit='Adet')
        m.db.session.add(self.product);m.db.session.commit()
        self.path='/faturalar/uyumsoft-gelen/'+self.uid
        self.form={'customer_ref':'1339 · ABİKA TEST','kind_0':'stock','product_0':f'{self.product.id} · BOS-01 · BOS Çalışma Koltuğu','remember_0':'yes','confirm':'yes'}
    def mock(self,c):
        c.return_value.inbox_invoice.return_value=self.root
        c.return_value.inbox_status.return_value='Approved'
        c.return_value.inbox_pdf.return_value=b'%PDF-1.4\n original test'
    def preview(self):
        page=self.client.get(self.path,base_url=BASE)
        self.assertEqual(page.status_code,200)
        self.assertIn('Koltuk &amp; Masa',page.text)
        self.form['token']=re.search(r'name="token" value="([^"]+)"',page.text)[1]
        return page
    def test_review_then_explicit_stock_mapping_idempotent(self):
        self.fixture()
        before=m.StockMovement.query.count()
        with patch('uyumsoft_inbox.Client') as c:
            self.mock(c);self.preview()
            self.assertEqual(m.Invoice.query.count(),0)
            c.return_value.inbox_pdf.assert_not_called()
            result=self.post(self.path,self.form);self.assertEqual(result.status_code,302,result.text)
            result=self.post(self.path,self.form);self.assertEqual(result.status_code,302)
        self.assertEqual(m.Invoice.query.count(),1)
        self.assertEqual(m.StockMovement.query.count(),before+1)
        inv=m.Invoice.query.one()
        self.assertEqual(inv.items[0].product_name,self.product.name)
        self.assertEqual(inv.total_amount,Decimal('198.00'))
        self.assertEqual(inv.invoice_type,'Satın Alma')
        self.assertEqual(m.db.session.execute(matches.select()).mappings().one()['product_id'],self.product.id)
        self.assertIn('Koltuk & Masa',m.db.session.execute(details.select()).mappings().one()['mapping'])
        saved=m.db.session.execute(imports.select()).mappings().one()
        self.assertTrue(saved['pdf'].startswith(b'%PDF-'))
        self.assertIn(b'Koltuk &amp; Masa',saved['xml'])
        self.post(f'/faturalar/{inv.id}/sil',{});self.assertEqual(m.Invoice.query.count(),1)
    def test_expense_no_stock_and_saved_suggestion(self):
        self.fixture();self.form.update(kind_0='expense',label_0='Nakliye hizmeti')
        with patch('uyumsoft_inbox.Client') as c:
            self.mock(c);self.preview();self.assertEqual(self.post(self.path,self.form).status_code,302)
            self.root.find('{'+B+'}UUID').text='33333333-3333-4444-5555-666666666666'
            self.path='/faturalar/uyumsoft-gelen/33333333-3333-4444-5555-666666666666'
            page=self.client.get(self.path,base_url=BASE)
            self.assertIn('value="expense" selected',page.text)
            self.assertIn('value="Nakliye hizmeti"',page.text)
        self.assertEqual(m.StockMovement.query.count(),0)
        inv=m.Invoice.query.one();self.assertIsNone(inv.items[0].product_id)
        self.assertEqual(inv.total_amount,Decimal('198.00'))
    def test_stale_and_missing_confirm_and_wrong_customer_no_write(self):
        self.fixture()
        with patch('uyumsoft_inbox.Client') as c:
            self.mock(c);self.preview()
            self.assertIn('kontrol onayını',self.post(self.path,dict(self.form,confirm='')).text)
            self.assertIn('eşleşen BOS carisini',self.post(self.path,dict(self.form,customer_ref='wrong')).text)
            self.root.find('{'+B+'}ID').text='INB2026000000002'
            self.assertIn('değişmiş',self.post(self.path,self.form).text)
            c.return_value.inbox_pdf.assert_not_called()
        self.assertEqual(m.Invoice.query.count(),0);self.assertEqual(m.StockMovement.query.count(),0)
    def test_rejected_status_and_pdf_failure_leave_no_partial_records(self):
        self.fixture()
        with patch('uyumsoft_inbox.Client') as c:
            self.mock(c);self.preview();c.return_value.inbox_status.return_value='Declined'
            self.assertIn('işlenemez',self.post(self.path,self.form).text)
            c.return_value.inbox_status.return_value='Approved';c.return_value.inbox_pdf.side_effect=UyumError('PDF alınamadı')
            self.assertIn('PDF alınamadı',self.post(self.path,self.form).text)
        self.assertEqual(m.Invoice.query.count(),0);self.assertEqual(m.StockMovement.query.count(),0)
    def test_identity_total_unit_and_duplicate_guards(self):
        self.fixture()
        with self.assertRaises(UyumError):parse_inbox(self.root,self.uid,'wrong')
        root=copy.deepcopy(self.root);root.find('{'+A+'}LegalMonetaryTotal/{'+B+'}PayableAmount').text='1'
        with self.assertRaises(UyumError):parse_inbox(root,self.uid,fixture.DEFAULT_SELLER['tax_number'])
        with patch('uyumsoft_inbox.Client') as c:
            self.mock(c);self.root.find('{'+A+'}InvoiceLine/{'+B+'}InvoicedQuantity').set('unitCode','KGM');self.preview()
            self.assertIn('adet birimli',self.post(self.path,self.form).text)
            self.root.find('{'+A+'}InvoiceLine/{'+B+'}InvoicedQuantity').set('unitCode','C62');self.preview()
            inv=m.Invoice(invoice_no='INB2026000000001',invoice_type='Satın Alma',customer_id=1339)
            m.db.session.add(inv);m.db.session.commit()
            self.assertIn('zaten var',self.post(self.path,self.form).text)
        self.assertEqual(m.Invoice.query.count(),1);self.assertEqual(m.StockMovement.query.count(),0)
    def test_listing_and_auth(self):
        self.fixture()
        with patch('uyumsoft_inbox.Client') as c:
            c.return_value.inbox_list.return_value=([dict(InvoiceId=self.uid,DocumentId='INB2026000000001',TargetTitle='Test supplier',PayableAmount='198.00')],2,26)
            p=self.client.get('/faturalar/uyumsoft-gelen?fetch=1&start=2026-10-01&end=2026-10-05',base_url=BASE)
            self.assertEqual(p.status_code,200);self.assertIn('Test supplier',p.text);self.assertIn('Sonraki',p.text)
            self.assertEqual(m.Invoice.query.count(),0)
        self.assertEqual(self.app.test_client().get(self.path,base_url=BASE).status_code,302)
        self.assertEqual(self.client.post(self.path,base_url=BASE,headers={'Origin':'https://evil.test'}).status_code,403)

class InboxSoapTests(unittest.TestCase):
    def test_request_and_result_shape(self):
        c=Client({'environment':'test','username':'x','password':'x'})
        r=ET.fromstring(f'<R xmlns="{T}"><Value TotalPages="1" TotalCount="1"><Items><InvoiceId>abc</InvoiceId><DocumentId>NUMBER</DocumentId></Items></Value></R>')
        with patch.object(c,'call',return_value=r) as call:
            rows,pages,total=c.inbox_list('2026-10-01','2026-10-05')
            self.assertEqual(rows[0]['DocumentId'],'NUMBER')
            q=call.call_args.args[1][0];self.assertEqual(q.get('OnlyNewestInvoices'),'false');self.assertEqual(q.get('PageSize'),'25')
        r=ET.fromstring(f'<R xmlns="{T}"><Value InvoiceId="wrong"><Data>JVBERi0=</Data></Value></R>')
        with patch.object(c,'call',return_value=r),self.assertRaises(UyumError):c.inbox_pdf('expected')
