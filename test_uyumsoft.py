import re
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch
from types import SimpleNamespace
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
import app as m
from uyumsoft import snapshot, validate, DEFAULT_SELLER, attempts, connection
from uyumsoft_client import Client, UyumError, invoice_xml, T, B, A, S, ET

class UyumTests(unittest.TestCase):
    setUp=SupplierRouteTests.setUp
    tearDown=SupplierRouteTests.tearDown
    def make_invoice(self):
        c=m.db.session.get(m.Customer,1339);c.tax_number='0123456789';c.tax_office='Test';c.city='İstanbul';c.address='Test adresi'
        i=m.Invoice(invoice_no='BOS-TEST',invoice_type='Satış',customer=c,invoice_date=date(2026,10,2),due_date=date(2026,11,2))
        i.items=[m.InvoiceItem(product_name='Koltuk & Masa',quantity=2,unit_price=Decimal('110'),vat_rate=10,vat_included=True,discount_rate=10,unit='Adet')]
        m.db.session.add(i);m.db.session.commit();self.invoice=i;self.path=f'/faturalar/{i.id}/uyumsoft';return i
    def post(self,path,data):return self.client.post(path,base_url=BASE,headers={'Origin':BASE},data=data)
    def connect(self):
        with patch('uyumsoft.Client') as client:
            client.return_value.is_einvoice.return_value=True
            r=self.post('/yonetim/uyumsoft',dict(DEFAULT_SELLER,username='private-user',password='secret-pass',action='connect'))
            self.assertEqual(r.status_code,302)
    def preview(self):
        r=self.post(self.path,{'action':'preview','district':'Ümraniye'})
        self.assertEqual(r.status_code,200)
        return re.search('name="preview" type="hidden" value="([^"]+)"',r.text)[1]
    def test_xml_calculations_tax_included_discount_and_escaping(self):
        i=self.make_invoice();d=snapshot(i,DEFAULT_SELLER,'Ümraniye');validate(d)
        root=invoice_xml(d,'fake-uuid',False)
        self.assertEqual(root.findtext('{'+B+'}ProfileID'),'EARSIVFATURA')
        self.assertEqual(root.findtext('.//{'+A+'}LegalMonetaryTotal/{'+B+'}PayableAmount'),'198.00')
        self.assertEqual(root.findtext('.//{'+A+'}InvoiceLine/{'+A+'}Price/{'+B+'}PriceAmount'),'100.000000')
        self.assertIn(b'Koltuk &amp; Masa',ET.tostring(root))
        self.assertEqual(root.findtext('.//{'+A+'}AccountingCustomerParty/{'+A+'}Party/{'+A+'}PartyIdentification/{'+B+'}ID'),'0123456789')
        d['items'][0]['vat']='0'
        with self.assertRaises(UyumError):validate(d)
    def test_connection_encrypted_and_invalid_credentials_not_saved(self):
        self.connect()
        with m.db.engine.connect() as c:encrypted=c.execute(connection.select()).mappings().one()['config']
        self.assertNotIn('secret-pass',encrypted)
        page=self.client.get('/yonetim/uyumsoft',base_url=BASE).text
        self.assertNotIn('secret-pass',page)
        with patch('uyumsoft.Client') as client:
            client.return_value.is_einvoice.side_effect=UyumError('Yetki yok')
            self.post('/yonetim/uyumsoft',dict(DEFAULT_SELLER,username='new',password='bad'))
        with m.db.engine.connect() as c:self.assertEqual(c.execute(connection.select()).mappings().one()['config'],encrypted)
    def test_send_idempotent_status_and_no_ledger_changes(self):
        i=self.make_invoice();self.connect();token=self.preview();before=i.customer.balance
        with patch('uyumsoft.Client') as client:
            client.return_value.is_einvoice.return_value=False
            client.return_value.save_draft.return_value={'number':'TEST2026000000001','scenario':'eArchive'}
            form={'action':'send','district':'Ümraniye','preview':token,'confirm':'yes'}
            self.assertEqual(self.post(self.path,form).status_code,302)
            self.assertEqual(self.post(self.path,form).status_code,302)
            self.assertEqual(client.return_value.save_draft.call_count,1)
            client.return_value.status.return_value='Approved'
            self.post(self.path,{'action':'status'})
        with m.db.engine.connect() as c:row=c.execute(attempts.select()).mappings().one()
        self.assertEqual(row['state'],'Approved');self.assertEqual(m.Invoice.query.count(),1)
        self.assertEqual(i.customer.balance,before)
        self.assertEqual(self.post(f'/faturalar/{i.id}/duzenle',{}).status_code,302)
        self.assertEqual(self.post(f'/faturalar/{i.id}/sil',{}).status_code,302)
        self.assertEqual(m.Invoice.query.count(),1)
    def test_timeout_blocks_second_send_and_retains_uuid(self):
        self.make_invoice();self.connect();token=self.preview()
        with patch('uyumsoft.Client') as client:
            client.return_value.is_einvoice.return_value=False
            client.return_value.save_draft.side_effect=UyumError('Zaman aşımı')
            form={'action':'send','district':'Ümraniye','preview':token,'confirm':'yes'}
            self.post(self.path,form);self.post(self.path,form)
            self.assertEqual(client.return_value.save_draft.call_count,1)
        with m.db.engine.connect() as c:row=c.execute(attempts.select()).mappings().one()
        self.assertEqual(row['state'],'unknown');self.assertEqual(len(row['uuid']),36)
    def test_stale_preview_missing_confirm_and_unsupported_invoice(self):
        i=self.make_invoice();self.connect();token=self.preview()
        with patch('uyumsoft.Client') as client:
            self.post(self.path,{'action':'send','district':'Ümraniye','preview':token})
            i.items[0].quantity=3;m.db.session.commit()
            r=self.post(self.path,{'action':'send','district':'Ümraniye','preview':token,'confirm':'yes'})
            self.assertIn('değişti',r.text);client.assert_not_called()
        i.invoice_type='Satın Alma';m.db.session.commit()
        self.assertEqual(self.client.get(self.path,base_url=BASE).status_code,403)
    def test_auth_and_no_get_write(self):
        self.make_invoice()
        self.assertEqual(self.app.test_client().get(self.path,base_url=BASE).status_code,302)
        self.assertEqual(self.client.post(self.path,base_url=BASE,headers={'Origin':'https://evil.test'}).status_code,403)
        with patch('uyumsoft.Client') as client:
            self.assertEqual(self.client.get(self.path,base_url=BASE).status_code,200);client.assert_not_called()
        with patch('uyumsoft.getattr',return_value=False):
            self.assertEqual(self.client.get('/yonetim/uyumsoft',base_url=BASE).status_code,403)

