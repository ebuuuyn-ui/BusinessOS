"""Customer returns reuse invoice accounting and stock movement semantics."""
import hashlib
import json
import uuid
from datetime import datetime, date
from zoneinfo import ZoneInfo
from flask import request, render_template, redirect, url_for, flash, abort
from sqlalchemy import inspect, select, func, text
from sqlalchemy.exc import IntegrityError
from itsdangerous import URLSafeTimedSerializer, BadSignature

RETURN_TYPE = 'Satış İadesi'


def register_invoice_returns(app, db, Invoice, InvoiceItem, StockMovement):
    # A separate additive table avoids changing existing live invoice columns.
    link = db.metadata.tables.get('invoice_return_line')
    if link is None:
        link = db.Table('invoice_return_line', db.metadata,
        db.Column('return_item_id', db.Integer, db.ForeignKey('invoice_item.id'), primary_key=True),
        db.Column('source_item_id', db.Integer, db.ForeignKey('invoice_item.id'), nullable=False, index=True),
        extend_existing=True)

    def ready():
        return inspect(db.session.connection()).has_table(link.name)

    def has_returns(invoice):
        if not ready():
            return False
        return db.session.execute(select(link.c.return_item_id).where(
            link.c.source_item_id.in_([i.id for i in invoice.items])).limit(1)).first() is not None

    def related(invoice):
        if not ready():
            return [], None
        ids = {i.id for i in invoice.items}
        rows = db.session.execute(select(link.c.return_item_id, link.c.source_item_id).where(
            link.c.return_item_id.in_(ids) | link.c.source_item_id.in_(ids))).all()
        returns, source = {}, None
        for returned_id, source_id in rows:
            if source_id in ids:
                item = db.session.get(InvoiceItem, returned_id)
                if item: returns[item.invoice_id] = item.invoice
            if returned_id in ids:
                item = db.session.get(InvoiceItem, source_id)
                if item: source = item.invoice
        return list(returns.values()), source

    def remove_links(invoice):
        if ready():
            db.session.execute(link.delete().where(link.c.return_item_id.in_([i.id for i in invoice.items])))

    app.extensions['invoice_returns'] = dict(has_returns=has_returns, related=related, remove_links=remove_links)

    def quantities(invoice):
        if not ready(): return {}
        return dict(db.session.execute(select(link.c.source_item_id, func.sum(InvoiceItem.quantity))
            .join(InvoiceItem, InvoiceItem.id == link.c.return_item_id)
            .where(link.c.source_item_id.in_([i.id for i in invoice.items]))
            .group_by(link.c.source_item_id)).all())

    def signature(invoice):
        return hashlib.sha256(json.dumps([invoice.customer_id, invoice.invoice_type,
            [[i.id, i.product_id, i.product_name, i.quantity, str(i.unit_price), str(i.discount_rate),
              str(i.vat_rate), i.vat_included, i.unit] for i in invoice.items]], ensure_ascii=False).encode()).hexdigest()

    @app.route('/faturalar/<int:invoice_id>/iade', methods=['GET', 'POST'])
    def invoice_return(invoice_id):
        source = db.get_or_404(Invoice, invoice_id)
        if source.invoice_type != 'Satış': abort(400)
        signer = URLSafeTimedSerializer(app.secret_key, salt='invoice-return-v1')
        today = datetime.now(ZoneInfo('Europe/Istanbul')).date()
        number = request.form.get('invoice_no', '').strip() or 'IA-' + today.strftime('%Y') + '-' + uuid.uuid4().hex[:10].upper()
        error = None
        if request.method == 'POST':
            try:
                if signer.loads(request.form.get('confirmation', ''), max_age=1800) != signature(source):
                    raise ValueError('Fatura değişti. Sayfayı yenileyip tekrar deneyin.')
                # Only this new table is provisioned, on explicit return submission.
                if not ready():
                    if db.engine.dialect.name == 'postgresql':
                        db.session.execute(text('SELECT pg_advisory_xact_lock(72109344)'))
                    link.create(db.session.connection(), checkfirst=True)
                if db.engine.dialect.name == 'postgresql':
                    db.session.execute(text('SELECT id FROM invoice WHERE id=:id FOR UPDATE'), {'id': source.id})
                    db.session.refresh(source)
                    if signer.loads(request.form.get('confirmation', ''), max_age=1800) != signature(source):
                        raise ValueError('Fatura değişti. Sayfayı yenileyip tekrar deneyin.')
                returned = quantities(source)
                chosen = []
                for item in source.items:
                    raw = request.form.get(f'quantity_{item.id}', '0').strip() or '0'
                    count = int(raw)
                    if count < 0 or count > item.quantity - returned.get(item.id, 0):
                        raise ValueError(f'{item.product_name}: iade adedi kalan miktarı aşamaz.')
                    if count: chosen.append((item, count))
                if not chosen: raise ValueError('En az bir ürün için iade adedi girin.')
                if len(number) > 80: raise ValueError('Belge numarası en fazla 80 karakter olabilir.')
                return_date = date.fromisoformat(request.form.get('invoice_date', ''))
                if return_date < source.invoice_date: raise ValueError('İade tarihi fatura tarihinden önce olamaz.')
                if Invoice.query.filter_by(invoice_no=number).first(): raise ValueError('Bu belge numarası zaten kayıtlı. Yeni kayıt oluşturulmadı.')
                document = Invoice(invoice_no=number, invoice_type=RETURN_TYPE, customer_id=source.customer_id,
                    invoice_date=return_date, due_date=return_date,
                    notes=f'İade alınan fatura: {source.invoice_no}\n' + request.form.get('notes', '').strip())
                db.session.add(document)
                for original, count in chosen:
                    item = InvoiceItem(invoice=document, product_id=original.product_id, product_name=original.product_name,
                        quantity=count, unit=original.unit, unit_price=original.unit_price,
                        discount_rate=original.discount_rate, vat_rate=original.vat_rate, vat_included=original.vat_included)
                    db.session.add(item)
                    if original.product_id:
                        movement = StockMovement(product_id=original.product_id, movement_type='Stok Girişi', quantity=count,
                            movement_date=return_date, note=f'Satış iadesi · {number} · {source.invoice_no}')
                        db.session.add(movement); db.session.flush(); item.stock_movement_id = movement.id
                    db.session.flush()
                    db.session.execute(link.insert().values(return_item_id=item.id, source_item_id=original.id))
                db.session.commit()
                flash('İade kaydedildi. Stok girişi ve cari alacak işlendi.', 'success')
                return redirect(url_for('invoice_detail', invoice_id=document.id))
            except BadSignature:
                db.session.rollback(); error = 'Onay süresi dolmuş. Sayfayı yenileyin.'
            except ValueError as exc:
                db.session.rollback()
                error = str(exc) if not str(exc).startswith(('invalid literal', 'Invalid isoformat')) else 'Adet veya tarih geçersiz.'
            except IntegrityError:
                db.session.rollback(); error = 'Bu iade kaydedilemedi; belge numarası daha önce kullanılmış olabilir.'
        returned = quantities(source)
        return render_template('invoice_return.html', invoice=source, returned=returned, error=error,
            today=today.isoformat(), number=number, confirmation=signer.dumps(signature(source))), (400 if error else 200)
