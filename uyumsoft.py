"""Encrypted company connection and idempotent, explicitly confirmed draft export."""
import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo
from calendar import monthrange
from types import SimpleNamespace
from flask import abort, g, request, render_template, redirect, url_for, flash
from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import MetaData, Table, Column, Integer, Text, String, select, insert, update, inspect
from sqlalchemy.exc import IntegrityError
from uyumsoft_client import Client, UyumError, invoice_xml

meta=MetaData()
connection=Table('businessos_uyumsoft_connection',meta,Column('id',Integer,primary_key=True),Column('config',Text,nullable=False))
attempts=Table('businessos_uyumsoft_draft',meta,Column('invoice_id',Integer,primary_key=True),
    Column('uuid',String(36),unique=True,nullable=False),Column('state',String(40),nullable=False),
    Column('number',Text),Column('snapshot',Text,nullable=False),Column('account',String(64),nullable=False),
    Column('actor',Text),Column('created_at',Text),Column('message',Text))
order_attempts=Table('businessos_uyumsoft_order_draft',meta,Column('order_id',Integer,primary_key=True),
    Column('uuid',String(36),unique=True,nullable=False),Column('state',String(40),nullable=False),
    Column('number',Text),Column('snapshot',Text,nullable=False),Column('account',String(64),nullable=False),
    Column('actor',Text),Column('created_at',Text),Column('message',Text))

rejected_history=Table('businessos_uyumsoft_rejected_history',meta,
    Column('uuid',String(36),primary_key=True),Column('source_table',Text,nullable=False),
    Column('record',Text,nullable=False),Column('archived_at',Text,nullable=False))

def sender_rejected(record):
    return bool(record and record['state'] in ('unknown','sender_rejected') and not record['number']
        and 'AccountingSupplierParty' in (record['message'] or '')
        and 'Vergi Kimlik Numarası' in (record['message'] or '')
        and 'farklıdır' in (record['message'] or ''))

def month_after(value):
    year=value.year + (value.month==12);month=value.month % 12 + 1
    return date(year,month,min(value.day,monthrange(year,month)[1]))

DEFAULT_SELLER={'name':'Abika Mobilya-Ebubekir Uyan','tax_number':'','tax_office':'Ümraniye',
 'address':'ATATÜRK MAH. ÇAVUŞBAŞI CAD. ELÇİNGÜL AKTAR NO: 17 A İÇ KAPI NO: 1','district':'Ümraniye','city':'İstanbul'}
STATUS={'sender_rejected':'Gönderici kimliği uyuşmadığı için reddedildi','sending':'Sonuç bekleniyor','unknown':'Sonuç belirsiz — yeniden gönderilmedi','Draft':'Uyumsoft’ta taslak',
 'SentToGib':'GİB’e gönderildi','Approved':'Başarılı','Queued':'Gönderim kuyruğunda','Processing':'İşleniyor',
 'WaitingForAprovement':'Alıcı yanıtı bekleniyor','Declined':'Reddedildi','Canceled':'İptal edildi','Error':'Uyumsoft hatası',
 'NotSend':'Gönderilmedi','NotPrepared':'Hazırlanmadı','Return':'İade','EArchivedCanceled':'E-Arşiv iptal edildi'}

def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value):return hashlib.sha256(canonical(value).encode()).hexdigest()
def account_id(cfg):return digest([cfg['environment'],cfg['username'],cfg['seller']['tax_number']])

def snapshot(invoice,seller,district=''):
    c=invoice.customer
    return {'invoice_id':invoice.id,'reference':invoice.invoice_no,'date':invoice.invoice_date.isoformat(),
      'due_date':invoice.due_date.isoformat() if invoice.due_date else '',
      'seller':seller,'buyer':{'name':c.name,'tax_number':c.tax_number or '', 'tax_office':c.tax_office or '',
       'address':c.address or '', 'city':c.city or '', 'district':district.strip()},
      'notes':invoice.notes or '', 'net':str(invoice.net_amount),'vat':str(invoice.vat_amount),
      'items':[{'name':i.product_name,'quantity':i.quantity,'price':str(i.unit_price),'vat':str(i.vat_rate),
                'discount':str(i.discount_rate),'included':bool(i.vat_included),'unit':i.unit} for i in invoice.items]}

