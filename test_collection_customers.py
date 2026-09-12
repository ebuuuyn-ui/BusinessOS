import os
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_data=tempfile.TemporaryDirectory(prefix='businessos-customer-collection-')
os.environ['BUSINESSOS_DATA_DIR']=_data.name
os.environ['DATABASE_URL']='sqlite:///:memory:'
from app import app, normalize_search_text
from collection_tracking import customer_summaries, filter_customers
from openpyxl import load_workbook


def item(oid,days,remaining,customer_id=1,collected='0'):
    today=date.today()
    remaining,collected=Decimal(remaining),Decimal(collected)
    return dict(customer=SimpleNamespace(id=customer_id,name='=BYS GROUP MOBİLYA'),order=SimpleNamespace(id=oid,order_no=f'SS-{oid}'),
                due_date=today+timedelta(days=days),delivered_at=today+timedelta(days=days-30),remaining=remaining,collected=collected,amount=remaining+collected,
                state='paid' if remaining==0 else 'overdue' if days<0 else 'open',state_label='Tahsil edildi' if remaining==0 else f'{days} gün')


class CustomerCollectionTests(unittest.TestCase):
    def test_weighted_partial_payment_and_paid_exclusion(self):
        orders=[item(1,10,'10000'),item(2,30,'30000'),item(3,-60,'0',collected='50000')]
        g=customer_summaries(orders,date.today())[0]
        self.assertEqual(g['average_days'],25)
        self.assertEqual(g['open_count'],2)
        self.assertEqual(g['order_count'],3)
        orders[0]=item(1,10,'5000',collected='5000')
        g=customer_summaries(orders,date.today())[0]
        self.assertEqual(g['average_days'],Decimal(950000)/35000)
        self.assertEqual(g['remaining'],35000)
        self.assertEqual(g['average_due_date'],date.today()+timedelta(days=27))

    def test_overdue_not_hidden_by_positive_average(self):
        groups=customer_summaries([item(1,-10,'10000'),item(2,30,'30000'),item(3,4,'500')],date.today())
        g=groups[0]
        self.assertGreater(g['average_days'],0)
        self.assertEqual(g['overdue_amount'],10000)
        self.assertEqual(g['state'],'overdue')
        self.assertEqual(g['oldest_due_date'],date.today()-timedelta(days=10))
        for state in ('overdue','due_soon','open'):
            self.assertEqual(filter_customers(groups,state,'',normalize_search_text),groups)
        self.assertEqual(len(filter_customers(groups,'all','SS-2',normalize_search_text)[0]['orders']),3)

    def test_paid_empty_same_name_distinct_customer_and_signed_days(self):
        self.assertEqual(customer_summaries([],date.today()),[])
        groups=customer_summaries([item(1,3,'0',collected='100'),item(2,-5,'100',customer_id=2)],date.today())
        self.assertEqual(len(groups),2)
        self.assertEqual(groups[0]['average_days'],-5)
        self.assertIsNone(groups[1]['average_days'])
        self.assertIsNone(groups[1]['oldest_due_date'])
        self.assertEqual(len(filter_customers(groups,'paid','',normalize_search_text)),1)

    def test_routes_exports_and_customer_filter_preserves_total(self):
        orders=[item(1,-10,'10000'),item(2,30,'30000'),item(3,-60,'0',collected='500')]
        client=app.test_client()
        with patch('app.delivered_sales_collection_tracking',return_value=orders):
            for query in ({},{'q':'SS-2'},{'state':'overdue'}):
                response=client.get('/tahsilat-takibi/excel',query_string=query)
                self.assertEqual(response.status_code,200)
                ws=load_workbook(BytesIO(response.data)).active
                self.assertEqual(ws['A6'].data_type,'s')
                self.assertEqual(ws['B6'].value,2)
                self.assertEqual(ws['C6'].value,40000)
                self.assertEqual(ws['D6'].value,10000)
                self.assertEqual(ws['F6'].value,20)
                self.assertEqual(ws.max_row,7)
                detail=load_workbook(BytesIO(client.get('/tahsilat-takibi/excel',query_string=dict(query,view='details')).data)).active
                self.assertEqual(detail.max_row,13)
                self.assertEqual(detail.cell(13,7).value,40000)
            html=client.get('/tahsilat-takibi').get_data(as_text=True)
            self.assertEqual(html.count('class="collection-customer-row"'),1)
            self.assertIn('aria-controls="collection-detail-1"',html)
            self.assertIn('Gecikme var',html)
            self.assertEqual(html.count('class="button small" data-native-download'),4)
            self.assertEqual(client.get('/tahsilat-takibi/excel?view=bad').status_code,400)
            for view in ('summary','details'):
                response=client.get('/tahsilat-takibi/pdf',query_string={'view':view})
                self.assertEqual(response.status_code,200)
                self.assertTrue(response.data.startswith(b'%PDF'))
                if os.environ.get('COLLECTION_QA_DIR'):
                    target=Path(os.environ['COLLECTION_QA_DIR']);target.mkdir(parents=True,exist_ok=True)
                    (target/f'customer-{view}.pdf').write_bytes(response.data)
            ws=load_workbook(BytesIO(client.get('/tahsilat-takibi/excel?q=missing').data)).active
            self.assertEqual(ws['C6'].value,0)

if __name__=='__main__': unittest.main()
