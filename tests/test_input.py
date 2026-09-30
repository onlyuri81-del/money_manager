import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from money_manager.db import connect, get, initialize, insert
from money_manager.input_service import decimal_input, friendly_error, save_account, save_cash, save_holding, save_transaction, won_input
from money_manager.models import CashBalance, Holding, Instrument, TaxEvent, Taxpayer, Transaction

NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class InputPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize(self.db)
        self.account = save_account(self.db, owner_id=None, owner_name="테스트", broker="예시증권",
            label="일반", account_type="GENERAL", opened_on="2026-01-01")
        self.asset = Instrument(ticker="BAC", name="Bank of America", market="US", currency="USD", asset_type="STOCK")

    def tearDown(self):
        self.db.close()

    def holding(self, **changes):
        values = dict(account_id=self.account.id, quantity="10.5", average_buy_price="40.123456", acquisition_cost_krw=550_000)
        values.update(changes)
        return save_holding(self.db, self.asset, **values)

    def test_amount_parsing_exact_and_bad_grouping_rejected(self):
        self.assertEqual(decimal_input("1,234.123456", "수량"), Decimal("1234.123456"))
        self.assertEqual(won_input("-1,250,000", "손실"), -1_250_000)
        self.assertIsNone(decimal_input("", "선택", optional=True))
        for invalid in ("1,2", "NaN", "Infinity", "1e5", "100만원", "", "1.0000001"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                decimal_input(invalid, "수량")
        with self.assertRaises(ValueError):
            won_input("1.5", "금액")

    def test_snapshot_update_keeps_identity_and_exact_decimal(self):
        first = self.holding()
        updated = self.holding(quantity="12.000001", current_price="45.123456", current_fx="1350.123456", valued_at=NOW)
        self.assertEqual(first.id, updated.id)
        self.assertEqual(get(self.db, Holding, first.id).quantity, Decimal("12.000001"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM holdings").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM instruments").fetchone()[0], 1)

    def test_invalid_holding_rolls_back_new_instrument(self):
        with self.assertRaises(ValueError):
            self.holding(quantity="0")
        self.assertEqual(self.db.execute("SELECT count(*) FROM instruments").fetchone()[0], 0)

    def test_conflicting_asset_does_not_overwrite_classification(self):
        self.holding()
        conflict = Instrument(**{**self.asset.model_dump(), "asset_type": "ETF_OTHER"})
        with self.assertRaises(ValueError):
            save_holding(self.db, Instrument.model_validate(conflict.model_dump()), account_id=self.account.id,
                         quantity="1", average_buy_price="1", acquisition_cost_krw=1)

    def test_us_holding_rejected_in_isa_and_atomic_owner_account(self):
        isa = save_account(self.db, owner_id=self.account.taxpayer_id, owner_name="",
            broker="ISA", label="ISA", account_type="ISA", isa_type="GENERAL",
            opened_on="2026-01-01", maturity_on="2029-01-01")
        with self.assertRaises(sqlite3.IntegrityError):
            self.holding(account_id=isa.id)
        self.assertEqual(self.db.execute("SELECT count(*) FROM instruments").fetchone()[0], 0)
        with self.assertRaises(ValueError):
            save_account(self.db, owner_id=None, owner_name="안 저장될 사람", broker="", label="",
                         account_type="GENERAL", opened_on="2026-01-01")
        self.assertEqual(self.db.execute("SELECT count(*) FROM taxpayers").fetchone()[0], 1)

    def test_cash_update_exact_and_invalid_update_preserves_value(self):
        first = save_cash(self.db, CashBalance(account_id=self.account.id, currency="USD", amount="12.5", fx="1350", valued_at=NOW))
        updated = save_cash(self.db, CashBalance(account_id=self.account.id, currency="USD", amount="20.000001", fx="1340", valued_at=NOW))
        self.assertEqual(first.id, updated.id)
        with self.assertRaises(ValueError):
            save_cash(self.db, updated.model_copy(update={"amount": Decimal("-1")}))
        self.assertEqual(get(self.db, CashBalance, first.id).amount, Decimal("20.000001"))

    def test_transaction_duplicate_and_invalid_event_roll_back(self):
        holding = self.holding()
        transaction = Transaction(account_id=self.account.id, instrument_id=holding.instrument_id, external_ref="ref-1",
            transaction_type="SELL", traded_on="2026-09-01", recognized_on="2026-09-03", currency="USD",
            gross_amount="400", fx="1350", quantity="10", unit_price="40")
        wrong_event = TaxEvent(transaction_id=transaction.id, account_id="wrong", recognized_on=transaction.recognized_on,
            event_type="CAPITAL_GAIN", treatment="STOCK_CAPITAL_GAIN", economic_income_krw=100_000,
            taxable_income_krw=100_000, basis_source="statement")
        with self.assertRaises(sqlite3.IntegrityError):
            save_transaction(self.db, transaction, wrong_event)
        self.assertEqual(self.db.execute("SELECT count(*) FROM transactions").fetchone()[0], 0)
        save_transaction(self.db, transaction)
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            save_transaction(self.db, transaction.model_copy(update={"id": "another"}))
        self.assertIn("이미 저장", friendly_error(caught.exception))
        self.assertEqual(self.db.execute("SELECT count(*) FROM transactions").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
