import io,os,tempfile,unittest,re,html,json
from datetime import date
from decimal import Decimal
from openpyxl import Workbook
_data=tempfile.TemporaryDirectory(prefix='stock-import-test-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,BUSINESSOS_BACKUP_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m
from stock_excel_import import read_products,protection,snapshots


def workbook(rows):
    w=Workbook();s=w.active;s.append(['Title']);s.append(['Ürün Kodu','Stok Adı','Liste Fiyat (TL)','ABİKA Satış Fiyatı'])
    for r in rows:s.append(r)
    b=io.BytesIO();w.save(b);return b.getvalue()

class StockImportTests(unittest.TestCase):
    def setUp(self):
        self.ctx=m.app.app_context();self.ctx.push();m.db.drop_all();m.db.create_all();self.client=m.app.test_client()
        self.used=m.Product(code='USED',name='Protected',unit_price=100,purchase_price=50)
        self.free=m.Product(code='FREE',name='Old',unit_price=5,purchase_price=2,group_name='Keep')
        self.absent=m.Product(code='ABSENT',name='Absent');self.blank=m.Product(code='BLANK',name='Missing price')
        m.db.session.add_all([self.used,self.free,self.absent,self.blank]);m.db.session.flush()
        m.db.session.add(m.StockMovement(product_id=self.used.id,movement_type='Stok Girişi',quantity=1,movement_date=date.today()))
        m.db.session.commit()
        self.rows=[['USED','Must not change',200,70],['FREE','New name',20.12,10.34],['NEW','New product',30,15],['BLANK','Skip',40,None]]
    def tearDown(self):m.db.session.remove();self.ctx.pop()
    def preview(self):
        r=self.client.post('/urunler/excel-guncelle',data={'file':(io.BytesIO(workbook(self.rows)),'test.xlsx'),'deactivate':'on','skip_missing':'on'})
        self.assertEqual(r.status_code,200,r.data)
        return html.unescape(re.search(r'name="plan" value="([^"]+)"',r.get_data(as_text=True)).group(1))
    def test_preview_atomic_apply_audit_and_replay(self):
        before=snapshots(m.Product.query.order_by(m.Product.id).all());token=self.preview()
        self.assertEqual(before,snapshots(m.Product.query.order_by(m.Product.id).all()))
        response=self.client.post('/urunler/excel-guncelle',data={'action':'apply','plan':token})
        self.assertEqual(response.status_code,302,response.data)
        self.assertEqual(snapshots([self.used])[0],before[0]);self.assertEqual(self.free.name,'New name');self.assertEqual(self.free.unit_price,Decimal('20.12'));self.assertEqual(self.free.group_name,'Keep');self.assertFalse(self.absent.active);self.assertTrue(self.blank.active)
        self.assertEqual(m.Product.query.count(),5);self.assertEqual(m.StockMovement.query.count(),1)
        audit=json.loads(m.StoredFile.query.one().content)
        self.assertEqual(audit['before'],before);self.assertEqual(audit['counts'],{'Güncelle':1,'Pasife al':1,'Yeni':1})
        self.assertEqual(len(audit['protected']),1)
        self.assertEqual(self.client.post('/urunler/excel-guncelle',data={'action':'apply','plan':token}).status_code,302)
        self.assertEqual(m.Product.query.count(),5);self.assertEqual(m.StoredFile.query.count(),1)
    def test_new_movement_after_preview_aborts_all(self):
        token=self.preview();m.db.session.add(m.StockMovement(product_id=self.free.id,movement_type='Stok Girişi',quantity=1));m.db.session.commit()
        self.assertEqual(self.client.post('/urunler/excel-guncelle',data={'action':'apply','plan':token}).status_code,400)
        self.assertEqual(self.free.name,'Old');self.assertTrue(self.absent.active);self.assertEqual(m.Product.query.count(),4)
    def test_validation_and_signature(self):
        for rows in [[['A','A',1,None]],[['A','A',1,1],['A','B',2,2]],[['A','A',-1,2]]]:
            with self.assertRaises(ValueError):read_products(workbook(rows))
        self.assertEqual(self.client.post('/urunler/excel-guncelle',data={'action':'apply','plan':'tampered'}).status_code,400)
    def test_all_reference_types_and_legacy_names_protected(self):
        customer=m.Customer(name='Customer');m.db.session.add(customer)
        order=m.Order(order_no='T',order_type='Satış',status='İptal Edildi',customer=customer)
        order.items.append(m.OrderItem(product_id=self.free.id,product_name='Old',quantity=1,unit_price=1))
        order.items.append(m.OrderItem(product_name='Missing price',quantity=1,unit_price=1))
        invoice=m.Invoice(customer=customer,invoice_no='I',invoice_type='Satış');invoice.items.append(m.InvoiceItem(product_id=self.absent.id,product_name='Absent',quantity=1,unit_price=1))
        m.db.session.add_all([order,invoice,m.OzonSale(product_id=self.used.id,product_name='Protected')]);m.db.session.commit()
        reasons=protection(m.db,m.Product.query.all())
        self.assertEqual(set(reasons),{self.used.id,self.free.id,self.absent.id,self.blank.id})
        token=self.preview();r=self.client.post('/urunler/excel-guncelle',data={'action':'apply','plan':token});self.assertEqual(r.status_code,302)
        self.assertEqual(self.free.name,'Old');self.assertTrue(self.absent.active)

if __name__=='__main__':unittest.main()
