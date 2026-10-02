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
DEFAULT_SELLER = dict(DEFAULT_SELLER, tax_number='1111111111')
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
    def test_sender_rejection_reprepare_preserves_history_and_blocks_timeout(self):
        from uyumsoft import rejected_history
        self.make_invoice();self.connect();token=self.preview()
        with patch('uyumsoft.Client') as client:
            client.return_value.is_einvoice.return_value=False
            client.return_value.save_draft.side_effect=UyumError('Uyumsoft: Vergi Kimlik Numarası AccountingSupplierParty alanındakinden farklıdır.')
            self.post(self.path,{'action':'send','district':'Ümraniye','preview':token,'confirm':'yes'})
        self.assertIn('Önce bağlantıdaki',self.post(self.path,{'action':'reprepare'}).text)
        with patch('uyumsoft.Client') as client:
            self.post('/yonetim/uyumsoft',dict(DEFAULT_SELLER,name='Test Kişi',tax_number='11111111110',username='private-user',password='',action='connect'))
        with patch('uyumsoft.Client') as client:
            self.assertEqual(self.post(self.path,{'action':'reprepare'}).status_code,302)
            client.assert_not_called()
        with m.db.engine.connect() as c:
            self.assertIsNone(c.execute(attempts.select()).first())
            self.assertEqual(len(c.execute(rejected_history.select()).all()),1)
        token=self.preview()
        with patch('uyumsoft.Client') as client:
            client.return_value.is_einvoice.return_value=False
            client.return_value.save_draft.side_effect=UyumError('Zaman aşımı')
            self.post(self.path,{'action':'send','district':'Ümraniye','preview':token,'confirm':'yes'})
        self.assertIn('Yalnızca kesin',self.post(self.path,{'action':'reprepare'}).text)
        root=invoice_xml(snapshot(self.invoice,dict(DEFAULT_SELLER,name='Test Kişi',tax_number='11111111110'),'Ümraniye'),'x',True)
        ident=root.find('.//{'+A+'}AccountingSupplierParty/{'+A+'}Party/{'+A+'}PartyIdentification/{'+B+'}ID')
        self.assertEqual(ident.get('schemeID'),'TCKN')
        self.assertEqual(root.findtext('.//{'+A+'}Person/{'+B+'}FamilyName'),'Kişi')

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

class OrderDraftTests(UyumTests):
    def make_order(self):
        o=m.db.session.get(m.Order,self.order_id);o.order_type='Satış';o.order_no='SS-TEST'
        o.customer.tax_number='0123456789';o.customer.tax_office='Test';o.customer.city='İstanbul';o.customer.address='Test adres'
        m.db.session.commit();self.path=f'/siparisler/{o.id}/uyumsoft';return o
    def test_order_entry_offline_preview_no_bookkeeping(self):
        from uyumsoft import month_after
        o=self.make_order();self.assertEqual(month_after(date(2026,1,31)),date(2026,2,28))
        r=self.client.get(f'/siparisler/{o.id}/faturaya-aktar',base_url=BASE)
        self.assertTrue(r.location.endswith(self.path))
        before=(m.Invoice.query.count(),m.StockMovement.query.count(),m.AccountTransaction.query.count())
        with patch('uyumsoft.Client') as c, patch('uyumsoft.DEFAULT_SELLER',DEFAULT_SELLER):
            page=self.client.get(self.path,base_url=BASE)
            self.assertIn('Uyumsoft tarafından verilecek',page.text)
            self.assertNotIn('name="invoice_no"',page.text)
            r=self.post(self.path,{'action':'preview','district':'Ümraniye','invoice_date':'2026-10-02','due_date':'2026-11-02'})
            self.assertIn('Aktarılacak taslağı kontrol edin',r.text);self.assertIn('disabled',r.text);c.assert_not_called()
        self.assertEqual(before,(m.Invoice.query.count(),m.StockMovement.query.count(),m.AccountTransaction.query.count()))
    def test_order_export_immutable_snapshot_and_no_invoice_number(self):
        from uyumsoft import order_attempts
        o=self.make_order();self.connect()
        form={'action':'preview','district':'Ümraniye','invoice_date':'2026-10-02','due_date':'2026-11-02','buyer_address':'Düzeltilen adres','notes':'Özel fatura notu'}
        r=self.post(self.path,form);token=re.search('name="preview" type="hidden" value="([^"]+)"',r.text)[1]
        with patch('uyumsoft.Client') as client:
            client.return_value.is_einvoice.return_value=False
            client.return_value.save_draft.return_value={'number':'TEST2026000000010','scenario':'eArchive'}
            form.update(action='send',preview=token,confirm='yes')
            self.assertEqual(self.post(self.path,form).status_code,302)
            self.post(self.path,form);self.assertEqual(client.return_value.save_draft.call_count,1)
            sent=client.return_value.save_draft.call_args.args[0]
            self.assertEqual(sent['reference'],'SS-TEST');self.assertEqual(sent['buyer']['address'],'Düzeltilen adres')
        self.assertEqual(m.Invoice.query.count(),0);self.assertEqual(m.StockMovement.query.count(),0);self.assertEqual(m.AccountTransaction.query.count(),0)
        self.assertEqual(o.customer.address,'Test adres')
        with m.db.engine.connect() as c:self.assertEqual(c.execute(order_attempts.select()).mappings().one()['state'],'Draft')
        page=self.client.get(self.path,base_url=BASE)
        self.assertEqual(page.status_code,200);self.assertIn('TEST2026000000010',page.text)
    def test_order_stale_preview_and_edit_preserve_values(self):
        o=self.make_order();self.connect()
        form={'action':'preview','district':'Ümraniye','invoice_date':'2026-10-02','due_date':'2026-11-02','notes':'Kalsın'}
        r=self.post(self.path,form);token=re.search('name="preview" type="hidden" value="([^"]+)"',r.text)[1]
        edited=self.post(self.path,dict(form,action='send',edit='yes',preview=token))
        self.assertIn('Kalsın',edited.text);self.assertNotIn('name="preview"',edited.text)
        o.items[0].quantity+=1;m.db.session.commit()
        with patch('uyumsoft.Client') as c:
            r=self.post(self.path,dict(form,action='send',preview=token,confirm='yes'));self.assertIn('değişti',r.text);c.assert_not_called()
    def test_order_with_existing_invoice_blocked_and_purchase_forbidden(self):
        o=self.make_order();i=self.make_invoice();i.order_id=o.id;m.db.session.commit();self.path=f'/siparisler/{o.id}/uyumsoft'
        r=self.post(self.path,{'action':'preview','district':'Ümraniye'});self.assertIn('bağlı BOS faturası var',r.text)
        o.order_type='Satın Alma';m.db.session.commit()
        self.assertEqual(self.client.get(self.path,base_url=BASE).status_code,403)
