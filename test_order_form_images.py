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
 def test_free_text_ome_in_purchase_and_supplier_pdf(self):
  from pypdf import PdfReader
  from pathlib import Path
  with m.app.app_context():
   m.db.drop_all();m.db.create_all()
   order=m.Order(order_no='SA-2026-00096',customer=m.Customer(name='ABİKA'),order_type='Satın Alma')
   order.items=[m.OrderItem(product_name='Ome',quantity=5,unit_price=0,variant='Lizbon siyah deri')]
   m.db.session.add(order);m.db.session.commit()
   images=product_images(m.app,m.db,order)
   expected=(Path(m.app.root_path)/'static/quote-sofa-sets/O057.jpg').read_bytes()
   key=f'bos:order:{order.id}:item:{order.items[0].id}'
   self.assertEqual(images[key]['content'],expected)
   self.assertIsNone(order.items[0].product_id)
   response=m.app.test_client().get(f'/siparisler/{order.id}/pdf')
   self.assertEqual(response.status_code,200)
   for pdf in (response.data,order_form_pdf(order_rows(order,'Test'),images)):
    reader=PdfReader(BytesIO(pdf))
    self.assertGreater(sum(len(page.images) for page in reader.pages),0)
    self.assertIn('Ome',' '.join(page.extract_text() for page in reader.pages))
   order.items[0].product_name='Ome başka model'
   self.assertEqual(product_images(m.app,m.db,order),{})
   order.items[0].product=m.Product(code='UNKNOWN',name='Ome',unit_price=0)
   order.items[0].product_name='Ome'
   self.assertEqual(product_images(m.app,m.db,order),{})
   m.db.session.rollback()
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