def validate(data):
    from decimal import Decimal
    for label,person in [('Gönderici',data['seller']),('Alıcı',data['buyer'])]:
        if any(not str(person.get(key,'')).strip() for key in ('name','tax_number','tax_office','address','city','district')):
            raise UyumError(label+' firma, vergi ve adres bilgileri eksik. İlçe alanını da doldurun.')
        if not re.fullmatch(r'(?:[0-9]{10}|[0-9]{11})',person['tax_number']):raise UyumError(label+' vergi numarası 10 veya 11 rakam olmalıdır.')
    if not data['items']:raise UyumError('Fatura kalemi bulunmuyor.')
    for item in data['items']:
        if item['unit'].strip().casefold() not in ('adet','ad','c62'):raise UyumError('İlk sürüm yalnız adet birimli ürünleri destekliyor.')
        if item['quantity']<=0 or Decimal(item['price'])<=0 or not 0<=Decimal(item['discount'])<100:
            raise UyumError('İlk sürüm pozitif miktar/tutar ve %100’den küçük iskonto gerektirir.')
        if Decimal(item['vat']) not in (Decimal(1),Decimal(10),Decimal(20)):
            raise UyumError('İlk sürüm %1, %10 ve %20 KDV’li normal satışlar içindir; istisna ve tevkifat desteklenmiyor.')


