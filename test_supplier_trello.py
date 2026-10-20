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
        self.chat_service.spaces().messages().create.side_effect=lambda **kw: MagicMock(**{'execute.return_value': {'name':kw['parent']+'/messages/test'}})
        chat_patch=patch('supplier_chat.chat_service',return_value=self.chat_service)
        chat_patch.start();self.addCleanup(chat_patch.stop)
        self.path=f'/siparisler/{self.order_id}/trello'
        self.mock=MagicMock();self.mock.lists.return_value=[{'id':'list1','name':'🧾 Sipariş'}]
        self.mock.cards.return_value=[]
        self.mock.call.return_value={'id':'card1','url':'https://trello.com/c/example'}
        self.p=patch('supplier_trello.Client',return_value=self.mock);self.p.start();self.addCleanup(self.p.stop)
        r=self.client.post('/yonetim/trello',base_url=BASE,headers={'Origin':BASE},data={'key':'testkey','token':'testtoken'})
        self.assertEqual(r.status_code,302);self.mock.reset_mock()
    def test_anonymous_and_other_suppliers_cannot_access(self):
        self.assertEqual(self.app.test_client().get(self.path,base_url=BASE).status_code,302)
        order=m.db.session.get(m.Order,self.order_id)
        other=m.Customer(name='Diğer Firma',city='ANKARA');m.db.session.add(other);order.customer=other;m.db.session.commit()
        self.assertEqual(self.client.get(self.path,base_url=BASE).status_code,200)
        order.order_type='Satış';m.db.session.commit()
        self.assertEqual(self.client.get(self.path,base_url=BASE).status_code,200)

    def test_images_for_both_order_types_and_retry_without_duplicate(self):
        order=m.db.session.get(m.Order,self.order_id)
        product=m.Product(name='Görselli Koltuk',code='IMAGE-TEST',unit_price=10)
        m.db.session.add(product);m.db.session.flush();order.items[0].product=product
        m.db.session.add(m.ProductQuoteImage(product_id=product.id,content=b'jpeg-test'));m.db.session.commit()
        token=self.token();self.post({'confirm':token})
        self.mock.attach_form.assert_called_once()
        self.assertTrue(self.mock.attach_form.call_args.args[2].startswith(b'%PDF'))
        self.mock.attach_image.assert_not_called()
        self.post({'confirm':token});self.mock.attach_form.assert_called_once()
        order.order_type='Satış';m.db.session.commit()
        self.assertIn('Ürün görseli sipariş formunda gösterilecek',self.client.get(self.path,base_url=BASE).text)
        self.assertNotIn('ABİKA Google Chat sohbetine',self.client.get(self.path,base_url=BASE).text)

    def test_image_change_invalidates_preview_and_can_complete_existing_card(self):
        order=m.db.session.get(m.Order,self.order_id)
        token=self.token();self.post({'confirm':token})
        product=m.Product(name='Yeni Görsel',code='IMG2',unit_price=10);m.db.session.add(product);m.db.session.flush()
        order.items[0].product=product;m.db.session.add(m.ProductQuoteImage(product_id=product.id,content=b'new'));m.db.session.commit()
        self.assertIn('Sipariş değişti',self.post({'confirm':token}).text)
        self.mock.cards.return_value=[{'id':'card1','url':'https://trello.com/c/example','desc':''}]
        self.mock.reset_mock();self.post({'confirm':self.token()})
        self.mock.attach_form.assert_called_once()
        self.assertFalse(any(call.args[:2]==('POST','cards') for call in self.mock.call.call_args_list))

    def test_image_failure_keeps_existing_card_for_retry(self):
        order=m.db.session.get(m.Order,self.order_id)
        product=m.Product(name='Koltuk',code='IMGFAIL',unit_price=10);m.db.session.add(product);m.db.session.flush();order.items[0].product=product
        m.db.session.add(m.ProductQuoteImage(product_id=product.id,content=b'image'));m.db.session.commit()
        self.mock.attach_form.side_effect=t.TrelloError('Görsel yüklenemedi')
        token=self.token();r=self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.assertEqual(r.status_code,409);self.assertEqual(r.json['remaining'],1)
        self.mock.attach_form.side_effect=None;self.mock.cards.return_value=[{'id':'card1','url':'https://trello.com/c/example','desc':''}];self.mock.reset_mock()
        r=self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.assertEqual(r.json['remaining'],0)
        self.assertFalse(any(call.args[:2]==('POST','cards') for call in self.mock.call.call_args_list))

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
    def test_ten_lines_complete_in_short_requests_without_duplicates(self):
        order=m.db.session.get(m.Order,self.order_id)
        for i in range(9): order.items.append(m.OrderItem(product_name=f'Ürün {i}',quantity=1,unit_price=12345))
        m.db.session.commit()
        self.mock.call.side_effect=[{'id':f'c{i}','url':f'https://trello.com/c/c{i}'} for i in range(10)]
        token=self.token(); counts=[]
        for _ in range(10):
            r=self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
            self.assertEqual(r.status_code,200);counts.append(r.json['completed'])
        self.assertEqual(counts,list(range(1,11)));self.assertEqual(r.json['remaining'],0)
        self.assertFalse(r.json['chat_done'])
        self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.assertEqual(self.mock.call.call_count,10)
    def test_batch_error_returns_partial_progress(self):
        order=m.db.session.get(m.Order,self.order_id)
        order.items.append(m.OrderItem(product_name='Second',quantity=1,unit_price=99));m.db.session.commit()
        self.mock.call.side_effect=[{'id':'first','url':'https://trello.com/c/first'},t.TrelloError('Bağlantı kesildi')]
        token=self.token()
        first=self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.assertEqual(first.status_code,200)
        r=self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
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
        for remaining in [3,2,1,0]:
            result=send()
            self.assertEqual(result.json['remaining'],remaining)
            self.assertFalse(result.json['chat_done'])
            self.chat_service.spaces().messages().create.assert_not_called()
        self.mock.cards.reset_mock();self.mock.lists.reset_mock()
        self.assertTrue(send().json['chat_done'])
        self.mock.cards.assert_not_called();self.mock.lists.assert_not_called()
        self.assertTrue(send().json['priced_done'])
        self.assertTrue(send().json['priced_done'])
        self.assertEqual(self.chat_service.spaces().messages().create.call_count,2)
        attachment_bytes=self.mock.attach_form.call_args.args[2]
        upload=self.chat_service.media().upload.call_args_list[0].kwargs['media_body']
        self.assertEqual(upload.getbytes(0,upload.size()),attachment_bytes)

    def test_chat_failure_resumes_without_new_cards(self):
        self.chat_service.spaces().messages().create.side_effect=[MagicMock(**{'execute.side_effect':TimeoutError()}), MagicMock(**{'execute.return_value':{'name':'spaces/AAQArMh8YcU/messages/test'}})]
        self.chat_service.reset_mock()
        token=self.token()
        def send(): return self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        cards=send()
        self.assertEqual(cards.status_code,200)
        self.assertFalse(cards.json['chat_done'])
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

    def test_staff_three_lines_pdf_failure_owner_resumes_same_cards_then_chat(self):
        self.chat_service.reset_mock()
        staff=self.staff_client('ahmet')
        self.mock.call.return_value={'id':'ahmet-id','username':'ahmet','fullName':'Ahmet'}
        self.assertEqual(staff.post('/hesabim/trello',base_url=BASE,headers={'Origin':BASE},data={'token':'ahmet-secret'}).status_code,302)
        result=staff.post('/siparisler/yeni',base_url=BASE,headers={'Origin':BASE},data={
            'order_type':'Satın Alma','customer_id':'1339',
            'product_name[]':['OME','NESSA','LETO'],'quantity[]':['1','2','10'],
            'unit_price[]':['1','1','1'],'delivery_city':'Balıkesir'})
        self.assertEqual(result.status_code,302)
        order=m.Order.query.order_by(m.Order.id.desc()).first()
        self.assertEqual(order.order_type,'Satın Alma')
        self.order_id=order.id;self.path=f'/siparisler/{order.id}/trello'
        token=re.search(r'name="confirm" value="([^"]+)"',staff.get(self.path,base_url=BASE).text)[1]
        remote=[]
        def create(method,path,**values):
            self.assertEqual((method,path),('POST','cards'))
            self.assertEqual(values['pos'],'top')
            card={'id':f'c{len(remote)}','url':f'https://trello.com/c/c{len(remote)}',**values}
            remote.append(card)
            return card
        self.mock.call.side_effect=create
        self.mock.cards.side_effect=lambda:list(remote)
        self.mock.attach_form.side_effect=[None,None,t.TrelloError('PDF kesildi'),None]
        def send(client,confirm): return client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':confirm})
        with patch('supplier_trello.Client',return_value=self.mock) as factory:
            for expected in [1,2]:
                result=send(staff,token)
                self.assertEqual(result.json['completed'],expected)
                self.assertEqual(factory.call_args.args[0]['token'],'ahmet-secret')
            failed=send(staff,token)
            self.assertEqual(failed.status_code,409)
            self.assertEqual(failed.json['completed'],2)
        page=staff.get(self.path,base_url=BASE).text
        self.assertIn('PDF aktarımı tamamlanmadı',page)
        self.chat_service.spaces().messages().create.assert_not_called()
        owner_token=self.token()
        self.assertEqual(send(self.client,owner_token).json['completed'],3)
        self.assertTrue(send(self.client,owner_token).json['chat_done'])
        self.assertEqual(len(remote),3)
        self.chat_service.spaces().messages().create.assert_called_once()
        self.assertTrue(send(staff,token).json['priced_done'])
        self.assertEqual(len(remote),3)
        self.assertEqual(self.chat_service.spaces().messages().create.call_count,2)

    def linked_sale(self):
        purchase=m.db.session.get(m.Order,self.order_id)
        product_name=purchase.items[0].product_name
        sale=m.Order(order_no='SS-2026-00095',order_type='Satış',customer=purchase.customer)
        sale.items=[m.OrderItem(product_name=product_name,quantity=6,unit_price=1)]
        m.db.session.add(sale);m.db.session.flush()
        purchase.source_order_id=sale.id
        purchase.items[0].source_order_item_id=sale.items[0].id
        m.db.session.commit()
        return sale

    def test_sale_export_routes_staff_and_owner_to_same_procurement(self):
        sale=self.linked_sale()
        path=f'/siparisler/{sale.id}/trello'
        for client in [self.client,self.staff_client('ahmet')]:
            page=client.get(path,base_url=BASE)
            self.assertEqual(page.status_code,303)
            self.assertTrue(page.location.endswith(self.path))
            result=client.post(path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'})
            self.assertEqual(result.status_code,409)
            self.assertEqual(result.json['redirect_url'],self.path)
        self.mock.call.assert_not_called()

    def test_multiple_procurements_require_selection_and_ignore_cancelled(self):
        sale=self.linked_sale()
        second=m.Order(order_no='SA-SECOND',order_type='Satın Alma',source_order_id=sale.id,customer=sale.customer)
        cancelled=m.Order(order_no='SA-CANCELLED',order_type='Satın Alma',source_order_id=sale.id,customer=sale.customer,status='İptal Edildi')
        m.db.session.add_all([second,cancelled]);m.db.session.commit()
        page=self.client.get(f'/siparisler/{sale.id}/trello',base_url=BASE)
        self.assertEqual(page.status_code,200)
        self.assertIn('Tedarik siparişini seçin',page.text)
        self.assertIn('SA-SECOND',page.text)
        self.assertNotIn('SA-CANCELLED',page.text)
        self.mock.call.assert_not_called()

    def test_legacy_sales_card_prevents_new_procurement_card(self):
        sale=self.linked_sale()
        from sqlalchemy import insert
        key=t.order_rows(sale,'test')[0][0]
        with m.db.engine.begin() as c:
            c.execute(insert(t.deliveries).values(key=key,card_id='sale-card',card_url='https://trello.com/c/sale'))
        result=self.post({'confirm':self.token()})
        self.assertIn('bağlı satış siparişinden daha önce aktarılmış',result.text)
        self.mock.call.assert_not_called()

    def test_existing_procurement_pdf_can_resume_despite_legacy_sales_duplicate(self):
        self.chat_service.reset_mock()
        sale=self.linked_sale()
        from sqlalchemy import insert
        rows=t.order_rows(m.db.session.get(m.Order,self.order_id),'test')
        with m.db.engine.begin() as c:
            c.execute(insert(t.deliveries),[
                dict(key=rows[0][0],card_id='purchase-card',card_url='https://trello.com/c/purchase'),
                dict(key=t.order_rows(sale,'test')[0][0],card_id='sale-card',card_url='https://trello.com/c/sale')])
        self.mock.cards.return_value=[dict(id='purchase-card',url='https://trello.com/c/purchase',**t.payload(rows[0]))]
        self.post({'confirm':self.token()})
        self.mock.attach_form.assert_called_once()
        self.assertFalse(any(call.args[:2]==('POST','cards') for call in self.mock.call.call_args_list))
        self.chat_service.spaces().messages().create.assert_called_once()

    def test_manually_linked_procurement_does_not_duplicate_existing_sales_cards(self):
        sale=self.linked_sale()
        purchase=m.db.session.get(m.Order,self.order_id)
        purchase.items[0].source_order_item_id=None
        m.db.session.commit()
        self.mock.cards.return_value=[dict(id='sale-card',url='https://trello.com/c/sale',**t.payload(t.order_rows(sale,'test')[0]))]
        result=self.post({'confirm':self.token()})
        self.assertIn('bağlı satış siparişinden daha önce aktarılmış',result.text)
        self.mock.call.assert_not_called()

    def test_priced_pdf_only_to_dilek_and_retry_preserves_snapshot(self):
        import trello_chat_bridge as bridge
        from pypdf import PdfReader
        from io import BytesIO
        token=self.token()
        def send(): return self.client.post(self.path,base_url=BASE,headers={'Origin':BASE,'Accept':'application/json'},data={'confirm':token})
        self.chat_service.reset_mock()
        self.assertFalse(send().json['chat_done'])
        self.assertFalse(send().json['priced_done'])
        self.chat_service.spaces().messages().create.side_effect=lambda **kw: MagicMock(**{'execute.side_effect':TimeoutError()})
        failed=send()
        self.assertEqual(failed.status_code,409)
        self.assertTrue(failed.json['chat_done']);self.assertFalse(failed.json['priced_done'])
        calls=self.chat_service.media().upload.call_args_list
        self.assertEqual([c.kwargs['parent'] for c in calls],['spaces/AAQArMh8YcU',bridge.DILEK_SPACE])
        group=calls[0].kwargs['media_body'];priced=calls[1].kwargs['media_body']
        group_text=''.join(p.extract_text() for p in PdfReader(BytesIO(group.getbytes(0,group.size()))).pages)
        priced_text=''.join(p.extract_text() for p in PdfReader(BytesIO(priced.getbytes(0,priced.size()))).pages)
        self.assertNotIn('987.654',group_text)
        self.assertIn('987.654',priced_text)
        self.assertIn('KDV',priced_text)
        previous_call=self.chat_service.spaces().messages().create.call_args
        self.chat_service.spaces().messages().create.side_effect=lambda **kw: MagicMock(**{'execute.return_value':{'name':kw['parent']+'/messages/done'}})
        self.assertTrue(send().json['priced_done'])
        self.assertEqual(self.chat_service.spaces().messages().create.call_args,previous_call)
        self.assertEqual(self.chat_service.media().upload.call_count,2)
        self.assertTrue(send().json['priced_done'])
        self.assertEqual(self.chat_service.spaces().messages().create.call_count,3)
        self.mock.call.assert_called_once()

    def test_priced_destination_guard_and_stale_price_preview(self):
        import trello_chat_bridge as bridge
        import supplier_chat as chat
        order=m.db.session.get(m.Order,self.order_id)
        with self.assertRaises(chat.ChatError):
            bridge.deliver(self.app,m.db,bridge.priced_key(order))
        token=self.token()
        order.items[0].unit_price=12345;m.db.session.commit()
        response=self.post({'confirm':token})
        self.assertIn('Sipariş değişti',response.text)
        self.mock.call.assert_not_called()
