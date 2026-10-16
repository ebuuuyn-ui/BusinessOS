"""Atomic, paired customer balance transfers; no cash or bank posting."""
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from flask import request, render_template, redirect, url_for, flash, abort
from itsdangerous import URLSafeTimedSerializer, BadSignature


def register_customer_transfers(app, db, Customer, Transaction, balances, backup):
    kinds = ('Cari Virman Çıkış', 'Cari Virman Giriş')
    signer = URLSafeTimedSerializer(app.secret_key, salt='customer-balance-transfer-v1')

    def pair(reference):
        return Transaction.query.filter(Transaction.reference_no == reference, Transaction.transaction_type.in_(kinds)).order_by(Transaction.id).with_for_update().all()

    def lock_customers(ids):
        rows = Customer.query.filter(Customer.id.in_(ids)).order_by(Customer.id).with_for_update().all()
        if len(rows) != 2:
            raise ValueError('İki farklı ve geçerli cari seçin.')
        return {row.id: row for row in rows}

    @app.route('/musteriler/virman', methods=['GET', 'POST'])
    def customer_transfers():
        preview = None
        token = None
        if request.method == 'POST':
            try:
                if request.form.get('confirmation'):
                    data = signer.loads(request.form['confirmation'], max_age=1800)
                else:
                    raw = request.form.get('amount', '').strip().replace(' ', '')
                    if ',' in raw:
                        raw = raw.replace('.', '').replace(',', '.')
                    elif '.' in raw and all(len(part) == 3 for part in raw.split('.')[1:]):
                        raw = raw.replace('.', '')
                    amount = Decimal(raw)
                    if not amount.is_finite() or amount <= 0 or amount > Decimal('999999999999.99') or amount != amount.quantize(Decimal('.01')):
                        raise ValueError('Tutar sıfırdan büyük ve en fazla iki ondalık basamaklı olmalıdır.')
                    data = dict(source=int(request.form.get('source', '')), target=int(request.form.get('target', '')), amount=str(amount), day=date.fromisoformat(request.form.get('day', '')).isoformat(), kind=request.form.get('kind'), note=request.form.get('note', '').strip()[:100], reference='CV-'+uuid4().hex)
                if data['kind'] not in ('credit', 'debit') or data['source'] == data['target']:
                    raise ValueError('İki farklı cari ve aktarılacak bakiye türünü seçin.')
                customers = lock_customers([data['source'], data['target']])
                if Transaction.query.filter_by(reference_no=data['reference']).first():
                    flash('Bu virman daha önce kaydedildi; tekrar oluşturulmadı.', 'success')
                    return redirect(url_for('customer_transfers'))
                current = balances([data['source'], data['target']])
                amount = Decimal(data['amount'])
                sign = Decimal(1) if data['kind'] == 'credit' else Decimal(-1)
                available = -current[data['source']] * sign
                if amount > available:
                    raise ValueError('Kaynak carinin seçilen türdeki mevcut bakiyesi bu tutar için yeterli değil.')
                after = {data['source']: current[data['source']] + sign*amount, data['target']: current[data['target']] - sign*amount}
                if request.form.get('confirmation') and data.get('before') != {str(k):str(v) for k,v in current.items()}:
                    raise ValueError('Bakiyeler değişti; yeniden kontrol edin.')
                if request.form.get('confirmation'):
                    backup()
                    entries = []
                    for cid, other, movement, delta in [(data['source'], data['target'], kinds[0], sign*amount), (data['target'], data['source'], kinds[1], -sign*amount)]:
                        entry = Transaction(customer_id=cid, transaction_date=date.fromisoformat(data['day']), transaction_type=movement, reference_no=data['reference'], description=(movement+' · '+customers[other].name+' · '+data['note'])[:240], debit=max(delta, 0), credit=max(-delta, 0))
                        db.session.add(entry); entries.append(entry)
                    db.session.flush()
                    entries[0].linked_transaction_id = entries[1].id
                    entries[1].linked_transaction_id = entries[0].id
                    db.session.commit()
                    flash('Virman iki carinin hesabına birlikte işlendi.', 'success')
                    return redirect(url_for('customer_transfers'))
                preview = dict(data=data, customers=customers, before=current, after=after)
                data['before'] = {str(k):str(v) for k,v in current.items()}
                token = signer.dumps(data)
            except (ValueError, InvalidOperation, BadSignature, KeyError):
                db.session.rollback()
                flash('Virman kaydedilmedi. Carileri, bakiye türünü, tutarı ve tarihi kontrol edin. Kaynak bakiyesi yeterli olmalıdır; onay ekranı 30 dakika geçerlidir.', 'error')
        customers = Customer.query.order_by(Customer.name).all()
        recent = Transaction.query.filter(Transaction.transaction_type.in_((kinds[0], kinds[0]+' İptal'))).order_by(Transaction.created_at.desc(), Transaction.id.desc()).limit(100).all()
        customer_balances = balances([c.id for c in customers])
        return render_template('customer_transfers.html', customers=customers, balances=customer_balances, recent=recent, preview=preview, confirmation=token, today=date.today().isoformat())

    @app.post('/musteriler/virman/<int:transaction_id>/iptal')
    def cancel_customer_transfer(transaction_id):
        initial = db.get_or_404(Transaction, transaction_id)
        if initial.transaction_type != kinds[0]:
            abort(400)
        partner = db.session.get(Transaction, initial.linked_transaction_id)
        if not partner:
            abort(409)
        lock_customers([initial.customer_id, partner.customer_id])
        entries = pair(initial.reference_no)
        if len(entries) != 2 or entries[0].linked_transaction_id != entries[1].id or entries[1].linked_transaction_id != entries[0].id or sum((e.debit-e.credit for e in entries), Decimal(0)) != 0:
            abort(409)
        backup()
        for entry in entries:
            entry.description = ("İptal edilen virman (" + str(max(entry.debit, entry.credit)) + " TL) · " + entry.description)[:240]
            entry.transaction_type += " İptal"
            entry.debit = entry.credit = Decimal(0)
        db.session.commit()
        flash('Virman iptal edildi; iki carinin bakiyesi birlikte geri alındı.', 'success')
        return redirect(url_for('customer_transfers'))
