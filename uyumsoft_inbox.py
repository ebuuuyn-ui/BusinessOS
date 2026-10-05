"""Review incoming official documents, then explicitly map and book them once."""
import hashlib
import io
import json
import uuid as uuidlib
from datetime import date,datetime,timedelta,timezone
from decimal import Decimal,InvalidOperation
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
from flask import request,render_template,redirect,url_for,flash,send_file,abort
from itsdangerous import URLSafeTimedSerializer,BadSignature
from sqlalchemy import Table,Column,String,Integer,Text,select,insert,update,inspect
from sqlalchemy.exc import IntegrityError
from uyumsoft_client import Client,UyumError,A,B
from uyumsoft_import import imports,meta,N,FINAL_STATES

matches=Table('businessos_uyumsoft_supplier_match',meta,Column('key',String(64),primary_key=True),
    Column('supplier',String(11),nullable=False),Column('original',Text,nullable=False),Column('kind',String(20),nullable=False),
    Column('product_id',Integer),Column('label',Text))
details=Table('businessos_uyumsoft_inbox_detail',meta,Column('invoice_id',Integer,primary_key=True),Column('mapping',Text,nullable=False))

def parse_inbox(root,uid,buyer):
    def text(path):return (root.findtext(path,namespaces=N) or '').strip()
    def number(node,path):
        try:
            v=Decimal(node.findtext(path,namespaces=N))
            if not v.is_finite() or v<0 or v>Decimal("9999999999.99"):raise ValueError()
            return v
        except (ValueError,TypeError,InvalidOperation):raise UyumError('Faturanın sayısal alanları okunamadı.') from None
    if text('b:UUID').lower()!=uid.lower():raise UyumError('ETTN eşleşmedi.')
    ids=root.findall('a:AccountingCustomerParty/a:Party/a:PartyIdentification/b:ID',N)
    if not any(x.get('schemeID') in ('TCKN','VKN') and x.text==buyer for x in ids):raise UyumError('Fatura işletmenizin kimlik numarasına ait değil.')
    supplier=root.find('a:AccountingSupplierParty/a:Party',N)
    if supplier is None:raise UyumError('Tedarikçi bilgisi eksik.')
    sid=next((x.text for x in supplier.findall('a:PartyIdentification/b:ID',N) if x.get('schemeID') in ('TCKN','VKN')),None)
    if not sid or not sid.isdigit() or len(sid) not in (10,11):raise UyumError('Tedarikçi kimlik numarası eksik veya geçersiz.')
    name=supplier.findtext('a:PartyName/b:Name',namespaces=N) or ' '.join(x.text or '' for x in supplier.findall('a:Person/*',N))
    if text('b:DocumentCurrencyCode')!='TRY' or text('b:InvoiceTypeCode')!='SATIS':raise UyumError('Bu ekranda yalnızca TL normal alış faturaları işlenebilir; iade, istisna ve tevkifat manuel incelenmelidir.')
    if root.findall('a:AllowanceCharge',N) or root.findall('.//a:WithholdingTaxTotal',N):raise UyumError('Fatura geneli iskonto/masraf veya tevkifat manuel incelenmelidir.')
    if len(root.findall('a:TaxTotal',N))!=1:raise UyumError('Birden fazla vergi toplamı manuel incelenmelidir.')
    no=text('b:ID')
    if not no or len(no)>80:raise UyumError('Fatura numarası geçersiz.')
    try:
        issued=date.fromisoformat(text('b:IssueDate')).isoformat()
        due=date.fromisoformat(text('a:PaymentTerms/b:PaymentDueDate')).isoformat() if text('a:PaymentTerms/b:PaymentDueDate') else ''
    except ValueError:raise UyumError('Fatura tarihi geçersiz.') from None
    lines=[]
    for line in root.findall('a:InvoiceLine',N):
        qty=number(line,'b:InvoicedQuantity');net=number(line,'b:LineExtensionAmount')
        if qty>2147483647 or net!=net.quantize(Decimal('.01')):raise UyumError('Kalem miktarı veya tutar hassasiyeti desteklenmiyor.')
        taxes=line.findall('a:TaxTotal/a:TaxSubtotal',N)
        if qty<=0 or len(taxes)!=1 or taxes[0].findtext('a:TaxCategory/a:TaxScheme/b:TaxTypeCode',namespaces=N)!='0015':raise UyumError('Kalemin miktar veya vergi yapısı manuel incelenmelidir.')
        rate=number(taxes[0],'b:Percent');tax=number(taxes[0],'b:TaxAmount')
        if rate not in (0,1,8,10,18,20) or (net*rate/100).quantize(Decimal('.01'))!=tax:raise UyumError('Kalem KDV tutarı uyuşmuyor.')
        unit=line.find('b:InvoicedQuantity',N).get('unitCode','')
        label=line.findtext('a:Item/b:Name',namespaces=N) or ''
        if not label:raise UyumError('Ürün adı eksik.')
        lines.append(dict(name=label,quantity=str(qty),unit=unit,net=str(net),tax=str(tax),rate=str(rate)))
    net=number(root,'a:LegalMonetaryTotal/b:TaxExclusiveAmount');tax=number(root,'a:TaxTotal/b:TaxAmount');total=number(root,'a:LegalMonetaryTotal/b:PayableAmount')
    if not lines or sum(Decimal(x['net']) for x in lines)!=net or sum(Decimal(x['tax']) for x in lines)!=tax or net+tax!=total:
        raise UyumError('Fatura toplamları kalemlerle uyuşmuyor; otomatik kayıt yapılamaz.')
    return dict(uuid=uid,number=no,date=issued,due=due,supplier_id=sid,supplier=name,lines=lines,net=str(net),tax=str(tax),total=str(total))

