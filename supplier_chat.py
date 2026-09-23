"""Explicit, fixed-recipient ABIKA PDF sharing. No background sending."""
import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet
from flask import abort, flash, g, redirect, render_template, request, session, url_for
from google.auth.transport.requests import Request
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from sqlalchemy import (MetaData, Table, Column, Integer, String, Text, LargeBinary,
                        DateTime, select, insert, update, inspect, text)
from supplier_sheets import allowed_order

SPACE = 'spaces/AAQArMh8YcU'
SPACE_TITLE = 'Abika Mobilya Toptan Satış'
SPACE_URL = 'https://mail.google.com/mail/u/2/#chat/space/AAQArMh8YcU'
ACCOUNT = 'bekir@abikamobilya.com'
SCOPE = 'https://www.googleapis.com/auth/chat.messages.create'
CALLBACK = 'https://ebuyan.site/yonetim/google-chat/callback'
meta = MetaData()
connection_table = Table('businessos_chat_connection', meta,
    Column('id', Integer, primary_key=True), Column('encrypted_config', Text, nullable=False))
delivery_table = Table('businessos_chat_delivery', meta,
    Column('key', String(64), primary_key=True), Column('order_id', Integer, nullable=False),
    Column('request_id', String(36), nullable=False), Column('filename', Text, nullable=False),
    Column('pdf', LargeBinary, nullable=False), Column('attachment', Text),
    Column('message_name', Text), Column('created_by', Text, nullable=False),
    Column('sent_by', Text), Column('created_at', DateTime(timezone=True), nullable=False),
    Column('sent_at', DateTime(timezone=True)))


class ChatError(Exception):
    pass


