import os,tempfile,unittest,re,html,json
_tmp=tempfile.TemporaryDirectory()
os.environ.update(BUSINESSOS_DATA_DIR=_tmp.name,BUSINESSOS_BACKUP_DIR=_tmp.name,DATABASE_URL='sqlite:///:memory:')
import app as m
from stock_excel_import import snapshots

class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.ctx=m.app.app_context();self.ctx.push();m.db.drop_all();m.db.create_all();self.client=m.app.test_client()
        self.keep=m.Product(code='Y00101',name='Current',unit_price=100)
        self.old=m.Product(code='01.001',name='Old',unit_price=50)
        self.inactive=m.Product(code='INACTIVE',name='Inactive',active=False)
        self.c=m.Customer(name='Customer');m.db.session.add_all([self.keep,self.old,self.inactive,self.c]);m.db.session.flush()
        self.order=m.Order(order_no='S1',order_type='Satış',customer=self.c)
        self.order.items.append(m.OrderItem(product_id=self.old.id,product_name='Historic order name',quantity=2,unit_price=10))
        self.invoice=m.Invoice(invoice_no='I1',invoice_type='Satış',customer=self.c)
        self.invoice.items.append(m.InvoiceItem(product_id=self.old.id,product_name='Historic invoice name',quantity=2,unit_price=10))
        m.db.session.add_all([self.order,self.invoice,m.StockMovement(product_id=self.old.id,movement_type='Stok Girişi',quantity=2)]);m.db.session.commit()
    def tearDown(self):m.db.session.remove();self.ctx.pop()
    def preview(self):
        response=self.client.post('/yonetim/stok-arsivle',data={'codes':'Y00101'})
        self.assertEqual(response.status_code,200,response.data)
        return html.unescape(re.search(r'name="plan" value="([^"]+)"',response.text).group(1))
    def test_history_preserved_and_only_active_changes_with_backup(self):
        before=snapshots(m.Product.query.order_by(m.Product.id).all())
        histories={model.__name__:snapshots(model.query.all()) for model in [m.Order,m.OrderItem,m.Invoice,m.InvoiceItem,m.StockMovement]}
        token=self.preview();self.assertTrue(self.old.active)
        r=self.client.post('/yonetim/stok-arsivle',data={'action':'apply','plan':token});self.assertEqual(r.status_code,302,r.data)
        expected=[dict(row,active=False) if row['id']==self.old.id else row for row in before]
        self.assertEqual(snapshots(m.Product.query.order_by(m.Product.id).all()),expected)
        for model in [m.Order,m.OrderItem,m.Invoice,m.InvoiceItem,m.StockMovement]:self.assertEqual(snapshots(model.query.all()),histories[model.__name__])
        saved=json.loads(m.StoredFile.query.one().content);self.assertEqual(saved['before'],before);self.assertEqual(saved['after'],expected)
        self.assertEqual(self.client.post('/yonetim/stok-arsivle',data={'action':'apply','plan':token}).status_code,302)
        self.assertEqual(m.StoredFile.query.count(),1)
    def test_stale_preview_cannot_apply(self):
        token=self.preview();self.old.unit_price=123;m.db.session.commit()
        self.assertEqual(self.client.post('/yonetim/stok-arsivle',data={'action':'apply','plan':token}).status_code,400);self.assertTrue(self.old.active)
    def test_invalid_unknown_empty_codes_cannot_deactivate(self):
        for codes in ['', 'NOT-FOUND','Y00101\n<script>']:
            self.assertEqual(self.client.post('/yonetim/stok-arsivle',data={'codes':codes}).status_code,400)
        self.assertTrue(self.old.active)
        self.assertEqual(self.client.post('/yonetim/stok-arsivle',data={'action':'apply','plan':'invalid'}).status_code,400)
    def test_stock_list_hides_inactive_but_history_still_accessible(self):
        r=self.client.get("/urunler")
        self.assertNotIn(b"Inactive",r.data)
        token=self.preview();self.client.post("/yonetim/stok-arsivle",data={"action":"apply","plan":token})
        r=self.client.get("/urunler?stock_product_id="+str(self.old.id))
        self.assertEqual(r.status_code,200)
        self.assertEqual(m.StockMovement.query.filter_by(product_id=self.old.id).count(),1)

    def test_owner_only(self):
        m.app.config['WEB_AUTH_ENABLED']=True
        try:self.assertEqual(self.client.get('/yonetim/stok-arsivle').status_code,403)
        finally:m.app.config['WEB_AUTH_ENABLED']=False

if __name__=='__main__':unittest.main()
