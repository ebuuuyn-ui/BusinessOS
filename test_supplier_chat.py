"""No live DB, Google uploads or messages: isolated fixtures and mocked transport."""
import re
import unittest
from unittest.mock import patch, MagicMock
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
import app as m
import supplier_chat as c
from sqlalchemy import select


class ChatTests(SupplierRouteTests):
    def setUp(self):
        super().setUp()
        self.path=f'/siparisler/{self.order_id}/google-chat'
        c.meta.create_all(m.db.engine)
        with m.db.engine.begin() as conn:
            c.write_config(self.app,conn,{'client_id':'test.apps.googleusercontent.com','client_secret':'secret-test','refresh_token':'refresh-test'})
        self.service=MagicMock()
        self.service.media().upload().execute.return_value={'attachmentDataRef':{'resourceName':'test-attachment'}}
        self.service.spaces().messages().create().execute.return_value={'name':c.SPACE+'/messages/test'}

    # Override inherited tests which are specific to Sheets.
    def test_preview_renders_without_financial_values_or_google_calls(self):
        with patch('supplier_chat.chat_service') as service:
            r=self.client.get(self.path,base_url=BASE)
        self.assertEqual(r.status_code,200)
        self.assertIn(c.SPACE_TITLE,r.text)
        self.assertIn('fiyatlar, KDV ve tutarlar',r.text)
        self.assertNotIn('secret-test',r.text)
        service.assert_not_called()
        with m.db.engine.connect() as conn:
            row=conn.execute(select(c.delivery_table)).mappings().one()
            self.assertTrue(row['pdf'].startswith(b'%PDF-'))
            encrypted=conn.execute(select(c.connection_table.c.encrypted_config)).scalar()
            self.assertNotIn('refresh-test',encrypted)

    def token(self):
        page=self.client.get(self.path,base_url=BASE).text
        return re.search(r'name="confirm" value="([^"]+)"',page)[1]

    def test_missing_and_stale_preview_prevent_export(self):
        with patch('supplier_chat.chat_service') as service:
            self.post({})
            token=self.token()
            order=m.db.session.get(m.Order,self.order_id);order.items[0].quantity=8;m.db.session.commit()
            r=self.post({'confirm':token,'ack':'yes'})
            self.assertIn('Sipariş değişmiş',r.text)
            service.assert_not_called()

    def test_confirmation_and_duplicate_submission(self):
        token=self.token()
        with patch('supplier_chat.chat_service',return_value=self.service):
            self.post({'confirm':token})
            self.service.media().upload.assert_called_once() # only mock setup call
            self.service.reset_mock()
            first=self.post({'confirm':token,'ack':'yes'})
            second=self.post({'confirm':token,'ack':'yes'})
        self.assertEqual((first.status_code,second.status_code),(302,302))
        self.service.media().upload.assert_called_once()
        create=self.service.spaces().messages().create
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs['parent'],c.SPACE)
        with m.db.engine.connect() as conn:
            row=conn.execute(select(c.delivery_table)).mappings().one()
            self.assertTrue(row['message_name']);self.assertTrue(row['sent_by'])

    def test_retry_keeps_same_uploaded_attachment_and_request(self):
        token=self.token()
        create=self.service.spaces().messages().create
        create.reset_mock()
        create().execute.side_effect=[TimeoutError(),{'name':c.SPACE+'/messages/test'}]
        create.reset_mock()
        self.service.media().upload.reset_mock()
        with patch('supplier_chat.chat_service',return_value=self.service):
            a=self.post({'confirm':token,'ack':'yes'})
            b=self.post({'confirm':token,'ack':'yes'})
        self.assertEqual((a.status_code,b.status_code),(200,302))
        self.service.media().upload.assert_called_once()
        self.assertEqual(create.call_args_list[0],create.call_args_list[1])

    def test_oauth_get_only_relays_without_auth_or_token_exchange(self):
        with patch('supplier_chat.requests.post') as exchange:
            response=self.app.test_client().get('/yonetim/google-chat/callback?code=test&state=s',base_url=BASE)
        self.assertEqual(response.status_code,200)
        self.assertIn('Bağlantıyı Tamamla',response.text)
        exchange.assert_not_called()
        self.assertEqual(self.app.config['SESSION_COOKIE_SAMESITE'],'Strict')

    def test_settings_owner_and_oauth_state(self):
        response=self.client.get('/yonetim/google-chat',base_url=BASE)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('secret-test',response.text)
        self.assertEqual(self.client.post('/yonetim/google-chat/callback',base_url=BASE,headers={'Origin':BASE},data={'state':'bad'}).status_code,400)
        with self.client.session_transaction(base_url=BASE) as sess:
            sess['chat_oauth']={'state':'valid','verifier':'v','nonce':'n','at':c.datetime.now(c.timezone.utc).timestamp()}
        response=MagicMock(ok=True)
        response.json.return_value={'id_token':'id','scope':c.SCOPE,'refresh_token':'new-token'}
        with patch('supplier_chat.requests.post',return_value=response),patch('supplier_chat.id_token.verify_oauth2_token',return_value={'email':'wrong@example.test','email_verified':True,'nonce':'n'}):
            r=self.client.post('/yonetim/google-chat/callback',base_url=BASE,headers={'Origin':BASE},data={'state':'valid','code':'test'})
        self.assertEqual(r.status_code,302)
        with m.db.engine.connect() as conn:
            self.assertEqual(c.read_config(self.app,conn)['refresh_token'],'refresh-test')

if __name__=='__main__':unittest.main()
