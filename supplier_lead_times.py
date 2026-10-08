"""Read-only supplier lead times from creation timestamps and status history."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal

TARGETS={'Sevk Edildi','Teslim Edildi'}
LOCAL=ZoneInfo('Europe/Istanbul')
def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
def local(value):
    return utc(value).astimezone(LOCAL) if value else None
def days(start,end):
    return Decimal(str((utc(end)-utc(start)).total_seconds()))/Decimal(86400)
def average(values):
    return sum(values,Decimal(0))/len(values) if values else None

def build_report(orders,normalize,filters,now=None):
    now=now or datetime.now(timezone.utc)
    result=[];groups={}
    for order in orders:
        created=order.created_at
        day=local(created).date().isoformat() if created else ''
        if filters.get('start_date') and day<filters['start_date']:continue
        if filters.get('end_date') and day>filters['end_date']:continue
        if any(t not in normalize(order.customer.name+' '+(order.customer.code or '')) for t in normalize(filters.get('customer_q','')).split()):continue
        if any(t not in normalize(order.order_no) for t in normalize(filters.get('q','')).split()):continue
        first=None;previous=None;uncertain=False
        for event in sorted(order.history,key=lambda e:(e.created_at,e.id or 0)):
            if event.status in TARGETS:
                if previous is None:uncertain=True
                elif event.status!=previous:
                    if first is None and previous not in TARGETS:first=event
            previous=event.status
        invalid=not created or any(e and utc(e.created_at)<utc(created) for e in (first,))
        cancelled=order.status=='İptal Edildi'
        missing=invalid or uncertain or (order.status in TARGETS and first is None)
        lead=days(created,first.created_at) if first and not missing and not cancelled else None
        state='İptal — Ortalama Dışı' if cancelled else 'Geçmiş Eksik / Tutarsız' if missing else 'Ölçüldü' if lead is not None else 'Devam Ediyor'
        row=dict(order=order,created=local(created),first=local(first.created_at) if first else None,first_status=first.status if first else None,lead=lead,state=state,age=days(created,now) if created and utc(created)<=utc(now) and state=='Devam Ediyor' else None)
        result.append(row)
        group=groups.setdefault(order.customer_id,dict(customer=order.customer,rows=[],leads=[],pending=0,missing=0,cancelled=0))
        group['rows'].append(row)
        if lead is not None:group['leads'].append(lead)
        group['pending']+=state=='Devam Ediyor';group['missing']+=state=='Geçmiş Eksik / Tutarsız';group['cancelled']+=cancelled
    for group in groups.values():
        group['average']=average(group['leads'])
    leads=[r['lead'] for r in result if r['lead'] is not None]
    return dict(report_rows=sorted(result,key=lambda r:r['created'] or datetime.min.replace(tzinfo=timezone.utc),reverse=True),supplier_groups=sorted(groups.values(),key=lambda g:normalize(g['customer'].name)),lead_average=average(leads),measured_count=len(leads),missing_count=sum(g['missing'] for g in groups.values()),pending_count=sum(g['pending'] for g in groups.values()),filters=filters)
