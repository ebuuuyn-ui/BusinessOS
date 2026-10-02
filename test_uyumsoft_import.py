import re, json, unittest
from unittest.mock import patch
from datetime import date
from types import SimpleNamespace
import app as m
from test_uyumsoft import UyumTests, OrderDraftTests, DEFAULT_SELLER
from test_web_auth import BASE
from uyumsoft import snapshot, meta, order_attempts, canonical
from uyumsoft_client import invoice_xml, B, A, Client, T, ET, UyumError
from uyumsoft_import import imports, parse_invoice

class ImportTests(unittest.TestCase):
    setUp=UyumTests.setUp
    tearDown=UyumTests.tearDown
    post=UyumTests.post
    connect=UyumTests.connect
    make_order=OrderDraftTests.make_order
    def fixture(self):
        o=self.make_order();self.connect();self.order=o
        product=m.Product(name=o.items[0].product_name,code='IMPORT-TEST');m.db.session.add(product);m.db.session.flush();o.items[0].product_id=product.id;m.db.session.commit()
        d=snapshot(SimpleNamespace(id=o.id,invoice_no=o.order_no,invoice_date=date(2026,10,2),due_date=date(2026,11,2),customer=o.customer,items=o.items,notes='',net_amount=o.net_amount,vat_amount=o.vat_amount),DEFAULT_SELLER,'Test')
        self.uid='11111111-2222-3333-4444-555555555555';self.root=invoice_xml(d,self.uid,True)
        self.root.find('{'+B+'}ID').text='ABC2026000000001'
        meta.create_all(m.db.engine)
        with m.db.engine.begin() as c:c.execute(order_attempts.insert().values(order_id=o.id,uuid=self.uid,state='Draft',snapshot=canonical(d),account='x'))
        self.path=f'/siparisler/{o.id}/uyumsoft-bos'
    def mocked(self,c):
        c.return_value.status.return_value='Approved';c.return_value.outbox_invoice.return_value=self.root;c.return_value.outbox_pdf.return_value=b'%PDF-1.4\n test'
    def test_import_pdf_stock_idempotence(self):
        self.fixture();before=m.StockMovement.query.count()
        with patch('uyumsoft_import.Client') as c:
            self.mocked(c)
            page=self.post(self.path,{'action':'fetch'})
            self.assertIn('ABC2026000000001',page.text)
            self.assertEqual(m.Invoice.query.count(),0)
            token=re.search('name="token" value="([^"]+)"',page.text)[1]
            r=self.post(self.path,{'action':'save','token':token});self.assertEqual(r.status_code,302)
            self.post(self.path,{'action':'save','token':token})
        self.assertEqual(m.Invoice.query.count(),1)
        self.assertEqual(m.StockMovement.query.count(),before+len(self.order.items))
        inv=m.Invoice.query.one()
        self.assertEqual(inv.total_amount,self.order.total_amount)
        pdf=self.client.get(f'/faturalar/{inv.id}/uyumsoft-pdf',base_url=BASE)
        self.assertTrue(pdf.data.startswith(b'%PDF-'))
        self.post(f'/faturalar/{inv.id}/sil',{});self.assertEqual(m.Invoice.query.count(),1)
    def test_draft_and_changed_remote_invoice_block(self):
        self.fixture()
        with patch('uyumsoft_import.Client') as c:
            self.mocked(c);c.return_value.status.return_value='Draft'
            self.assertIn('henüz',self.post(self.path,{'action':'fetch'}).text)
            c.return_value.outbox_invoice.assert_not_called()
            c.return_value.status.return_value='Approved'
            token=re.search('name="token" value="([^"]+)"',self.post(self.path,{'action':'fetch'}).text)[1]
            self.root.find('{'+B+'}ID').text='ABC2026000000002'
            self.assertIn('değişti',self.post(self.path,{'action':'save','token':token}).text)
            c.return_value.outbox_pdf.assert_not_called()
        self.assertEqual(m.Invoice.query.count(),0)
    def test_identity_totals_and_pdf_mismatch(self):
        self.fixture()
        with self.assertRaises(UyumError):parse_invoice(self.root,'different',DEFAULT_SELLER['tax_number'],'0123456789')
        self.root.find('{'+A+'}LegalMonetaryTotal/{'+B+'}PayableAmount').text='1'
        with self.assertRaises(UyumError):parse_invoice(self.root,self.uid,DEFAULT_SELLER['tax_number'],'0123456789')
        c=Client({'environment':'test','username':'x','password':'x'})
        result=ET.fromstring(f'<R xmlns="{T}"><Value InvoiceId="wrong"><Data>JVBERi0=</Data></Value></R>')
        with patch.object(c,'call',return_value=result),self.assertRaises(UyumError):c.outbox_pdf(self.uid)
