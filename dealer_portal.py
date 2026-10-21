"""Owner-only dealer inbox. Pulling requests never creates sales or ledger entries.

Disabled without explicit environment configuration. DDL is an explicit owner POST,
never an import, login, GET, or application-start side effect.
"""
import hashlib
import hmac
import json
import os
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit
import requests
from flask import abort, flash, g, has_request_context, redirect, render_template, request, session, url_for
from vercel.headers import HeadersContext
from vercel.oidc import get_vercel_oidc_token_sync, VercelOidcTokenError
from sqlalchemy import Boolean, Column, DateTime, Integer, MetaData, String, Table, Text, UniqueConstraint, inspect, select, func
from sqlalchemy.exc import IntegrityError

meta=MetaData()
inbox=Table('dealer_portal_inbox',meta,
    Column('id',Integer,primary_key=True),Column('source',String(60),nullable=False),
    Column('portal_order_id',Integer,nullable=False),Column('portal_user_id',Integer,nullable=False),
    Column('payload_hash',String(64),nullable=False),Column('payload',Text,nullable=False),
    Column('order_id',Integer),Column('order_no',String(30)),Column('ack_pending',Boolean,nullable=False,default=False),
    Column('approved_by',String(100)),Column('created_at',DateTime,nullable=False),Column('approved_at',DateTime),
    UniqueConstraint('source','portal_order_id'))
links=Table('dealer_portal_customer',meta,
    Column('source',String(60),primary_key=True),Column('portal_user_id',Integer,primary_key=True),
    Column('customer_id',Integer,nullable=False),Column('linked_by',String(100),nullable=False),Column('linked_at',DateTime,nullable=False))
PAYMENTS={'havale':('Banka Havalesi',50),'kart-tek-cekim':('Kredi Kartı Tek Çekim',50),
          'kart-taksit':('Kredi Kartı 3 Taksit',45),'cek-90-gun':('Çek',45)}


class PortalError(ValueError):pass


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


def settings():
    if os.environ.get('BOS_PORTAL_ENABLED')!='1':return None
    origin=os.environ.get('BOS_PORTAL_URL','').rstrip('/')
    source=os.environ.get('BOS_PORTAL_SOURCE_ID','');key=os.environ.get('BOS_PORTAL_SIGNING_KEY','')
    parsed=urlsplit(origin)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or parsed.port not in (None,443):
        raise PortalError('Portalın tam HTTPS adresi yapılandırılmalı.')
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,59}',source) or not re.fullmatch(r'[a-f0-9]{64}',key):
        raise PortalError('Ayrı portal bağlantı kimliği ve imza anahtarı yapılandırılmalı.')
    return {'origin':origin,'source':source,'key':key}


def remote(config,method,target,payload=None):
    body=encoded(payload) if payload is not None else b'';stamp=str(int(time.time()))
    message=f'{method}\n{target}\n{stamp}\n{hashlib.sha256(body).hexdigest()}'.encode()
    headers={'X-BOS-Source':config['source'],'X-BOS-Timestamp':stamp,
             'X-BOS-Signature':hmac.new(config['key'].encode(),message,hashlib.sha256).hexdigest()}
    if payload is not None:headers['Content-Type']='application/json'
    if urlsplit(config['origin']).hostname.endswith('.vercel.app'):
        # Short-lived identity of this deployment, never a static protection bypass.
        try:
            with HeadersContext(dict(request.headers) if has_request_context() else None).use():
                token=get_vercel_oidc_token_sync()
            if not token:raise VercelOidcTokenError('Missing deployment identity')
        except VercelOidcTokenError:
            raise PortalError('BOS yayın kimliği doğrulanamadı; güvenilir kaynak ayarını kontrol edin.') from None
        headers['x-vercel-trusted-oidc-idp-token']=token
    # Never follow redirects: credentials cannot be forwarded to another host.
    try:
        with requests.request(method,config['origin']+target,data=body,headers=headers,
                              timeout=(5,15),allow_redirects=False,stream=True) as response:
            if response.status_code!=200:raise PortalError('Portal yanıtı doğrulanamadı; bağlantı veya yayın korumasını kontrol edin.')
            chunks=[];size=0
            for chunk in response.iter_content(65536):
                size+=len(chunk)
                if size>2_200_000:raise PortalError('Portal yanıtı çok büyük.')
                chunks.append(chunk)
            return json.loads(b''.join(chunks))
    except (requests.RequestException,ValueError) as error:
        if isinstance(error,PortalError):raise
        raise PortalError('Portal bağlantısı doğrulanamadı. Yeniden deneyebilirsiniz.') from None


