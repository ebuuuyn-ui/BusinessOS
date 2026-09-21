"""Zero-price, non-stock invoice rows and explicit PostgreSQL schema preparation."""
from decimal import Decimal
from sqlalchemy import inspect, text


def is_ssh_order_item(item):
    return item.product_id is None and (item.unit_price or Decimal('0')) == 0


def allows_nonstock_rows(connection):
    return next(c['nullable'] for c in inspect(connection).get_columns('invoice_item')
                if c['name'] == 'product_id')


def prepare_nonstock_rows(engine):
    """Relax one constraint; do not rebuild tables or change existing rows."""
    if engine.dialect.name != 'postgresql':
        raise ValueError('Bu işlem yalnızca web PostgreSQL veritabanı içindir.')
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("SET LOCAL statement_timeout = '15s'"))
        if not allows_nonstock_rows(connection):
            connection.execute(text('ALTER TABLE invoice_item ALTER COLUMN product_id DROP NOT NULL'))