def order_fingerprint(order):
    fields = ('id','order_no','order_type','customer_id','order_date','delivery_date','status',
              'delivery_city','shipment_contact','shipment_phone','shipment_address','shipment_note','notes','payment_method')
    item_fields = ('id','product_id','product_name','variant','detail_2','detail_3','quantity','unit',
                   'unit_price','discount_rate','vat_rate','vat_included','note')
    data = {'order':{k:str(getattr(order,k,None)) for k in fields},
            'customer':order.customer.name,
            'items':[{k:str(getattr(i,k,None)) for k in item_fields} for i in sorted(order.items,key=lambda i:i.id)]}
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def cipher(app):
    key = hashlib.sha256(('businessos-chat-v1:' + app.secret_key).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def schema_ready(db):
    with db.engine.connect() as conn:
        return all(inspect(conn).has_table(t.name) for t in (connection_table,delivery_table))


def read_config(app, conn):
    value = conn.execute(select(connection_table.c.encrypted_config).where(connection_table.c.id==1)).scalar()
    if not value:
        return {}
    try:
        return json.loads(cipher(app).decrypt(value.encode()))
    except Exception:
        raise ChatError('Google bağlantısı okunamadı. Yönetici bağlantıyı yeniden kurmalı.') from None


def write_config(app, conn, config):
    value = cipher(app).encrypt(json.dumps(config).encode()).decode()
    if conn.execute(select(connection_table.c.id).where(connection_table.c.id==1)).scalar():
        conn.execute(update(connection_table).where(connection_table.c.id==1).values(encrypted_config=value))
    else:
        conn.execute(insert(connection_table).values(id=1,encrypted_config=value))


def chat_service(config):
    if not config.get('refresh_token'):
        raise ChatError('Önce yönetici Google Chat hesabını bağlamalı.')
    credentials = Credentials(None, refresh_token=config['refresh_token'],
        token_uri='https://oauth2.googleapis.com/token', client_id=config['client_id'],
        client_secret=config['client_secret'], scopes=[SCOPE])
    # httplib2 uses an explicit timeout; no unbounded network wait on serverless.
    import httplib2
    import google_auth_httplib2
    http = google_auth_httplib2.AuthorizedHttp(credentials,http=httplib2.Http(timeout=15))
    return build('chat','v1',http=http,cache_discovery=False,static_discovery=True)


def register_supplier_chat(app, db, Order, OrderHistory):
    app.jinja_env.globals['supplier_chat_allowed'] = lambda o: app.config.get('WEB_AUTH_ENABLED') and allowed_order(o)

    def owner():
        if not app.config.get('WEB_AUTH_ENABLED'): abort(404)
        if not getattr(g,'web_is_owner',False): abort(403)

    @app.route('/yonetim/google-chat', methods=['GET','POST'])
    def supplier_chat_settings():
        owner()
        ready = schema_ready(db)
        if request.method == 'POST':
            # Add only these two new tables; never run create_all on business models.
            meta.create_all(db.engine)
            with db.engine.begin() as conn:
                config = read_config(app,conn)
                client_id_value = request.form.get('client_id','').strip()
                secret = request.form.get('client_secret','').strip()
                if not client_id_value.endswith('.apps.googleusercontent.com') or not secret:
                    flash('Google istemci kimliği ve bağlantı anahtarı gerekli.','error')
                else:
                    write_config(app,conn,{'client_id':client_id_value,'client_secret':secret})
                    flash('Bağlantı ayarları kaydedildi. Şimdi Google hesabını bağlayın.','success')
            return redirect(url_for('supplier_chat_settings'))
        with db.engine.connect() as conn:
            config = read_config(app,conn) if ready else {}
        return render_template('supplier_chat_settings.html',configured=bool(config.get('client_id')),
            connected=bool(config.get('refresh_token')),account=ACCOUNT,space_title=SPACE_TITLE,callback=CALLBACK)

    @app.post('/yonetim/google-chat/baglan')
    def supplier_chat_connect():
        owner()
        if not schema_ready(db): abort(409)
        with db.engine.connect() as conn: config=read_config(app,conn)
        if not config.get('client_id'): abort(409)
        state=secrets.token_urlsafe(32); verifier=secrets.token_urlsafe(64); nonce=secrets.token_urlsafe(32)
        session['chat_oauth']={'state':state,'verifier':verifier,'nonce':nonce,'at':datetime.now(timezone.utc).timestamp()}
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        oauth_url='https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({
            'client_id':config['client_id'],'redirect_uri':CALLBACK,'response_type':'code',
            'scope':'openid email '+SCOPE,'access_type':'offline','prompt':'consent',
            'login_hint':ACCOUNT,'state':state,'nonce':nonce,'code_challenge':challenge,'code_challenge_method':'S256'})
        return render_template('supplier_chat_oauth.html',oauth_url=oauth_url)

    @app.route('/yonetim/google-chat/callback',methods=['GET','POST'])
    def supplier_chat_callback():
        if request.method == 'GET':
            return render_template('supplier_chat_oauth.html',oauth_url=None,
                code=request.args.get('code',''),state=request.args.get('state',''),error=request.args.get('error',''))
        owner()
        flow=session.pop('chat_oauth',{})
        if not flow or not hmac.compare_digest(flow['state'],request.form.get('state','')) or datetime.now(timezone.utc).timestamp()-flow['at']>600:
            abort(400)
        try:
            if request.form.get('error'): raise ChatError('Google bağlantısına izin verilmedi.')
            with db.engine.connect() as conn: config=read_config(app,conn)
            response=requests.post('https://oauth2.googleapis.com/token',data={
                'client_id':config['client_id'],'client_secret':config['client_secret'],
                'code':request.form.get('code',''),'code_verifier':flow['verifier'],
                'grant_type':'authorization_code','redirect_uri':CALLBACK},timeout=15)
            if not response.ok: raise ChatError('Google bağlantısı tamamlanamadı; tekrar deneyin.')
            tokens=response.json()
            identity=id_token.verify_oauth2_token(tokens.get('id_token',''),Request(),config['client_id'])
            if identity.get('email')!=ACCOUNT or not identity.get('email_verified') or identity.get('nonce')!=flow['nonce']:
                raise ChatError('Yalnızca '+ACCOUNT+' hesabı bağlanabilir.')
            if SCOPE not in tokens.get('scope','').split() or not tokens.get('refresh_token'):
                raise ChatError('Chat gönderim izni alınamadı. Google hesabını yeniden bağlayın.')
            config['refresh_token']=tokens['refresh_token']
            with db.engine.begin() as conn: write_config(app,conn,config)
            flash('Google Chat hesabı bağlandı. Henüz mesaj gönderilmedi.','success')
        except Exception:
            flash('Google Chat bağlantısı tamamlanamadı. Doğru hesap ve gönderim izniyle yeniden deneyin.','error')
        return redirect(url_for('supplier_chat_settings'))

    def scoped_order(order_id):
        if not app.config.get('WEB_AUTH_ENABLED'): abort(404)
        order=db.get_or_404(Order,order_id)
        if not allowed_order(order): abort(403)
        return order

    @app.route('/siparisler/<int:order_id>/google-chat', methods=['GET','POST'])
    def supplier_chat_preview(order_id):
        order=scoped_order(order_id)
        if not schema_ready(db):
            return render_template('supplier_chat_preview.html',order=order,ready=False,space_title=SPACE_TITLE,space_url=SPACE_URL)
        with db.engine.connect() as conn: config=read_config(app,conn)
        if not config.get('refresh_token'):
            return render_template('supplier_chat_preview.html',order=order,ready=False,space_title=SPACE_TITLE,space_url=SPACE_URL)
        key=order_fingerprint(order)
        from itsdangerous import URLSafeTimedSerializer
        signer=URLSafeTimedSerializer(app.secret_key,salt='chat-confirm-v1')
        error=None
        if request.method=='POST':
            try:
                preview=signer.loads(request.form.get('confirm',''),max_age=1800)
                if request.form.get('ack') != 'yes': raise ChatError('PDF ve hedef sohbet onayı gerekli.')
                if preview!={'key':key,'order_id':order.id,'actor':g.web_username}: raise ChatError('Sipariş değişmiş veya onay süresi dolmuş. Önizlemeyi yeniden açın.')
                with db.engine.begin() as conn:
                    if db.engine.dialect.name=='postgresql':
                        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                        conn.execute(text('SELECT pg_advisory_xact_lock(72109343)'))
                    delivery=conn.execute(select(delivery_table).where(delivery_table.c.key==key)).mappings().first()
                    if not delivery: raise ChatError('Önizlemeyi yeniden açın.')
                    if not delivery['message_name']:
                        service=chat_service(config)
                        attachment=json.loads(delivery['attachment']) if delivery['attachment'] else None
                        if not attachment:
                            attachment=service.media().upload(parent=SPACE,body={'filename':delivery['filename']},media_body=MediaIoBaseUpload(BytesIO(delivery['pdf']),mimetype='application/pdf',resumable=False)).execute()
                            if not attachment.get('attachmentDataRef'): raise ChatError('PDF yüklemesi doğrulanamadı.')
                            conn.execute(update(delivery_table).where(delivery_table.c.key==key).values(attachment=json.dumps(attachment)))
                # Commit the attachment first so every retry sends an identical payload.
                with db.engine.begin() as conn:
                    if db.engine.dialect.name=='postgresql':
                        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                        conn.execute(text('SELECT pg_advisory_xact_lock(72109343)'))
                    delivery=conn.execute(select(delivery_table).where(delivery_table.c.key==key)).mappings().one()
                    if not delivery['message_name']:
                        service=chat_service(config)
                        attachment=json.loads(delivery['attachment'])
                        result=service.spaces().messages().create(parent=SPACE,requestId=delivery['request_id'],messageId='client-bos-'+key[:48],body={'text':order.order_no+' · Satın alma siparişi','attachment':[attachment]}).execute()
                        name=result.get('name','')
                        if not name.startswith(SPACE+'/messages/'): raise ChatError('Gönderim sonucu doğrulanamadı.')
                        conn.execute(update(delivery_table).where(delivery_table.c.key==key).values(message_name=name,sent_by=g.web_username,sent_at=datetime.now(timezone.utc)))
                # Delivery table is the authoritative durable audit; business data is unchanged.
                flash('PDF Google Chat sohbetine gönderildi. Aynı sipariş içeriği yeniden gönderilmez.','success')
                return redirect(url_for('supplier_chat_preview',order_id=order.id))
            except ChatError as exc: error=str(exc)
            except Exception: error='Gönderim tamamlanamadı veya sonucu alınamadı. Yeniden denemede aynı gönderim kimliği kullanılır.'
        with db.engine.begin() as conn:
            delivery=conn.execute(select(delivery_table).where(delivery_table.c.key==key)).mappings().first()
            if not delivery:
                response=app.view_functions['export_order_pdf'](order.id)
                response.direct_passthrough=False
                pdf=response.get_data(); response.close()
                if not pdf.startswith(b'%PDF-'): raise ChatError('PDF hazırlanamadı.')
                # Stable primary key and request UUID prevent duplicate confirmations.
                values=dict(key=key,order_id=order.id,request_id=str(uuid.uuid5(uuid.NAMESPACE_URL,SPACE+':'+key)),filename=order.order_no+'.pdf',pdf=pdf,created_by=g.web_username,created_at=datetime.now(timezone.utc))
                if db.engine.dialect.name=='postgresql':
                    from sqlalchemy.dialects.postgresql import insert as pg_insert
                    conn.execute(pg_insert(delivery_table).values(**values).on_conflict_do_nothing(index_elements=['key']))
                else:
                    conn.execute(insert(delivery_table).values(**values))
                delivery=conn.execute(select(delivery_table).where(delivery_table.c.key==key)).mappings().first()
        return render_template('supplier_chat_preview.html',order=order,ready=True,delivery=delivery,
            space_title=SPACE_TITLE,space_url=SPACE_URL,error=error,
            token=signer.dumps({'key':key,'order_id':order.id,'actor':g.web_username}))

    @app.get('/siparisler/<int:order_id>/google-chat/pdf/<key>')
    def supplier_chat_pdf(order_id,key):
        scoped_order(order_id)
        if not schema_ready(db): abort(404)
        with db.engine.connect() as conn:
            row=conn.execute(select(delivery_table).where(delivery_table.c.key==key,delivery_table.c.order_id==order_id)).mappings().first()
        if not row: abort(404)
        from flask import send_file
        return send_file(BytesIO(row['pdf']),mimetype='application/pdf',as_attachment=True,download_name=row['filename'])
