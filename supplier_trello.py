"""Explicit price-free ABIKA line export to a fixed Trello board."""
import json
import re
import hashlib
import base64
from urllib.parse import urlencode
from datetime import datetime, timezone
import requests
from cryptography.fernet import Fernet
from flask import abort, g, request, render_template, redirect, url_for, flash, jsonify, session
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import MetaData, Table, Column, Integer, Text, String, select, insert, update, inspect
from sqlalchemy.exc import IntegrityError
from supplier_sheets import allowed_order as chat_allowed, order_rows as sheet_rows, fingerprint
from pathlib import Path

def allowed_order(order): return order.order_type in ("Satış","Satın Alma")
def order_rows(order,actor):
    rows=sheet_rows(order,actor,trello=True)
    if order.order_type=="Satış":
        for row in rows: row[7]=order.customer.city or ""
    return rows

from order_images import product_images, pdf_image

from trello_locations import location

BOARD = '6abd5e01fddae7338973fe6b'
BOARD_URL = 'https://trello.com/b/zGa5gdZO'
meta = MetaData()
connection = Table('businessos_trello_connection', meta,
    Column('id', Integer, primary_key=True), Column('config', Text, nullable=False))
personal_connections = Table('businessos_trello_personal_connection', meta,
    Column('user_id', Integer, primary_key=True), Column('config', Text, nullable=False))
deliveries = Table('businessos_trello_delivery', meta,
    Column('key', String(100), primary_key=True), Column('card_id', Text),
    Column('card_url', Text), Column('actor', Text), Column('created_at', Text))

extras = Table('businessos_trello_card_extras', meta,
    Column('key', String(100), primary_key=True), Column('card_id', Text))

class TrelloError(Exception): pass

def order_form_pdf(rows,images=None):
    images=images or {}
    from io import BytesIO
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from pdf_fonts import register_pdf_fonts
    font, bold = register_pdf_fonts()
    normal = ParagraphStyle('body', fontName=font, fontSize=9, leading=13)
    heading = ParagraphStyle('heading', fontName=bold, fontSize=16, leading=22)
    para = lambda value: Paragraph(escape(str(value or '')).replace('\n','<br/>'), normal)
    out = BytesIO()
    story = [Paragraph('ABİKA · SİPARİŞ FORMU', heading), Spacer(1,12)]
    if rows:
        r=rows[0]
        for label, value in [('Sipariş',r[1]),('Sipariş tarihi',r[2]),('Teslim tarihi',r[3]),('Teslim ili',r[7])]:
            if value: story.append(para(f'{label}: {value}'))
        story.append(Spacer(1,12))
        data=[[para('Görsel'),para('Ürün / Ayrıntılar'),para('Adet')]]
        for r in rows:
            details=[str(r[13])]+[f'{label}: {r[i]}' for i,label in [(12,'Kod'),(14,'Açıklama'),(15,'Ayrıntı 1'),(16,'Ayrıntı 2'),(17,'Ayrıntı 3'),(20,'Not')] if r[i]]
            image=pdf_image(images[r[0]]['content']) if r[0] in images else None
            data.append([image or para('Görsel yok'),para('\n'.join(details)),para(f'{r[18]} {r[19]}')])
        table=Table(data,colWidths=[85,360,70],repeatRows=1,hAlign='LEFT')
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#eaf1fa')),('VALIGN',(0,0),(-1,-1),'TOP'),('GRID',(0,0),(-1,-1),.4,colors.HexColor('#ccd5e0')),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]))
        story.append(table)
        if rows[0][21]: story.extend([Spacer(1,12),para('Sipariş notu: '+str(rows[0][21]))])
    SimpleDocTemplate(out,pagesize=A4,leftMargin=40,rightMargin=40,topMargin=35,bottomMargin=35,invariant=1).build(story)
    return out.getvalue()


def payload(row):
    # Explicit non-financial fields only; one stable marker per order line.
    labels = [(7,'Teslim ili'),(12,'Ürün kodu'),(14,'Açıklama'),(15,'Ayrıntı 1'),(16,'Ayrıntı 2'),
              (17,'Ayrıntı 3'),(20,'Kalem notu'),(21,'Sipariş notu')]
    desc = [f'Sipariş: {row[1]}',f'Ürün: {row[13]}',f'Adet: {row[18]} {row[19]}',
            f'İstenen teslim tarihi: {row[3] or "Belirtilmedi"}']
    desc += [f'{label}: {row[i]}' for i,label in labels if row[i]]
    desc += [f'BOS kayıt: {row[0]}']
    match = re.fullmatch(r'SA-\d{4}-(\d+)', str(row[1]))
    number = str(int(match.group(1))) if match else row[1]
    return {'name':f'{number} · {row[13]} · {row[18]} {row[19]}', 'desc':'\n\n'.join(desc), **location(row[7])}

