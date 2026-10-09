import unittest
from datetime import date,datetime
from decimal import Decimal as D
from types import SimpleNamespace as NS
from seller_reports import creation_owners,build_report

class SellerTests(unittest.TestCase):
    def order(self,id=1,**extra):
        values=dict(id=id,order_no='S-'+str(id),created_at=datetime(2026,9,1),order_date=date(2026,9,1),order_type='Satış',status='Bekliyor',customer=NS(name='Cari',code='1'),total_quantity=2,net_amount=D('100.25'),vat_amount=D('20.05'),total_amount=D('120.30'))
        values.update(extra);return NS(**values)
    def event(self,order,actor='user:1',name='ahmet',**extra):
        e=dict(operation='INSERT',table_name='order',endpoint='new_order',actor_id=actor,actor_name=name,after_data=dict(id=order.id,order_no=order.order_no,created_at=order.created_at.isoformat()))
        e.update(extra);return e
    def report(self,orders,events=(),filters=None):return build_report(orders,creation_owners(events),filters or {},str.casefold)
    def test_missing_creation_belongs_to_bekir(self):
        r=self.report([self.order()]);self.assertEqual(r['sales_rows'][0]['seller'],'Bekir')
    def test_only_creation_identifies_seller(self):
        o=self.order();r=self.report([o],[self.event(o),self.event(o,actor='owner',name='Bekir',operation='UPDATE')]);self.assertEqual(r['sales_rows'][0]['seller'],'Ahmet')
    def test_owner_name_and_reused_id(self):
        o=self.order();self.assertEqual(self.report([o],[self.event(o,actor='owner',name='Ebubekir')])['sales_rows'][0]['seller'],'Bekir')
        old=self.order(order_no='Old');self.assertEqual(self.report([o],[self.event(old)])['sales_rows'][0]['seller'],'Bekir')
    def test_import_does_not_assign_importer(self):
        o=self.order();self.assertEqual(self.report([o],[self.event(o,endpoint='restore')])['sales_rows'][0]['seller'],'Bekir')
    def test_totals_exclude_cancelled_and_purchase(self):
        orders=[self.order(),self.order(2),self.order(3,status='İptal Edildi'),self.order(4,order_type='Satın Alma')]
        r=self.report(orders,[self.event(orders[1])]);self.assertEqual(len(r['sales_rows']),2);self.assertEqual(r['sales_totals']['total'],D('240.60'));self.assertEqual([g['count'] for g in r['seller_groups']],[1,1])
    def test_seller_date_and_search_filters(self):
        o=self.order();events=[self.event(o)]
        self.assertEqual(len(self.report([o],events,dict(seller='Bekir'))['sales_rows']),0)
        self.assertEqual(len(self.report([o],events,dict(start_date='2026-09-02'))['sales_rows']),0)
        self.assertEqual(len(self.report([o],events,dict(seller='Ahmet',end_date='2026-09-01',q='cari'))['sales_rows']),1)

if __name__=='__main__':unittest.main()