def validate(record,source):
    if not isinstance(record,dict) or not isinstance(record.get('payload'),dict):raise PortalError('Geçersiz sipariş paketi.')
    p=record['payload'];digest=hashlib.sha256(encoded(p)).hexdigest()
    if record.get('payloadHash')!=digest or p.get('schemaVersion')!=1 or p.get('sourceId')!=source:raise PortalError('Sipariş kaynağı veya özeti uyuşmuyor.')
    def integer(value,minimum,maximum):
        if type(value) is not int or not minimum<=value<=maximum:raise PortalError('Geçersiz sayısal sipariş alanı.')
        return value
    for field in ('portalOrderId','portalUserId'):integer(p.get(field),1,2_000_000_000)
    if not isinstance(p.get('portalOrderNo'),str) or not re.fullmatch(r'BAY-\d{8}-[A-F0-9]{10}',p['portalOrderNo']):raise PortalError('Geçersiz portal sipariş numarası.')
    try:date.fromisoformat(p['orderDate'])
    except (ValueError,KeyError,TypeError):raise PortalError('Geçersiz sipariş tarihi.') from None
    customer=p.get('customer')
    if not isinstance(customer,dict):raise PortalError('Müşteri bilgileri eksik.')
    for field,maximum in [('company',200),('name',160),('email',254),('phone',30),('address',1000)]:
        if not isinstance(customer.get(field),str) or not 1<=len(customer[field])<=maximum:raise PortalError('Müşteri alanları eksik veya çok uzun.')
    if not isinstance(customer.get('note',''),str) or len(customer.get('note',''))>1000:raise PortalError('Sipariş notu geçersiz.')
    pricing=p.get('pricing',{})
    if not isinstance(pricing,dict):raise PortalError('Ödeme bilgileri geçersiz.')
    method=pricing.get('methodId')
    if not isinstance(method,str) or method not in PAYMENTS or type(pricing.get('rate')) is not int or pricing.get('rate')!=PAYMENTS[method][1]:raise PortalError('Ödeme veya iskonto uyuşmuyor.')
    if customer.get('paymentMethod')!=method:raise PortalError('Ödeme alanları uyuşmuyor.')
    items=p.get('items')
    if not isinstance(items,list) or not 1<=len(items)<=150:raise PortalError('Sipariş ürünleri geçersiz.')
    subtotal=0
    for item in items:
        if not isinstance(item,dict) or not isinstance(item.get('code'),str) or not re.fullmatch(r'[A-Z0-9._-]{1,60}',item['code']):raise PortalError('Ürün kodu geçersiz.')
        subtotal+=integer(item.get('quantity'),1,999)*integer(item.get('unitCents'),0,1_000_000_000)
        for field,maximum in [('name',160),('label',160),('detail',1000),('note',1000),('colorLabel',100)]:
            if not isinstance(item.get(field,''),str) or len(item.get(field,''))>maximum:raise PortalError('Ürün açıklaması geçersiz.')
        if not item.get('name'):raise PortalError('Ürün adı eksik.')
    discount=int((Decimal(subtotal)*PAYMENTS[method][1]/100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
    for field,value in [('subtotalCents',subtotal),('discountCents',discount),('totalCents',subtotal-discount)]:
        if type(pricing.get(field)) is not int or pricing[field]!=value:raise PortalError('Sipariş toplamı doğrulanamadı.')
    return p,digest


def ready(conn):return all(inspect(conn).has_table(table.name) for table in meta.sorted_tables)


def register_portal(app,db,Customer,Product,Order,OrderItem,OrderHistory):
    def owner():
        if not app.config.get('WEB_AUTH_ENABLED') or not getattr(g,'web_is_owner',False):abort(403)
    def configuration():
        config=settings()
        if not config:abort(404)
        return config
    def row_for(identity,lock=False):
        query=select(inbox).where(inbox.c.id==identity,inbox.c.source==configuration()['source'])
        if lock:query=query.with_for_update()
        row=db.session.execute(query).mappings().first()
        if not row:abort(404)
        return row
    def ack(identity):
        row=row_for(identity)
        if not row['order_id']:raise PortalError('Henüz onaylanmamış sipariş bildirilemez.')
        response=remote(configuration(),'POST','/api/bos/v1/ack',{'portalOrderId':row['portal_order_id'],
                        'payloadHash':row['payload_hash'],'bosOrderNo':row['order_no']})
        if not isinstance(response,dict) or response.get('acknowledged') is not True or response.get('portalOrderId')!=row['portal_order_id']:
            raise PortalError('Portal onay bildirimi doğrulanamadı.')
        db.session.execute(inbox.update().where(inbox.c.id==identity).values(ack_pending=False));db.session.commit()

    @app.get('/yonetim/bayi-portali')
    def dealer_portal():
        owner();error=None;config=None
        try:config=settings()
        except PortalError as e:error=str(e)
        with db.engine.connect() as conn:
            installed=ready(conn)
            rows=conn.execute(select(inbox).where(inbox.c.source==config['source']).order_by(inbox.c.id.desc()).limit(100)).mappings().all() if installed and config else []
        last_pull=session.get('dealer_portal_last_pull')
        if not isinstance(last_pull,dict) or not config or last_pull.get('source')!=config['source']:last_pull=None
        return render_template('dealer_portal.html',connected=bool(config),installed=installed,rows=rows,error=error,last_pull=last_pull)

    @app.post('/yonetim/bayi-portali/kur')
    def dealer_portal_init():
        owner();configuration()
        if request.form.get('confirm')!='yes':abort(400)
        with db.engine.begin() as conn:meta.create_all(conn)
        flash('Yalnızca portal bekleme ve müşteri eşleme tabloları hazırlandı. Eski kayıtlar değiştirilmedi.','success')
        return redirect(url_for('dealer_portal'))

    @app.post('/yonetim/bayi-portali/esitle')
    def dealer_portal_pull():
        owner();config=configuration()
        with db.engine.connect() as conn:
            if not ready(conn):abort(409)
            after=conn.execute(select(func.max(inbox.c.portal_order_id)).where(inbox.c.source==config['source'])).scalar() or 0
        try:
            result=remote(config,'GET',f'/api/bos/v1/orders?after={after}')
            if not isinstance(result,dict) or result.get('sourceId')!=config['source'] or not isinstance(result.get('orders'),list) or len(result['orders'])>20:
                raise PortalError('Sipariş listesi doğrulanamadı.')
            records=[validate(record,config['source']) for record in result['orders']]
            if any(p['portalOrderId']<=after for p,_ in records) or len({p['portalOrderId'] for p,_ in records})!=len(records):raise PortalError('Sipariş sayfası sırası geçersiz.')
            for payload,digest in records:
                existing=db.session.execute(select(inbox).where(inbox.c.source==config['source'],inbox.c.portal_order_id==payload['portalOrderId'])).mappings().first()
                if existing:
                    if existing['payload_hash']!=digest:raise PortalError('Önceden alınmış sipariş değişmiş; kayıt korunuyor.')
                    continue
                db.session.execute(inbox.insert().values(source=config['source'],portal_order_id=payload['portalOrderId'],
                    portal_user_id=payload['portalUserId'],payload_hash=digest,payload=encoded(payload).decode(),created_at=datetime.now(timezone.utc).replace(tzinfo=None),ack_pending=False))
            db.session.commit()
            session['dealer_portal_last_pull']={'source':config['source'],'ok':True,'count':len(records),'checkedAt':datetime.now(timezone.utc).isoformat()}
            flash(f'{len(records)} sipariş isteği kontrol edildi. Henüz satış, stok veya cari hareket oluşturulmadı.','success')
        except (PortalError,IntegrityError):
            session['dealer_portal_last_pull']={'source':config['source'],'ok':False,'checkedAt':datetime.now(timezone.utc).isoformat()}
            db.session.rollback();flash('Aktarım tamamlanmadı. Kayıtlar korundu; bağlantıyı ve paketleri kontrol ederek tekrar deneyin.','error')
        return redirect(url_for('dealer_portal'))

    @app.get('/yonetim/bayi-portali/<int:identity>')
    def dealer_portal_detail(identity):
        owner();row=row_for(identity);payload=json.loads(row['payload'])
        link=db.session.execute(select(links).where(links.c.source==row['source'],links.c.portal_user_id==row['portal_user_id'])).mappings().first()
        customer=db.session.get(Customer,link['customer_id']) if link else None
        codes={item['code'] for item in payload['items']}
        found={p.code for p in Product.query.filter(Product.code.in_(codes),Product.active.is_(True)).all()}
        return render_template('dealer_portal_detail.html',row=row,payload=payload,customer=customer,
                               missing=sorted(codes-found),payment=PAYMENTS[payload['pricing']['methodId']][0])

    @app.route('/yonetim/bayi-portali/musteriler',methods=['GET','POST'])
    def dealer_portal_customers():
        owner();config=configuration();error=None;customers=[];mapped={}
        after=request.args.get('after','0')
        if not re.fullmatch(r'\d{1,16}',after):abort(400)
        try:
            result=remote(config,'GET',f'/api/bos/v1/customers?after={after}')
            if result.get('sourceId')!=config['source'] or not isinstance(result.get('customers'),list):raise PortalError('Müşteri listesi doğrulanamadı.')
            customers=result['customers']
            if request.method=='POST':
                uid=request.form.get('portal_user_id','')
                if not uid.isdigit() or int(uid) not in {c['id'] for c in customers} or request.form.get('confirm')!='yes':abort(400)
                uid=int(uid);customer=Customer.query.filter_by(code=request.form.get('customer_code','').strip()).first()
                if not customer:raise PortalError('BOS müşteri kodu bulunamadı; eşleme yapılmadı.')
                existing=db.session.execute(select(links).where(links.c.source==config['source'],links.c.portal_user_id==uid)).mappings().first()
                if existing and existing['customer_id']!=customer.id:abort(409,'Mevcut eşleme bu formdan değiştirilemez.')
                if not existing:
                    db.session.execute(links.insert().values(source=config['source'],portal_user_id=uid,customer_id=customer.id,
                        linked_by=g.web_username,linked_at=datetime.now(timezone.utc).replace(tzinfo=None)))
                    db.session.commit()
                flash('Bayi cari hesabıyla eşleştirildi. Bu bayi kendi geçmiş siparişlerini ve hesap hareketlerini görebilir.','success')
                return redirect(url_for('dealer_portal_customers',after=after))
            rows=db.session.execute(select(links).where(links.c.source==config['source'])).mappings().all()
            mapped={r['portal_user_id']:db.session.get(Customer,r['customer_id']) for r in rows}
        except PortalError as exc:error=str(exc)
        return render_template('dealer_portal_customers.html',customers=customers,mapped=mapped,error=error,
            next_after=customers[-1]['id'] if len(customers)==100 else None)

    @app.post('/yonetim/bayi-portali/<int:identity>/eslestir')
    def dealer_portal_link(identity):
        owner();row=row_for(identity,True)
        if request.form.get('confirm')!='yes':abort(400)
        code=request.form.get('customer_code','').strip()
        customer=Customer.query.filter_by(code=code).first() if code else None
        if not customer:abort(400,'BOS müşteri kodu bulunamadı.')
        existing=db.session.execute(select(links).where(links.c.source==row['source'],links.c.portal_user_id==row['portal_user_id'])).mappings().first()
        if existing and existing['customer_id']!=customer.id:abort(409,'Mevcut müşteri eşlemesi bu işlemle değiştirilemez.')
        if not existing:
            db.session.execute(links.insert().values(source=row['source'],portal_user_id=row['portal_user_id'],customer_id=customer.id,
                linked_by=g.web_username,linked_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        db.session.commit();flash('Müşteri eşleştirildi; henüz satış siparişi oluşturulmadı.','success')
        return redirect(url_for('dealer_portal_detail',identity=identity))

    @app.post('/yonetim/bayi-portali/<int:identity>/onayla')
    def dealer_portal_approve(identity):
        owner();row=row_for(identity,True)
        if request.form.get('confirm')!='yes' or request.form.get('payload_hash')!=row['payload_hash']:abort(409)
        if not row['order_id']:
            link=db.session.execute(select(links).where(links.c.source==row['source'],links.c.portal_user_id==row['portal_user_id'])).mappings().first()
            if not link or not db.session.get(Customer,link['customer_id']):abort(409,'Önce müşteri eşleştirilmeli.')
            payload,digest=validate({'payload':json.loads(row['payload']),'payloadHash':row['payload_hash']},row['source'])
            try:
                vat=Decimal(request.form.get('vat_rate',''))
                if not vat.is_finite() or not 0<=vat<=100 or vat.as_tuple().exponent < -2:raise InvalidOperation()
            except InvalidOperation:abort(400,'KDV oranını açıkça seçin.')
            codes={item['code'] for item in payload['items']}
            products={p.code:p for p in Product.query.filter(Product.code.in_(codes),Product.active.is_(True)).all()}
            if set(products)!=codes:abort(409,'Eksik veya pasif ürün kodları var; satış oluşturulmadı.')
            number='BAYI-'+hashlib.sha256(row['source'].encode()).hexdigest()[:6].upper()+'-'+str(row['portal_order_id'])
            customer=payload['customer'];notes=f"Portal: {payload['portalOrderNo']}\nTeslimat: {customer['name']} · {customer['phone']}\n{customer['address']}\n{customer.get('note','')}"
            if payload['pricing']['methodId']=='cek-90-gun':notes+='\nÖdeme vadesi: 90 gün vadeli çek.'
            order=Order(order_no=number,order_type='Satış',customer_id=link['customer_id'],order_date=date.fromisoformat(payload['orderDate']),
                        payment_method=PAYMENTS[payload['pricing']['methodId']][0],status='Onaylandı',notes=notes)
            for item in payload['items']:
                product=products[item['code']]
                detail=' · '.join(x for x in (item.get('label'),item.get('colorLabel'),item.get('detail')) if x)
                order.items.append(OrderItem(product_id=product.id,product_name=item['name'],variant=(item.get('colorLabel') or item.get('label') or '')[:120],
                    description=detail,note=item.get('note',''),quantity=item['quantity'],unit='Adet',unit_price=Decimal(item['unitCents'])/100,
                    discount_rate=payload['pricing']['rate'],cost_unit_price=product.purchase_price or 0,vat_rate=vat,vat_included=False))
            order.history.append(OrderHistory(status='Onaylandı',note=f'Bayi portalından yönetici onayı · {g.web_username}'[:240]))
            if order.net_amount.quantize(Decimal('0.01'),rounding=ROUND_HALF_UP)!=Decimal(payload['pricing']['totalCents'])/100:
                abort(409,'Kuruş yuvarlaması portal toplamıyla uyuşmuyor; satış oluşturulmadı.')
            try:
                db.session.add(order);db.session.flush()
                db.session.execute(inbox.update().where(inbox.c.id==identity).values(order_id=order.id,order_no=number,ack_pending=True,
                    approved_by=g.web_username,approved_at=datetime.now(timezone.utc).replace(tzinfo=None)))
                db.session.commit()
            except IntegrityError:
                db.session.rollback();flash('Aynı sipariş eşzamanlı işlenmiş olabilir. Listeyi yenileyin; ikinci satış oluşturulmadı.','error')
                return redirect(url_for('dealer_portal_detail',identity=identity))
        try:
            ack(identity);flash('BOS satış siparişi oluşturuldu; portal görünürlüğü onaylandı. Fatura, stok ve cari hareket oluşturulmadı.','success')
        except PortalError:
            db.session.rollback();flash('BOS siparişi hazır; portal bildirimi bekliyor. Tekrar bildirmek ikinci satış oluşturmaz.','error')
        return redirect(url_for('dealer_portal_detail',identity=identity))

    @app.post('/yonetim/bayi-portali/<int:identity>/bildir')
    def dealer_portal_ack(identity):
        owner()
        try:ack(identity);flash('Portal onay bildirimi tamamlandı.','success')
        except PortalError:db.session.rollback();flash('Bildirim tamamlanmadı. BOS siparişi korundu; tekrar deneyebilirsiniz.','error')
        return redirect(url_for('dealer_portal_detail',identity=identity))
