"""Signed dealer bridge: explicit customer links, read-only ledger/Trello projections."""
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from concurrent.futures import ThreadPoolExecutor
from cryptography.fernet import Fernet
from flask import abort, current_app, request, jsonify
from sqlalchemy import select, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from dealer_portal import settings, ready, inbox, links, validate, encoded, PortalError


def cents(value):
    return int((Decimal(value or 0)*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))


def trello_client(app,db):
    from supplier_trello import connection, personal_connections, Client, TrelloError
    value=None
    if inspect(db.engine).has_table(personal_connections.name):
        value=db.session.execute(select(personal_connections.c.config).where(personal_connections.c.user_id==0)).scalar()
    if not value and inspect(db.engine).has_table(connection.name):
        value=db.session.execute(select(connection.c.config).where(connection.c.id==1)).scalar()
    if not value:raise TrelloError('Not configured')
    cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256(('bos-trello-v1:'+app.secret_key).encode()).digest()))
    return Client(json.loads(cipher.decrypt(value.encode())))


def trello_states(app, db, Order, OrderItem, orders):
    """Never matches names; resolve linked purchase lines or exact sales-line keys."""
    from supplier_trello import connection, personal_connections, deliveries, BOARD, Client, TrelloError
    states={};now=datetime.now(timezone.utc).isoformat()
    if not orders:return states,{'connected':True,'checkedAt':now}
    if not inspect(db.engine).has_table(deliveries.name):return states,{'connected':False,'checkedAt':None}
    ids=[o.id for o in orders]
    purchases=db.session.execute(select(OrderItem,Order).join(Order,Order.id==OrderItem.order_id).where(
        Order.source_order_id.in_(ids),Order.order_type=='Satın Alma',Order.status.notin_(['İptal','İptal Edildi']))).all()
    keys={i.id:[f'bos:order:{o.id}:item:{i.id}'] for o in orders for i in o.items}
    for item,purchase in purchases:
        if item.source_order_item_id in keys:
            keys[item.source_order_item_id].append(f'bos:order:{purchase.id}:item:{item.id}')
    all_keys=[key for candidates in keys.values() for key in candidates]
    rows=db.session.execute(select(deliveries.c.key,deliveries.c.card_id).where(deliveries.c.key.in_(all_keys))).all() if all_keys else []
    card_ids=dict(rows)
    if not any(card_ids.values()):return states,{'connected':True,'checkedAt':now}
    try:
        client=trello_client(app,db)
        # Only two read requests regardless of order count; no card details or links leave BOS.
        with ThreadPoolExecutor(max_workers=2) as pool:
            lists_future=pool.submit(client.call,'GET',f'boards/{BOARD}/lists',filter='all',fields='name')
            cards_future=pool.submit(client.call,'GET',f'boards/{BOARD}/cards',filter='all',fields='idList,closed')
            lists={r['id']:r['name'] for r in lists_future.result()}
            cards={r['id']:r for r in cards_future.result()}
        missing=False
        for item_id,candidates in keys.items():
            matched=[cards.get(card_ids[k]) for k in candidates if card_ids.get(k)]
            labels=[]
            for card in matched:
                if not card or card.get('idList') not in lists:
                    missing=True;continue
                label=lists[card['idList']]
                if card.get('closed'):label+=' (arşivlendi)'
                if label not in labels:labels.append(label)
            if labels:states[item_id]=' / '.join(labels)
        return states,{'connected':not missing,'checkedAt':now}
    except Exception:
        # Token, card contents and customer data must never enter logs/errors.
        app.logger.warning('DEALER_TRELLO_READ_UNAVAILABLE')
        return {},{'connected':False,'checkedAt':None}


