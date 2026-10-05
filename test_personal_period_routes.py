"""Personal finance writes must not depend on SQLite-only SQL functions."""
import unittest
from sqlalchemy import event
import test_supplier_sheets_routes as fixture
from test_web_auth import BASE
m=fixture.m

class PersonalPeriodTests(unittest.TestCase):
    setUp=fixture.SupplierRouteTests.setUp
    tearDown=fixture.SupplierRouteTests.tearDown
    def post(self,**data):
        return self.client.post('/sahsi-hesaplar',base_url=BASE,headers={'Origin':BASE},data=data,follow_redirects=True)
    def forbid_sqlite_function(self):
        def check(conn,cursor,statement,parameters,context,executemany):
            if 'normalize_tr(' in statement:raise AssertionError('SQLite-only SQL used')
        event.listen(m.db.engine,'before_cursor_execute',check)
    def test_new_period_copies_setup_not_previous_payments(self):
        source=m.PersonalMonth(month='Eylül 2026')
        source.entries=[m.PersonalPayment(kind='card',name='Kart',credit_limit=10000,debt=5000,payment=500,minimum_payment=1000,due_day=15,sort_order=3)]
        m.db.session.add(source);m.db.session.commit();self.forbid_sqlite_function()
        response=self.post(action='new_month',month='Eylül 2026',new_month='  Ekim   2026  ')
        self.assertEqual(response.status_code,200);self.assertIn('Yeni dönem oluşturuldu',response.text)
        target=m.db.session.get(m.PersonalMonth,'Ekim 2026')
        self.assertEqual(len(target.entries),1)
        row=target.entries[0];self.assertEqual((row.name,row.credit_limit,row.due_day,row.sort_order),('Kart',10000,15,3))
        self.assertIsNone(row.payment);self.assertIsNone(row.debt);self.assertIsNone(row.minimum_payment)
        self.assertEqual(source.entries[0].payment,500)
    def test_duplicate_turkish_case_and_invalid_period(self):
        m.db.session.add(m.PersonalMonth(month='EKİM 2026'));m.db.session.commit();self.forbid_sqlite_function()
        for label in ('ekim 2026','Ekım 2026','', 'x'*81):
            response=self.post(action='new_month',month='EKİM 2026',new_month=label)
            self.assertEqual(response.status_code,200);self.assertIn('Dönem adı boş, çok uzun veya zaten mevcut',response.text)
        self.assertEqual(m.PersonalMonth.query.count(),1)
    def test_person_name_uses_same_portable_duplicate_check(self):
        self.forbid_sqlite_function()
        self.post(action='add_person',tab='ledger',person_name='IŞIK')
        self.post(action='add_person',tab='ledger',person_name='ışık')
        self.assertEqual(m.PersonalPerson.query.count(),1)
