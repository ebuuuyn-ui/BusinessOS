"""Read-only invoice FIFO report; explicit, audited overrides for unknown costs."""
import calendar
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from flask import request, render_template, redirect, url_for, flash, session
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import selectinload, joinedload
from fifo_costing import calculate_fifo

CENT = Decimal('0.01')
ZERO = Decimal('0')

def cents(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def signature(item):
    payload = [item.id, item.invoice.invoice_date.isoformat(), item.product_id,
               item.product_name, item.quantity, str(item.net_amount)]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def register_profitability(app, db, Invoice, InvoiceItem, StockMovement, Expense):
    journal = db.metadata.tables.get('invoice_fifo_manual_cost')
    if journal is None:
        journal = db.Table('invoice_fifo_manual_cost', db.metadata,
            db.Column('id', db.Integer, primary_key=True),
            db.Column('invoice_item_id', db.Integer, db.ForeignKey('invoice_item.id'), nullable=False, index=True),
            db.Column('item_signature', db.String(64), nullable=False),
            db.Column('quantity', db.Integer, nullable=False),
            db.Column('amount', db.Numeric(18, 2), nullable=False),
            db.Column('reason', db.String(500), nullable=False),
            db.Column('actor', db.String(80), nullable=False),
            db.Column('created_at', db.DateTime, nullable=False))

    def load(end):
        documents = Invoice.query.options(selectinload(Invoice.items), joinedload(Invoice.customer)).filter(
            Invoice.invoice_date <= end).order_by(Invoice.invoice_date, Invoice.id).all()
        items = {i.id: i for doc in documents for i in doc.items}
        inspector = inspect(db.session.connection())
        manual = {}
        if inspector.has_table(journal.name):
            for row in db.session.execute(select(journal).order_by(journal.c.id)).mappings():
                item = items.get(row['invoice_item_id'])
                if item and row['item_signature'] == signature(item):
                    manual[item.id] = dict(row)
        links = {}
        link = db.metadata.tables.get('invoice_return_line')
        if link is not None and inspector.has_table(link.name):
            links = dict(db.session.execute(select(link.c.return_item_id, link.c.source_item_id)).all())
        events, linked_movements = [], set()
        for doc in documents:
            purchase_totals = {item.id: cents(item.net_amount) for item in doc.items}
            if doc.invoice_type == 'Satın Alma' and doc.items:
                # Preserve the supplier's header net total, including its rounding
                # difference from line totals, by assigning the residual once.
                residual = cents(doc.net_amount) - sum(purchase_totals.values(), ZERO)
                eligible = [item for item in doc.items if item.quantity > 0]
                if eligible:
                    purchase_totals[eligible[-1].id] += residual
            for item in doc.items:
                if item.stock_movement_id:
                    linked_movements.add(item.stock_movement_id)
                key = ('product', item.product_id) if item.product_id else ('unlinked', item.id)
                direction = {'Satın Alma': 'in', 'Satış': 'out', 'Satış İadesi': 'return'}.get(doc.invoice_type)
                if not direction:
                    continue
                events.append(dict(id=item.id, date=doc.invoice_date,
                    priority={'in': 0, 'out': 1, 'return': 2}[direction], key=key,
                    quantity=item.quantity, direction=direction,
                    unit_cost=purchase_totals[item.id]/item.quantity if item.quantity and direction=='in' else None,
                    total_cost=purchase_totals[item.id], source_id=links.get(item.id), manual=manual.get(item.id)))
        # Opening/count/manual movements have quantities but no historical cost.
        # Include them as unknown layers; never use today's product-card price.
        for movement in StockMovement.query.filter(StockMovement.movement_date <= end).all():
            if movement.id in linked_movements:
                continue  # The invoice and its movement describe one transaction.
            incoming = movement.movement_type in {'Açılış Stoğu', 'Stok Girişi', 'Sayım Artışı'}
            events.append(dict(id=-movement.id, date=movement.movement_date, priority=0 if incoming else 1,
                key=('product', movement.product_id), quantity=movement.quantity,
                direction='in' if incoming else 'out', unit_cost=None))
        results = calculate_fifo(events)
        # The engine allocates and reverses cents cumulatively per FIFO layer.
        for result in results.values():
            result['cost'] = cents(result['cost'])
        return documents, items, results, manual

    app.extensions['invoice_fifo'] = dict(load=load, journal=journal)

    @app.route('/kar-zarar', methods=['GET', 'POST'])
    def profitability():
        period = request.values.get('period', 'month')
        if period not in {'day', 'month', 'year'}:
            period = 'month'
        try:
            selected = date.fromisoformat(request.values.get('date', ''))
        except ValueError:
            selected = date.today()
        if period == 'day':
            start = end = selected; title = selected.strftime('%d.%m.%Y')
        elif period == 'year':
            start, end = date(selected.year, 1, 1), date(selected.year, 12, 31); title = str(selected.year)
        else:
            start = date(selected.year, selected.month, 1)
            end = date(selected.year, selected.month, calendar.monthrange(selected.year, selected.month)[1])
            title = start.strftime('%m.%Y')
        signer = URLSafeTimedSerializer(app.secret_key, salt='invoice-fifo-manual-v1')
        documents, items, results, manual = load(end)
        if request.method == 'POST':
            try:
                token = signer.loads(request.form.get('confirmation', ''), max_age=1800)
                item = items.get(token['item_id'])
                if not item or item.invoice.invoice_type != 'Satış' or token['signature'] != signature(item):
                    raise ValueError('Fatura değişti. Sayfayı yenileyin.')
                result = results[item.id]
                qty = result.get('raw_missing', result['missing_quantity'])
                if not qty or str(qty) != token['quantity']:
                    raise ValueError('FIFO dağılımı değişti. Sayfayı yenileyip maliyeti tekrar kontrol edin.')
                current = manual.get(item.id)
                if token['revision'] != (current['id'] if current else None):
                    raise ValueError('Maliyet başka bir işlemde değişti. Sayfayı yenileyin.')
                raw = request.form.get('amount', '').strip().replace(' ', '')
                if ',' in raw:
                    raw = raw.replace('.', '').replace(',', '.')
                amount = Decimal(raw)
                if not amount.is_finite() or amount < 0 or amount > Decimal('9999999999999999.99') or amount != cents(amount):
                    raise ValueError('KDV hariç toplam maliyeti en fazla iki ondalıkla girin.')
                reason = request.form.get('reason', '').strip()
                if not reason or len(reason) > 500:
                    raise ValueError('Maliyet için kısa bir açıklama yazın (en fazla 500 karakter).')
                if db.engine.dialect.name == 'postgresql':
                    db.session.execute(text('SELECT pg_advisory_xact_lock(72109345)'))
                journal.create(db.session.connection(), checkfirst=True)
                # Guard concurrent submissions after taking the database lock.
                last = db.session.execute(select(journal.c.id).where(
                    journal.c.invoice_item_id == item.id,
                    journal.c.item_signature == signature(item)).order_by(journal.c.id.desc()).limit(1)).scalar()
                if last != token['revision']:
                    raise ValueError('Maliyet başka bir işlemde değişti. Sayfayı yenileyin.')
                db.session.execute(journal.insert().values(invoice_item_id=item.id,
                    item_signature=signature(item), quantity=int(qty), amount=amount, reason=reason,
                    actor=str(session.get('web_user_id') or 'owner'), created_at=datetime.utcnow()))
                db.session.commit()
                flash('Eksik adedin maliyeti kaydedildi. Stok ve fatura tutarları değişmedi.', 'success')
                return redirect(url_for('profitability', period=period, date=selected.isoformat()))
            except (BadSignature, KeyError, InvalidOperation):
                db.session.rollback(); flash('Maliyet kaydedilemedi. Sayfayı yenileyip tutarı kontrol edin.', 'error')
            except ValueError as exc:
                db.session.rollback(); flash(str(exc), 'error')
            documents, items, results, manual = load(end)
        rows, missing, adjustments = [], [], []
        for doc in documents:
            if not start <= doc.invoice_date <= end or doc.invoice_type not in {'Satış', 'Satış İadesi'}:
                continue
            sign = -1 if doc.invoice_type == 'Satış İadesi' else 1
            cost = sum((results[i.id]['cost'] for i in doc.items), ZERO)
            unknown = sum((results[i.id]['missing_quantity'] for i in doc.items), ZERO)
            row = dict(invoice=doc, revenue=sign*cents(doc.net_amount), cost=cost, missing=unknown)
            row['profit'] = row['revenue'] - cost
            rows.append(row)
            for line_no, item in enumerate(doc.items, 1):
                result = results[item.id]
                if result['missing_quantity']:
                    missing.append(dict(item=item, line_no=line_no, quantity=result['missing_quantity'],
                                        editable=doc.invoice_type=='Satış', source=items.get(results[item.id].get('source_id'))))
                if doc.invoice_type=='Satış' and result.get('raw_missing'):
                    current = manual.get(item.id)
                    adjustments.append(dict(item=item, line_no=line_no, quantity=result['raw_missing'],
                        applied=result['manual_applied'], manual=current,
                        confirmation=signer.dumps(dict(item_id=item.id, signature=signature(item),
                            quantity=str(result['raw_missing']), revision=current['id'] if current else None))))
        revenue = sum((r['revenue'] for r in rows), ZERO)
        cost = sum((r['cost'] for r in rows), ZERO)
        expenses = Expense.query.filter(Expense.expense_date.between(start, end)).all()
        expenses_total = sum((cents(e.amount) for e in expenses), ZERO)
        gross = revenue-cost
        buckets = {}
        for r in rows:
            day = r['invoice'].invoice_date
            key = day.replace(day=1) if period == 'year' else day
            bucket = buckets.setdefault(key, dict(date=key, revenue=ZERO, cost=ZERO, expense=ZERO, missing=False))
            bucket['revenue'] += r['revenue']; bucket['cost'] += r['cost']
            bucket['missing'] = bucket['missing'] or bool(r['missing'])
        for expense in expenses:
            day = expense.expense_date
            key = day.replace(day=1) if period == 'year' else day
            bucket = buckets.setdefault(key, dict(date=key, revenue=ZERO, cost=ZERO, expense=ZERO, missing=False))
            bucket['expense'] += cents(expense.amount)
        for bucket in buckets.values():
            bucket['net'] = bucket['revenue'] - bucket['cost'] - bucket['expense']
        return render_template('profitability.html', period=period, selected=selected, title=title,
            buckets=sorted(buckets.values(), key=lambda b:b["date"]), rows=sorted(rows, key=lambda r:(r['invoice'].invoice_date,r['invoice'].id), reverse=True),
            revenue=revenue, cost=cost, gross_profit=gross, expenses_total=expenses_total,
            net_profit=gross-expenses_total, missing=missing, adjustments=adjustments)
