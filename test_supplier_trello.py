import re
from unittest.mock import patch, MagicMock
from test_supplier_sheets_routes import SupplierRouteTests
from test_web_auth import BASE
import supplier_trello as t
import app as m
from sqlalchemy import select

class TrelloTests(SupplierRouteTests):
    def setUp(self):
        super().setUp()
        import supplier_chat as chat
        chat.meta.create_all(m.db.engine)
        with m.db.engine.begin() as c:
            chat.write_config(self.app,c,{'refresh_token':'test','client_id':'test','client_secret':'test'})
        self.chat_service=MagicMock()
        self.chat_service.media().upload().execute.return_value={'attachmentDataRef':{'resourceName':'test-file'}}
        self.chat_service.spaces().messages().create().execute.return_value={'name':chat.SPACE+'/messages/test'}
        chat_patch=patch('supplier_chat.chat_service',return_value=self.chat_service)
        chat_patch.start();self.addCleanup(chat_patch.stop)
        self.path=f'/siparisler/{self.order_id}/trello'
        self.mock=MagicMock();self.mock.lists.return_value=[{'id':'list1','name':'🧾 Sipariş'}]
        self.mock.cards.return_value=[]
        self.mock.call.return_value={'id':'card1','url':'https://trello.com/c/example'}
        self.p=patch('supplier_trello.Client',return_value=self.mock);self.p.start();self.addCleanup(self.p.stop)
        r=self.client.post('/yonetim/trello',base_url=BASE,headers={'Origin':BASE},data={'key':'testkey','token':'testtoken'})
        self.assertEqual(r.status_code,302);self.mock.reset_mock()
    def staff_client(self, name):
        password='test-password-1234'
        r=self.client.post('/kullanicilar',base_url=BASE,headers={'Origin':BASE},
            data={'username':name,'password':password,'confirmation':password})
        self.assertEqual(r.status_code,302)
        client=self.app.test_client()
        r=client.post('/giris',base_url=BASE,headers={'Origin':BASE},data={'username':name,'password':password})
        self.assertEqual(r.status_code,302)
        return client

    def test_unconnected_staff_never_uses_owner_token(self):
        staff=self.staff_client('ahmet')
        page=staff.get(self.path,base_url=BASE)
        self.assertIn('Kendi Trello Hesabımı Bağla',page.text)
        token=t.URLSafeTimedSerializer(self.app.secret_key,salt='trello-export').dumps({'order':self.order_id,'hash':t.fingerprint(t.order_rows(m.db.session.get(m.Order,self.order_id),'ahmet')),'actor':'ahmet'})
        self.mock.reset_mock()
        result=staff.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.assertEqual(result.status_code,409)
        self.mock.call.assert_not_called()
        self.mock.attach_form.assert_not_called()

    def test_personal_connection_is_private_and_used_for_export(self):
        staff=self.staff_client('ahmet')
        other=self.staff_client('ayse')
        self.mock.call.return_value={'id':'ahmet-id','username':'ahmet','fullName':'Ahmet Test'}
        r=staff.post('/hesabim/trello',base_url=BASE,headers={'Origin':BASE},data={'token':'ahmet-secret','user_id':'0'})
        self.assertEqual(r.status_code,302)
        with m.db.engine.connect() as c:
            stored=c.execute(select(t.personal_connections.c.config)).scalar_one()
        self.assertNotIn('ahmet-secret',stored)
        page=staff.get('/hesabim/trello',base_url=BASE).text
        self.assertIn('Ahmet Test',page);self.assertNotIn('ahmet-secret',page)
        self.assertNotIn('Ahmet Test',other.get('/hesabim/trello',base_url=BASE).text)
        self.assertIn('Kendi Trello Hesabımı Bağla',other.get(self.path,base_url=BASE).text)
        token=re.search(r'name="confirm" value="([^"]+)"',staff.get(self.path,base_url=BASE).text)[1]
        self.mock.call.return_value={'id':'new-card','url':'https://trello.com/c/new-card'}
        with patch('supplier_trello.Client',return_value=self.mock) as factory:
            result=staff.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
            self.assertEqual(result.status_code,200)
            self.assertEqual(factory.call_args.args[0]['token'],'ahmet-secret')
        self.assertEqual(other.get('/yonetim/trello',base_url=BASE).status_code,403)

    def test_invalid_personal_token_not_saved(self):
        staff=self.staff_client('ahmet')
        self.mock.call.side_effect=t.TrelloError('Geçersiz bağlantı')
        r=staff.post('/hesabim/trello',base_url=BASE,headers={'Origin':BASE},data={'token':'bad-secret'})
        self.assertIn('Geçersiz bağlantı',r.text)
        with m.db.engine.connect() as c: self.assertEqual(c.execute(select(t.personal_connections)).all(),[])
        self.assertEqual(self.app.test_client().get('/hesabim/trello',base_url=BASE).status_code,302)

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
    def test_ten_lines_complete_in_four_batches_without_duplicates(self):
        order=m.db.session.get(m.Order,self.order_id)
        for i in range(9): order.items.append(m.OrderItem(product_name=f'Ürün {i}',quantity=1,unit_price=12345))
        m.db.session.commit()
        self.mock.call.side_effect=[{'id':f'c{i}','url':f'https://trello.com/c/c{i}'} for i in range(10)]
        token=self.token(); counts=[]
        for _ in range(4):
            r=self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
            self.assertEqual(r.status_code,200);counts.append(r.json['completed'])
        self.assertEqual(counts,[3,6,9,10]);self.assertEqual(r.json['remaining'],0)
        self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.assertEqual(self.mock.call.call_count,10)
    def test_batch_error_returns_partial_progress(self):
        order=m.db.session.get(m.Order,self.order_id)
        order.items.append(m.OrderItem(product_name='Second',quantity=1,unit_price=99));m.db.session.commit()
        self.mock.call.side_effect=[{'id':'first','url':'https://trello.com/c/first'},t.TrelloError('Bağlantı kesildi')]
        r=self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':self.token()})
        self.assertEqual(r.status_code,409);self.assertEqual(r.json['completed'],1);self.assertEqual(r.json['remaining'],1)
    def test_pdf_and_city_and_retry_without_duplicate_card(self):
        order=m.db.session.get(m.Order,self.order_id);order.delivery_city='İstanbul';m.db.session.commit()
        self.mock.attach_form.side_effect=[t.TrelloError('upload timeout'),None]
        token=self.token()
        r=self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.assertEqual(r.status_code,409);self.assertEqual(r.json['completed'],0)
        desc=self.mock.call.call_args.kwargs['desc'];self.assertIn('Teslim ili: İstanbul',desc)
        self.mock.cards.return_value=[{'id':'card1','url':'https://trello.com/c/example','desc':desc,**t.location('İstanbul')}]
        r=self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.assertEqual(r.json['completed'],1);self.mock.call.assert_called_once()
        self.assertTrue(self.mock.attach_form.call_args.args[2].startswith(b'%PDF-'))

    def test_legacy_card_enriched_without_overwriting_notes(self):
        from sqlalchemy import insert
        order=m.db.session.get(m.Order,self.order_id);order.delivery_city='Edirne';m.db.session.commit()
        row=t.order_rows(order,'test')[0]
        with m.db.engine.begin() as c: c.execute(insert(t.deliveries).values(key=row[0],card_id='old',card_url='https://trello.com/c/old'))
        self.mock.cards.return_value=[{'id':'old','url':'https://trello.com/c/old','desc':'Merve üretim notu'}]
        self.post({'confirm':self.token()})
        self.mock.call.assert_called_once_with('PUT','cards/old',desc='Merve üretim notu\n\nTeslim ili: Edirne',**t.location('Edirne'))
        self.mock.attach_form.assert_called_once()

    def test_upload_reconciles_existing_attachment(self):
        client=self.p.temp_original({'key':'x','token':'y'})
        with patch.object(client,'call',return_value=[{'name':'form.pdf'}]) as call:
            client.attach_form('card','form.pdf',b'%PDF-test')
            call.assert_called_once_with('GET','cards/card/attachments',fields='name')

    def test_chat_only_after_all_cards_and_once(self):
        order=m.db.session.get(m.Order,self.order_id)
        for i in range(3): order.items.append(m.OrderItem(product_name=f'Kalem {i}',quantity=1,unit_price=1))
        m.db.session.commit()
        self.mock.call.side_effect=[{'id':f'c{i}','url':f'https://trello.com/c/c{i}'} for i in range(4)]
        self.chat_service.reset_mock()
        token=self.token()
        def send(): return self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.assertEqual(send().json['remaining'],1)
        self.chat_service.spaces().messages().create.assert_not_called()
        self.assertTrue(send().json['chat_done'])
        self.assertTrue(send().json['chat_done'])
        self.chat_service.spaces().messages().create.assert_called_once()
        attachment_bytes=self.mock.attach_form.call_args.args[2]
        upload=self.chat_service.media().upload.call_args.kwargs['media_body']
        self.assertEqual(upload.getbytes(0,upload.size()),attachment_bytes)

    def test_chat_failure_resumes_without_new_cards(self):
        self.chat_service.spaces().messages().create().execute.side_effect=[TimeoutError(),{'name':'spaces/AAQArMh8YcU/messages/test'}]
        self.chat_service.reset_mock()
        token=self.token()
        def send(): return self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        a=send();b=send()
        self.assertEqual(a.status_code,409);self.assertEqual(a.json['remaining'],0)
        self.assertEqual(b.status_code,200);self.assertTrue(b.json['chat_done'])
        self.mock.call.assert_called_once()
        self.chat_service.media().upload.assert_called_once()
        calls=self.chat_service.spaces().messages().create.call_args_list
        self.assertEqual(calls[0],calls[1])

    def test_delivery_location_not_supplier_city_and_normalized(self):
        order=m.db.session.get(m.Order,self.order_id)
        order.customer.city='Ankara'
        order.delivery_city='  İSTANBUL  '
        m.db.session.commit()
        self.post({'confirm':self.token()})
        values=self.mock.call.call_args.kwargs
        self.assertEqual(values['locationName'],'İstanbul')
        self.assertEqual(values['address'],'İstanbul, Türkiye')
        self.assertIn('coordinates[latitude]',values)
        self.assertEqual(t.location('istanbul'),t.location('İSTANBUL'))
        self.assertEqual(t.location('ŞANLIURFA'),t.location('sanliurfa'))
        self.assertEqual(len(t.location('')),0)
        self.assertEqual(len(t.location('unknown city')),0)
        order.delivery_city=''
        self.assertNotIn('locationName',t.payload(t.order_rows(order,'test')[0]))

    def test_location_request_uses_form_coordinates(self):
        client=self.p.temp_original({'key':'x','token':'y'})
        with patch('supplier_trello.requests.request') as request:
            request.return_value.ok=True
            request.return_value.json.return_value={'id':'card'}
            client.call('POST','cards',name='Test',idList='list',**t.location('İstanbul'))
            values=request.call_args.kwargs
            self.assertIsNone(values['json'])
            self.assertEqual(values['data']['coordinates[latitude]'],'41.00638100')
