import unittest
from datetime import date
from decimal import Decimal as D
from fifo_costing import calculate_fifo

class FifoTests(unittest.TestCase):
    def event(self, id, day, qty, direction, price=None, key=1):
        return dict(id=id, date=date(2026, 1, day), priority=0 if direction=='in' else 1,
                    key=key, quantity=qty, direction=direction, unit_cost=D(price) if price is not None else None)
    def test_oldest_layers_and_historical_consumption(self):
        events=[self.event(1,1,3,'in','100'), self.event(2,2,4,'in','200'),
                self.event(3,3,2,'out'),self.event(4,4,3,'out')]
        r=calculate_fifo(events)
        self.assertEqual(r[3]['cost'],D('200'))
        self.assertEqual(r[4]['cost'],D('500'))
        self.assertEqual(r[4]['missing_quantity'],0)
    def test_shortage_not_costed_by_future_purchase(self):
        r=calculate_fifo([self.event(1,1,1,'in','0'),self.event(2,2,3,'out'),
                          self.event(3,3,5,'in','100')])
        self.assertEqual(r[2]['missing_quantity'],2)
        self.assertEqual(r[2]['cost'],0)
    def test_unknown_opening_layers_are_not_free_goods(self):
        r=calculate_fifo([self.event(1,1,2,'in'),self.event(2,2,2,'in','25'),self.event(3,3,3,'out')])
        self.assertEqual(r[3]['missing_quantity'],2)
        self.assertEqual(r[3]['cost'],25)
    def test_partial_returns_restore_original_cost_not_selling_price(self):
        events=[self.event(1,1,2,'in','10'),self.event(2,1,2,'in','20'),
                self.event(3,2,4,'out')]
        for id,qty in [(4,1),(5,2)]:
            e=self.event(id,3,qty,'return'); e.update(priority=2,source_id=3);events.append(e)
        events.append(self.event(6,4,3,'out'))
        r=calculate_fifo(events)
        self.assertEqual(r[4]['cost'],D('-10'))
        self.assertEqual(r[5]['cost'],D('-30'))
        self.assertEqual(r[6]['cost'],D('40'))

    def test_split_purchases_allocate_every_cent(self):
        events=[self.event(1,1,3,'in','33.33333333333333333333333333')]
        events += [self.event(n+2,n+2,1,'out') for n in range(3)]
        results=calculate_fifo(events)
        self.assertEqual([results[n]['cost'] for n in (2,3,4)],
                         [D('33.33'),D('33.34'),D('33.33')])
        self.assertEqual(sum((r['cost'] for r in results.values()),D('0')),D('100.00'))

    def test_manual_returns_and_resales_preserve_every_cent(self):
        sale=self.event(1,1,3,'out');sale['manual']=dict(quantity=3,amount=D('100'))
        events=[sale]
        for n in range(3):
            ret=self.event(2+n,2+n,1,'return');ret.update(priority=2,source_id=1);events.append(ret)
        events.append(self.event(5,6,3,'out'))
        r=calculate_fifo(events)
        self.assertEqual(sum((r[n]['cost'] for n in (2,3,4)),D('0')),D('-100.00'))
        self.assertEqual(r[5]['cost'],D('100.00'))

    def test_products_never_share_layers_and_exact_decimal(self):
        r=calculate_fifo([self.event(1,1,3,'in','0.33333333'),
                          self.event(2,2,3,'out',key=2),self.event(3,2,3,'out')])
        self.assertEqual(r[2]['missing_quantity'],3)
        self.assertEqual(r[3]['cost'],D('1.00'))

if __name__=='__main__': unittest.main()
