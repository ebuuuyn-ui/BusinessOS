"""Historical sale price projection; no database or external requests."""
import unittest
from decimal import Decimal
from types import SimpleNamespace
from dealer_account import item_pricing


class PricingTests(unittest.TestCase):
    def line(self,unit='100',quantity=2,discount='25',vat='20',included=False):
        unit,rate,tax=map(Decimal,(unit,discount,vat))
        effective=max(Decimal(0),min(rate,Decimal(100)))
        after=unit*quantity*(100-effective)/100
        net=after*100/(100+tax) if included else after
        tax_amount=after-net if included else net*tax/100
        return SimpleNamespace(unit_price=unit,quantity=quantity,discount_rate=rate,vat_rate=tax,
            vat_included=included,net_amount=net,vat_amount=tax_amount,total_amount=net+tax_amount,
            cost_unit_price=Decimal('22'),product=SimpleNamespace(unit_price=99999))

    def test_exclusive_and_inclusive_tax(self):
        price=item_pricing(self.line())
        self.assertEqual(price,{'listUnitCents':10000,'listLineCents':20000,'discountRate':25.0,
            'discountCents':5000,'discountedUnitCents':7500,'priceIncludesVat':False,
            'vatRate':20.0,'netCents':15000,'vatCents':3000,'totalCents':18000})
        included=item_pricing(self.line(unit='120',included=True))
        self.assertEqual((included['netCents'],included['vatCents'],included['totalCents']),(15000,3000,18000))
        self.assertEqual((included['listUnitCents'],included['discountCents'],included['discountedUnitCents']),(12000,6000,9000))
        self.assertTrue(included['priceIncludesVat'])

    def test_zero_full_fractional_discount_and_rounding(self):
        for rate,net in [('0',20000),('100',0),('12.5',17500)]:
            price=item_pricing(self.line(discount=rate,vat='0'))
            self.assertEqual(price['netCents'],net)
            self.assertEqual(price['listLineCents']-price['discountCents'],net)
        price=item_pricing(self.line(unit='10.01',quantity=3,discount='33.33',vat='10'))
        self.assertEqual((price['discountedUnitCents'],price['netCents'],price['vatCents'],price['totalCents']),(667,2002,200,2202))
        self.assertEqual(price['listLineCents']-price['discountCents'],price['netCents'])
        self.assertNotIn('cost_unit_price',price)

    def test_uses_saved_price_not_product_card(self):
        line=self.line();before=item_pricing(line)
        line.product.unit_price=1;line.cost_unit_price=500
        self.assertEqual(item_pricing(line),before)
