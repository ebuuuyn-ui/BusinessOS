"""Supplier export tests: synthetic orders, mocked Sheets; never live data."""
import unittest
from datetime import date, datetime
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from flask import Flask
import os
from supplier_sheets import (HEADERS, SheetsClient, SheetExportError, allowed_order,
                             order_rows, build_updates, cell, fingerprint)


def sample():
    item = NS(id=1, product=NS(code='Y00101'), product_name='BARON', description='Uzun sırt',
              variant='Siyah', detail_2='', detail_3='', quantity=6, unit='Adet', note='',
              unit_price=987654, cost_price=765432, tax_rate=20)
    return NS(id=42, order_type='Satın Alma', customer_id=1339, order_no='SA-TEST',
              order_date=date(2026,9,21), delivery_date=date(2026,9,30), status='Bekliyor',
              customer=NS(name='ABİKA'), customer_company='Test firma', delivery_city='İstanbul',
              shipment_contact='', shipment_phone='', shipment_address='', shipment_note='',
              notes='', items=[item], total=654321)


class SupplierSheetsTests(unittest.TestCase):
    def test_only_abika_purchases(self):
        order = sample()
        self.assertTrue(allowed_order(order))
        for field, value in [('order_type','Satış'), ('customer_id',99)]:
            other=sample(); setattr(other,field,value)
            with self.assertRaises(SheetExportError): order_rows(other,'test')

    def test_no_money_fields_and_required_details(self):
        rows=order_rows(sample(),'test',datetime(2026,9,21,12,0))
        self.assertEqual(len(rows[0]),len(HEADERS))
        row=dict(zip(HEADERS,rows[0]))
        self.assertEqual(row['Adet'],6)
        self.assertEqual(row['Teslim Tarihi'],'30.09.2026')
        self.assertEqual(row['Ürün Kodu'],'Y00101')
        self.assertNotIn('987654',str(rows)); self.assertNotIn('765432',str(rows))
        self.assertNotIn('654321',str(rows))

    def test_free_product_without_stock_card(self):
        order=sample(); order.items[0].product=None
        self.assertEqual(order_rows(order,'test')[0][12],'')
        self.assertEqual(order_rows(order,'test')[0][13],'BARON')

    def test_metadata_does_not_invalidate_preview_but_quantity_does(self):
        order=sample()
        a=order_rows(order,'one',datetime(2026,1,1))
        b=order_rows(order,'two',datetime(2026,2,2))
        self.assertEqual(fingerprint(a),fingerprint(b))
        order.items[0].quantity=7
        self.assertNotEqual(fingerprint(a),fingerprint(order_rows(order,'one')))

    def test_formula_input_is_literal_text(self):
        self.assertEqual(cell('=IMPORTXML("url")'),{'userEnteredValue':{'stringValue':'=IMPORTXML("url")'}})
        self.assertEqual(cell(6),{'userEnteredValue':{'numberValue':6}})

    def test_first_export_appends_repeat_updates(self):
        rows=order_rows(sample(),'test')
        first=build_updates(rows,[])
        self.assertEqual(list(first[0]),['appendCells'])
        repeat=build_updates(rows,[[rows[0][0]]])
        self.assertEqual(list(repeat[0]),['updateCells'])
        self.assertEqual(repeat[0]['updateCells']['start']['rowIndex'],1)

    def test_removed_line_clears_only_own_values_other_orders_untouched(self):
        rows=order_rows(sample(),'test')
        requests=build_updates(rows,[[rows[0][0]],['bos:order:42:item:2'],['bos:order:43:item:1']])
        self.assertEqual(len(requests),3)
        removed=requests[2]['updateCells']
        self.assertEqual(removed['range']['startRowIndex'],2)
        self.assertEqual(removed['range']['endColumnIndex'],24)
        self.assertEqual(removed['fields'],'userEnteredValue')
        self.assertEqual(removed['rows'],[])

    def test_repeat_export_never_writes_supplier_status(self):
        rows = order_rows(sample(), 'test')
        requests = build_updates(rows, [[rows[0][0]]])
        written_columns = []
        for request in requests:
            update = request['updateCells']
            start = update['start']['columnIndex']
            written_columns.extend(range(start, start + len(update['rows'][0]['values'])))
        self.assertEqual(written_columns, list(range(4)) + list(range(5, 24)))
        first = build_updates(rows, [])[0]['appendCells']['rows'][0]['values']
        self.assertEqual(first[4], {**cell('Bekliyor'), 'userEnteredFormat': {'wrapStrategy':'WRAP', 'verticalAlignment':'TOP'}})

    def test_existing_manual_status_does_not_fail_readback(self):
        rows = order_rows(sample(), 'test')
        actual = [list(rows[0])]; actual[0][4] = 'Üretimde'
        client = self.client(rows, actual)
        client.values.side_effect = [[HEADERS], [[rows[0][0]]], [[rows[0][0]]]]
        self.assertEqual(client.export(rows), 1)
        actual[0][18] = 99
        client = self.client(rows, actual)
        client.values.side_effect = [[HEADERS], [[rows[0][0]]], [[rows[0][0]]]]
        with self.assertRaises(SheetExportError): client.export(rows)

    def test_new_status_still_verified(self):
        rows = order_rows(sample(), 'test')
        actual = [list(rows[0])]; actual[0][4] = 'Üretimde'
        with self.assertRaises(SheetExportError): self.client(rows, actual).export(rows)

    def test_duplicate_keys_and_empty_orders_blocked(self):
        rows=order_rows(sample(),'test')
        with self.assertRaises(SheetExportError): build_updates(rows,[[rows[0][0]],[rows[0][0]]])
        with self.assertRaises(SheetExportError): build_updates([],[])

    def client(self, rows, actual=None):
        c=SheetsClient.__new__(SheetsClient)
        c.target=Mock(return_value={'title':'Sayfa1','gridProperties':{'rowCount':1000}})
        c.values=Mock(side_effect=[[HEADERS],[],[[r[0]] for r in rows]])
        c.api=Mock(side_effect=[{}, {'valueRanges':[{'values':[r]} for r in (rows if actual is None else actual)]}])
        return c

    def test_export_reads_back_written_values(self):
        rows=order_rows(sample(),'test'); client=self.client(rows)
        self.assertEqual(client.export(rows),1)
        self.assertEqual(client.api.call_args_list[0].args,('POST',':batchUpdate'))

    def test_readback_mismatch_is_not_success(self):
        rows=order_rows(sample(),'test'); wrong=[list(rows[0])]; wrong[0][18]=99
        with self.assertRaises(SheetExportError): self.client(rows,wrong).export(rows)
        with self.assertRaises(SheetExportError): self.client(rows,[]).export(rows)

    def test_keyless_credentials_use_request_token_and_only_sheets_scope(self):
        app = Flask(__name__)
        audience = '//iam.googleapis.com/projects/164533587659/locations/global/workloadIdentityPools/businessos-production/providers/vercel'
        with patch.dict(os.environ, {'BUSINESSOS_GOOGLE_WIF_AUDIENCE':audience}), app.test_request_context(headers={'x-vercel-oidc-token':'synthetic-test-token'}):
            client = SheetsClient()
            credentials = client.session.credentials
            self.assertEqual(credentials.scopes,['https://www.googleapis.com/auth/spreadsheets'])
            self.assertEqual(credentials.retrieve_subject_token(None),'synthetic-test-token')
            self.assertEqual(credentials.service_account_email,'businessos-supplier-sheets@august-edge-509315-r9.iam.gserviceaccount.com')
            client.session.close()
        with patch.dict(os.environ, {'BUSINESSOS_GOOGLE_WIF_AUDIENCE':audience}), app.test_request_context():
            with self.assertRaises(SheetExportError): SheetsClient()

    def test_unexpected_header_prevents_write(self):
        c=self.client(order_rows(sample(),'test')); c.values.side_effect=[[['Existing user data']]]
        with self.assertRaises(SheetExportError): c.export(order_rows(sample(),'test'))
        c.api.assert_not_called()

if __name__=='__main__': unittest.main()
