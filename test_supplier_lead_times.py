import unittest
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace as NS
from decimal import Decimal
from supplier_lead_times import build_report

class LeadTests(unittest.TestCase):
    def order(self,events,status='Teslim Edildi',id=1):
        start=datetime(2026,1,1)
        return NS(id=id,order_no='SA-'+str(id),created_at=start,order_date=start.date()-timedelta(days=100),customer_id=1,customer=NS(id=1,name='Firma',code='1'),status=status,history=[NS(id=i,created_at=start+timedelta(days=d),status=s) for i,(d,s) in enumerate(events)])
    def report(self,*orders,filters=None):
        return build_report(orders,lambda s:s.casefold(),filters or {},datetime(2026,2,1,tzinfo=timezone.utc))
    def test_shipping_wait_is_excluded_and_first_shipped_wins(self):
        o=self.order([(0,'Bekliyor'),(5,'Sevkiyat Bekliyor'),(7,'Sevk Edildi'),(9,'Teslim Edildi'),(10,'Teslim Edildi')])
        r=self.report(o);self.assertEqual(r['lead_average'],7);self.assertNotIn('delivery_average',r)
    def test_first_transition_survives_reversal(self):
        o=self.order([(0,'Bekliyor'),(2.5,'Sevk Edildi'),(3,'Bekliyor'),(7,'Teslim Edildi')]);r=self.report(o)
        self.assertEqual(r['lead_average'],Decimal('2.5'))
    def test_shipping_wait_remains_pending(self):
        r=self.report(self.order([(0,'Bekliyor'),(5,'Sevkiyat Bekliyor')],status='Sevkiyat Bekliyor'))
        self.assertIsNone(r['lead_average']);self.assertEqual(r['pending_count'],1)
    def test_direct_delivery_counts_without_shipped(self):
        r=self.report(self.order([(0,'Bekliyor'),(6,'Teslim Edildi')]))
        self.assertEqual(r['lead_average'],6)
    def test_missing_pending_cancelled_excluded(self):
        r=self.report(self.order([],id=1),self.order([(0,'Teslim Edildi')],id=2),self.order([(0,'Bekliyor')],status='Bekliyor',id=3),self.order([(0,'Bekliyor'),(4,'Teslim Edildi')],status='İptal Edildi',id=4))
        self.assertIsNone(r['lead_average']);self.assertEqual(r['missing_count'],2);self.assertEqual(r['pending_count'],1)
    def test_order_weight_and_created_date_filter(self):
        a=self.order([(0,'Bekliyor'),(2,'Teslim Edildi')]);b=self.order([(0,'Bekliyor'),(8,'Teslim Edildi')],id=2)
        r=self.report(a,b,filters={'start_date':'2026-01-01','end_date':'2026-01-01'});self.assertEqual(r['lead_average'],5);self.assertEqual(r['measured_count'],2)
        self.assertEqual(self.report(a,filters={'start_date':'2026-01-02'})['measured_count'],0)
    def test_negative_duration_is_not_reported(self):
        r=self.report(self.order([(-3,'Bekliyor'),(-1,'Teslim Edildi')]))
        self.assertEqual(r['missing_count'],1);self.assertIsNone(r['lead_average'])
if __name__=='__main__':unittest.main()
