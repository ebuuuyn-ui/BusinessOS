import unittest
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
import app as m

class CustomerCaseSearchTests(unittest.TestCase):
    setUp=SupplierRouteTests.setUp
    tearDown=SupplierRouteTests.tearDown

    def test_turkish_names_in_both_database_expressions(self):
        names=['YANILMAZ MOBİLYA', 'İŞIK ÇÖZÜM', 'ÖZGÜR ŞEN', 'Firma %50']
        for name in names: m.db.session.add(m.Customer(name=name))
        m.db.session.commit()
        connection=m.db.session.connection().connection.driver_connection
        connection.create_function('translate',3,lambda value,a,b: value.translate(str.maketrans(a,b)) if value is not None else None)
        cases={'yanılmaz':'YANILMAZ MOBİLYA','yanilmaz':'YANILMAZ MOBİLYA','YANILMAZ':'YANILMAZ MOBİLYA',
               'ışık çözüm':'İŞIK ÇÖZÜM','isik cozum':'İŞIK ÇÖZÜM','özgür şen':'ÖZGÜR ŞEN','ozgur sen':'ÖZGÜR ŞEN','%50':'Firma %50'}
        for dialect in ['sqlite','postgresql']:
            for query,expected in cases.items():
                with self.subTest(dialect=dialect,query=query):
                    result=m.apply_customer_text_filter(m.Customer.query,query,dialect).all()
                    self.assertEqual([r.name for r in result],[expected])
        page=self.client.get('/musteriler?q=yanilmaz',base_url=BASE)
        self.assertEqual(page.status_code,200)
        self.assertIn('YANILMAZ MOBİLYA',page.text)

if __name__=='__main__':unittest.main()
