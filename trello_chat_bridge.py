"""Fixed-board Trello events to the fixed ABIKA Chat, with durable deduplication."""
import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import datetime, timezone
from io import BytesIO
from flask import abort, g, request, render_template, redirect, url_for, flash
from sqlalchemy import MetaData, Table, Column, String, Text, LargeBinary, Integer, select, insert, update, inspect, text
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError
import supplier_chat as chat

meta=MetaData()
outbox=Table('businessos_trello_chat_outbox',meta,
    Column('key',String(100),primary_key=True),Column('body',Text,nullable=False),
    Column('pdf',LargeBinary),Column('filename',Text),Column('attachment',Text),
    Column('message_name',Text),Column('created_at',Text),Column('actor',Text))
forms=Table('businessos_trello_chat_forms',meta,
    Column('order_id',Integer,primary_key=True),Column('pdf',LargeBinary,nullable=False))
settings=Table('businessos_trello_chat_settings',meta,
    Column('id',Integer,primary_key=True),Column('encrypted',Text,nullable=False))


def lock(db,conn):
    if db.engine.dialect.name=='postgresql':
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text('SELECT pg_advisory_xact_lock(72109345)'))


def read_settings(app,db):
    if not inspect(db.engine).has_table(settings.name): return {}
    with db.engine.connect() as conn: value=conn.execute(select(settings.c.encrypted).where(settings.c.id==1)).scalar()
    return json.loads(chat.cipher(app).decrypt(value.encode())) if value else {}


def save_settings(app,db,value):
    with db.engine.begin() as conn:
        encrypted=chat.cipher(app).encrypt(json.dumps(value).encode()).decode()
        if conn.execute(select(settings.c.id).where(settings.c.id==1)).scalar(): conn.execute(update(settings).where(settings.c.id==1).values(encrypted=encrypted))
        else: conn.execute(insert(settings).values(id=1,encrypted=encrypted))


def enqueue(app,db,key,body,pdf=None,filename=None,actor='Trello'):
    meta.create_all(db.engine)
    with db.engine.begin() as conn:
        lock(db,conn)
        if not conn.execute(select(outbox.c.key).where(outbox.c.key==key)).scalar():
            conn.execute(insert(outbox).values(key=key,body=body,pdf=pdf,filename=filename,actor=actor,created_at=datetime.now(timezone.utc).isoformat()))


def deliver(app,db,key):
    if not chat.schema_ready(db): raise chat.ChatError('Google Chat bağlantısı kurulmamış.')
    with db.engine.begin() as conn:
        lock(db,conn)
        row=conn.execute(select(outbox).where(outbox.c.key==key)).mappings().one()
        if row['message_name']: return
        service=chat.chat_service(chat.read_config(app,conn))
        if row['pdf'] and not row['attachment']:
            attachment=service.media().upload(parent=chat.SPACE,body={'filename':row['filename']},
                media_body=MediaIoBaseUpload(BytesIO(row['pdf']),mimetype='application/pdf',resumable=False)).execute()
            if not attachment.get('attachmentDataRef'): raise chat.ChatError('PDF yüklemesi doğrulanamadı.')
            conn.execute(update(outbox).where(outbox.c.key==key).values(attachment=json.dumps(attachment)))
    # Commit uploaded media before sending; retries use the same body, file and IDs.
    with db.engine.begin() as conn:
        lock(db,conn)
        row=conn.execute(select(outbox).where(outbox.c.key==key)).mappings().one()
        if row['message_name']: return
        service=chat.chat_service(chat.read_config(app,conn))
        digest=hashlib.sha256(key.encode()).hexdigest()[:48]
        message_id='client-bos-tc-'+digest
        body={'text':row['body']}
        if row['attachment']: body['attachment']=[json.loads(row['attachment'])]
        try:
            result=service.spaces().messages().create(parent=chat.SPACE,
                requestId=str(uuid.uuid5(uuid.NAMESPACE_URL,chat.SPACE+':trello-chat:'+key)),
                messageId=message_id,body=body).execute()
            name=result.get('name','')
            if not name.startswith(chat.SPACE+'/messages/'): raise chat.ChatError('Chat gönderimi doğrulanamadı.')
        except HttpError as exc:
            if exc.resp.status!=409: raise
            # A retry after an accepted-but-timed-out request can return ALREADY_EXISTS.
            name=chat.SPACE+'/messages/'+message_id
        conn.execute(update(outbox).where(outbox.c.key==key).values(message_name=name))


def order_key(order): return f'order:{order.id}'

def order_sent(db,order):
    if not inspect(db.engine).has_table(outbox.name): return False
    with db.engine.connect() as conn: return bool(conn.execute(select(outbox.c.message_name).where(outbox.c.key==order_key(order))).scalar())


def form_pdf(db,order,rows):
    from supplier_trello import order_form_pdf
    meta.create_all(db.engine)
    with db.engine.begin() as conn:
        lock(db,conn)
        pdf=conn.execute(select(forms.c.pdf).where(forms.c.order_id==order.id)).scalar()
        if pdf is None:
            pdf=order_form_pdf(rows)
            conn.execute(insert(forms).values(order_id=order.id,pdf=pdf))
        return pdf


