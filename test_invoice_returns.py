import re
import unittest
from datetime import date
from decimal import Decimal
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
import app as m

class ReturnTests(unittest.TestCase):
    tearDown = SupplierRouteTests.tearDown
    def setUp(self):
        SupplierRouteTests.setUp(self)
        product=m.Product(name='Test koltuk',code='TEST')
        invoice=m.Invoice(invoice_no='SALE',invoice_type='Satış',customer_id=1339,invoice_date=date(2026,1,1))
        invoice.items=[m.InvoiceItem(product=product,product_name='Test koltuk',quantity=5,unit_price=100,discount_rate=10,vat_rate=20,vat_included=False)]
        m.db.session.add(invoice);m.db.session.commit()
        self.inv_id=invoice.id;self.item_id=invoice.items[0].id
        self.path=f'/faturalar/{invoice.id}/iade'
    def submit(self,count,number='RET'):
        page=self.client.get(self.path,base_url=BASE)
        self.assertEqual(page.status_code,200)
        token=re.search(r'name="confirmation" value="([^"]+)"',page.text)[1]
        return self.client.post(self.path,base_url=BASE,headers={'Origin':BASE},data={'confirmation':token,'invoice_no':number,'invoice_date':'2026-09-28',f'quantity_{self.item_id}':str(count)})
    def test_partial_return_stock_balance_link_and_delete(self):
        r=self.submit(2);self.assertEqual(r.status_code,302)
        doc=m.Invoice.query.filter_by(invoice_no='RET').one()
        self.assertEqual(doc.total_amount,Decimal('216'))
        self.assertEqual(m.db.session.get(m.Customer,1339).balance,Decimal('324'))
        movement=m.StockMovement.query.one();self.assertEqual((movement.movement_type,movement.quantity),('Stok Girişi',2))
        self.assertIn('SALE',self.client.get(r.location,base_url=BASE).text)
        self.assertEqual(self.submit(4,'EXCESS').status_code,400)
        self.assertEqual(m.Invoice.query.count(),2)
        self.client.post(f'/faturalar/{self.inv_id}/sil',base_url=BASE,headers={'Origin':BASE})
        self.assertIsNotNone(m.db.session.get(m.Invoice,self.inv_id))
        self.client.post(f'/faturalar/{doc.id}/sil',base_url=BASE,headers={'Origin':BASE})
        self.assertEqual(m.StockMovement.query.count(),0)
        self.assertEqual(self.submit(5,'FULL').status_code,302)
    def test_duplicate_zero_negative_and_invalid(self):
        for value in [0,-1,'1.5',6]:
            self.assertEqual(self.submit(value).status_code,400)
        self.assertEqual(self.submit(1).status_code,302)
        self.assertEqual(self.submit(1).status_code,400)
        self.assertEqual(m.Invoice.query.count(),2)
    def test_first_use_creates_only_return_link_table(self):
        table=m.db.metadata.tables['invoice_return_line']
        table.drop(m.db.engine)
        self.assertEqual(self.submit(1).status_code,302)
        self.assertEqual(m.db.session.query(m.Invoice).count(),2)
        self.assertEqual(m.db.session.query(m.Order).count(),1)
    def test_inclusive_vat_and_nonstock_return(self):
        original=m.db.session.get(m.InvoiceItem,self.item_id)
        original.product_id=None;original.vat_included=True
        m.db.session.commit()
        self.assertEqual(self.submit(2).status_code,302)
        doc=m.Invoice.query.filter_by(invoice_no='RET').one()
        self.assertEqual(doc.net_amount,Decimal('150'))
        self.assertEqual(doc.vat_amount,Decimal('30'))
        self.assertEqual(m.StockMovement.query.count(),0)

    def test_return_cannot_be_edited_into_purchase(self):
        self.submit(1)
        doc=m.Invoice.query.filter_by(invoice_no='RET').one()
        r=self.client.post(f'/faturalar/{doc.id}/duzenle',base_url=BASE,headers={'Origin':BASE},data={'invoice_type':'Satın Alma'})
        self.assertEqual(r.status_code,302)
        self.assertEqual(doc.invoice_type,'Satış İadesi')

if __name__=='__main__':unittest.main()