def register_uyumsoft(app,db,Invoice,Order):
    cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256(('bos-uyumsoft-v1:'+app.secret_key).encode()).digest()))
    signer=URLSafeTimedSerializer(app.secret_key,salt='uyumsoft-preview-v1')
    def config():
        if not inspect(db.engine).has_table(connection.name):return {}
        with db.engine.connect() as conn:value=conn.execute(select(connection.c.config).where(connection.c.id==1)).scalar()
        if not value:return {}
        try:return json.loads(cipher.decrypt(value.encode()))
        except (InvalidToken,ValueError):raise UyumError('Uyumsoft bağlantı ayarları okunamadı; yönetici bağlantıyı yenilemeli.') from None
    def record(source_id,table=attempts):
        if not inspect(db.engine).has_table(table.name):return None
        key=table.c.invoice_id if table is attempts else table.c.order_id
        with db.engine.connect() as conn:return conn.execute(select(table).where(key==source_id)).mappings().first()
    def access():
        if not app.config.get('WEB_AUTH_ENABLED'):abort(404)
    def owner():
        access()
        if not getattr(g,'web_is_owner',False):abort(403)
    def update_record(source_id,table=attempts,**values):
        key=table.c.invoice_id if table is attempts else table.c.order_id
        with db.engine.begin() as conn:conn.execute(update(table).where(key==source_id).values(**values))

    @app.before_request
    def protect_exported_invoice():
        if request.method=='POST' and request.endpoint in ('edit_invoice','delete_invoice') and app.config.get('WEB_AUTH_ENABLED'):
            invoice_id=(request.view_args or {}).get('invoice_id')
            if invoice_id and record(invoice_id):
                flash('Uyumsoft aktarım kaydı bulunan fatura değiştirilemez veya silinemez. Önce Uyumsoft kaydını kontrol edin.','error')
                return redirect(url_for('uyumsoft_preview',invoice_id=invoice_id))

    @app.route('/yonetim/uyumsoft',methods=['GET','POST'])
    def uyumsoft_settings():
        owner();error=None;message=None;cfg={}
        try:
            cfg=config()
            if request.method=='POST':
                if request.form.get('action')=='test':
                    Client({'environment':'test','username':'Uyumsoft','password':'Uyumsoft'}).is_einvoice(DEFAULT_SELLER['tax_number'])
                    message='Uyumsoft test servisine bağlantı ve kullanıcı doğrulaması başarılı. Fatura gönderilmedi.'
                else:
                    seller={key:request.form.get(key,'').strip()[:300] for key in DEFAULT_SELLER}
                    if any(not v for v in seller.values()) or not re.fullmatch(r'[0-9]{10,11}',seller['tax_number']):raise UyumError('Gönderici bilgilerini eksiksiz doldurun.')
                    username=request.form.get('username','').strip()
                    password=request.form.get('password','') or (cfg.get('password','') if username==cfg.get('username') else '')
                    if not username or not password:raise UyumError('Servis kullanıcı adı ve şifresi gereklidir.')
                    candidate={'environment':'live','username':username,'password':password,'seller':seller}
                    Client(candidate).is_einvoice(seller['tax_number'])
                    meta.create_all(db.engine)
                    encrypted=cipher.encrypt(canonical(candidate).encode()).decode()
                    with db.engine.begin() as conn:
                        conn.execute(connection.delete().where(connection.c.id==1));conn.execute(insert(connection).values(id=1,config=encrypted))
                    flash('Uyumsoft canlı bağlantısı doğrulandı ve kaydedildi. Henüz fatura aktarılmadı.','success')
                    return redirect(url_for('uyumsoft_settings'))
        except UyumError as exc:error=str(exc)
        return render_template('uyumsoft_settings.html',seller=cfg.get('seller',DEFAULT_SELLER),username=cfg.get('username',''),connected=bool(cfg),error=error,message=message)

    @app.route('/faturalar/<int:invoice_id>/uyumsoft',methods=['GET','POST'])
    def uyumsoft_preview(invoice_id):
        access();invoice=db.get_or_404(Invoice,invoice_id)
        if invoice.invoice_type!='Satış':abort(403)
        if invoice.order_id and record(invoice.order_id,order_attempts):
            return redirect(url_for('uyumsoft_order_preview',order_id=invoice.order_id))
        return draft_page(invoice)

    @app.route('/siparisler/<int:order_id>/uyumsoft',methods=['GET','POST'])
    def uyumsoft_order_preview(order_id):
        access();order=db.get_or_404(Order,order_id)
        if order.order_type!='Satış':abort(403)
        return draft_page(order,True)

    def draft_page(source,order_mode=False):
        source_id=source.id;table=order_attempts if order_mode else attempts
        endpoint='uyumsoft_order_preview' if order_mode else 'uyumsoft_preview'
        route_args={'order_id' if order_mode else 'invoice_id':source_id}
        cfg={};error=None;data=None;token='';current=record(source_id,table)
        today=datetime.now(ZoneInfo('Europe/Istanbul')).date()
        invoice=source
        if order_mode:
            invoice=SimpleNamespace(id=source.id,invoice_no=source.order_no,customer=source.customer,
                invoice_date=today,due_date=month_after(today),notes=source.notes,items=source.items,
                net_amount=source.net_amount,vat_amount=source.vat_amount)
        try:
            cfg=config()
            data=snapshot(invoice,cfg.get('seller',DEFAULT_SELLER),request.form.get('district',''))
            data['alias']=request.form.get('alias','').strip()[:300]
            if order_mode:
                data['order_id']=data.pop('invoice_id')
                if request.method=='POST' and not current:
                    for field,key in [('invoice_date','date'),('due_date','due_date')]:
                        raw=request.form.get(field,data[key])
                        try:data[key]=date.fromisoformat(raw).isoformat()
                        except ValueError:raise UyumError('Fatura ve vade tarihlerini kontrol edin.') from None
                    if data['due_date']<data['date']:raise UyumError('Vade tarihi fatura tarihinden önce olamaz.')
                    data['notes']=request.form.get('notes',data['notes'])[:2000]
                    for key in ('name','tax_number','tax_office','address','city'):
                        data['buyer'][key]=request.form.get('buyer_'+key,data['buyer'][key]).strip()[:300]
            if request.method=='POST':
                action='edit' if request.form.get('edit')=='yes' else request.form.get('action')
                if current:
                    if action=='reprepare':
                        owner()
                        if not sender_rejected(current):raise UyumError('Yalnızca kesin gönderici kimliği reddi yeniden hazırlanabilir.')
                        old=json.loads(current['snapshot'])
                        if not cfg or cfg['seller']['tax_number']==old['seller']['tax_number']:
                            raise UyumError('Önce bağlantıdaki gönderici kimlik numarasını düzeltin.')
                        meta.create_all(db.engine)
                        key=table.c.order_id if order_mode else table.c.invoice_id
                        with db.engine.begin() as conn:
                            removed=conn.execute(table.delete().where(key==source_id,table.c.uuid==current['uuid'],table.c.state==current['state']))
                            if removed.rowcount==1:
                                conn.execute(insert(rejected_history).values(uuid=current['uuid'],source_table=table.name,
                                    record=canonical(dict(current)),archived_at=datetime.now(timezone.utc).isoformat()))
                        flash('Reddedilen deneme geçmişte saklandı. Güncel göndericiyle yeniden önizleyebilirsiniz. Fatura gönderilmedi.','success')
                    elif action=='status':
                        if not cfg or current['account']!=account_id(cfg):raise UyumError('Aktarımın yapıldığı Uyumsoft hesabıyla bağlanın.')
                        state=Client(cfg).status(current['uuid']);update_record(source_id,table,state=state,message='')
                        flash('Uyumsoft durumu güncellendi.','success')
                    else:flash('Bu kayıt için aktarım zaten başlatılmış. İkinci taslak oluşturulmadı.','error')
                    return redirect(url_for(endpoint,**route_args))
                if order_mode and source.invoices:
                    raise UyumError('Bu siparişe bağlı BOS faturası var. Tekrar tam sipariş faturası oluşturmadan mevcut faturayı kontrol edin.')
                if order_mode and source.status=='İptal Edildi':raise UyumError('İptal edilmiş sipariş aktarılamaz.')
                if action=='edit':
                    return render_template('uyumsoft_preview.html',invoice=invoice,order_mode=order_mode,data=data,connected=bool(cfg),error=None,preview='',current=None,status_labels=STATUS)
                validate(data)
                identity=account_id(cfg) if cfg else ''
                if action=='preview':
                    invoice_xml(data,str(uuid.uuid4()),True)
                    token=signer.dumps({'hash':digest(data),'account':identity})
                elif action=='send':
                    if not cfg:raise UyumError('Önce yönetici Uyumsoft bağlantısını tamamlamalı.')
                    try:approved=signer.loads(request.form.get('preview',''),max_age=900)
                    except BadSignature:raise UyumError('Önizleme süresi doldu. Faturayı tekrar kontrol edin.') from None
                    if approved!={'hash':digest(data),'account':identity}:raise UyumError('Sipariş, fatura veya bağlantı önizlemeden sonra değişti; yeniden önizleyin.')
                    if request.form.get('confirm')!='yes':raise UyumError('Taslak aktarım onayını işaretleyin.')
                    client=Client(cfg);einvoice=client.is_einvoice(data['buyer']['tax_number'])
                    if einvoice:data['alias']=client.resolve_alias(data['buyer']['tax_number'],data['alias'])
                    draft_id=str(uuid.uuid4());invoice_xml(data,draft_id,einvoice)
                    meta.create_all(db.engine)
                    try:
                        with db.engine.begin() as conn:conn.execute(insert(table).values(**route_args,uuid=draft_id,state='sending',snapshot=canonical(data),account=identity,actor=getattr(g,'web_username',''),created_at=datetime.now(timezone.utc).isoformat()))
                    except IntegrityError:
                        flash('Aktarım zaten başlatıldı. Durumu kontrol edin.','error');return redirect(url_for(endpoint,**route_args))
                    try:
                        result=client.save_draft(data,draft_id,einvoice)
                        update_record(source_id,table,state='Draft',number=result['number'],message='')
                        flash('Uyumsoft taslağı oluşturuldu. Son kontrol ve gönderim Uyumsoft portalında yapılır.','success')
                    except UyumError as exc:
                        update_record(source_id,table,state='sender_rejected' if sender_rejected({'state':'unknown','number':'','message':str(exc)}) else 'unknown',message=str(exc));flash(str(exc),'error')
                    return redirect(url_for(endpoint,**route_args))
                else:abort(400)
        except UyumError as exc:error=str(exc)
        saved=json.loads(current['snapshot']) if current else None
        return render_template('uyumsoft_preview.html',invoice=invoice,order_mode=order_mode,data=saved or data,
            connected=bool(cfg),error=error,preview=token,current=current,status_labels=STATUS,sender_rejected=sender_rejected(current))
