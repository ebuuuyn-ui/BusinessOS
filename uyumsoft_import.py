"""Read finalized Uyumsoft invoices and book once, with their original PDF."""
import base64
import hashlib
import io
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET
from flask import request, render_template, redirect, url_for, flash, send_file
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import MetaData, Table, Column, String, Integer, Text, LargeBinary, select, inspect, insert
from sqlalchemy.exc import IntegrityError
from uyumsoft_client import Client, UyumError, A, B, T

meta=MetaData()
imports=Table('businessos_uyumsoft_import',meta,
    Column('uuid',String(36),primary_key=True),Column('invoice_id',Integer,unique=True,nullable=False),
    Column('number',String(80),unique=True,nullable=False),Column('xml',LargeBinary,nullable=False),
    Column('pdf',LargeBinary,nullable=False),Column('created_at',Text,nullable=False))
FINAL_STATES={'SentToGib','Approved','WaitingForAprovement'}
N={'a':A,'b':B}

def parse_invoice(root,expected_uuid,seller_id,buyer_id):
    def txt(path,default=''):
        return root.findtext(path,default,namespaces=N).strip()
    def dec(element,path):
        try:
            value=Decimal(element.findtext(path,namespaces=N))
            if not value.is_finite():raise ValueError()
            return value
        except (InvalidOperation,TypeError,ValueError):raise UyumError('Faturadaki tutar okunamadı.') from None
    if txt('b:UUID').lower()!=expected_uuid.lower():raise UyumError('Fatura ETTN eşleşmedi.')
    for kind,expected in [('AccountingSupplierParty',seller_id),('AccountingCustomerParty',buyer_id)]:
        ids=root.findall('a:'+kind+'/a:Party/a:PartyIdentification/b:ID',N)
        if not any(x.get('schemeID') in ('VKN','TCKN') and x.text==expected for x in ids):
            raise UyumError('Faturanın gönderici veya alıcı kimliği BOS kaydıyla eşleşmedi.')
    if txt('b:InvoiceTypeCode')!='SATIS' or txt('b:DocumentCurrencyCode')!='TRY':raise UyumError('Yalnızca TL satış faturaları otomatik işlenebilir.')
    if root.findall('a:AllowanceCharge',N) or root.findall('.//a:WithholdingTaxTotal',N):raise UyumError('Fatura geneli iskonto/masraf veya tevkifat için manuel kontrol gerekiyor.')
    number=txt('b:ID')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,80}',number):raise UyumError('Resmî fatura numarası bulunamadı.')
    try:
        issued=date.fromisoformat(txt('b:IssueDate'))
        due=date.fromisoformat(txt('a:PaymentTerms/b:PaymentDueDate')) if txt('a:PaymentTerms/b:PaymentDueDate') else None
    except ValueError:raise UyumError('Fatura tarihi okunamadı.') from None
    lines=[]
    for line in root.findall('a:InvoiceLine',N):
        quantity=dec(line,'b:InvoicedQuantity');qnode=line.find('b:InvoicedQuantity',N)
        if quantity<=0 or quantity!=quantity.to_integral_value() or qnode.get('unitCode')!='C62':raise UyumError('Adet dışı veya kesirli miktar için manuel kontrol gerekiyor.')
        net=dec(line,'b:LineExtensionAmount');taxes=line.findall('a:TaxTotal/a:TaxSubtotal',N)
        if len(taxes)!=1 or taxes[0].findtext('a:TaxCategory/a:TaxScheme/b:TaxTypeCode',namespaces=N)!='0015':raise UyumError('Faturanın vergi yapısı otomatik işlenemiyor.')
        rate=dec(taxes[0],'b:Percent');vat=dec(taxes[0],'b:TaxAmount')
        # Net unit price is lossless only when BOS two-decimal storage can represent it.
        price=(net/quantity).quantize(Decimal('.01'))
        if rate not in (1,10,20) or net<=0 or price*quantity!=net or (net*rate/100).quantize(Decimal('.01'))!=vat:
            raise UyumError('Kalem tutarı BOS fiyat hassasiyetiyle uyuşmuyor; manuel kontrol gerekiyor.')
        name=line.findtext('a:Item/b:Name',namespaces=N) or ''
        if not name or len(name)>160:raise UyumError('Ürün adı okunamadı.')
        lines.append(dict(name=name,quantity=int(quantity),price=str(price),vat=str(rate),net=str(net),tax=str(vat)))
    if not lines:raise UyumError('Fatura kalemi bulunamadı.')
    net=dec(root,'a:LegalMonetaryTotal/b:TaxExclusiveAmount');tax=dec(root,'a:TaxTotal/b:TaxAmount')
    total=dec(root,'a:LegalMonetaryTotal/b:PayableAmount')
    if sum(Decimal(x['net']) for x in lines)!=net or sum(Decimal(x['tax']) for x in lines)!=tax or net+tax!=total:
        raise UyumError('Fatura toplamları kalemlerle uyuşmuyor; kayıt yapılmadı.')
    return dict(number=number,date=issued.isoformat(),due=due.isoformat() if due else '',lines=lines,
        net=str(net),tax=str(tax),total=str(total),notes='\n'.join(x.text or '' for x in root.findall('b:Note',N)))

