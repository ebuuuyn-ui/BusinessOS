"""Route scope and stale previews tested in an isolated in-memory database."""
import os
import re
import tempfile
import unittest
from unittest.mock import patch

_data=tempfile.TemporaryDirectory(prefix='bos-supplier-route-tests-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name, BUSINESSOS_BACKUP_DIR=_data.name,
                  DATABASE_URL='sqlite:///:memory:')
import app as m
from test_web_auth import ENV, BASE, login


class SupplierRouteTests(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ,ENV):
            self.app=m.create_app({'TESTING':True,'SQLALCHEMY_DATABASE_URI':'sqlite:///:memory:',
                                   'SECRET_KEY':ENV['SECRET_KEY']})
        self.ctx=self.app.app_context(); self.ctx.push(); m.db.create_all()
        customer=m.Customer(id=1339,name='ABİKA TEST')
        order=m.Order(order_no='SA-TEST',customer=customer,order_type='Satın Alma')
        order.items=[m.OrderItem(product_name='SSH minder',quantity=6,unit_price=987654)]
        m.db.session.add(order); m.db.session.commit(); self.order_id=order.id
        self.path=f'/siparisler/{order.id}/tedarikci-tablosu'
        self.client=self.app.test_client(); login(self.client)

    def tearDown(self):
        m.db.session.remove(); m.db.engine.dispose(); self.ctx.pop()

    def post(self,data):
        return self.client.post(self.path,base_url=BASE,headers={'Origin':BASE},data=data)

    def test_preview_renders_without_financial_values_or_google_calls(self):
        with patch('supplier_sheets.SheetsClient') as client:
            response=self.client.get(self.path,base_url=BASE)
        self.assertEqual(response.status_code,200)
        self.assertIn('SSH minder',response.text); self.assertNotIn('987654',response.text)
        self.assertIn('kurulumu bekleniyor',response.text); client.assert_not_called()

    def test_anonymous_and_other_suppliers_cannot_access(self):
        self.assertEqual(self.app.test_client().get(self.path,base_url=BASE).status_code,302)
        order=m.db.session.get(m.Order,self.order_id); order.order_type='Satış'; m.db.session.commit()
        self.assertEqual(self.client.get(self.path,base_url=BASE).status_code,403)

    def test_missing_and_stale_preview_prevent_export(self):
        with patch('supplier_sheets.SheetsClient') as client:
            self.assertEqual(self.post({}).status_code,400)
            page=self.client.get(self.path,base_url=BASE).text
            token=re.search(r'name="preview" value="([^"]+)"',page)[1]
            order=m.db.session.get(m.Order,self.order_id); order.items[0].quantity=7; m.db.session.commit()
            response=self.post({'preview':token})
            self.assertEqual(response.status_code,400)
            self.assertIn('Sipariş önizlemeden sonra değişti',response.text)
            client.assert_not_called()

    def test_cross_origin_submission_blocked(self):
        r=self.client.post(self.path,base_url=BASE,headers={'Origin':'https://wrong.test'})
        self.assertEqual(r.status_code,403)

if __name__=='__main__': unittest.main()
