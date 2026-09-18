"""Exercise every generated PDF route without OS-installed fonts."""
import os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from datetime import date
_data=tempfile.TemporaryDirectory(prefix='bos-pdf-test-')
os.environ.update(BUSINESSOS_DATA_DIR=_data.name,BUSINESSOS_BACKUP_DIR=_data.name,DATABASE_URL='sqlite:///:memory:')
import app as m
from reportlab.pdfbase import pdfmetrics

class PortablePDFTests(unittest.TestCase):
    def test_all_pdf_routes_without_system_fonts(self):
        real=Path.is_file
        def portable(path):
            return False if str(path).startswith(('/System/Library/Fonts/','/usr/share/fonts/')) else real(path)
        with m.app.app_context(),patch.object(Path,'is_file',portable):
            m.db.drop_all();m.db.create_all()
            customer=m.Customer(name='İnci Şığ ÇÖÜ müşteri');product=m.Product(name='Çalışma koltuğu')
            person=m.PersonalPerson(name='İnci Şığ ÇÖÜ kişi')
            m.db.session.add_all([customer,product,person]);m.db.session.flush()
            order=m.Order(customer=customer,order_no='S-TEST',order_type='Satış',status='Bekliyor')
            order.items.append(m.OrderItem(product=product,product_name=product.name,quantity=2,unit_price=100))
            m.db.session.add(order)
            for kind in ('Satış','Satın Alma'):
                inv=m.Invoice(customer=customer,invoice_no=kind,invoice_type=kind,invoice_date=date.today(),due_date=date.today())
                inv.items.append(m.InvoiceItem(product=product,product_name=product.name,quantity=1,unit_price=100))
                m.db.session.add(inv)
            m.db.session.commit()
            routes=[f'/tahsilat-takibi/pdf?kind={kind}&view={view}&state=all' for kind in ('sales','purchase') for view in ('summary','details')]
            routes += ['/musteriler/bakiyeler/pdf',f'/siparisler/{order.id}/pdf',f'/sahsi-hesaplar/borc-alacak/{person.id}/pdf']
            routes += [f'/musteriler/{customer.id}/cari-hesap/ekstre/{detail}/pdf' for detail in ('ozet','ayrintili')]
            client=m.app.test_client()
            output=os.getenv('PDF_QA_DIR')
            if output:Path(output).mkdir(parents=True,exist_ok=True)
            for i,route in enumerate(routes):
                with self.subTest(route=route):
                    response=client.get(route)
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.mimetype,'application/pdf')
                    self.assertIn('attachment',response.headers['Content-Disposition'])
                    self.assertTrue(response.data.startswith(b'%PDF'))
                    if output:Path(output,f'{i+1:02}.pdf').write_bytes(response.data)
                    response.close()
            for name in ('BusinessPDF','CollectionFont','CollectionSummary','StatementFont'):
                font=pdfmetrics.getFont(name)
                self.assertTrue(all(ord(c) in font.face.charToGlyph for c in 'İıŞşĞğÇçÖöÜü'))
                self.assertIn('Vera',font.face.filename.decode() if isinstance(font.face.filename,bytes) else font.face.filename)

if __name__=='__main__':unittest.main()
