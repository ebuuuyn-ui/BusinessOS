"""Order-line allocations without duplicating invoices, stock or ledger entries."""
from collections import defaultdict
from sqlalchemy import inspect, func, select


def ready():
    from app import db, InvoiceOrderAllocation
    return inspect(db.engine).has_table(InvoiceOrderAllocation.__tablename__)


def for_order(order_id):
    from app import InvoiceOrderAllocation as A
    return A.query.filter_by(order_id=order_id).all() if ready() else []


def for_invoice(invoice_id):
    from app import InvoiceOrderAllocation as A
    return A.query.filter_by(invoice_id=invoice_id).order_by(A.invoice_item_id,A.order_id,A.order_item_id).all() if ready() else []


def linked_orders(invoice):
    orders={invoice.order_id:invoice.order} if invoice.order else {}
    for a in for_invoice(invoice.id):orders[a.order_id]=a.order
    return sorted(orders.values(),key=lambda x:x.order_no)


def remaining(items):
    from app import db, InvoiceOrderAllocation as A
    ids=[x.id for x in items]
    used=dict(db.session.query(A.order_item_id,func.sum(A.quantity)).filter(A.order_item_id.in_(ids)).group_by(A.order_item_id).all()) if ids and ready() else {}
    return {x.id:max(0,x.quantity-used.get(x.id,0)) for x in items}


def summaries(orders):
    from app import InvoiceOrderAllocation as A
    ids=[x.id for x in orders];alloc=defaultdict(list)
    if ids and ready():
        for a in A.query.filter(A.order_id.in_(ids)).all():alloc[a.order_id].append(a)
    result={}
    for order in orders:
        rows=alloc[order.id];invoice_ids={x.id for x in order.invoices}|{x.invoice_id for x in rows}
        used=sum(x.quantity for x in rows);total=order.total_quantity
        state=('Faturalandı' if used>=total else 'Kısmen Faturalandı') if rows else ('Faturalandı' if invoice_ids else 'Fatura Bekliyor')
        result[order.id]=dict(count=len(invoice_ids),state=state,quantity=used,total=total,allocated=bool(rows))
    return result


def filter_orders(records,state):
    from app import db, Invoice, Order, OrderItem, InvoiceOrderAllocation as A
    legacy=Invoice.query.filter(Invoice.order_id==Order.id).exists()
    if not ready():return records.filter(legacy if state=='Faturalandı' else (db.false() if state=='Kısmen Faturalandı' else ~legacy))
    any_alloc=A.query.filter(A.order_id==Order.id).exists()
    used=select(func.coalesce(func.sum(A.quantity),0)).where(A.order_item_id==OrderItem.id).correlate(OrderItem).scalar_subquery()
    incomplete=OrderItem.query.filter(OrderItem.order_id==Order.id,OrderItem.quantity>used).exists()
    complete=db.or_(db.and_(any_alloc,~incomplete),db.and_(~any_alloc,legacy))
    if state=='Faturalandı':return records.filter(complete)
    if state=='Kısmen Faturalandı':return records.filter(any_alloc,incomplete)
    return records.filter(~complete)


def validate_distribution(doc, choices, prepared, candidates, customer_id, require_all=False):
    """Validate across the whole invoice, not just each row in isolation."""
    from uyumsoft_client import UyumError
    available=remaining(list(candidates.values()));used=defaultdict(int);result=[]
    for index,(line,choice,row) in enumerate(zip(doc['lines'],choices,prepared),1):
        allocations=[];seen=set()
        for split in choice['allocations']:
            item_id=split['item'];quantity=split['quantity']
            if not item_id:
                if quantity:raise UyumError(f'{index}. kalemde dağıtım adedi için sipariş satırı seçin.')
                continue
            try:item=candidates[int(item_id)];qty=int(quantity)
            except (KeyError,ValueError,TypeError):raise UyumError(f'{index}. kalemde geçerli sipariş satırı ve adet seçin.') from None
            if str(item.id) in seen:raise UyumError('Aynı sipariş satırını aynı kalemde iki kez seçmeyin.')
            seen.add(str(item.id))
            if not row['product'] or item.product_id!=row['product'].id or item.order.customer_id!=customer_id or item.order.order_type!='Satın Alma':
                raise UyumError(f'{index}. kalemde BOS stok kartı, tedarikçi ve seçilen sipariş ürünü aynı olmalıdır.')
            if (item.unit or '').casefold() not in ('adet','ad','c62') or qty<=0:raise UyumError('Dağıtım pozitif tam adet olmalıdır.')
            used[item.id]+=qty
            if used[item.id]>available[item.id]:raise UyumError(f'{item.order.order_no} / {item.product_name}: kalan faturasız adet aşılıyor.')
            allocations.append((item,qty))
        if allocations and sum(q for _,q in allocations)!=row['quantity']:raise UyumError(f'{index}. kalemde dağıtılan adet fatura adedine eşit olmalıdır.')
        if require_all and row['product'] and not allocations:raise UyumError(f'{index}. kalemi seçtiğiniz siparişlere dağıtın.')
        result.append(allocations)
    return result
