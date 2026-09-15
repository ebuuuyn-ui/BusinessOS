"""Read-only invoice maturity allocation, reconciled to the complete customer ledger."""
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

ZERO = Decimal('0')


def build_maturity_groups(customers, invoices, transactions, today, purchase=False):
    """Net the same signed entries as the account balance; allocate offsets FIFO.

    No stored invoice-payment matching exists. Offsets reduce the oldest charge
    by document date and ID. Manual debit/credit exposure has no inferred due date.
    """
    direction = -1 if purchase else 1
    grouped = {c.id: dict(customer=c, charges=[], offsets=ZERO, signed_balance=ZERO) for c in customers}
    for invoice in invoices:
        g = grouped[invoice.customer_id]
        signed = invoice.total_amount * (1 if invoice.invoice_type == 'Satış' else -1)
        g['signed_balance'] += signed
        amount = signed * direction
        if amount > 0:
            g['charges'].append(dict(invoice=invoice, reference=invoice.invoice_no,
                document_date=invoice.invoice_date, due_date=invoice.due_date,
                description='Satın Alma Faturası' if purchase else 'Satış Faturası',
                amount=amount, key=(invoice.invoice_date, 0, invoice.id)))
        else:
            g['offsets'] -= amount
    for tx in transactions:
        g = grouped[tx.customer_id]
        signed = (tx.debit or ZERO) - (tx.credit or ZERO)
        g['signed_balance'] += signed
        amount = signed * direction
        if amount > 0:
            g['charges'].append(dict(invoice=None, reference=tx.reference_no or tx.transaction_type,
                document_date=tx.transaction_date, due_date=None, description=tx.transaction_type,
                amount=amount, key=(tx.transaction_date, 1, tx.id)))
        else:
            g['offsets'] -= amount
    result = []
    for g in grouped.values():
        net = g['signed_balance'] * direction
        # A negative balance belongs to the opposite tab; zero with history is closed.
        if net < 0 or not g['charges']:
            continue
        available = g['offsets']
        entries = sorted(g.pop('charges'), key=lambda i: i['key'])
        for item in entries:
            item['collected'] = min(available, item['amount'])
            available -= item['collected']
            item['remaining'] = item['amount'] - item['collected']
            due = item['due_date']
            days = (due - today).days if due else None
            if not item['remaining']:
                state, label = 'paid', 'Kapandı'
            elif item['invoice'] is None:
                state, label = 'other', 'Fatura dışı bakiye'
            elif due is None:
                state, label = 'undated', 'Vadesi belirtilmemiş'
            elif days < 0:
                state, label = 'overdue', f'{-days} gün gecikti'
            elif days == 0:
                state, label = 'due_today', 'Bugün vadeli'
            elif days <= 7:
                state, label = 'due_soon', f'{days} gün kaldı'
            else:
                state, label = 'open', f'{days} gün kaldı'
            item.update(state=state, state_label=label)
        opened = [i for i in entries if i['remaining'] > 0]
        dated = [i for i in opened if i['due_date']]
        dated_amount = sum((i['remaining'] for i in dated), ZERO)
        undated = sum((i['remaining'] for i in opened if i['invoice'] is not None and not i['due_date']), ZERO)
        other = sum((i['remaining'] for i in opened if i['invoice'] is None), ZERO)
        overdue = sum((i['remaining'] for i in dated if i['due_date'] < today), ZERO)
        soon = sum((i['remaining'] for i in dated if today <= i['due_date'] <= today + timedelta(days=7)), ZERO)
        average = sum((i['remaining'] * (i['due_date'] - today).days for i in dated), ZERO) / dated_amount if dated_amount else None
        days = int(average.quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if average is not None else None
        state = 'paid' if not opened else 'overdue' if overdue else 'due_soon' if soon else 'undated' if undated else 'other' if other and not dated else 'open'
        labels = dict(paid='Kapandı', overdue='Gecikme var', due_soon='7 gün içinde vadeli', undated='Fatura vadesi eksik', other='Fatura dışı bakiye', open='Açık ödeme' if purchase else 'Açık alacak')
        g.update(entries=entries, entry_count=len(entries), open_count=len(opened),
            amount=sum((i['amount'] for i in entries), ZERO), collected=sum((i['collected'] for i in entries), ZERO),
            remaining=net, overdue_amount=overdue, due_soon_amount=soon, undated_amount=undated, other_amount=other,
            average_days=average, average_due_date=today+timedelta(days=days) if days is not None else None,
            average_label=('Vadesi belirtilmemiş' if undated else '—') if days is None else f'{-days} gün geçti' if days < 0 else 'Bugün' if days == 0 else f'{days} gün kaldı',
            oldest_due_date=min((i['due_date'] for i in dated), default=None), state=state, state_label=labels[state])
        assert sum((i['remaining'] for i in entries), ZERO) == net
        result.append(g)
    return sorted(result, key=lambda g: (not g['overdue_amount'], g['oldest_due_date'] or date.max, g['customer'].id))


def filter_groups(groups, state, query, normalize):
    q = normalize(query)
    def matches(g):
        if state == 'open' and not g['remaining']: return False
        if state == 'paid' and g['remaining']: return False
        if state == 'overdue' and not g['overdue_amount']: return False
        if state == 'due_soon' and not g['due_soon_amount']: return False
        if state == 'undated' and not g['undated_amount']: return False
        if state == 'other' and not g['other_amount']: return False
        return not q or q in normalize(g['customer'].name) or q in normalize(g['customer'].code or '') or any(q in normalize(i['reference']) for i in g['entries'])
    return [g for g in groups if matches(g)]
