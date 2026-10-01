from unittest.mock import patch, MagicMock
from sqlalchemy import insert,select
from test_supplier_trello import TrelloTests
from test_web_auth import BASE
import app as m
import supplier_trello as t
import trello_chat_bridge as b

class BridgeTests(TrelloTests):
    def setUp(self):
        super().setUp()
        b.meta.create_all(m.db.engine)
        b.save_settings(self.app,m.db,{'secret':'callback-test-secret','enabled_at':'2026-10-01T00:00:00+00:00'})
        self.hook='/integrations/trello-chat/callback-test-secret'
        order=m.db.session.get(m.Order,self.order_id)
        key=t.order_rows(order,'test')[0][0]
        with m.db.engine.begin() as c: c.execute(insert(t.deliveries).values(key=key,card_id='card1',card_url='https://trello.com/c/test'))
        self.action={'id':'a'*24,'type':'commentCard','date':'2026-10-02T00:00:00Z',
            'data':{'board':{'id':t.BOARD},'card':{'id':'card1','name':'67 · Koltuk'},'text':'Kumaş bekliyoruz.'},
            'memberCreator':{'fullName':'Merve'}}
        self.mock.call.return_value=self.action
        self.chat_service.reset_mock()
        self.anonymous=self.app.test_client()
    # Run only bridge tests; inherited Trello tests are separately covered.
    def hook_post(self): return self.anonymous.post(self.hook,base_url=BASE,json={'action':{'id':'a'*24,'type':'commentCard','data':{'text':'FORGED'}}})
    def test_webhook_secret_and_head(self):
        self.assertEqual(self.anonymous.head(self.hook,base_url=BASE).status_code,200)
        self.assertEqual(self.anonymous.post('/integrations/trello-chat/wrong',base_url=BASE).status_code,404)
        self.assertEqual(self.anonymous.post('/yonetim/trello-chat',base_url=BASE).status_code,401)
        self.chat_service.spaces().messages().create.assert_not_called()
    def test_comment_uses_verified_text_and_deduplicates(self):
        self.assertEqual(self.hook_post().status_code,200)
        self.assertEqual(self.hook_post().status_code,200)
        create=self.chat_service.spaces().messages().create
        create.assert_called_once()
        body=create.call_args.kwargs['body']
        self.assertIn('Merve: Kumaş bekliyoruz.',body['text']);self.assertNotIn('FORGED',body['text'])
        self.assertNotIn('attachment',body)
    def test_wrong_board_old_comment_unmapped_card_ignored(self):
        self.action['data']['board']['id']='wrong';self.assertEqual(self.hook_post().status_code,200)
        self.action['data']['board']['id']=t.BOARD;self.action['date']='2026-09-30T00:00:00Z';self.assertEqual(self.hook_post().status_code,200)
        self.action['date']='2026-10-02T00:00:00Z';self.action['data']['card']['id']='other';self.assertEqual(self.hook_post().status_code,200)
        self.chat_service.spaces().messages().create.assert_not_called()
    def test_comment_failure_retained_for_retry(self):
        self.chat_service.spaces().messages().create().execute.side_effect=[TimeoutError(),{'name':'spaces/AAQArMh8YcU/messages/test'}]
        self.chat_service.reset_mock()
        self.assertEqual(self.hook_post().status_code,503)
        with m.db.engine.connect() as c: self.assertIsNone(c.execute(select(b.outbox.c.message_name)).scalar())
        self.assertEqual(self.hook_post().status_code,200)
        calls=self.chat_service.spaces().messages().create.call_args_list
        self.assertEqual(calls[0],calls[1])
    def test_non_comment_ignored(self):
        response=self.anonymous.post(self.hook,base_url=BASE,json={'action':{'type':'updateCard'}})
        self.assertEqual(response.status_code,200);self.mock.call.assert_not_called()

# Don't rerun inherited integration tests with the bridge's preexisting card fixture.
for name in dir(TrelloTests):
    if name.startswith('test_') and name not in BridgeTests.__dict__:
        setattr(BridgeTests,name,None)
