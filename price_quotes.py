"""Versioned quotations and explicit, idempotent conversion to sales orders."""
import base64, io, json, uuid
from document_language import LABELS,label as document_label
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from flask import request, render_template, redirect, url_for, flash, send_file, abort, g, jsonify
from sqlalchemy import inspect
from PIL import Image, ImageOps, UnidentifiedImageError

CONDITIONS=['Teslimat','Ödeme','Süre','Garanti','İade','Teklif Geçerlilik Süresi']
CENT=Decimal('.01')
def amount(value):
    try:
        d=Decimal(str(value))
        if not d.is_finite() or d<0 or d>Decimal('999999999'):raise ValueError()
        return d
    except (InvalidOperation,ValueError,TypeError):raise ValueError('Fiyat ve oranlar geçerli, sıfır veya pozitif sayılar olmalıdır.')
def rounded(value):return value.quantize(CENT,rounding=ROUND_HALF_UP)
def contact():
    user=getattr(g,'web_username','bekir').casefold()
    if getattr(g,'web_is_owner',False) or user in ('bekir','ebubekir','bekir@abikamobilya.com'):
        return {'user':user,'name':'Ebubekir Uyan','email':'bekir@abikamobilya.com','phone':'0554 886 78 19'}
    if user in ('ahmet','ahmet@abikamobilya.com') or user.startswith('ahmet.'):
        return {'user':user,'name':'Ahmet','email':'ahmet@abikamobilya.com','phone':''}
    raise ValueError('Bu kullanıcı için teklif e-posta adresi henüz tanımlı değil.')
def totals(lines):
    listed=sum((Decimal(x['list_price'])*x['quantity'] for x in lines),Decimal(0))
    net=sum((Decimal(x['total']) for x in lines),Decimal(0));tax=sum((Decimal(x['tax']) for x in lines),Decimal(0))
    return dict(listed=str(rounded(listed)),discount=str(rounded(listed-net)),net=str(net),tax=str(tax),total=str(net+tax))
def validate(data,products,image_for):
    try:date.fromisoformat(data.get('date',''))
    except (ValueError,TypeError):raise ValueError('Teklif tarihini kontrol edin.')
    if not str(data.get('customer_name','')).strip():raise ValueError('Müşteri adı zorunludur.')
    lines=data.get('lines',[])
    if not isinstance(lines,list) or not 1<=len(lines)<=100:raise ValueError('Teklife 1 ile 100 arasında ürün kalemi ekleyin.')
    if not isinstance(data.get('conditions',{}),dict):raise ValueError('Teklif koşulları geçersiz.')
    currency=data.get('currency','TRY')
    if currency not in ('TRY','USD'):raise ValueError('Para birimi TL veya USD olmalıdır.')
    rate=amount(data.get('exchange_rate','1')) if currency=='USD' else Decimal(1)
    if rate<=0:raise ValueError('USD kuru sıfırdan büyük olmalıdır.')
    out=[]
    for row in lines:
        if not isinstance(row,dict):raise ValueError('Ürün kalemi geçersiz.')
        product=products.get(str(row.get('product_id')))
        if not product:raise ValueError('Her kalemde geçerli bir stok kartı seçin.')
        qty=amount(row.get('quantity'));lp=rounded(amount(row.get('list_price')))
        if qty!=int(qty) or not 1<=qty<=100000:raise ValueError('Adet pozitif tam sayı olmalıdır.')
        mode=row.get('mode')
        if mode=='discount':
            discount=amount(row.get('discount'))
            if discount>100:raise ValueError('İskonto %100’ü aşamaz.')
            price=rounded(lp*(100-discount)/100)
        elif mode=='price':
            price=rounded(amount(row.get('price')))
            discount=rounded((lp-price)*100/lp) if lp else Decimal(0)
        elif mode=='usd' and currency=='USD':
            usd_price=rounded(amount(row.get('usd_price')))
            price=rounded(usd_price*rate)
            discount=rounded((lp-price)*100/lp) if lp else Decimal(0)
        else:raise ValueError('Fiyat giriş yöntemini seçin.')
        vat=amount(row.get('vat',10))
        if vat not in (0,1,8,10,18,20):raise ValueError('Geçerli KDV oranı seçin.')
        source_lp,source_price=lp,price
        if currency=='USD':
            lp=rounded(lp/rate)
            price=usd_price if mode=='usd' else rounded(price/rate)
        net=rounded(price*qty);tax=rounded(net*vat/100)
        name=str(row.get('name','')).strip()
        if not name or len(name)>200:raise ValueError('Ürün adı 1-200 karakter olmalıdır.')
        out.append(dict(product_id=product.id,code=product.code or '',name=name,description=str(row.get('description',''))[:500],unit=product.unit,
            quantity=int(qty),source_list_price=str(source_lp),source_price=str(source_price),usd_price=str(price) if currency=='USD' else '',list_price=str(lp),mode=mode,discount=str(discount),price=str(price),vat=str(vat),total=str(net),tax=str(tax),image=image_for(product,row)))
    clean={k:str(data.get(k,''))[:1000] for k in ('customer_name','customer_code','address','recipient','phone','date')}
    clean['conditions']={k:str(data.get('conditions',{}).get(k,''))[:5000] for k in CONDITIONS}
    from document_language import language
    clean['language']=language(data.get('language','tr'))
    clean['currency']=currency;clean['exchange_rate']=str(rate)
    clean['lines']=out;clean['totals']=totals(out)
    return clean

