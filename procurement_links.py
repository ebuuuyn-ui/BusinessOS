"""Recover former sales row identities from immutable deletion history, read-only."""
import json
from types import SimpleNamespace
from sqlalchemy import inspect, text


def historical_sale_items(db, order_id, purchase_items):
    missing = {item.source_order_item_id for item in purchase_items if item.source_order_item_id}
    if not missing:
        return {}
    connection = db.session.connection()
    if not inspect(connection).has_table('business_audit_event'):
        return {}
    predicate = "before_data->>'order_id' = :order_id" if connection.dialect.name == 'postgresql' else "CAST(json_extract(before_data, '$.order_id') AS TEXT) = :order_id"
    rows = db.session.execute(text("SELECT before_data FROM business_audit_event WHERE table_name='order_item' AND operation='DELETE' AND " + predicate + " ORDER BY id"), {'order_id': str(order_id)})
    result = {}
    for row in rows:
        data = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if data and data.get('id') in missing:
            fields = {key: data.get(key) for key in ('product_id', 'product_name', 'variant', 'detail_2', 'detail_3', 'unit')}
            result[data['id']] = SimpleNamespace(**fields)
    return result