def register_dealer_account(app, db, Customer, Order, OrderItem, statement):
    def authorize():
        config=settings()
        if not config:abort(404)
        if request.headers.get('Origin'):abort(403)
        if request.content_length and request.content_length>1_000_000:abort(413)
        stamp=request.headers.get('X-BOS-Timestamp','')
        supplied=request.headers.get('X-BOS-Signature','')
        target=request.full_path.rstrip('?');body=request.get_data()
        message=f'{request.method}\n{target}\n{stamp}\n{hashlib.sha256(body).hexdigest()}'.encode()
        if (not re.fullmatch(r'\d{10}',stamp) or abs(int(stamp)-int(time.time()))>120
            or request.headers.get('X-BOS-Source')!=config['source'] or not re.fullmatch(r'[a-f0-9]{64}',supplied)
            or not hmac.compare_digest(supplied,hmac.new(config['key'].encode(),message,hashlib.sha256).hexdigest())):abort(401)
        with db.engine.connect() as conn:
            if not ready(conn):abort(503)
        return config

    @app.get('/api/dealer-portal/v1/health')
    def dealer_machine_health():
        config=authorize()
        from supplier_trello import BOARD
        try:
            lists=trello_client(app,db).call('GET',f'boards/{BOARD}/lists',filter='all',fields='name')
            trello={'connected':True,'stages':[row['name'] for row in lists]}
        except Exception:trello={'connected':False}
        return {'sourceId':config['source'],'ready':True,'trello':trello}

    @app.post('/api/dealer-portal/v1/orders')
    def dealer_machine_orders():
        config=authorize()
        try:payload,digest=validate(request.get_json(silent=True),config['source'])
        except PortalError:return {'error':'Geçersiz sipariş.'},400
        predicate=(inbox.c.source==config['source'],inbox.c.portal_order_id==payload['portalOrderId'])
        existing=db.session.execute(select(inbox).where(*predicate)).mappings().first()
        if not existing:
            try:
                db.session.execute(inbox.insert().values(source=config['source'],portal_order_id=payload['portalOrderId'],
                    portal_user_id=payload['portalUserId'],payload_hash=digest,payload=encoded(payload).decode(),
                    created_at=datetime.now(timezone.utc).replace(tzinfo=None),ack_pending=False))
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                existing=db.session.execute(select(inbox).where(*predicate)).mappings().first()
                if not existing:raise
        if existing and existing['payload_hash']!=digest:return {'error':'Sipariş kimliği uyuşmuyor.'},409
        return {'received':True,'portalOrderId':payload['portalOrderId'],'payloadHash':digest}

    @app.get('/api/dealer-portal/v1/account')
    def dealer_machine_account():
        config=authorize();uid=request.args.get('userId','')
        if set(request.args)-{'userId','start','end'} or not re.fullmatch(r'[1-9]\d{0,9}',uid):abort(400)
        uid=int(uid);start=request.args.get('start','');end=request.args.get('end','')
        try:
            for value in (start,end):
                if value and (not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) or date.fromisoformat(value).isoformat()!=value):raise ValueError()
            if start and end and start>end:raise ValueError()
        except ValueError:abort(400)
        result={'sourceId':config['source'],'portalUserId':uid,'linked':False,'orders':[]}
        link=db.session.execute(select(links).where(links.c.source==config['source'],links.c.portal_user_id==uid)).mappings().first()
        customer=db.session.get(Customer,link['customer_id']) if link else None
        if customer is None:return result
        entries=statement(customer)
        current=sum(cents(e['debit'])-cents(e['credit']) for e in entries)
        opening=sum(cents(e['debit'])-cents(e['credit']) for e in entries if start and e['date'].isoformat()<start)
        running=opening;debit=credit=0;transactions=[]
        for i,e in enumerate(entries):
            day=e['date'].isoformat()
            if start and day<start or end and day>end:continue
            dr,cr=cents(e['debit']),cents(e['credit']);running+=dr-cr;debit+=dr;credit+=cr
            transactions.append({'id':i,'entryDate':day,'reference':e['reference'],'description':e['description'],
                                 'debitCents':dr,'creditCents':cr,'balanceCents':running})
        query=Order.query.options(selectinload(Order.items).selectinload(OrderItem.product)).filter_by(customer_id=customer.id,order_type='Satış')
        if start:query=query.filter(Order.order_date>=date.fromisoformat(start))
        if end:query=query.filter(Order.order_date<=date.fromisoformat(end))
        orders=query.order_by(Order.order_date.desc(),Order.id.desc()).limit(501).all()
        truncated=len(orders)>500;orders=orders[:500]
        states,trello=trello_states(app,db,Order,OrderItem,orders)
        portal_ids=dict(db.session.execute(select(inbox.c.order_id,inbox.c.portal_order_id).where(
            inbox.c.source==config['source'],inbox.c.portal_user_id==uid,inbox.c.order_id.is_not(None))).all())
        history=[]
        for order in orders:
            items=[{'name':item.product_name,'code':item.product.code if item.product else '',
                    'quantity':float(item.quantity),'detail':item.description or '', 'colorLabel':item.variant or '',
                    'status':states.get(item.id,'Takip bilgisi bekleniyor'),'statusSource':'Trello' if item.id in states else None}
                   for item in order.items]
            labels=list(dict.fromkeys(i['status'] for i in items if i['statusSource']))
            status=labels[0] if len(labels)==1 and all(i['statusSource'] for i in items) else 'Ürünler farklı aşamalarda' if labels else order.status
            if order.status in ('İptal','İptal Edildi'):status=order.status
            history.append({'id':'bos-'+str(order.id),'portalOrderId':portal_ids.get(order.id),'orderNo':order.order_no,
                'orderDate':order.order_date.isoformat(),'status':status,'statusSource':'Trello' if labels else 'Sipariş',
                'totalCents':cents(order.total_amount),'netCents':cents(order.net_amount),'vatCents':cents(order.vat_amount),
                'taxIncluded':True,'items':items})
        result.update(linked=True,ledgerConnected=True,ordersConnected=True,ledgerSyncedAt=datetime.now(timezone.utc).isoformat(),
            currentCents=current,openingCents=opening,closingCents=running,debitCents=debit,creditCents=credit,
            transactions=transactions,orders=history,historyTruncated=truncated,trello=trello)
        return jsonify(result)