class SoapTests(unittest.TestCase):
    def test_boolean_response_and_no_send_methods(self):
        c=Client({'environment':'test','username':'user','password':'password'})
        response=SimpleNamespace(status_code=200,content=(f'<s:Envelope xmlns:s="{S}"><s:Body><IsEInvoiceUserResponse xmlns="{T}"><IsEInvoiceUserResult IsSucceded="true" Value="false"/></IsEInvoiceUserResponse></s:Body></s:Envelope>').encode())
        with patch('uyumsoft_client.requests.post',return_value=response) as post:
            self.assertFalse(c.is_einvoice('1111111111'))
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            self.assertIn(b'UsernameToken',post.call_args.kwargs['data'])
        for method in ('SendInvoice','SendDraft'):
            with self.assertRaises(UyumError):c.call(method)
    def test_alias_and_status_lookup(self):
        c=Client({'environment':'test','username':'u','password':'p'})
        result=ET.fromstring(f'<Result xmlns="{T}"><Value><ReceiverboxAliases Enabled="true" Alias="urn:mail:test"/></Value></Result>')
        with patch.object(c,'call',return_value=result):self.assertEqual(c.resolve_alias('1111111111'),'urn:mail:test')
        result=ET.fromstring(f'<Result xmlns="{T}"><Value InvoiceId="abc" Status="Draft"/></Result>')
        with patch.object(c,'call',return_value=result):self.assertEqual(c.status('ABC'),'Draft')

if __name__=='__main__':unittest.main()