def register_import(app,db,Invoice,Order,InvoiceItem,StockMovement,config,record,order_attempts,access,backup):
    signer=URLSafeTimedSerializer(app.secret_key,salt='uyumsoft-import-v1')
    def imported(invoice_id):
        if not inspect(db.engine).has_table(imports.name):return None
        return db.session.execute(select(imports.c.uuid).where(imports.c.invoice_id==invoice_id)).scalar()
    app.jinja_env.globals['uyumsoft_imported']=imported
    @app.before_request
    def protect_import():
        if request.method=='POST' and request.endpoint in ('edit_invoice','delete_invoice'):
            iid=(request.view_args or {}).get('invoice_id')
            if iid and imported(iid):
                flash('Uyumsoft’tan alınan resmî fatura değiştirilemez veya silinemez.','error')
                return redirect(url_for('invoice_detail',invoice_id=iid))
    @app.get('/faturalar/<int:invoice_id>/uyumsoft-pdf')
    def uyumsoft_invoice_pdf(invoice_id):
        access();db.get_or_404(Invoice,invoice_id)
        if not imported(invoice_id):return ('PDF bulunamadı',404)
        row=db.session.execute(select(imports).where(imports.c.invoice_id==invoice_id)).mappings().one()
        return send_file(io.BytesIO(row['pdf']),mimetype='application/pdf',as_attachment=True,download_name=row['number']+'.pdf')
    @app.route('/siparisler/<int:order_id>/uyumsoft-bos',methods=['GET','POST'])
    def uyumsoft_import(order_id):
        access();order=db.get_or_404(Order,order_id);error=None;document=None;token=''
        try:
            attempt=record(order_id,order_attempts)
            if not attempt:raise UyumError('Bu sipariş için Uyumsoft aktarımı bulunamadı.')
            if inspect(db.engine).has_table(imports.name):
                existing=db.session.execute(select(imports.c.invoice_id).where(imports.c.uuid==attempt['uuid'])).scalar()
                if existing:return redirect(url_for('invoice_detail',invoice_id=existing))
            if request.method=='POST':
                cfg=config()
                if not cfg:raise UyumError('Uyumsoft bağlantısı gerekli.')
                import json
                saved=json.loads(attempt['snapshot'])
                if cfg['seller']['tax_number']!=saved['seller']['tax_number']:raise UyumError('Gönderici hesabı aktarım kaydıyla eşleşmiyor.')
                client=Client(cfg);state=client.status(attempt['uuid'])
                if state not in FINAL_STATES:raise UyumError('Fatura henüz işlenebilir gönderim durumunda değil: '+state+'. Önce Uyumsoft portalında gönderimi tamamlayın.')
                root=client.outbox_invoice(attempt['uuid'])
                document=parse_invoice(root,attempt['uuid'],cfg['seller']['tax_number'],saved['buyer']['tax_number'])
                document['state']=state
                # Product matching never guesses from list position or fuzzy name.
                mapping={}
                for item in order.items:
                    if item.product_id:mapping.setdefault(item.product_name,set()).add(item.product_id)
                for line in document['lines']:
                    choices=mapping.get(line['name'],set())
                    if len(choices)!=1:raise UyumError('Stok kartı kesin eşleştirilemedi: '+line['name']+'. Manuel kontrol gerekiyor.')
                    line['product_id']=next(iter(choices))
                if order.customer.tax_number!=saved['buyer']['tax_number']:raise UyumError('Carinin kimlik bilgisi değişmiş; manuel kontrol gerekiyor.')
                from uyumsoft import digest
                fingerprint=digest(dict(document=document,uuid=attempt['uuid'],order=order.id,customer=order.customer_id))
                if request.form.get('action')=='save':
                    try:approved=signer.loads(request.form.get('token',''),max_age=900)
                    except BadSignature:raise UyumError('Önizleme süresi doldu; tekrar getirin.') from None
                    if approved!=fingerprint:raise UyumError('Fatura veya eşleştirme değişti; tekrar getirip kontrol edin.')
                    pdf=client.outbox_pdf(attempt['uuid'])
                    xml=ET.tostring(root,encoding='utf-8',xml_declaration=True)
                    backup()
                    meta.create_all(db.engine)
                    try:
                        # Lock the order and enforce unique UUID/number in the same transaction as stock writes.
                        db.session.query(Order).filter_by(id=order.id).with_for_update().one()
                        existing=db.session.execute(select(imports.c.invoice_id).where(imports.c.uuid==attempt['uuid'])).scalar()
                        if existing:
                            db.session.rollback();return redirect(url_for('invoice_detail',invoice_id=existing))
                        if Invoice.query.filter_by(invoice_no=document['number']).first() or Invoice.query.filter_by(order_id=order.id).first():
                            raise UyumError('Fatura numarası veya sipariş için BOS kaydı zaten var. Çift kayıt oluşturmamak için durduruldu.')
                        inv=Invoice(invoice_no=document['number'],invoice_type='Satış',customer_id=order.customer_id,order_id=order.id,
                            invoice_date=date.fromisoformat(document['date']),due_date=date.fromisoformat(document['due']) if document['due'] else None,
                            notes=(document['notes']+'\nUyumsoft ETTN: '+attempt['uuid']).strip())
                        db.session.add(inv);db.session.flush()
                        for line in document['lines']:
                            movement=StockMovement(product_id=line['product_id'],movement_type='Stok Çıkışı',quantity=line['quantity'],movement_date=inv.invoice_date,note='Satış faturası · '+inv.invoice_no)
                            db.session.add(movement);db.session.flush()
                            db.session.add(InvoiceItem(invoice_id=inv.id,product_id=line['product_id'],product_name=line['name'],quantity=line['quantity'],unit='Adet',
                                unit_price=Decimal(line['price']),discount_rate=0,vat_rate=Decimal(line['vat']),vat_included=False,stock_movement_id=movement.id))
                        db.session.execute(insert(imports).values(uuid=attempt['uuid'],invoice_id=inv.id,number=inv.invoice_no,xml=xml,pdf=pdf,created_at=datetime.now(timezone.utc).isoformat()))
                        db.session.commit()
                        flash('Uyumsoft faturası ve PDF’si BOS’a kaydedildi; cari ve stok etkisi işlendi.','success')
                        return redirect(url_for('invoice_detail',invoice_id=inv.id))
                    except IntegrityError:
                        db.session.rollback();raise UyumError('Fatura zaten kaydedilmiş olabilir. Çift kayıt oluşturulmadı.') from None
                token=signer.dumps(fingerprint)
        except UyumError as exc:
            db.session.rollback();error=str(exc)
        return render_template('uyumsoft_import.html',order=order,document=document,error=error,token=token)
