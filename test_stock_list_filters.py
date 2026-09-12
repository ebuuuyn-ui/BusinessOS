"""Render stock column filtering using isolated fixture data only."""
import os
import tempfile
import unittest
from unittest.mock import patch

_data = tempfile.TemporaryDirectory(prefix='businessos-stock-filter-')
os.environ['BUSINESSOS_DATA_DIR'] = _data.name
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
from app import app, db, Product
from stock_sorting import stock_text_sort_key


class StockListFilterTests(unittest.TestCase):
    def setUp(self):
        self.context = app.app_context()
        self.context.push()
        db.drop_all()
        db.create_all()
        db.session.add_all([Product(name=f'KOLTUK {n}', code=f'01.{n:03}') for n in range(35)])
        db.session.add(Product(name='IŞIK MASASI', code='03.010'))
        db.session.add(Product(name='ÖZEL <ÜRÜN> & KOLTUK', code=None))
        db.session.commit()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        self.context.pop()

    def test_every_row_is_available_for_instant_filtering(self):
        html = self.client.get('/urunler?code_prefix=01&product_name=koltuk').get_data(as_text=True)
        self.assertEqual(html.count('class="stock-card-link"'), 37)
        self.assertIn('01.034', html)
        self.assertIn('ÖZEL &lt;ÜRÜN&gt; &amp; KOLTUK', html)
        for text in ('id="stock-code-filter"', 'id="stock-name-filter"', 'stock-list-filter.js', 'stock-list-filter.css'):
            self.assertIn(text, html)
        self.assertEqual(Product.query.count(), 37)
        self.assertNotIn('id="stock-list-search"', html)
        self.assertNotIn('placeholder="Ürün adı, kod veya grup ara"', html)
        self.assertIn('stock_sort=code', html)
        self.assertIn('stock_sort=name', html)

    def test_text_sort_directions_and_unchanged_numeric_sort(self):
        for key in ('code', 'name', 'physical'):
            for direction in ('asc', 'desc'):
                with patch('app.render_template', return_value='ok') as render:
                    response = self.client.get(f'/urunler?stock_sort={key}&stock_direction={direction}')
                self.assertEqual(response.status_code, 200)
                context = render.call_args.kwargs
                rows = context['stock_rows']
                values = [stock_text_sort_key(getattr(row['product'], key)) if key in ('code', 'name') else row[key] for row in rows]
                self.assertEqual(values, sorted(values, reverse=direction == 'desc'))
                self.assertEqual(context['stock_sort'], key)
                self.assertEqual(context['stock_direction'], direction)

    def test_turkish_alphabet_and_numeric_code_order(self):
        names = ['ŞULE', 'ZENO', 'İPEK', 'ÖMER', 'CAN', 'ÇINAR', 'IŞIK', 'SUNA']
        self.assertEqual(sorted(names, key=stock_text_sort_key), ['CAN', 'ÇINAR', 'IŞIK', 'İPEK', 'ÖMER', 'SUNA', 'ŞULE', 'ZENO'])
        self.assertEqual(sorted(['01.10', '02.1', '01.2', None], key=stock_text_sort_key), [None, '01.2', '01.10', '02.1'])

    def test_general_search_and_sort_remain_supported(self):
        html = self.client.get('/urunler?q=isik&stock_sort=physical&stock_direction=asc').get_data(as_text=True)
        self.assertEqual(html.count('class="stock-card-link"'), 1)
        self.assertIn('IŞIK MASASI', html)
        self.assertIn('class="stock-sort"', html)

    def test_no_results_still_has_editable_column_filters(self):
        html = self.client.get('/urunler?q=not-found').get_data(as_text=True)
        self.assertEqual(html.count('class="stock-card-link"'), 0)
        self.assertIn('id="stock-code-filter"', html)
        self.assertIn('id="stock-name-filter"', html)
        self.assertIn('Aradığınız ürün bulunamadı.', html)

    def test_assets_are_served(self):
        for filename in ('stock-list-filter.js', 'stock-list-filter.css'):
            with self.client.get('/static/' + filename) as response:
                self.assertEqual(response.status_code, 200)


if __name__ == '__main__':
    unittest.main()