class Client:
    def __init__(self, config): self.config=config
    def call(self, method, path, files=None, **values):
        # Credentials in headers, never URLs or error text.
        headers={'Authorization': 'OAuth oauth_consumer_key="'+self.config['key']+'", oauth_token="'+self.config['token']+'"'}
        try:
            # Trello's coordinate parameters use form bracket notation.
            form_encoded=bool(files) or 'coordinates[latitude]' in values
            r=requests.request(method,'https://api.trello.com/1/'+path,headers=headers,
                params=values if method=='GET' else None,json=values if method!='GET' and not form_encoded else None,
                data=values if form_encoded else None,files=files,timeout=12)
            if not r.ok: raise TrelloError('Trello işlemi tamamlanamadı. Bağlantı ve izinleri kontrol edin.')
            return r.json()
        except (requests.RequestException, ValueError):
            raise TrelloError('Trello yanıtı alınamadı. Tekrar kart oluşturulmadı; sonucu kontrol edin.') from None
    def attach_form(self, card, filename, pdf):
        existing=self.call('GET',f'cards/{card}/attachments',fields='name')
        if not any(x.get('name')==filename for x in existing):
            self.call('POST',f'cards/{card}/attachments',name=filename,
                      files={'file':(filename,pdf,'application/pdf')})

    def lists(self): return self.call('GET',f'boards/{BOARD}/lists',filter='open',fields='name,closed')
    def cards(self): return self.call('GET',f'boards/{BOARD}/cards',filter='all',fields='id,desc,url,locationName,address,coordinates')

