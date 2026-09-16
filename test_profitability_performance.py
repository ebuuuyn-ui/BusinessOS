"""Cost parity and bounded query count, using only an isolated SQLite database."""
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from sqlalchemy import event

_data = tempfile.TemporaryDirectory(prefix='businessos-profitability-test-')
os.environ['BUSINESSOS_DATA_DIR'] = _data.name
os.environ['BUSINESSOS_BACKUP_DIR'] = _data.name
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
from app import (app, db, Customer, Product, Order, OrderItem, OrderHistory,
                 effective_sales_item_cost, order_realization_date, sales_item_costs_for_report)


class ProfitabilityPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context(); self.ctx.push()
        db.drop_all(); db.create_all()
        self.customer = Customer(name='Test cari')
        self.product = Product(name='İŞIK Masa')
        db.session.add_all([self.customer, self.product]); db.session.flush()
        self.serial = 0

    def tearDown(self):
        db.session.remove(); self.ctx.pop()

    def order(self, kind, when, price, *, status='Teslim Edildi', linked=True, quantity=1, history=True, discount=0, vat=0, included=False):
        self.serial += 1
        stamp = datetime.combine(when, datetime.min.time())
        o = Order(customer=self.customer, order_no=f'T-{self.serial}', order_type=kind,
                  status=status, order_date=when, created_at=stamp, updated_at=stamp)
        o.items.append(OrderItem(product=self.product if linked else None, product_name='İŞIK Masa',
                       quantity=quantity, unit_price=price, discount_rate=discount, vat_rate=vat, vat_included=included))
        if history:
            o.history.append(OrderHistory(status=status, created_at=stamp))
        db.session.add(o); db.session.flush()
        return o

    def test_legacy_cost_selection_matches_bulk(self):
        d = date(2026, 8, 1)
        self.order('Satın Alma', d, 120, discount=10, vat=20, included=True)
        # Same day: larger order ID wins; later same-order item ID wins too.
        same = self.order('Satın Alma', d, 150)
        same.items.append(OrderItem(product=self.product, product_name='İŞIK Masa',quantity=2,unit_price=180,vat_rate=0))
        self.order('Satın Alma', d+timedelta(days=5), 90, status='Bekliyor')
        self.order('Satın Alma', d+timedelta(days=10), 240, history=False, discount=20, vat=20, included=True)
        self.order('Satın Alma', d+timedelta(days=20), 999, quantity=0)
        self.order('Satın Alma', d+timedelta(days=40), 9999)
        sales = [self.order('Satış', d+timedelta(days=days), 500, linked=linked)
                 for days in (-1, 0, 7, 15, 25) for linked in (True, False)]
        # Earliest realized history, not latest update or invoice date, is authoritative.
        special = self.order('Satın Alma', d+timedelta(days=30), 195)
        special.history.append(OrderHistory(status='Sevk Edildi', created_at=datetime(2026,8,12)))
        db.session.commit()
        dates = {o.id: order_realization_date(o) for o in sales}
        expected = {i.id: effective_sales_item_cost(i, dates[o.id]) for o in sales for i in o.items}
        actual = sales_item_costs_for_report(sales, dates)
        self.assertEqual(actual, expected)
        self.assertEqual([actual[o.items[0].id] for o in sales[::2]], [0,180,180,195,0])

    def test_report_html_matches_legacy_cost_oracle_and_is_read_only(self):
        d = date.today()
        self.order('Satın Alma', d-timedelta(days=10), 120, discount=10, vat=20, included=True)
        self.order('Satış', d, 350, quantity=2)
        self.order('Satış', d-timedelta(days=40), 350, linked=False)
        db.session.commit()
        def oracle(sales, dates):
            return {i.id: effective_sales_item_cost(i, dates[o.id]) for o in sales for i in o.items}
        def dump():
            raw = db.engine.raw_connection()
            try: return '\n'.join(raw.iterdump())
            finally: raw.close()
        before = dump()
        client = app.test_client()
        for period in ('day','month','year'):
            url = '/kar-zarar?period='+period
            with patch('app.sales_item_costs_for_report', oracle):
                expected = client.get(url).data
            db.session.remove()
            with patch('app.effective_sales_item_cost', side_effect=AssertionError('Repeated cost lookup')):
                response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, expected)
        self.assertEqual(before, dump())

    def test_query_count_does_not_grow_per_sale(self):
        d = date.today()
        self.order('Satın Alma', d-timedelta(days=1), 100)
        counts = []
        for size in (20, 200):
            for _ in range(size-len(counts)*20):
                self.order('Satış', d, 200)
            db.session.commit(); db.session.remove()
            statements = []
            def record(conn,cursor,statement,parameters,context,executemany): statements.append(statement)
            event.listen(db.engine, 'before_cursor_execute', record)
            try:
                response = app.test_client().get('/kar-zarar')
            finally:
                event.remove(db.engine, 'before_cursor_execute', record)
            self.assertEqual(response.status_code, 200)
            counts.append(len(statements))
        self.assertEqual(counts[0], counts[1])
        self.assertLessEqual(counts[1], 8)

    def test_empty_period(self):
        self.assertEqual(sales_item_costs_for_report([], {}), {})
        self.assertEqual(app.test_client().get('/kar-zarar').status_code, 200)


if __name__ == '__main__': unittest.main()
