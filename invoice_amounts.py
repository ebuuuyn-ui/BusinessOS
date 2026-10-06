"""Exact source invoice amounts; additive preparation for existing databases."""
from decimal import Decimal
from sqlalchemy import inspect, text


def schema_ready(connection):
    inspector = inspect(connection)
    for table in ('invoice', 'invoice_item'):
        columns = {c['name']: c for c in inspector.get_columns(table)}
        if not {'source_net_amount', 'source_vat_amount'} <= columns.keys():
            return False
        if table == 'invoice_item' and connection.dialect.name == 'postgresql':
            if columns['unit_price']['type'].scale < 8:
                return False
    return True


def prepare_schema(engine):
    with engine.begin() as connection:
        if connection.dialect.name == 'postgresql':
            connection.execute(text("SET LOCAL lock_timeout = '5s'"))
            connection.execute(text("SET LOCAL statement_timeout = '30s'"))
        for table in ('invoice', 'invoice_item'):
            columns = {c['name']: c for c in inspect(connection).get_columns(table)}
            for name in ('source_net_amount', 'source_vat_amount'):
                if name not in columns:
                    connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} NUMERIC(18, 2)'))
            if table == 'invoice_item' and connection.dialect.name == 'postgresql' and columns['unit_price']['type'].scale < 8:
                connection.execute(text('ALTER TABLE invoice_item ALTER COLUMN unit_price TYPE NUMERIC(20, 8)'))


def precise_money(value):
    whole, fraction = f'{Decimal(str(value or 0)):,.8f}'.split('.')
    return whole.replace(',', '.') + ',' + fraction.rstrip('0').ljust(2, '0')


def register_schema(app, db):
    from flask import abort, g, request, render_template, redirect, url_for

    @app.route('/yonetim/fatura-tutar-hassasiyeti', methods=['GET', 'POST'])
    def invoice_amount_schema():
        if not app.config.get('WEB_AUTH_ENABLED'):
            abort(404)
        if not getattr(g, 'web_is_owner', False):
            abort(403)
        if request.method == 'POST':
            if request.form.get('operation') != 'prepare-invoice-amounts':
                abort(400)
            prepare_schema(db.engine)
            return redirect(url_for('invoice_amount_schema'))
        with db.engine.connect() as connection:
            ready = schema_ready(connection)
        return render_template('invoice_amount_schema.html', ready=ready)
