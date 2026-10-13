import os,tempfile,unittest
from io import BytesIO
from PIL import Image
from openpyxl import load_workbook
_data=tempfile.TemporaryDirectory()
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m
from supplier_trello import order_form_pdf,order_rows
from order_images import product_images

class FormImages(unittest.TestCase):
 def test_images_in_sales_and_purchase_downloads_and_supplier_pdf(self):
  with m.app.app_context():
   m.db.create_all();c=m.Customer(name='Test');p=m.Product(name='Koltuk',code='TEST-IMAGE',unit_price=10)
   m.db.session.add_all([c,p]);m.db.session.flush()
   b=BytesIO();Image.new('RGB',(120,80),'blue').save(b,'JPEG')
   m.db.session.add(m.ProductQuoteImage(product_id=p.id,content=b.getvalue()))
   for i,kind in enumerate(('Satış','Satın Alma')):
    order=m.Order(order_no=f'TEST-{i}',customer=c,order_type=kind)
    order.items=[m.OrderItem(product=p,product_name=p.name,quantity=2,unit_price=10),m.OrderItem(product_name='Görselsiz',quantity=1,unit_price=5)]
    m.db.session.add(order);m.db.session.commit()
    for ext in ('pdf','excel'):
     r=m.app.test_client().get(f'/siparisler/{order.id}/{ext}');self.assertEqual(r.status_code,200)
     if ext=='pdf':self.assertIn(b'/Subtype /Image',r.data);open('/tmp/bos-form-'+str(i)+'.pdf','wb').write(r.data)
     else:
      w=load_workbook(BytesIO(r.data));self.assertEqual(len(w.active._images),1);self.assertEqual(w.active['J9'].value,'=F9*H9*(1-I9)')
    pdf=order_form_pdf(order_rows(order,'Bekir'),product_images(m.app,m.db,order));self.assertIn(b'/Subtype /Image',pdf)
    open('/tmp/bos-trello-form.pdf','wb').write(pdf)