def send_order(app,db,order,rows):
    enqueue(app,db,order_key(order),f'{order.order_no} · Satın alma siparişi\nÜrün kartları Trello panosuna aktarıldı.',
        pdf=form_pdf(db,order,rows),filename=f'{order.order_no}-Siparis-Formu.pdf',actor=g.web_username)
    deliver(app,db,order_key(order))


def register(app,db,Order,get_trello_config):
    import supplier_trello
    from supplier_trello import BOARD,deliveries,TrelloError

    @app.route('/yonetim/trello-chat',methods=['GET','POST'])
    def trello_chat_settings():
        if not app.config.get('WEB_AUTH_ENABLED'): abort(404)
        if not getattr(g,'web_is_owner',False): abort(403)
        error=None
        cfg=read_settings(app,db)
        if request.method=='POST':
            try:
                client=supplier_trello.Client(get_trello_config())
                if not chat.schema_ready(db): raise chat.ChatError('Önce Google Chat bağlantısını kurun.')
                with db.engine.connect() as conn:
                    if not chat.read_config(app,conn).get('refresh_token'): raise chat.ChatError('Google Chat bağlantısı gerekli.')
                meta.create_all(db.engine)
                if request.form.get('retry')=='yes':
                    with db.engine.connect() as conn: pending=conn.execute(select(outbox.c.key).where(outbox.c.message_name.is_(None)).limit(3)).scalars().all()
                    for key in pending: deliver(app,db,key)
                    flash('Bekleyen bildirimler yeniden denendi.','success')
                elif not cfg.get('webhook_id'):
                    if not cfg:
                        cfg={'secret':secrets.token_urlsafe(32),'enabled_at':datetime.now(timezone.utc).isoformat()}
                        save_settings(app,db,cfg)
                    callback='https://ebuyan.site/integrations/trello-chat/'+cfg['secret']
                    hook=client.call('POST','webhooks',idModel=BOARD,callbackURL=callback,description='BusinessOS ABIKA yorum bildirimleri')
                    if not hook.get('id') or not hook.get('active'): raise TrelloError('Trello bildirim bağlantısı doğrulanamadı.')
                    cfg['webhook_id']=hook['id'];save_settings(app,db,cfg)
                    flash('Yeni Trello yorumları ABİKA Google Chat sohbetine bildirilecek.','success')
                return redirect(url_for('trello_chat_settings'))
            except Exception:
                error='İşlem tamamlanamadı. Bağlantıları kontrol edip yeniden deneyin.'
        pending=0
        if inspect(db.engine).has_table(outbox.name):
            with db.engine.connect() as conn: pending=len(conn.execute(select(outbox.c.key).where(outbox.c.message_name.is_(None))).all())
        return render_template('trello_chat_settings.html',enabled=bool(cfg.get('webhook_id')),error=error,pending=pending)

    @app.route('/integrations/trello-chat/<secret>',methods=['HEAD','POST'])
    def trello_chat_webhook(secret):
        if not app.config.get('WEB_AUTH_ENABLED'): abort(404)
        cfg=read_settings(app,db)
        if not cfg.get('secret') or not hmac.compare_digest(secret,cfg['secret']): abort(404)
        if request.method=='HEAD': return '',200
        if request.content_length and request.content_length>262144: abort(413)
        event=request.get_json(silent=True) or {}
        if not isinstance(event,dict): abort(400)
        incoming=event.get('action') or {}
        if not isinstance(incoming,dict) or incoming.get('type')!='commentCard': return '',200
        action_id=incoming.get('id','')
        if not isinstance(action_id,str) or not re.fullmatch('[0-9a-f]{24}',action_id): abort(400)
        try:
            # Treat the callback only as a hint. Fetch the authoritative event with
            # our existing Trello credentials; never forward caller-provided text.
            action=supplier_trello.Client(get_trello_config()).call('GET',f'actions/{action_id}')
            data=action.get('data',{})
            if action.get('id')!=action_id or action.get('type')!='commentCard' or data.get('board',{}).get('id')!=BOARD: return '',200
            when=datetime.fromisoformat(action['date'].replace('Z','+00:00'))
            if when<datetime.fromisoformat(cfg['enabled_at']): return '',200
            card=data.get('card',{})
            with db.engine.connect() as conn: mapping=conn.execute(select(deliveries).where(deliveries.c.card_id==card.get('id'))).mappings().first()
            if not mapping: return '',200
            match=re.fullmatch(r'bos:order:(\d+):item:(\d+)',mapping['key'])
            if not match: return '',200
            order=db.session.get(Order,int(match[1]))
            from supplier_sheets import allowed_order
            if not order or not allowed_order(order): return '',200
            member=action.get('memberCreator',{})
            author=member.get('fullName') or member.get('username') or 'Trello kullanıcısı'
            # Plain text only, suppress Chat mention/link syntax from user comments.
            safe=lambda value:str(value or '').replace('<','‹').replace('>','›')
            body=f'{safe(order.order_no)} · {safe(card.get("name"))}\n{safe(author)}: {safe(data.get("text"))[:12000]}\nKart: {mapping["card_url"]}'
            enqueue(app,db,'comment:'+action_id,body,actor=author)
            deliver(app,db,'comment:'+action_id)
            return '',200
        except Exception:
            return '',503 # Trello retries; durable pending entries also have an owner retry action.
