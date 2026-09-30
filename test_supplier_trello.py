import re
from unittest.mock import patch, MagicMock
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
import supplier_trello as t
import app as m
from sqlalchemy import select

class TrelloTests(SupplierRouteTests):
    def setUp(self):
        super().setUp(); self.path=f'/siparisler/{self.order_id}/trello'
        self.mock=MagicMock();self.mock.lists.return_value=[{'id':'list1','name':'🧾 Sipariş'}]
        self.mock.cards.return_value=[]
        self.mock.call.return_value={'id':'card1','url':'https://trello.com/c/example'}
        self.p=patch('supplier_trello.Client',return_value=self.mock);self.p.start();self.addCleanup(self.p.stop)
        r=self.client.post('/yonetim/trello',base_url=BASE,headers={'Origin':BASE},data={'key':'testkey','token':'testtoken'})
        self.assertEqual(r.status_code,302);self.mock.reset_mock()
    def token(self):
        return re.search(r'name="confirm" value="([^"]+)"',self.client.get(self.path,base_url=BASE).text)[1]
    def test_preview_renders_without_financial_values_or_google_calls(self):
        page=self.client.get(self.path,base_url=BASE)
        self.assertEqual(page.status_code,200);self.assertNotIn('987654',page.text)
        self.assertIn('SSH minder',page.text);self.mock.call.assert_not_called()
        with m.db.engine.connect() as c: self.assertNotIn('testtoken',c.execute(select(t.connection.c.config)).scalar())
    def test_missing_and_stale_preview_prevent_export(self):
        self.post({}); self.mock.call.assert_not_called()
        token=self.token();order=m.db.session.get(m.Order,self.order_id);order.items[0].quantity=9;m.db.session.commit()
        self.assertIn('Sipariş değişti',self.post({'confirm':token}).text);self.mock.call.assert_not_called()
    def test_duplicate_and_prices(self):
        token=self.token();self.post({'confirm':token});self.post({'confirm':token})
        self.mock.call.assert_called_once();self.assertNotIn('987654',str(self.mock.call.call_args))
    def test_ambiguous_timeout_never_resends(self):
        self.mock.call.side_effect=t.TrelloError('timeout')
        token=self.token();self.post({'confirm':token});r=self.post({'confirm':token})
        self.assertIn('sonucu belirsiz',r.text);self.mock.call.assert_called_once()
    def test_recover_remote_success(self):
        self.mock.call.side_effect=t.TrelloError('timeout')
        token=self.token();self.post({'confirm':token})
        row=t.order_rows(m.db.session.get(m.Order,self.order_id),'test')[0]
        self.mock.cards.return_value=[{'id':'remote','url':'https://trello.com/c/remote','desc':t.payload(row)['desc']}]
        self.post({'confirm':token});self.mock.call.assert_called_once()
        self.assertIn('https://trello.com/c/remote',self.client.get(self.path,base_url=BASE).text)
