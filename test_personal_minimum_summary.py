"""Summary regression tests using only a temporary, in-memory database."""
import os
import tempfile
import unittest
from decimal import Decimal

_test_data = tempfile.TemporaryDirectory(prefix="businessos-minimum-test-")
os.environ["BUSINESSOS_DATA_DIR"] = _test_data.name
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from app import app, db, PersonalMonth, PersonalPayment


class MinimumSummaryTests(unittest.TestCase):
    def test_remaining_minimum(self):
        for minimum, paid, expected in [(100, 40, 60), (100, 150, 0), (100, 100, 0), (None, 40, 0), (100, None, 100), (None, None, 0), (Decimal('100.25'), Decimal('20.10'), Decimal('80.15'))]:
            with self.subTest(minimum=minimum, paid=paid):
                row = PersonalPayment(minimum_payment=minimum, payment=paid)
                self.assertEqual(row.calculated_remaining_minimum, expected)

    def test_selected_month_summary_does_not_offset_other_rows(self):
        with app.app_context():
            db.create_all()
            db.session.add_all([PersonalMonth(month='TEST'), PersonalMonth(month='OTHER')])
            db.session.flush()
            db.session.add_all([
                PersonalPayment(month='TEST', name='Partial', kind='card', minimum_payment=100, payment=40),
                PersonalPayment(month='TEST', name='Overpaid', kind='card', minimum_payment=100, payment=150),
                PersonalPayment(month='OTHER', name='Other month', kind='card', minimum_payment=9999),
            ])
            db.session.commit()
            response = app.test_client().get('/sahsi-hesaplar?tab=payments&month=TEST')
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('personal-summary payment-summary', html)
            self.assertIn('KALAN ASGARİ ÖDEME</small><strong>₺60,00</strong>', html)


if __name__ == '__main__':
    unittest.main()
