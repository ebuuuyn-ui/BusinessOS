"""Decimal FIFO replay. Unknown cost layers consume quantity but never invent a price."""
from collections import defaultdict, deque
from decimal import Decimal, ROUND_HALF_UP


def calculate_fifo(events):
    """Events: date, priority, id, key, quantity, direction, unit_cost.

    Inputs precede outputs on the same date (documents have no movement time).
    Uncovered outputs remain uncovered: later purchases cannot retroactively cost them.
    """
    layers = defaultdict(deque)
    results = {}
    returned = defaultdict(lambda: Decimal("0"))
    return_value = defaultdict(lambda: Decimal("0"))
    return_cents = defaultdict(lambda: Decimal("0"))
    for event in sorted(events, key=lambda e: (e['date'], e['priority'], e['id'])):
        quantity = Decimal(str(event['quantity']))
        if quantity <= 0:
            results[event['id']] = dict(cost=Decimal('0'), missing_quantity=Decimal('0'),
                raw_missing=Decimal('0'), quantity=quantity, allocations=[], manual_applied=False)
            continue
        queue = layers[event['key']]
        if event['direction'] == 'return':
            source = results.get(event.get('source_id'), {})
            skip = returned[event.get('source_id')]
            remaining, cost, missing = quantity, Decimal('0'), Decimal('0')
            for available, price in source.get('allocations', []):
                ignored = min(skip, available)
                skip -= ignored
                available -= ignored
                used = min(remaining, available)
                if used:
                    if price is None:
                        missing += used
                        restored_price = None
                    else:
                        source_id = event.get('source_id')
                        return_value[source_id] += used * price
                        cumulative = return_value[source_id].quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                        restored = cumulative - return_cents[source_id]
                        return_cents[source_id] = cumulative
                        cost += restored
                        restored_price = restored / used
                    queue.append([used, restored_price, Decimal("0"), Decimal("0")])
                    remaining -= used
                if not remaining:
                    break
            if remaining:
                queue.append([remaining, None, Decimal("0"), Decimal("0")])
                missing += remaining
            returned[event.get('source_id')] += quantity
            results[event['id']] = dict(cost=-cost, missing_quantity=missing,
                                       quantity=quantity, allocations=[], source_id=event.get('source_id'))
            continue
        if event['direction'] == 'in':
            price = event.get('unit_cost')
            if price is not None:
                total = Decimal(str(event.get('total_cost', quantity * price))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                price = total / quantity
            queue.append([quantity, price, Decimal('0'), Decimal('0')])
            continue
        remaining, cost, missing = quantity, Decimal('0'), Decimal('0')
        allocations = []
        while remaining and queue:
            layer = queue[0]
            used = min(remaining, layer[0])
            if layer[1] is None:
                missing += used
            else:
                layer[2] += used
                cumulative = (layer[2] * layer[1]).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                allocated = cumulative - layer[3]
                layer[3] = cumulative
                cost += allocated
            allocations.append((used, allocated / used if layer[1] is not None else None))
            layer[0] -= used
            remaining -= used
            if not layer[0]:
                queue.popleft()
        missing += remaining
        if remaining:
            allocations.append((remaining, None))
        raw_missing = missing
        manual = event.get('manual')
        applied = bool(manual and Decimal(str(manual['quantity'])) == missing and missing)
        if applied:
            amount = Decimal(str(manual['amount']))
            price = amount / missing
            allocations = [(used, price if old_price is None else old_price)
                           for used, old_price in allocations]
            cost += amount
            missing = Decimal('0')
        results[event['id']] = dict(cost=cost, missing_quantity=missing,
                                    raw_missing=raw_missing, manual_applied=applied,
                                    quantity=quantity, allocations=allocations)
    return results
