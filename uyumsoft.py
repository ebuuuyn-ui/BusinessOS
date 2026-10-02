"""Encrypted company connection and idempotent, explicitly confirmed draft export."""
import base64
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
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
DEFAULT_SELLER={'name':'Abika Mobilya-Ebubekir Uyan','tax_number':'8970492798','tax_office':'Ümraniye',
 'address':'ATATÜRK MAH. ÇAVUŞBAŞI CAD. ELÇİNGÜL AKTAR NO: 17 A İÇ KAPI NO: 1','district':'Ümraniye','city':'İstanbul'}
STATUS={'sending':'Sonuç bekleniyor','unknown':'Sonuç belirsiz — yeniden gönderilmedi','Draft':'Uyumsoft’ta taslak',
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


def register_uyumsoft(app,db,Invoice):
    cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256(('bos-uyumsoft-v1:'+app.secret_key).encode()).digest()))
    signer=URLSafeTimedSerializer(app.secret_key,salt='uyumsoft-preview-v1')
    def config():
        if not inspect(db.engine).has_table(connection.name):return {}
        with db.engine.connect() as conn:value=conn.execute(select(connection.c.config).where(connection.c.id==1)).scalar()
        if not value:return {}
        try:return json.loads(cipher.decrypt(value.encode()))
        except (InvalidToken,ValueError):raise UyumError('Uyumsoft bağlantı ayarları okunamadı; yönetici bağlantıyı yenilemeli.') from None
    def record(invoice_id):
        if not inspect(db.engine).has_table(attempts.name):return None
        with db.engine.connect() as conn:return conn.execute(select(attempts).where(attempts.c.invoice_id==invoice_id)).mappings().first()
    def access():
        if not app.config.get('WEB_AUTH_ENABLED'):abort(404)
    def owner():
        access()
        if not getattr(g,'web_is_owner',False):abort(403)
    def update_record(invoice_id,**values):
        with db.engine.begin() as conn:conn.execute(update(attempts).where(attempts.c.invoice_id==invoice_id).values(**values))

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
        cfg={};error=None;data=None;token='';current=record(invoice_id)
        try:
            cfg=config()
            data=snapshot(invoice,cfg.get('seller',DEFAULT_SELLER),request.form.get('district',''))
            data['alias']=request.form.get('alias','').strip()[:300]
            if request.method=='POST':
                action=request.form.get('action')
                if not cfg:raise UyumError('Önce yönetici Uyumsoft bağlantısını tamamlamalı.')
                if current:
                    if action=='status':
                        if current['account']!=account_id(cfg):raise UyumError('Aktarımın yapıldığı Uyumsoft hesabıyla bağlanın.')
                        state=Client(cfg).status(current['uuid']);update_record(invoice_id,state=state,message='')
                        flash('Uyumsoft durumu güncellendi.','success')
                    else:flash('Bu fatura için aktarım kaydı zaten var. İkinci taslak oluşturulmadı.','error')
                    return redirect(url_for('uyumsoft_preview',invoice_id=invoice_id))
                validate(data)
                if action=='preview':
                    invoice_xml(data,str(uuid.uuid4()),True) # Check arithmetic before confirmation.
                    token=signer.dumps({'hash':digest(data),'account':account_id(cfg)})
                elif action=='send':
                    try:approved=signer.loads(request.form.get('preview',''),max_age=900)
                    except BadSignature:raise UyumError('Önizleme süresi doldu. Faturayı tekrar kontrol edin.') from None
                    if approved!={'hash':digest(data),'account':account_id(cfg)}:raise UyumError('Fatura veya bağlantı önizlemeden sonra değişti; yeniden önizleyin.')
                    if request.form.get('confirm')!='yes':raise UyumError('Taslak aktarım onayını işaretleyin.')
                    client=Client(cfg);einvoice=client.is_einvoice(data['buyer']['tax_number'])
                    if einvoice:data['alias']=client.resolve_alias(data['buyer']['tax_number'],data['alias'])
                    draft_id=str(uuid.uuid4());invoice_xml(data,draft_id,einvoice)
                    meta.create_all(db.engine)
                    try:
                        with db.engine.begin() as conn:conn.execute(insert(attempts).values(invoice_id=invoice_id,uuid=draft_id,state='sending',snapshot=canonical(data),account=account_id(cfg),actor=getattr(g,'web_username',''),created_at=datetime.now(timezone.utc).isoformat()))
                    except IntegrityError:
                        flash('Aktarım zaten başlatıldı. Durumu kontrol edin.','error');return redirect(url_for('uyumsoft_preview',invoice_id=invoice_id))
                    try:
                        result=client.save_draft(data,draft_id,einvoice)
                        update_record(invoice_id,state='Draft',number=result['number'],message='')
                        flash('Fatura Uyumsoft’a taslak olarak aktarıldı. Son kontrol ve gönderim Uyumsoft portalında yapılır.','success')
                    except UyumError as exc:
                        update_record(invoice_id,state='unknown',message=str(exc))
                        flash(str(exc),'error')
                    return redirect(url_for('uyumsoft_preview',invoice_id=invoice_id))
                else:abort(400)
        except UyumError as exc:error=str(exc)
        saved=json.loads(current['snapshot']) if current else None
        return render_template('uyumsoft_preview.html',invoice=invoice,data=saved or data,connected=bool(cfg),error=error,preview=token,current=current,status_labels=STATUS)
