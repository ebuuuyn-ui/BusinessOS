"""Sales attribution from immutable order creation events, never later edits."""
import json
from decimal import Decimal
from datetime import datetime
from sqlalchemy import inspect, text

UNKNOWN='Satıcısı belirlenemeyen'

def creation_owners(events):
    owners={}
    for event in events:
        if event.get('operation')!='INSERT' or event.get('table_name')!='order': continue
        data=event.get('after_data') or {}
        if isinstance(data,str): data=json.loads(data)
        if event.get('endpoint') not in ('new_order','quote_to_order'): continue
        actor=event.get('actor_id','')
        name=(event.get('actor_name') or '').strip()
        seller='Bekir' if actor=='owner' else 'Ahmet' if actor.startswith('user:') and name.casefold()=='ahmet' else name if actor.startswith('user:') and name else UNKNOWN
        # Include creation timestamp and number so reused numeric IDs cannot misattribute orders.
        if data.get('created_at') and data.get('order_no'):
            key=(str(data.get('id')),data['order_no'],datetime.fromisoformat(data['created_at']))
            owners.setdefault(key,set()).add(seller)
    return {k:next(iter(v)) if len(v)==1 else UNKNOWN for k,v in owners.items()}

def load_owners(db):
    if not inspect(db.session.connection()).has_table('business_audit_event'): return {}
    records=db.session.execute(text("SELECT actor_id,actor_name,endpoint,operation,table_name,after_data FROM business_audit_event WHERE table_name='order' AND operation='INSERT' ORDER BY id")).mappings()
    return creation_owners(records)

def build_report(orders,owners,filters,normalize):
    rows=[]
    groups={name:dict(name=name,count=0,quantity=Decimal(0),net=Decimal(0),vat=Decimal(0),total=Decimal(0)) for name in ('Bekir','Ahmet')}
    for order in orders:
        if order.order_type!='Satış' or order.status=='İptal Edildi': continue
        day=order.order_date.isoformat()
        if filters.get('start_date') and day<filters['start_date']:continue
        if filters.get('end_date') and day>filters['end_date']:continue
        seller=owners.get((str(order.id),order.order_no,order.created_at),'Bekir')
        if filters.get('seller') and seller!=filters['seller']:continue
        haystack=normalize(order.order_no+' '+order.customer.name+' '+(order.customer.code or ''))
        if any(t not in haystack for t in normalize(filters.get('q','')).split()):continue
        amounts=dict(quantity=Decimal(str(order.total_quantity)),net=order.net_amount,vat=order.vat_amount,total=order.total_amount)
        group=groups.setdefault(seller,dict(name=seller,count=0,quantity=Decimal(0),net=Decimal(0),vat=Decimal(0),total=Decimal(0)))
        group['count']+=1
        for k,v in amounts.items():group[k]+=v
        rows.append(dict(order=order,seller=seller,**amounts))
    options=sorted(set(owners.values())|{'Bekir','Ahmet'})
    summary=[g for name,g in groups.items() if not filters.get('seller') or name==filters['seller']]
    totals={k:sum((g[k] for g in summary),Decimal(0)) for k in ('quantity','net','vat','total')}
    return dict(sales_rows=sorted(rows,key=lambda r:(r['order'].order_date,r['order'].id),reverse=True),seller_groups=summary,seller_options=options,sales_totals=totals,filters=filters)
