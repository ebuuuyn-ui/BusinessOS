import os,tempfile,unittest,json
from sqlalchemy import text
_data=tempfile.TemporaryDirectory()
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m

class ProcurementLinksTests(unittest.TestCase):
    def setUp(self):
        self.context=m.app.app_context();self.context.push()
        m.db.drop_all();m.db.create_all()
        c=m.Customer(name='Test'); p=m.Product(name='CEBRA',code='CEBRA')
        self.sale=m.Order(order_no='SS-93',customer=c,order_type='Satış',payment_method='Banka Havalesi')
        self.sale.items=[m.OrderItem(product=p,product_name='CEBRA',quantity=4,unit='Adet',variant='Lacivert',unit_price=10)]
        m.db.session.add(self.sale);m.db.session.flush()
        self.purchase=m.Order(order_no='SA-90',customer=c,order_type='Satın Alma',source_order_id=self.sale.id)
        self.purchase.items=[m.OrderItem(product=p,product_name='CEBRA',quantity=4,unit='Adet',variant='Lacivert',detail_2='Decofisten gelenlerin içinde',unit_price=5,source_order_item_id=self.sale.items[0].id)]
        m.db.session.add(self.purchase);m.db.session.commit()
        self.client=m.app.test_client()
    def tearDown(self):
        m.db.session.rollback()
        m.db.session.execute(text('DROP TABLE IF EXISTS business_audit_event'));m.db.session.commit()
        m.db.session.remove();self.context.pop()
    def edit(self,order,**overrides):
        item=order.items[0]
        data={'customer_id':str(order.customer_id),'payment_method':'Banka Havalesi','source_order_id':str(order.source_order_id or ''),'order_item_id[]':str(item.id),'product_id[]':str(item.product_id),'product_name[]':item.product_name,'quantity[]':str(item.quantity),'unit[]':'Adet','variant[]':item.variant,'detail_2[]':'Not değişti','unit_price[]':'12','vat_rate[]':'10'}
        data.update(overrides)
        return self.client.post(f'/siparisler/{order.id}/duzenle',data=data)
    def test_edit_preserves_both_ids_and_purchase_source(self):
        sale_id=self.sale.items[0].id;purchase_id=self.purchase.items[0].id
        self.assertEqual(self.edit(self.sale).status_code,302)
        self.assertEqual(self.edit(self.purchase).status_code,302)
        self.assertEqual(self.sale.items[0].id,sale_id)
        self.assertEqual(self.purchase.items[0].id,purchase_id)
        self.assertEqual(self.purchase.items[0].source_order_item_id,sale_id)
        self.assertEqual(m.procurement_summary(self.sale)['code'],'complete')
    def test_stale_form_cannot_replace_existing_rows(self):
        old=self.sale.items[0].id
        response=self.client.post(f'/siparisler/{self.sale.id}/duzenle',data={'customer_id':self.sale.customer_id,'payment_method':'Banka Havalesi','product_name[]':'CEBRA','quantity[]':'4'})
        self.assertEqual(response.status_code,302)
        self.assertIn('/duzenle',response.location)
        self.assertEqual(self.sale.items[0].id,old)
    def test_foreign_row_id_is_rejected(self):
        self.assertEqual(self.edit(self.sale,**{'order_item_id[]':str(self.purchase.items[0].id)}).status_code,400)
    def test_changed_product_does_not_count_previous_purchase(self):
        old=self.sale.items[0].id
        p=m.Product(name='OTHER',code='OTHER');m.db.session.add(p);m.db.session.commit()
        self.assertEqual(self.edit(self.sale,**{'product_id[]':str(p.id),'product_name[]':'OTHER'}).status_code,302)
        self.assertNotEqual(self.sale.items[0].id,old)
        self.assertEqual(m.procurement_summary(self.sale)['covered_quantity'],0)
    def test_immutable_history_recovers_link_despite_supplier_note(self):
        item=self.sale.items[0]
        self.purchase.items[0].source_order_item_id=999
        m.db.session.commit()
        self.assertEqual(m.procurement_summary(self.sale)['covered_quantity'],0)
        m.db.session.execute(text('CREATE TABLE business_audit_event (id INTEGER PRIMARY KEY, table_name TEXT, operation TEXT, before_data TEXT)'))
        snapshot={'id':999,'order_id':self.sale.id,**{field:getattr(item,field) for field in ('product_id','product_name','variant','detail_2','detail_3','unit')}}
        m.db.session.execute(text("INSERT INTO business_audit_event VALUES (1,'order_item','DELETE',:data)"),{'data':json.dumps(snapshot)})
        m.db.session.commit()
        self.assertEqual(m.procurement_summary(self.sale)['covered_quantity'],4)
        self.assertEqual(self.purchase.items[0].source_order_item_id,999)
        self.purchase.status='İptal Edildi';m.db.session.commit()
        self.assertEqual(m.procurement_summary(self.sale)['covered_quantity'],0)