def register_inbox(app,db,Invoice,Order,InvoiceItem,StockMovement,Customer,Product,config,access,backup):
    signer=URLSafeTimedSerializer(app.secret_key,salt='uyumsoft-inbox-review-v1')
    def ready(table):return inspect(db.engine).has_table(table.name)
    def existing(uid):
        return db.session.execute(select(imports.c.invoice_id).where(imports.c.uuid==uid)).scalar() if ready(imports) else None
    def connection():
        cfg=config()
        if not cfg:raise UyumError('Önce Uyumsoft bağlantısını tamamlayın.')
        return cfg,Client(cfg)
    def key(supplier,label):
        from app import normalize_search_text
        return hashlib.sha256((supplier+'|'+normalize_search_text(label)).encode()).hexdigest()
    @app.get('/faturalar/uyumsoft-gelen')
    def uyumsoft_inbox():
        access();today=datetime.now(ZoneInfo('Europe/Istanbul')).date();error=None;rows=[];pages=0;total=0
        start=request.args.get('start',(today-timedelta(days=30)).isoformat());end=request.args.get('end',today.isoformat());page=max(0,request.args.get('page',0,type=int))
        if request.args.get('fetch'):
            try:
                if date.fromisoformat(start)>date.fromisoformat(end):raise ValueError()
                cfg,client=connection();rows,pages,total=client.inbox_list(start,end,page)
                for row in rows:
                    uid=row.get('InvoiceId','')
                    try:uuidlib.UUID(uid)
                    except ValueError:raise UyumError('Servisten geçersiz ETTN geldi.') from None
                    row['bos_id']=existing(uid)
                    numbered=Invoice.query.filter_by(invoice_no=row.get('DocumentId','')).first()
                    row['number_exists']=bool(numbered and not row['bos_id'])
                    if numbered and not row['bos_id']:row['bos_id']=numbered.id
            except ValueError:error='Tarih aralığını kontrol edin.'
            except UyumError as exc:error=str(exc)
        return render_template('uyumsoft_inbox.html',rows=rows,error=error,start=start,end=end,page=page,pages=pages,total=total)
    @app.route('/faturalar/uyumsoft-gelen/<uuid:uid>',methods=['GET','POST'])
    def uyumsoft_inbox_review(uid):
        access();uid=str(uid);saved=existing(uid)
        if saved:return redirect(url_for('invoice_detail',invoice_id=saved))
        error=None;doc=None;token='';choices=[];customer_ref=request.form.get('customer_ref','');order_ref=request.form.get('order_ref','')
        customers=Customer.query.order_by(Customer.name).all();products=Product.query.filter_by(active=True).order_by(Product.name).all()
        product_refs={str(x.id)+' · '+(x.code or 'Kodsuz')+' · '+x.name:x for x in products}
        customer_refs={str(x.id)+' · '+x.name:x for x in customers}
        try:
            cfg,client=connection();root=client.inbox_invoice(uid);doc=parse_inbox(root,uid,cfg['seller']['tax_number'])
            xml=ET.tostring(root,encoding='utf-8',xml_declaration=True);fingerprint=hashlib.sha256(xml).hexdigest()
            if request.method=='GET':
                found=[ref for ref,c in customer_refs.items() if c.tax_number==doc['supplier_id']]
                if len(found)==1:customer_ref=found[0]
            for i,line in enumerate(doc['lines']):
                suggestion=db.session.execute(select(matches).where(matches.c.key==key(doc['supplier_id'],line['name']))).mappings().first() if ready(matches) else None
                product_ref=next((r for r,p in product_refs.items() if suggestion and p.id==suggestion['product_id']),'')
                choices.append(dict(kind=request.form.get(f'kind_{i}',suggestion['kind'] if suggestion else 'stock'),
                    product=request.form.get(f'product_{i}',product_ref),label=request.form.get(f'label_{i}',suggestion['label'] if suggestion and suggestion['label'] else line['name'][:160]),
                    remember=request.form.get(f'remember_{i}')=='yes' if request.method=='POST' else True))
            token=signer.dumps(dict(uuid=uid,hash=fingerprint,buyer=cfg['seller']['tax_number']))
            if request.method=='POST':
                try:approved=signer.loads(request.form.get('token',''),max_age=1800)
                except BadSignature:raise UyumError('Önizleme süresi doldu. Güncel bilgileri tekrar kontrol edin.') from None
                if approved!=dict(uuid=uid,hash=fingerprint,buyer=cfg['seller']['tax_number']):raise UyumError('Uyumsoft faturası değişmiş. Güncel bilgileri kontrol edip yeniden onaylayın.')
                if request.form.get('confirm')!='yes':raise UyumError('Kaydetmeden önce kontrol onayını işaretleyin.')
                customer=customer_refs.get(customer_ref)
                if not customer or customer.tax_number!=doc['supplier_id']:raise UyumError('Tedarikçinin VKN/TCKN bilgisiyle eşleşen BOS carisini seçin; gerekirse cari kartını güncelleyin.')
                order=Order.query.filter_by(order_no=order_ref).first() if order_ref else None
                if order_ref and (not order or order.order_type!='Satın Alma' or order.customer_id!=customer.id):raise UyumError('Satın alma siparişi seçilen tedarikçiye ait olmalı.')
                prepared=[]
                for line,choice in zip(doc['lines'],choices):
                    if choice['kind']=='stock':
                        product=product_refs.get(choice['product']);qty=Decimal(line['quantity'])
                        if not product or line['unit']!='C62' or qty!=int(qty) or (product.unit or '').casefold() not in ('adet','ad','c62'):raise UyumError('Her ürün için adet birimli aktif stok kartını seçin. Diğer birimler manuel incelenmelidir.')
                        price=(Decimal(line['net'])/qty).quantize(Decimal('.01'))
                        if price*qty!=Decimal(line['net']):raise UyumError('Net birim fiyat iki ondalıkla tutarı karşılamıyor; manuel kontrol gerekiyor.')
                        prepared.append(dict(product=product,quantity=int(qty),price=price,name=product.name))
                    elif choice['kind']=='expense':
                        from invoice_ssh import allows_nonstock_rows
                        if not allows_nonstock_rows(db.session.connection()):raise UyumError('Stoksuz fatura satırı desteği henüz hazır değil.')
                        label=choice['label'].strip()
                        if not label or len(label)>160:raise UyumError('Gider açıklaması 1–160 karakter olmalıdır.')
                        prepared.append(dict(product=None,quantity=1,price=Decimal(line['net']),name=label))
                    else:raise UyumError('Geçersiz kalem türü.')
                if sum(Decimal(x['net'])*Decimal(x['rate'])/100 for x in doc['lines'])!=Decimal(doc['tax']):
                    raise UyumError('KDV yuvarlama farkı var; BOS toplamıyla birebir eşleşmediği için manuel kontrol gerekiyor.')
                state=client.inbox_status(uid)
                if state not in FINAL_STATES:raise UyumError('Bu durumdaki fatura işlenemez: '+state)
                pdf=client.inbox_pdf(uid);backup();meta.create_all(db.engine)
                try:
                    db.session.query(Customer).filter_by(id=customer.id).with_for_update().one()
                    saved=existing(uid)
                    if saved:
                        db.session.rollback();return redirect(url_for('invoice_detail',invoice_id=saved))
                    if Invoice.query.filter_by(invoice_no=doc['number']).first():raise UyumError('Bu fatura numarası BOS’ta zaten var; ikinci kayıt oluşturulmadı.')
                    inv=Invoice(invoice_no=doc['number'],invoice_type='Satın Alma',customer_id=customer.id,order_id=order.id if order else None,
                        invoice_date=date.fromisoformat(doc['date']),due_date=date.fromisoformat(doc['due']) if doc['due'] else None,notes='Uyumsoft gelen fatura ETTN: '+uid)
                    db.session.add(inv);db.session.flush();mapping=[]
                    for line,choice,row in zip(doc['lines'],choices,prepared):
                        movement=None
                        if row['product']:
                            movement=StockMovement(product_id=row['product'].id,movement_type='Stok Girişi',quantity=row['quantity'],movement_date=inv.invoice_date,note='Satın Alma faturası · '+inv.invoice_no)
                            db.session.add(movement);db.session.flush()
                        db.session.add(InvoiceItem(invoice_id=inv.id,product_id=row['product'].id if row['product'] else None,product_name=row['name'],unit='Adet',quantity=row['quantity'],unit_price=row['price'],vat_rate=Decimal(line['rate']),discount_rate=0,vat_included=False,stock_movement_id=movement.id if movement else None))
                        mapping.append(dict(original=line,kind=choice['kind'],bos_name=row['name'],product_id=row['product'].id if row['product'] else None))
                        if choice['remember']:
                            k=key(doc['supplier_id'],line['name']);values=dict(supplier=doc['supplier_id'],original=line['name'],kind=choice['kind'],product_id=row['product'].id if row['product'] else None,label=row['name'])
                            changed=db.session.execute(update(matches).where(matches.c.key==k).values(**values))
                            if changed.rowcount==0:db.session.execute(insert(matches).values(key=k,**values))
                    db.session.execute(insert(imports).values(uuid=uid,invoice_id=inv.id,number=inv.invoice_no,xml=xml,pdf=pdf,created_at=datetime.now(timezone.utc).isoformat()))
                    db.session.execute(insert(details).values(invoice_id=inv.id,mapping=json.dumps(mapping,ensure_ascii=False)))
                    db.session.commit();flash('Gelen fatura ve PDF kaydedildi. Ürünlere stok girişi işlendi; gider satırları stoğu etkilemedi.','success')
                    return redirect(url_for('invoice_detail',invoice_id=inv.id))
                except IntegrityError:
                    db.session.rollback();raise UyumError('Kayıt çakışması oluştu; ikinci fatura veya stok hareketi oluşturulmadı.') from None
        except UyumError as exc:db.session.rollback();error=str(exc)
        return render_template('uyumsoft_inbox_review.html',doc=doc,error=error,token=token,choices=choices,customer_refs=customer_refs,product_refs=product_refs,customer_ref=customer_ref,order_ref=order_ref,uid=uid)
    @app.get('/faturalar/uyumsoft-gelen/<uuid:uid>/pdf')
    def uyumsoft_inbox_pdf(uid):
        access();uid=str(uid)
        try:
            cfg,client=connection();root=client.inbox_invoice(uid)
            # Verify recipient even for documents the accounting parser does not support.
            if not any(x.get('schemeID') in ('VKN','TCKN') and x.text==cfg['seller']['tax_number'] for x in root.findall('a:AccountingCustomerParty/a:Party/a:PartyIdentification/b:ID',N)):abort(403)
            return send_file(io.BytesIO(client.inbox_pdf(uid)),mimetype='application/pdf',as_attachment=True,download_name=uid+'.pdf')
        except UyumError as exc:return str(exc),400
