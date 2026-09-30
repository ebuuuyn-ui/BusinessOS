"""Explicit price-free ABIKA line export to a fixed Trello board."""
import json
import re
import hashlib
import base64
from datetime import datetime, timezone
import requests
from cryptography.fernet import Fernet
from flask import abort, g, request, render_template, redirect, url_for, flash, jsonify
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import MetaData, Table, Column, Integer, Text, String, select, insert, update, inspect
from sqlalchemy.exc import IntegrityError
from supplier_sheets import allowed_order, order_rows, fingerprint

BOARD = '6abd5e01fddae7338973fe6b'
BOARD_URL = 'https://trello.com/b/zGa5gdZO'
meta = MetaData()
connection = Table('businessos_trello_connection', meta,
    Column('id', Integer, primary_key=True), Column('config', Text, nullable=False))
deliveries = Table('businessos_trello_delivery', meta,
    Column('key', String(100), primary_key=True), Column('card_id', Text),
    Column('card_url', Text), Column('actor', Text), Column('created_at', Text))

class TrelloError(Exception): pass

def payload(row):
    # Explicit non-financial fields only; one stable marker per order line.
    labels = [(12,'Ürün kodu'),(14,'Açıklama'),(15,'Ayrıntı 1'),(16,'Ayrıntı 2'),
              (17,'Ayrıntı 3'),(20,'Kalem notu'),(21,'Sipariş notu')]
    desc = [f'Sipariş: {row[1]}',f'Ürün: {row[13]}',f'Adet: {row[18]} {row[19]}',
            f'İstenen teslim tarihi: {row[3] or "Belirtilmedi"}']
    desc += [f'{label}: {row[i]}' for i,label in labels if row[i]]
    desc += [f'BOS kayıt: {row[0]}']
    match = re.fullmatch(r'SA-\d{4}-(\d+)', str(row[1]))
    number = str(int(match.group(1))) if match else row[1]
    return {'name':f'{number} · {row[13]} · {row[18]} {row[19]}', 'desc':'\n\n'.join(desc)}

class Client:
    def __init__(self, config): self.config=config
    def call(self, method, path, **values):
        # Credentials in headers, never URLs or error text.
        headers={'Authorization': 'OAuth oauth_consumer_key="'+self.config['key']+'", oauth_token="'+self.config['token']+'"'}
        try:
            r=requests.request(method,'https://api.trello.com/1/'+path,headers=headers,
                params=values if method=='GET' else None,json=values if method!='GET' else None,timeout=12)
            if not r.ok: raise TrelloError('Trello işlemi tamamlanamadı. Bağlantı ve izinleri kontrol edin.')
            return r.json()
        except (requests.RequestException, ValueError):
            raise TrelloError('Trello yanıtı alınamadı. Tekrar kart oluşturulmadı; sonucu kontrol edin.') from None
    def lists(self): return self.call('GET',f'boards/{BOARD}/lists',filter='open',fields='name,closed')
    def cards(self): return self.call('GET',f'boards/{BOARD}/cards',filter='all',fields='id,desc,url')

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
    def owner():
        if not app.config.get('WEB_AUTH_ENABLED'): abort(404)
        if not getattr(g,'web_is_owner',False): abort(403)
    app.jinja_env.globals['trello_allowed']=lambda o: app.config.get('WEB_AUTH_ENABLED') and allowed_order(o)

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
        rows=order_rows(order,g.web_username)
        signer=URLSafeTimedSerializer(app.secret_key,salt='trello-export')
        identity={'order':order.id,'hash':fingerprint(rows),'actor':g.web_username}
        error=None; cfg={}
        batch_request = request.method == 'POST' and request.headers.get('Accept') == 'application/json'
        try:
            cfg=config()
            if request.method=='POST':
                try: signed=signer.loads(request.form.get('confirm',''),max_age=1800)
                except BadSignature: raise TrelloError('Önizleme süresi doldu. Sayfayı yenileyin.') from None
                if signed!=identity: raise TrelloError('Sipariş değişti; önizlemeyi yenileyin.')
                if not cfg: raise TrelloError('Önce yönetici Trello bağlantısını kurmalı.')
                client=Client(cfg)
                if not any(x['id']==cfg['list_id'] for x in client.lists()): raise TrelloError('Hedef liste kapalı veya bulunamadı.')
                cards=client.cards()
                sent=0
                for row in rows:
                    with db.engine.connect() as c: previous=c.execute(select(deliveries).where(deliveries.c.key==row[0])).mappings().first()
                    if previous and previous['card_id']: continue
                    matches=[x for x in cards if f'BOS kayıt: {row[0]}' in x.get('desc','').split('\n\n')]
                    if len(matches)>1: raise TrelloError('Bu kalem için birden fazla kart bulundu. Yönetici kontrol etmeli.')
                    if matches:
                        card=matches[0]
                        with db.engine.begin() as c:
                            if not previous: c.execute(insert(deliveries).values(key=row[0],actor=g.web_username))
                            c.execute(update(deliveries).where(deliveries.c.key==row[0]).values(card_id=card['id'],card_url=card['url']))
                        continue
                    if previous: raise TrelloError('Önceki aktarımın sonucu belirsiz. Mükerrer kartı önlemek için durduruldu; yönetici kontrol etmeli.')
                    if sent>=3: break # Bounded serverless request; continue remaining lines explicitly.
                    try:
                        with db.engine.begin() as c: c.execute(insert(deliveries).values(key=row[0],actor=g.web_username,created_at=datetime.now(timezone.utc).isoformat()))
                    except IntegrityError: raise TrelloError('Bu kalem başka bir işlemde aktarılıyor. Sayfayı yenileyin.') from None
                    data=payload(row)
                    if order.delivery_date: data['due']=order.delivery_date.isoformat()+'T09:00:00+03:00'
                    card=client.call('POST','cards',idList=cfg['list_id'],**data)
                    with db.engine.begin() as c: c.execute(update(deliveries).where(deliveries.c.key==row[0]).values(card_id=card['id'],card_url=card['url']))
                    sent+=1
                if not batch_request:
                    flash(f'{sent} yeni ürün kartı oluşturuldu. Kalan kalemler varsa devam edebilirsiniz.','success')
                    return redirect(url_for('trello_export',order_id=order.id))
        except TrelloError as exc: error=str(exc)
        states={}
        if ready():
            with db.engine.connect() as c: states={x['key']:dict(x) for x in c.execute(select(deliveries).where(deliveries.c.key.in_([r[0] for r in rows]))).mappings()}
        remaining = sum(not states.get(r[0],{}).get('card_id') for r in rows)
        if batch_request:
            return jsonify(total=len(rows), completed=len(rows)-remaining, remaining=remaining, error=error), (409 if error else 200)
        return render_template('trello_export.html',order=order,rows=rows,states=states,connected=bool(cfg),error=error,
            confirm=signer.dumps(identity),board_url=BOARD_URL,payload=payload,
            remaining=remaining)