def register_supplier_trello(app, db, Order):
    def cipher():
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(('bos-trello-v1:'+app.secret_key).encode()).digest()))
    def ready(): return inspect(db.engine).has_table(connection.name)
    def config():
        if not ready(): return {}
        with db.engine.connect() as c: value=c.execute(select(connection.c.config).where(connection.c.id==1)).scalar()
        if not value: return {}
        try: return json.loads(cipher().decrypt(value.encode()))
        except Exception: raise TrelloError('Trello ayarları okunamadı; yönetici yeniden bağlamalı.') from None
    def personal_id():
        if getattr(g, 'web_is_owner', False): return 0
        uid=session.get('web_user_id')
        if not isinstance(uid,int) or uid<1: abort(403)
        return uid
    def personal_config():
        uid=personal_id()
        if inspect(db.engine).has_table(personal_connections.name):
            with db.engine.connect() as c:
                value=c.execute(select(personal_connections.c.config).where(personal_connections.c.user_id==uid)).scalar()
            if value:
                try: return json.loads(cipher().decrypt(value.encode()))
                except Exception: raise TrelloError('Kişisel Trello bağlantınızı yeniden kurun.') from None
        # Only the owner may retain the original owner connection.
        return config() if uid==0 else {}

    @app.route('/hesabim/trello',methods=['GET','POST'])
    def trello_personal_settings():
        if not app.config.get('WEB_AUTH_ENABLED'): abort(404)
        uid=personal_id(); error=None; cfg={}; authorize_url=None
        try:
            shared=config()
            cfg=personal_config()
            if shared.get('key'):
                authorize_url='https://trello.com/1/authorize?'+urlencode({
                    'key':shared['key'],'name':'BusinessOS Kişisel Sipariş Aktarımı',
                    'response_type':'token','scope':'read,write','expiration':'never'})
            if request.method=='POST':
                if not shared.get('key'): raise TrelloError('Yönetici önce Trello uygulama bağlantısını kurmalı.')
                token=request.form.get('token','').strip()
                if not token or len(token)>1024 or any(x in token for x in ('"','\r','\n')):
                    raise TrelloError('Geçerli bir Trello belirteci girin.')
                candidate={'key':shared['key'],'token':token}
                client=Client(candidate)
                member=client.call('GET','members/me',fields='id,username,fullName')
                if not isinstance(member,dict) or not member.get('id') or not member.get('username'):
                    raise TrelloError('Trello hesabı doğrulanamadı.')
                lists=client.lists()
                target=[x for x in lists if x['name'].strip() in ('Sipariş','🧾 Sipariş')]
                if len(target)!=1: raise TrelloError('Hesabınız hedef panoya erişebilmeli ve panoda tek bir Sipariş listesi bulunmalı.')
                candidate.update(list_id=target[0]['id'],list_name=target[0]['name'],
                    member_id=member['id'],member_name=member.get('fullName') or member['username'],member_username=member['username'])
                meta.create_all(db.engine)
                encrypted=cipher().encrypt(json.dumps(candidate).encode()).decode()
                with db.engine.begin() as c:
                    if c.execute(select(personal_connections.c.user_id).where(personal_connections.c.user_id==uid)).scalar() is not None:
                        c.execute(update(personal_connections).where(personal_connections.c.user_id==uid).values(config=encrypted))
                    else: c.execute(insert(personal_connections).values(user_id=uid,config=encrypted))
                flash('Kendi Trello bağlantınız kaydedildi. Yeni kartlar bu hesapla oluşturulacak.','success')
                return redirect(url_for('trello_personal_settings'))
        except TrelloError as exc: error=str(exc)
        return render_template('trello_personal_settings.html',connected=bool(cfg),
            member_name=cfg.get('member_name'),member_username=cfg.get('member_username'),
            authorize_url=authorize_url,error=error,board_url=BOARD_URL)

    def owner():
        if not app.config.get('WEB_AUTH_ENABLED'): abort(404)
        if not getattr(g,'web_is_owner',False): abort(403)
    app.jinja_env.globals['trello_allowed']=lambda o: app.config.get('WEB_AUTH_ENABLED') and allowed_order(o)

    from trello_chat_bridge import register, send_order, order_sent, form_pdf, send_priced_order, priced_order_sent
    register(app, db, Order, config)

    @app.route('/yonetim/trello',methods=['GET','POST'])
    def trello_settings():
        owner()
        error=None
        try:
            cfg=config()
            if request.method=='POST':
                key=request.form.get('key','').strip(); token=request.form.get('token','').strip()
                if not key or not token or any(x in key+token for x in ('"','\r','\n')): raise TrelloError('Geçerli anahtar ve belirteç gerekli.')
                candidate={'key':key,'token':token}
                lists=Client(candidate).lists() # Validate access read-only before persistence.
                target=[x for x in lists if x['name'].strip() in ('Sipariş','🧾 Sipariş')]
                if len(target)!=1: raise TrelloError('Panoda tek bir Sipariş listesi bulunmalı.')
                candidate['list_id']=target[0]['id']; candidate['list_name']=target[0]['name']
                meta.create_all(db.engine)
                with db.engine.begin() as c:
                    encrypted=cipher().encrypt(json.dumps(candidate).encode()).decode()
                    if c.execute(select(connection.c.id).where(connection.c.id==1)).scalar(): c.execute(update(connection).where(connection.c.id==1).values(config=encrypted))
                    else: c.execute(insert(connection).values(id=1,config=encrypted))
                flash('Trello bağlantısı doğrulandı ve kaydedildi. Hiçbir kart oluşturulmadı.','success')
                return redirect(url_for('trello_settings'))
        except TrelloError as exc: error=str(exc); cfg={}
        return render_template('trello_settings.html',connected=bool(cfg),error=error,board_url=BOARD_URL)

    @app.route('/siparisler/<int:order_id>/trello',methods=['GET','POST'])
    def trello_export(order_id):
        if not app.config.get('WEB_AUTH_ENABLED'): abort(404)
        order=db.get_or_404(Order,order_id)
        if not allowed_order(order): abort(403)
        # A sale and its procurement have different line IDs. Never offer a
        # second export of the sale once procurement exists, regardless of user.
        if order.order_type == 'Satış':
            purchases=Order.query.filter_by(source_order_id=order.id,order_type='Satın Alma').filter(Order.status!='İptal Edildi').order_by(Order.id).all()
            if purchases:
                target=url_for('trello_export',order_id=purchases[0].id) if len(purchases)==1 else None
                if request.method=='POST' and request.headers.get('Accept')=='application/json':
                    return jsonify(error='Bu satış siparişinin tedarik siparişinden aktarım yapılmalı.',
                        redirect_url=target or url_for('trello_export',order_id=order.id)),409
                if target: return redirect(target,code=303)
                return render_template('trello_purchase_orders.html',order=order,purchases=purchases)
        rows=order_rows(order,g.web_username)
        images=product_images(app,db,order)
        signer=URLSafeTimedSerializer(app.secret_key,salt='trello-export')
        identity={'order':order.id,'hash':fingerprint(rows),'actor':g.web_username}
        if chat_allowed(order):
            from supplier_chat import order_fingerprint
            identity['priced_hash']=order_fingerprint(order)
        if images: identity['images']={key:value['key'] for key,value in images.items()}
        form_version=hashlib.sha256(json.dumps(dict(hash=identity['hash'],images=identity.get('images',{})),sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
        form_keys={r[0]:r[0]+':form:'+form_version for r in rows}
        error=None; cfg={}
        batch_request = request.method == 'POST' and request.headers.get('Accept') == 'application/json'
        try:
            cfg=personal_config()
            if request.method=='POST':
                try: signed=signer.loads(request.form.get('confirm',''),max_age=1800)
                except BadSignature: raise TrelloError('Önizleme süresi doldu. Sayfayı yenileyin.') from None
                if signed!=identity: raise TrelloError('Sipariş değişti; önizlemeyi yenileyin.')
                if not cfg: raise TrelloError('Önce kendi Trello hesabınızı bağlayın.')
                client=Client(cfg)
                meta.create_all(db.engine)
                cards=None
                pdf=None
                filename=f'{order.order_no}-Siparis-Formu-{form_version}.pdf'
                sent=0
                for row in rows:
                    with db.engine.connect() as c: previous=c.execute(select(deliveries).where(deliveries.c.key==row[0])).mappings().first()
                    with db.engine.connect() as c: done=c.execute(select(extras.c.card_id).where(extras.c.key==form_keys[row[0]])).scalar()
                    if previous and previous['card_id'] and done==previous['card_id']: continue
                    # Keep each browser request bounded: one card/PDF per
                    # request, then Chat in its own request after the last PDF.
                    if sent>=1: break
                    if cards is None:
                        if not any(x['id']==cfg['list_id'] for x in client.lists()): raise TrelloError('Hedef liste kapalı veya bulunamadı.')
                        cards=client.cards()
                    card=None
                    if previous and previous['card_id']:
                        card=next((x for x in cards if x['id']==previous['card_id']),None)
                        if card is None: raise TrelloError('Aktarılan kart panoda bulunamadı; kartın konumunu kontrol edin.')
                    matches=[x for x in cards if f'BOS kayıt: {row[0]}' in x.get('desc','').split('\n\n')]
                    if len(matches)>1: raise TrelloError('Bu kalem için birden fazla kart bulundu. Yönetici kontrol etmeli.')
                    if matches and not card:
                        card=matches[0]
                        with db.engine.begin() as c:
                            if not previous: c.execute(insert(deliveries).values(key=row[0],actor=g.web_username))
                            c.execute(update(deliveries).where(deliveries.c.key==row[0]).values(card_id=card['id'],card_url=card['url']))
                    if previous and not card: raise TrelloError('Önceki aktarımın sonucu belirsiz. Mükerrer kartı önlemek için durduruldu; yönetici kontrol etmeli.')
                    if not card:
                        item=next(item for item in order.items if row[0]==f'bos:order:{order.id}:item:{item.id}')
                        if order.order_type=='Satın Alma' and order.source_order_id:
                            source_prefix=f'bos:order:{order.source_order_id}:item:'
                            source_key=source_prefix+str(item.source_order_item_id) if item.source_order_item_id else None
                            with db.engine.connect() as c:
                                predicate=deliveries.c.key==source_key if source_key else deliveries.c.key.startswith(source_prefix)
                                source_delivery=c.execute(select(deliveries.c.key).where(predicate).limit(1)).scalar()
                            source_remote=any(
                                part==f'BOS kayıt: {source_key}' if source_key else part.startswith(f'BOS kayıt: {source_prefix}')
                                for x in cards for part in x.get('desc','').split('\n\n'))
                            if source_delivery or source_remote:
                                raise TrelloError('Bu kalem bağlı satış siparişinden daha önce aktarılmış. Çift kart oluşturmamak için durduruldu; yönetici mevcut kartı kontrol etmeli.')
                        try:
                            with db.engine.begin() as c: c.execute(insert(deliveries).values(key=row[0],actor=g.web_username,created_at=datetime.now(timezone.utc).isoformat()))
                        except IntegrityError: raise TrelloError('Bu kalem başka bir işlemde aktarılıyor. Sayfayı yenileyin.') from None
                        data=payload(row)
                        if order.delivery_date: data['due']=order.delivery_date.isoformat()+'T09:00:00+03:00'
                        card=client.call('POST','cards',idList=cfg['list_id'],pos='top',**data)
                        card['desc']=data['desc']
                        card['locationName']=data.get('locationName')
                        card['address']=data.get('address')
                        with db.engine.begin() as c: c.execute(update(deliveries).where(deliveries.c.key==row[0]).values(card_id=card['id'],card_url=card['url']))
                    updates={}
                    place=location(row[7])
                    if place and (card.get('locationName')!=place['locationName'] or card.get('address')!=place['address']):
                        updates.update(place)
                    if row[7] and f'Teslim ili: {row[7]}' not in card.get('desc','').split('\n\n'):
                        updates['desc']=card.get('desc','')+'\n\nTeslim ili: '+str(row[7])
                    if updates: client.call('PUT',f'cards/{card["id"]}',**updates)
                    if pdf is None: pdf=order_form_pdf(rows,images)
                    client.attach_form(card['id'],filename,pdf)
                    with db.engine.begin() as c:
                        if done: c.execute(update(extras).where(extras.c.key==form_keys[row[0]]).values(card_id=card['id']))
                        else: c.execute(insert(extras).values(key=form_keys[row[0]],card_id=card['id']))
                    sent+=1
        except TrelloError as exc: error=str(exc)
        states={}
        if ready():
            with db.engine.connect() as c: states={x['key']:dict(x) for x in c.execute(select(deliveries).where(deliveries.c.key.in_([r[0] for r in rows]))).mappings()}
        completed={}
        if inspect(db.engine).has_table(extras.name):
            with db.engine.connect() as c: completed=dict(c.execute(select(extras.c.key,extras.c.card_id).where(extras.c.key.in_(list(form_keys.values())))).all())
        remaining = sum(not states.get(r[0],{}).get('card_id') or completed.get(form_keys[r[0]])!=states[r[0]]['card_id'] for r in rows)
        chat_enabled=chat_allowed(order)
        chat_done=order_sent(db,order) if chat_enabled else True
        priced_done=priced_order_sent(db,order) if chat_enabled else True
        group_was_done=chat_done
        if request.method=='POST' and not error and not remaining and not chat_done and (not batch_request or sent==0):
            try:
                send_order(app,db,order,rows)
                chat_done=True
            except Exception as exc:
                # Record only the exception type, never credentials or API URLs.
                app.logger.warning('Trello Chat delivery failed for order %s (%s)',order.id,type(exc).__name__)
                error='Trello kartları hazır, ancak Google Chat gönderimi tamamlanamadı. Tekrar deneyin; kartlar çoğaltılmaz.'
        if request.method=='POST' and not error and not remaining and group_was_done and not priced_done and (not batch_request or sent==0):
            try:
                send_priced_order(app,db,order)
                priced_done=True
            except Exception as exc:
                app.logger.warning('Dilek priced PDF delivery failed for order %s (%s)',order.id,type(exc).__name__)
                error='Trello ve grup gönderimi tamamlandı; Dilek’e fiyatlı PDF gönderilemedi. Tekrar deneyin; önceki gönderimler çoğaltılmaz.'
        if request.method=='POST' and not batch_request and not error:
            return redirect(url_for('trello_export',order_id=order.id))
        if batch_request:
            return jsonify(total=len(rows), completed=len(rows)-remaining, remaining=remaining, error=error, chat_done=chat_done,priced_done=priced_done,chat_enabled=chat_enabled), (409 if error else 200)
        row_completed={r[0]:bool(states.get(r[0],{}).get('card_id') and completed.get(form_keys[r[0]])==states[r[0]]['card_id']) for r in rows}
        return render_template('trello_export.html',order=order,rows=rows,states=states,row_completed=row_completed,connected=bool(cfg),error=error,
            confirm=signer.dumps(identity),board_url=BOARD_URL,payload=payload,
            remaining=remaining,chat_done=chat_done,priced_done=priced_done,chat_enabled=chat_enabled,images=images,trello_member_name=cfg.get("member_name"))
