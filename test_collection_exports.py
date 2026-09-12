import os
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_data = tempfile.TemporaryDirectory(prefix='businessos-collection-test-')
os.environ['BUSINESSOS_DATA_DIR'] = _data.name
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
from app import app
from desktop import EXPORT_PATH
from openpyxl import load_workbook


def fixtures():
    today = date.today()
    result = []
    for index, (state, days, collected) in enumerate([('overdue', -5, '12.34'), ('due_today', 0, '0'), ('due_soon', 5, '10'), ('open', 20, '0'), ('paid', -8, '100')]):
        result.append(dict(customer=SimpleNamespace(name='=İNCİ MOBİLYA <A> & TİCARET' if index == 0 else f'Cari {index}', id=index+1), order=SimpleNamespace(order_no=f'SS-TEST-{index}', id=index+1), delivered_at=today+timedelta(days=days-30), due_date=today+timedelta(days=days), amount=Decimal('100'), collected=Decimal(collected), remaining=Decimal('100')-Decimal(collected), state=state, state_label='Tahsil edildi' if state == 'paid' else f'{abs(days)} gün gecikti' if days < 0 else 'Bugün vadesi doluyor' if days == 0 else f'{days} gün kaldı'))
    return result


class CollectionExportTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.items = fixtures()
        self.mock = patch('app.delivered_sales_collection_tracking', return_value=self.items)
        self.tracking_mock = self.mock.start()

    def tearDown(self):
        self.mock.stop()

    def workbook(self, **query):
        query.setdefault('view', 'details')
        response = self.client.get('/tahsilat-takibi/excel', query_string=query)
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response.headers['Content-Disposition'])
        return load_workbook(BytesIO(response.data)).active

    def test_state_filters_and_totals(self):
        for state, ids in [('open',[0,1,2,3]), ('overdue',[0]), ('due_soon',[1,2]), ('paid',[4]), ('all',[0,1,2,3,4]), ('invalid',[0,1,2,3])]:
            with self.subTest(state=state):
                ws = self.workbook(state=state)
                self.assertEqual([ws.cell(r,2).value for r in range(10,10+len(ids))], [self.items[i]['order'].order_no for i in ids])
                self.assertEqual(ws.cell(ws.max_row,7).value, float(sum((self.items[i]['remaining'] for i in ids), Decimal('0'))))
                self.assertEqual(ws['B7'].value, 377.66)
                self.assertEqual(ws['C10'].number_format,'dd.mm.yyyy')

    def test_search_and_safe_text(self):
        ws = self.workbook(state='all', q='inci')
        self.assertEqual(ws.max_row,11)
        self.assertEqual(ws['A10'].value, self.items[0]['customer'].name)
        self.assertEqual(ws['A10'].data_type,'s')
        ws = self.workbook(state='all', q='SS-TEST-4')
        self.assertEqual(ws['B10'].value,'SS-TEST-4')

    def test_empty_and_pdf_filters(self):
        ws = self.workbook(q='YOK')
        self.assertEqual(ws.cell(ws.max_row,7).value,0)
        for q in ('', 'inci', 'YOK'):
            response = self.client.get('/tahsilat-takibi/pdf',query_string={'q':q})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.mimetype,'application/pdf')
            self.assertTrue(response.data.startswith(b'%PDF'))

    def test_links_and_native_download_allowlist(self):
        html = self.client.get('/tahsilat-takibi?state=overdue&q=inci').get_data(as_text=True)
        self.assertIn('/tahsilat-takibi/excel?state=overdue&amp;q=inci',html)
        self.assertIn('/tahsilat-takibi/pdf?state=overdue&amp;q=inci',html)
        self.assertIn('Excel İndir',html)
        for suffix in ('excel','pdf'):
            self.assertTrue(EXPORT_PATH.fullmatch('/tahsilat-takibi/'+suffix))
        self.assertFalse(EXPORT_PATH.fullmatch('/tahsilat-takibi/delete'))
        self.assertEqual(self.client.get('/tahsilat-takibi/csv').status_code,404)

    def test_multipage_pdf(self):
        large = []
        for index in range(55):
            item = dict(self.items[index % 5])
            item['customer'] = SimpleNamespace(name='UZUN CARİ ÜNVANI İNŞAAT MOBİLYA NAKLİYE TURİZM TİCARET VE SANAYİ LİMİTED ŞİRKETİ '+str(index), id=index+1)
            item['order'] = SimpleNamespace(order_no=f'SS-TEST-{index:04d}',id=index+1)
            large.append(item)
        self.tracking_mock.return_value = large
        response = self.client.get('/tahsilat-takibi/pdf?state=all')
        self.assertEqual(response.status_code,200)
        if os.environ.get('COLLECTION_QA_DIR'):
            target = Path(os.environ['COLLECTION_QA_DIR'])
            target.mkdir(parents=True, exist_ok=True)
            (target/'tahsilat-test.pdf').write_bytes(response.data)


if __name__ == '__main__':
    unittest.main()
