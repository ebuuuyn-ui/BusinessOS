"""Book internal GR documents using the existing stock and customer ledger."""
import hashlib
import json
import secrets
from datetime import date, datetime
from zoneinfo import ZoneInfo

from flask import abort, flash, redirect, render_template, request, url_for
from itsdangerous import URLSafeTimedSerializer, BadSignature
from sqlalchemy import update, inspect, select


def fingerprint(order):
    data = [order.customer_id, order.order_type, order.status, order.notes,
            [[i.id, i.product_id, i.product_name, i.quantity, i.unit,
              str(i.unit_price), str(i.discount_rate), str(i.vat_rate), i.vat_included]
             for i in order.items]]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def register_gr(app, db, Order, Invoice, InvoiceItem, StockMovement, backup):
    @app.route('/siparisler/<int:order_id>/gr', methods=['GET', 'POST'])
    def order_gr(order_id):
        from invoice_order_allocation import for_order
        from invoice_ssh import allows_nonstock_rows
        from uyumsoft import order_attempts, month_after
        from app import GRSequence
        serializer = URLSafeTimedSerializer(app.secret_key, salt='gr-order')
        order = db.get_or_404(Order, order_id)
        today = datetime.now(ZoneInfo('Europe/Istanbul')).date()
        issued = request.form.get('invoice_date', today.isoformat())
        due = request.form.get('due_date', month_after(today).isoformat())
        error = None
        if request.method == 'POST':
            GRSequence.__table__.create(db.engine, checkfirst=True)
            # Serialize submissions for this order on PostgreSQL and SQLite.
            db.session.execute(update(Order).where(Order.id == order_id).values(updated_at=Order.updated_at))
            db.session.expire_all()
            order = db.get_or_404(Order, order_id)
            existing = Invoice.query.filter_by(order_id=order_id).first()
            if existing:
                db.session.rollback()
                flash('Bu sipariş zaten kaydedilmiş; ikinci kayıt oluşturulmadı.', 'warning')
                return redirect(url_for('invoice_detail', invoice_id=existing.id))
            try:
                token = serializer.loads(request.form.get('preview', ''), max_age=3600)
                if token != [order.id, fingerprint(order)]:
                    raise ValueError('Sipariş önizlemeden sonra değişti. Sayfayı yenileyin.')
                if order.order_type not in ('Satış', 'Satın Alma') or order.status == 'İptal Edildi':
                    raise ValueError('İptal edilmiş veya geçersiz türdeki sipariş kaydedilemez.')
                if for_order(order.id):
                    raise ValueError('Siparişin kalemleri başka bir faturaya bağlı. GR kaydı oluşturulmadı.')
                conn = db.session.connection()
                attempt = (db.session.execute(select(order_attempts.c.order_id).where(order_attempts.c.order_id == order.id)).first()
                           if inspect(conn).has_table(order_attempts.name) else None)
                if attempt:
                    raise ValueError('Bu sipariş için Uyumsoft aktarımı var. Çift kayıt oluşmaması için GR kaydı oluşturulmadı.')
                if not order.items or any(i.quantity <= 0 for i in order.items):
                    raise ValueError('Siparişte pozitif adetli en az bir kalem bulunmalıdır.')
                if any(i.product_id is None for i in order.items) and not allows_nonstock_rows(db.session.connection()):
                    raise ValueError('Stok kartı olmayan kalemler için SSH veritabanı uyumluluğunu tamamlayın.')
                invoice_date = date.fromisoformat(issued)
                due_date = date.fromisoformat(due) if due else None
                backup()
                invoice = Invoice(invoice_no='GR-PENDING-' + secrets.token_hex(16),
                    invoice_type=order.order_type, customer_id=order.customer_id, order_id=order.id,
                    invoice_date=invoice_date, due_date=due_date,
                    notes='GR · İç kayıt. Resmî fatura değildir; Uyumsoft’a gönderilmedi.' +
                          ('\n' + order.notes if order.notes else ''))
                db.session.add(invoice)
                db.session.flush()
                # Atomic counter is independent of invoice IDs, dates and deletions.
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert
                from sqlalchemy.dialects.postgresql import insert as postgres_insert
                insert = postgres_insert if db.engine.dialect.name == 'postgresql' else sqlite_insert
                db.session.execute(insert(GRSequence).values(id=1, last_number=1000).on_conflict_do_nothing(index_elements=['id']))
                while True:
                    number = db.session.execute(update(GRSequence).where(GRSequence.id == 1)
                        .values(last_number=GRSequence.last_number + 1).returning(GRSequence.last_number)).scalar_one()
                    code = f'GR{number}'
                    if not Invoice.query.filter_by(invoice_no=code).first():
                        invoice.invoice_no = code
                        break
                for source in order.items:
                    item = InvoiceItem(invoice=invoice, product_id=source.product_id,
                        product_name=source.product_name, quantity=source.quantity, unit=source.unit,
                        unit_price=source.unit_price, discount_rate=source.discount_rate,
                        vat_rate=source.vat_rate, vat_included=source.vat_included)
                    db.session.add(item)
                    if source.product_id is not None:
                        movement = StockMovement(product_id=source.product_id,
                            movement_type='Stok Çıkışı' if order.order_type == 'Satış' else 'Stok Girişi',
                            quantity=source.quantity, movement_date=invoice_date,
                            note=f'GR iç kayıt · {invoice.invoice_no}')
                        db.session.add(movement)
                        db.session.flush()
                        item.stock_movement_id = movement.id
                db.session.commit()
                flash(f'{invoice.invoice_no} GR kaydı oluşturuldu; cari ve stok hareketleri işlendi.', 'success')
                return redirect(url_for('invoice_detail', invoice_id=invoice.id))
            except BadSignature:
                error = 'Önizleme süresi dolmuş veya geçersiz. Sayfayı yenileyin.'
            except ValueError as exc:
                error = str(exc) if str(exc).endswith('.') else 'Geçerli bir kayıt ve vade tarihi seçin.'
            db.session.rollback()
        return render_template('order_gr.html', order=order, error=error,
            issued=issued, due=due, preview=serializer.dumps([order.id, fingerprint(order)])), (400 if error else 200)