def register_quotes(app,db,Product,Customer,Quote,ProductImage):
    def ready(model):return inspect(db.engine).has_table(model.__tablename__)
    def image_data(product):
        obj=db.session.get(ProductImage,product.id) if ready(ProductImage) else None
        if obj:return obj.content
        path=Path(app.root_path)/'static'/'quote-products'/((product.code or '')+'.jpg')
        if product.code and '/' not in product.code and path.is_file():return path.read_bytes()
        return None
    def catalog():return Product.query.filter_by(active=True).order_by(Product.name).all()
    def get_quote(uid):
        if not ready(Quote):abort(404)
        return db.get_or_404(Quote,uid)
    @app.get('/fiyat-teklifleri')
    def quotes():
        query=request.args.get('q','').strip();rows=Quote.query.order_by(Quote.created_at.desc()).all() if ready(Quote) else []
        from app import normalize_search_text
        if query:rows=[x for x in rows if normalize_search_text(query) in normalize_search_text(x.number+' '+x.customer_name)]
        return render_template('quotes.html',rows=rows,query=query)
    @app.route('/fiyat-teklifleri/yeni',methods=['GET','POST'])
    @app.route('/fiyat-teklifleri/<uid>/duzenle',methods=['GET','POST'])
    def quote_edit(uid=None):
        quote=get_quote(uid) if uid else None;data=json.loads(quote.payload) if quote else {'date':date.today().isoformat(),'lines':[],'conditions':{}}
        creation_id=request.form.get('creation_id') or str(uuid.uuid4())
        error=None;products=catalog();by_id={str(x.id):x for x in products}
        # Existing inactive products can remain in an older saved quote.
        if quote:
            for row in data['lines']:
                p=db.session.get(Product,row['product_id'])
                if p:by_id[str(p.id)]=p
        try:author=contact()
        except ValueError as e:author=None;error=str(e)
        if request.method=='POST':
            try:
                if not author:raise ValueError(error)
                submitted=json.loads(request.form.get('payload','{}'))
                if not isinstance(submitted,dict):raise ValueError('Teklif verisi geçersiz.')
                old_data=data;data=submitted
                if quote and str(quote.version)!=request.form.get('version'):raise ValueError('Teklif başka bir oturumda değişti. Sayfayı yenileyip tekrar kontrol edin.')
                def choose_image(product,row):
                    # Preserve the saved image unless explicitly refreshed by selecting/uploading.
                    if quote and not row.get('refresh_image'):
                        original=next((x for x in old_data['lines'] if x['product_id']==product.id),None)
                        if original:return original.get('image')
                    binary=image_data(product)
                    return base64.b64encode(binary).decode() if binary else None
                clean=validate(data,by_id,choose_image)
                clean['author']=old_data['author'] if quote else author
                if quote and old_data.get('packing_list'):clean['packing_list']=old_data['packing_list']
                Quote.__table__.create(db.engine,checkfirst=True)
                if quote:
                    changed=Quote.query.filter_by(id=quote.id,version=quote.version).update(dict(payload=json.dumps(clean,ensure_ascii=False),customer_name=clean['customer_name'],version=quote.version+1),synchronize_session=False)
                    if changed!=1:raise ValueError('Teklif başka bir oturumda değişti; sayfayı yenileyin.')
                else:
                    uid=str(uuid.UUID(creation_id))
                    existing=db.session.get(Quote,uid)
                    if existing:return redirect(url_for('quote_detail',uid=existing.id))
                    quote=Quote(id=uid,number='FT-'+date.today().strftime('%Y')+'-'+uid[:8].upper(),customer_name=clean['customer_name'],payload=json.dumps(clean,ensure_ascii=False))
                    db.session.add(quote)
                db.session.commit();flash('Fiyat teklifi kaydedildi.','success');return redirect(url_for('quote_detail',uid=quote.id))
            except (ValueError,TypeError,KeyError) as e:
                db.session.rollback();error=str(e)
                if not isinstance(data,dict) or not isinstance(data.get('lines',[]),list):data={'date':date.today().isoformat(),'lines':[],'conditions':{}}
        product_data=[dict(id=x.id,name=x.name,code=x.code or '',price=str(x.unit_price),description=x.default_variant or '',image=url_for('quote_product_image',pid=x.id)) for x in by_id.values()]
        customers=[dict(id=x.id,name=x.name,code=x.code or '',address=x.address or '',recipient=x.contact_name or '',phone=x.phone or x.mobile or '') for x in Customer.query.order_by(Customer.name).all()]
        return render_template('quote_edit.html',quote=quote,data=data,author=author,error=error,products=product_data,customers=customers,conditions=CONDITIONS,creation_id=creation_id,language_labels=LABELS,english=request.args.get('english')=='1')
    @app.get('/fiyat-teklifleri/<uid>')
    def quote_detail(uid):
        quote=get_quote(uid);data=json.loads(quote.payload)
        from app import QuoteOrderTransfer, Order
        transfer=db.session.get(QuoteOrderTransfer,uid) if ready(QuoteOrderTransfer) else None
        linked_order=db.session.get(Order,transfer.order_id) if transfer else None
        return render_template('quote_detail.html',quote=quote,data=data,transfer=transfer,linked_order=linked_order,t=lambda s:document_label(s,data.get('language','tr')))
    @app.route('/fiyat-teklifleri/<uid>/siparise-aktar',methods=['GET','POST'])
    def quote_to_order(uid):
        from app import QuoteOrderTransfer, Order, OrderItem, OrderHistory, next_order_no, ORDER_PAYMENT_METHODS, normalize_search_text
        from sqlalchemy.exc import IntegrityError
        quote=get_quote(uid);data=json.loads(quote.payload)
        def linked():
            return db.session.get(QuoteOrderTransfer,uid) if ready(QuoteOrderTransfer) else None
        def existing_response(transfer):
            if db.session.get(Order,transfer.order_id):
                return redirect(url_for('order_detail',order_id=transfer.order_id))
            flash('Bu teklif daha önce '+transfer.order_no+' siparişine aktarıldı. Sipariş silinmiş; tekrar aktarım yapılmadı.','error')
            return redirect(url_for('quote_detail',uid=uid))
        transfer=linked()
        if transfer:return existing_response(transfer)
        customers=Customer.query.order_by(Customer.name).all()
        code=data.get('customer_code','').strip()
        candidates=[c for c in customers if (c.code or '').strip()==code] if code else [c for c in customers if normalize_search_text(c.name)==normalize_search_text(data['customer_name'])]
        selected=request.form.get('customer_id',type=int) if request.method=='POST' else (candidates[0].id if len(candidates)==1 else None)
        error=None
        if request.method=='POST':
            try:
                if request.form.get('version')!=str(quote.version):raise ValueError('Teklif değişti. Güncel kalemleri kontrol edip tekrar onaylayın.')
                if not selected or not db.session.get(Customer,selected):raise ValueError('Siparişin bağlanacağı cari kartını seçin.')
                payment=request.form.get('payment_method','')
                if payment not in ORDER_PAYMENT_METHODS:raise ValueError('Ödeme yöntemini seçin.')
                order_date=date.fromisoformat(request.form.get('order_date',''))
                delivery=date.fromisoformat(request.form['delivery_date']) if request.form.get('delivery_date') else None
                if delivery and delivery<order_date:raise ValueError('Teslim tarihi sipariş tarihinden önce olamaz.')
                items=[]
                for row in data['lines']:
                    product=db.session.get(Product,row['product_id'])
                    if not product or not product.active:raise ValueError(row['name']+': stok kartı silinmiş veya pasif. Önce teklifi güncelleyin.')
                    price=Decimal(row['price']);listed=Decimal(row['list_price']);discount=Decimal(row['discount'])
                    if data.get('currency')=='USD':
                        rate=amount(data['exchange_rate'])
                        if rate<=0:raise ValueError('Teklif kuru geçersiz; önce teklifi güncelleyin.')
                        price=rounded(price*rate);listed=rounded(listed*rate)
                    # Keep the listed price/discount when they reproduce the agreed rounded unit price exactly.
                    keep_discount=0<=discount<=100 and discount==rounded(discount) and listed*(100-discount)/100==price
                    items.append(OrderItem(product_id=product.id,product_name=row['name'],quantity=row['quantity'],unit=row.get('unit') or 'Adet',
                        unit_price=listed if keep_discount else price,discount_rate=discount if keep_discount else Decimal(0),
                        vat_rate=Decimal(row['vat']),vat_included=False,cost_unit_price=0,description=row.get('description',''),
                        note='Teklif: '+quote.number+' | Birim fiyatı: '+row['list_price']+(' USD' if data.get('currency')=='USD' else ' TL')+' | İskonto: %'+row['discount']+' | '+row.get('description','')))
                QuoteOrderTransfer.__table__.create(db.engine,checkfirst=True)
                # Compare-and-swap serializes two submissions of the same version without a second order.
                changed=Quote.query.filter_by(id=uid,version=quote.version).update({Quote.version:Quote.version+1},synchronize_session=False)
                if changed!=1:
                    db.session.rollback()
                    transfer=linked()
                    if transfer:return existing_response(transfer)
                    raise ValueError('Teklif değişti; sayfayı yenileyip tekrar kontrol edin.')
                # A fresh edit version must not make an already transferred quote eligible again.
                transfer=linked()
                if transfer:
                    db.session.rollback();return existing_response(transfer)
                notes='Fiyat teklifinden aktarıldı: '+quote.number+'\n'+ '\n'.join(k+': '+v for k,v in data.get('conditions',{}).items() if v)
                if data.get('currency')=='USD':notes+='\nTeklif Para Birimi: USD | 1 USD = '+data['exchange_rate']+' TL. Sipariş fiyatları bu kurla TL’ye çevrildi.'
                order=Order(order_no=next_order_no('Satış'),order_type='Satış',customer_id=selected,order_date=order_date,delivery_date=delivery,
                            payment_method=payment,status='Bekliyor',notes=notes,items=items)
                order.history.append(OrderHistory(status='Bekliyor',note=quote.number+' fiyat teklifinden oluşturuldu'))
                db.session.add(order);db.session.flush()
                db.session.add(QuoteOrderTransfer(quote_id=uid,order_id=order.id,order_no=order.order_no))
                db.session.commit()
                flash(order.order_no+' numaralı satış siparişi oluşturuldu.','success')
                return redirect(url_for('order_detail',order_id=order.id))
            except (ValueError,KeyError,TypeError,IntegrityError) as e:
                db.session.rollback()
                if isinstance(e,IntegrityError):
                    transfer=linked()
                    if transfer:return existing_response(transfer)
                    error='Sipariş oluşturulamadı. Kayıt çakışması nedeniyle işlem geri alındı; tekrar deneyebilirsiniz.'
                else:error=str(e)
        order_data=json.loads(json.dumps(data))
        if data.get('currency')=='USD':
            rate=Decimal(data['exchange_rate'])
            for row in order_data['lines']:
                row['price']=str(rounded(Decimal(row['price'])*rate))
                row['list_price']=str(rounded(Decimal(row['list_price'])*rate))
                row['total']=str(rounded(Decimal(row['price'])*row['quantity']))
                row['tax']=str(rounded(Decimal(row['total'])*Decimal(row['vat'])/100))
            order_data['totals']=totals(order_data['lines'])
        return render_template('quote_to_order.html',quote=quote,data=order_data,customers=customers,selected=selected,error=error,
                               payment_methods=ORDER_PAYMENT_METHODS,today=date.today().isoformat())
    @app.get('/fiyat-teklifleri/<uid>/pdf')
    def quote_pdf(uid):
        from quote_pdf import build_pdf
        quote=get_quote(uid)
        return send_file(build_pdf(json.loads(quote.payload),quote.number),mimetype='application/pdf',as_attachment=True,download_name=quote.number+'.pdf')
    @app.post('/fiyat-teklifleri/onizleme.pdf')
    def quote_preview():
        try:
            data=json.loads(request.form.get('payload','{}'));products={str(p.id):p for p in catalog()}
            data=validate(data,products,lambda p,r:base64.b64encode(image_data(p)).decode() if image_data(p) else None);data['author']=contact()
            from quote_pdf import build_pdf
            return send_file(build_pdf(data,'ÖNİZLEME'),mimetype='application/pdf',as_attachment=True,download_name='Abika-Teklif-Onizleme.pdf')
        except (ValueError,TypeError,KeyError):abort(400,'Teklif alanlarını kontrol edin.')
    @app.route('/fiyat-teklifleri/urun/<int:pid>/gorsel',methods=['GET','POST'])
    def quote_product_image(pid):
        p=db.get_or_404(Product,pid)
        if request.method=='GET':
            content=image_data(p)
            if not content:return ('',404)
            return send_file(io.BytesIO(content),mimetype='image/jpeg')
        f=request.files.get('image')
        if not f:abort(400)
        blob=f.read(5*1024*1024+1)
        if len(blob)>5*1024*1024:abort(400,'Görsel en fazla 5 MB olabilir.')
        try:
            im=Image.open(io.BytesIO(blob))
            if im.width*im.height>24000000:raise ValueError()
            im=ImageOps.exif_transpose(im);im.thumbnail((800,800));bg=Image.new('RGB',im.size,'white')
            if im.mode=='RGBA':bg.paste(im,mask=im.getchannel('A'))
            else:bg.paste(im.convert('RGB'))
            b=io.BytesIO();bg.save(b,'JPEG',quality=85)
        except (UnidentifiedImageError,ValueError,OSError,Image.DecompressionBombError):abort(400,'Geçerli PNG, JPEG veya WebP görsel seçin.')
        ProductImage.__table__.create(db.engine,checkfirst=True)
        obj=db.session.get(ProductImage,pid)
        if not obj:obj=ProductImage(product_id=pid);db.session.add(obj)
        obj.content=b.getvalue();db.session.commit();return jsonify(ok=True)
