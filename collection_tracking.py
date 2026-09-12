"""Customer summaries over the existing FIFO order allocation; no database writes."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP


def customer_summaries(items, today):
    groups = {}
    for item in items:
        group = groups.setdefault(item['customer'].id, {'customer': item['customer'], 'orders': []})
        group['orders'].append(item)
    for group in groups.values():
        orders = sorted(group['orders'], key=lambda i: (i['due_date'], i['order'].id))
        group['orders'] = orders
        opened = [i for i in orders if i['remaining'] > 0]
        group.update({key: sum((i[key] for i in orders), Decimal('0')) for key in ('amount', 'collected', 'remaining')})
        group['order_count'] = len(orders)
        group['open_count'] = len(opened)
        group['overdue_amount'] = sum((i['remaining'] for i in opened if i['due_date'] < today), Decimal('0'))
        group['due_soon_amount'] = sum((i['remaining'] for i in opened if today <= i['due_date'] <= today + timedelta(days=7)), Decimal('0'))
        group['oldest_due_date'] = min((i['due_date'] for i in opened), default=None)
        if group['remaining'] > 0:
            weighted = sum((i['remaining'] * (i['due_date'] - today).days for i in opened), Decimal('0')) / group['remaining']
            # Keep exact decimal for calculations; round only for display/date.
            group['average_days'] = weighted
            days = int(weighted.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            group['average_due_date'] = today + timedelta(days=days)
            group['average_label'] = f'{abs(days)} gün geçti' if days < 0 else 'Bugün' if days == 0 else f'{days} gün kaldı'
        else:
            group.update(average_days=None, average_due_date=None, average_label='—')
        if group['overdue_amount'] > 0:
            state, label = 'overdue', 'Gecikme var'
        elif not opened:
            state, label = 'paid', 'Tahsil edildi'
        elif group['oldest_due_date'] == today:
            state, label = 'due_today', 'Bugün vadeli'
        elif group['due_soon_amount'] > 0:
            state, label = 'due_soon', '7 gün içinde vadeli'
        else:
            state, label = 'open', 'Açık tahsilat'
        group.update(state=state, state_label=label)
    return sorted(groups.values(), key=lambda g: (g['overdue_amount'] <= 0, g['oldest_due_date'] or today + timedelta(days=365000), g['customer'].id))


def filter_customers(groups, state, query, normalize):
    def matches(g):
        if state == 'overdue' and g['overdue_amount'] <= 0: return False
        if state == 'due_soon' and g['due_soon_amount'] <= 0: return False
        if state == 'paid' and g['remaining'] > 0: return False
        if state == 'open' and g['remaining'] <= 0: return False
        q = normalize(query)
        return not q or q in normalize(g['customer'].name) or any(q in normalize(i['order'].order_no) for i in g['orders'])
    # Filter whole customers, never the orders used to calculate their balances.
    return [g for g in groups if matches(g)]
