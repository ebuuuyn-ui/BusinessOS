import re
import unittest
from datetime import date
from decimal import Decimal as D
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
import app as m

class InvoiceFifoTests(unittest.TestCase):
    setUp = SupplierRouteTests.setUp
    tearDown = SupplierRouteTests.tearDown
    def invoice(self, no, kind, day, qty, price, product=None, **kw):
        if product is None:
            product = getattr(self, 'product', None)
        doc=m.Invoice(invoice_no=no,invoice_type=kind,customer_id=1339,invoice_date=date.fromisoformat(day))
        item=m.InvoiceItem(product=product,product_name='Test',quantity=qty,unit_price=D(price),vat_rate=0,**kw)
        doc.items=[item];m.db.session.add(doc);m.db.session.flush();return doc,item
    def load(self, day='2026-12-31'):
        return self.app.extensions['invoice_fifo']['load'](date.fromisoformat(day))
    def make_product(self):
        self.product=m.Product(name='Test',code='FIFO');m.db.session.add(self.product);m.db.session.flush()
    def test_fifo_consumes_history_not_last_price_or_order(self):
        self.make_product()
        self.invoice('BUY1','Satın Alma','2026-01-01',3,'100')
        self.invoice('BUY2','Satın Alma','2026-02-01',4,'200')
        self.invoice('OLD','Satış','2026-02-02',2,'300')
        doc,item=self.invoice('NEW','Satış','2026-03-01',3,'400')
        m.db.session.commit()
        r=self.load()[2][item.id]
        self.assertEqual(r['cost'],D('500.00'));self.assertEqual(r['missing_quantity'],0)
        for period in ['month','year']:
            page=self.client.get('/kar-zarar?date=2026-03-01&period='+period,base_url=BASE)
            self.assertEqual(page.status_code,200);self.assertIn('NEW',page.text)
        self.assertNotIn('SA-TEST',page.text)
    def test_manual_cost_persists_rejects_stale_and_changes_no_stock(self):
        self.make_product()
        self.invoice('BUY','Satın Alma','2026-01-01',1,'100')
        doc,item=self.invoice('SALE','Satış','2026-03-01',3,'300')
        m.db.session.commit()
        path='/kar-zarar?date=2026-03-01'
        page=self.client.get(path,base_url=BASE)
        self.assertIn('Kesinleşmedi',page.text)
        token=re.search(r'name="confirmation" value="([^"]+)"',page.text)[1]
        data={'period':'month','date':'2026-03-01','confirmation':token,'amount':'250,01','reason':'Eski alış belgesi'}
        response=self.client.post(path,base_url=BASE,headers={'Origin':BASE},data=data)
        self.assertEqual(response.status_code,302)
        result=self.load()[2][item.id]
        self.assertEqual(result['cost'],D('350.01'));self.assertEqual(result['missing_quantity'],0)
        self.assertEqual(m.StockMovement.query.count(),0);self.assertEqual(doc.net_amount,D('900'))
        page=self.client.get(path,base_url=BASE);self.assertNotIn('Kesinleşmedi',page.text)
        self.client.post(path,base_url=BASE,headers={'Origin':BASE},data=data)
        journal=self.app.extensions['invoice_fifo']['journal']
        self.assertEqual(m.db.session.execute(m.db.select(m.db.func.count()).select_from(journal)).scalar(),1)
        item.quantity=4;m.db.session.commit()
        self.assertEqual(self.load()[2][item.id]['missing_quantity'],3)
    def test_return_and_rounding_annual_equals_months(self):
        self.make_product()
        self.invoice('BUY','Satın Alma','2026-01-01',3,'0.33333333')
        source,item=self.invoice('SALE','Satış','2026-02-01',3,'10')
        link=m.db.metadata.tables['invoice_return_line']
        returned=[]
        for n in range(3):
            _,ri=self.invoice('RET'+str(n),'Satış İadesi',f'2026-0{3+n}-01',1,'10')
            m.db.session.execute(link.insert().values(return_item_id=ri.id,source_item_id=item.id));returned.append(ri)
        m.db.session.commit();r=self.load()[2]
        self.assertEqual(r[item.id]['cost'],D('1.00'))
        self.assertEqual(sum((r[i.id]['cost'] for i in returned),D('0')),D('-1.00'))
    def test_invoice_movement_not_doubled_unknown_opening_consumed(self):
        self.make_product()
        doc,item=self.invoice('BUY','Satın Alma','2026-02-01',1,'100')
        move=m.StockMovement(product=self.product,movement_type='Stok Girişi',quantity=1,movement_date=doc.invoice_date)
        m.db.session.add(move);m.db.session.flush();item.stock_movement_id=move.id
        m.db.session.add(m.StockMovement(product=self.product,movement_type='Açılış Stoğu',quantity=1,movement_date=date(2026,1,1)))
        _,sale=self.invoice('SALE','Satış','2026-03-01',3,'300');m.db.session.commit()
        r=self.load()[2][sale.id];self.assertEqual(r['cost'],D('100'));self.assertEqual(r['missing_quantity'],2)
    def test_monthly_totals_add_to_annual_without_cent_drift(self):
        self.make_product()
        self.invoice('BUY','Satın Alma','2026-01-01',3,'33.33333333')
        for month in (2,3,4):
            self.invoice('S'+str(month),'Satış',f'2026-0{month}-01',1,'50')
        m.db.session.commit()
        from flask import template_rendered
        def context(path):
            captured=[]
            def capture(sender,template,context,**extra):captured.append(context)
            template_rendered.connect(capture,self.app)
            try:self.assertEqual(self.client.get(path,base_url=BASE).status_code,200)
            finally:template_rendered.disconnect(capture,self.app)
            return captured[-1]
        annual=context('/kar-zarar?period=year&date=2026-04-01')
        months=[context(f'/kar-zarar?period=month&date=2026-0{n}-01') for n in (2,3,4)]
        for field in ('revenue','cost','expenses_total','net_profit'):
            self.assertEqual(annual[field],sum((c[field] for c in months),D('0')))
        self.assertEqual(annual['cost'],D('100.00'))
        mali=context('/mali-tablolar?period=year&date=2026-12-31')
        self.assertEqual(mali['sales_cost'],annual['cost'])
        self.assertEqual(mali['net_profit'],annual['net_profit'])

    def test_invalid_manual_values_and_explicit_zero(self):
        _,item=self.invoice('SALE','Satış','2026-03-01',1,'30');m.db.session.commit()
        path='/kar-zarar?date=2026-03-01'
        page=self.client.get(path,base_url=BASE)
        token=re.search(r'name="confirmation" value="([^\"]+)"',page.text)[1]
        data=dict(date='2026-03-01',confirmation=token,reason='Bedelsiz ürün')
        for amount in ('-1','NaN','Infinity','1.001',''):
            self.client.post(path,base_url=BASE,headers={'Origin':BASE},data=dict(data,amount=amount))
            self.assertEqual(self.load()[2][item.id]['missing_quantity'],1)
        self.assertEqual(self.client.post(path,base_url=BASE,headers={'Origin':BASE},data=dict(data,amount='0,00')).status_code,302)
        self.assertEqual(self.load()[2][item.id]['missing_quantity'],0)

    def test_purchase_header_rounding_difference_is_preserved(self):
        self.make_product()
        doc,item=self.invoice('BUY','Satın Alma','2026-01-01',1,'0.33333333')
        doc.items.append(m.InvoiceItem(product=self.product,product_name='Test',quantity=1,unit_price=D('0.33333333'),vat_rate=0))
        doc.source_net_amount=D('0.67')
        _,sale=self.invoice('SALE','Satış','2026-02-01',2,'1');m.db.session.commit()
        self.assertEqual(self.load()[2][sale.id]['cost'],D('0.67'))

    def test_unlinked_sales_need_manual_cost_and_get_does_not_create_table(self):
        _,item=self.invoice('SALE','Satış','2026-03-01',1,'30');m.db.session.commit()
        journal=self.app.extensions['invoice_fifo']['journal'];journal.drop(m.db.engine)
        page=self.client.get('/kar-zarar?date=2026-03-01',base_url=BASE)
        self.assertEqual(page.status_code,200)
        from sqlalchemy import inspect
        self.assertFalse(inspect(m.db.engine).has_table(journal.name))
        self.assertEqual(self.load()[2][item.id]['missing_quantity'],1)

if __name__=='__main__':unittest.main()
