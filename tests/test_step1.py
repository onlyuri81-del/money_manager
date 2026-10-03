import sqlite3
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from pydantic import ValidationError

from money_manager.db import connect, from_scaled, get, initialize, insert, to_scaled
from money_manager.models import (
    Account, CashBalance, Holding, Instrument, PensionContribution, TaxEvent,
    Taxpayer, TaxpayerYear, TaxPolicy, Transaction,
)

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
TODAY = date(2026, 9, 30)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize(self.db)
        self.owner = Taxpayer(name="Example")
        insert(self.db, self.owner)
        self.account = Account(taxpayer_id=self.owner.id, broker="Example", label="General",
                               account_type="GENERAL", opened_on="2026-01-01")
        insert(self.db, self.account)
        self.us = Instrument(ticker="AAPL", name="Apple", market="US", currency="USD", asset_type="STOCK")
        self.kr = Instrument(ticker="005930", name="Samsung", market="KR", currency="KRW", asset_type="STOCK")
        self.etf = Instrument(ticker="379800", name="KR-listed overseas ETF", market="KR", currency="KRW",
                              asset_type="ETF_OTHER", pension_eligible=True)
        for instrument in (self.us, self.kr, self.etf):
            insert(self.db, instrument)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def holding(self, account=None, instrument=None, **changes):
        values = dict(account_id=(account or self.account).id, instrument_id=(instrument or self.us).id,
                      quantity="1.234567", average_buy_price="100.123456", acquisition_cost_krw=170_000,
                      current_price="101.234567", current_fx="1350.123456", valued_at=NOW)
        values.update(changes)
        return Holding(**values)

    def transaction(self, account=None, instrument=None, **changes):
        instrument = instrument or self.us
        values = dict(account_id=(account or self.account).id, instrument_id=instrument.id,
                      external_ref="broker-sale-1", transaction_type="SELL", traded_on=TODAY, recognized_on=TODAY,
                      currency=instrument.currency, gross_amount="100", fees="0.1",
                      fx="1350" if instrument.market == "US" else "1", quantity="1", unit_price="100")
        values.update(changes)
        return Transaction(**values)

    def event(self, transaction, **changes):
        values = dict(transaction_id=transaction.id, account_id=transaction.account_id,
                      recognized_on=transaction.recognized_on, event_type="CAPITAL_GAIN",
                      treatment="STOCK_CAPITAL_GAIN", economic_income_krw=50_000,
                      taxable_income_krw=50_000, basis_source="example-statement")
        values.update(changes)
        return TaxEvent(**values)

    def special_account(self, account_type, **changes):
        values = dict(taxpayer_id=self.owner.id, broker="Example", label=account_type,
                      account_type=account_type, opened_on="2026-01-01")
        if account_type == "ISA":
            values.update(isa_type="GENERAL", maturity_on="2029-01-01")
        values.update(changes)
        result = Account(**values)
        insert(self.db, result)
        return result

    def test_initialize_idempotent_and_foreign_keys(self):
        initialize(self.db)
        self.assertEqual(self.db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_reject_unmanaged_database(self):
        other = connect()
        try:
            other.execute("CREATE TABLE existing (id INTEGER)")
            with self.assertRaisesRegex(RuntimeError, "unmanaged"):
                initialize(other)
        finally:
            other.close()

    def test_newer_version_is_not_overwritten(self):
        self.db.execute("PRAGMA user_version = 2")
        with self.assertRaisesRegex(RuntimeError, "unsupported"):
            initialize(self.db)

    def test_foreign_key_required(self):
        self.db.execute("PRAGMA foreign_keys = OFF")
        with self.assertRaisesRegex(RuntimeError, "foreign_keys"):
            initialize(self.db)

    def test_decimal_roundtrip_and_json_roundtrip(self):
        value = self.holding()
        insert(self.db, value)
        self.assertEqual(get(self.db, Holding, value.id), value)
        self.assertEqual(Holding.model_validate_json(value.model_dump_json()), value)
        row = self.db.execute("SELECT quantity_micros, typeof(quantity_micros) FROM holdings").fetchone()
        self.assertEqual(tuple(row), (1234567, "integer"))

    def test_maximum_decimal_has_no_precision_loss(self):
        for value in ("0.000001", "123456.987654", "9000000000000", "-9000000000000"):
            self.assertEqual(from_scaled(to_scaled(Decimal(value))), Decimal(value))
        with self.assertRaises(ValueError):
            to_scaled(Decimal("0.0000001"))
        with self.assertRaises(ValueError):
            to_scaled(Decimal("10000000000000"))

    def test_float_nan_precision_and_negative_quantity_rejected(self):
        for value in (0.1, "NaN", "Infinity", "1.0000001", "-1", "0", True):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.holding(quantity=value)

    def test_quote_must_be_complete_and_timezone_aware(self):
        with self.assertRaises(ValidationError):
            self.holding(current_fx=None)
        with self.assertRaises(ValidationError):
            self.holding(valued_at=datetime(2026, 9, 30))
        unknown = self.holding(current_price=None, current_fx=None, valued_at=None)
        insert(self.db, unknown)
        self.assertIsNone(get(self.db, Holding, unknown.id).current_price)

    def test_isa_metadata_and_duplicate_active_isa(self):
        with self.assertRaises(ValidationError):
            Account(taxpayer_id=self.owner.id, broker="X", label="ISA", account_type="ISA", opened_on=TODAY)
        self.special_account("ISA")
        with self.assertRaises(sqlite3.IntegrityError):
            self.special_account("ISA")

    def test_closed_isa_permits_new_contract(self):
        first = self.special_account("ISA", closed_on="2026-09-01")
        second = self.special_account("ISA", opened_on="2026-09-02", maturity_on="2029-09-02")
        self.assertNotEqual(first.id, second.id)

    def test_isa_and_pensions_reject_us_direct_holdings(self):
        for kind in ("ISA", "IRP", "PENSION_SAVINGS"):
            account = self.special_account(kind)
            with self.subTest(kind=kind), self.assertRaises(sqlite3.IntegrityError):
                insert(self.db, self.holding(account=account))

    def test_pension_rejects_direct_kr_stock_and_accepts_eligible_etf(self):
        account = self.special_account("IRP")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, self.holding(account, self.kr, current_fx="1"))
        value = self.holding(account, self.etf, current_fx="1")
        insert(self.db, value)
        self.assertEqual(get(self.db, Holding, value.id), value)

    def test_holding_update_rechecks_eligibility_and_currency(self):
        value = self.holding()
        insert(self.db, value)
        account = self.special_account("ISA")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE holdings SET account_id = ? WHERE id = ?", (account.id, value.id))
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, self.holding(instrument=self.kr, current_fx="1350"))

    def test_holding_requires_existing_account_and_unique_position(self):
        value = self.holding()
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, value.model_copy(update={"account_id": "missing"}))
        insert(self.db, value)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, self.holding())

    def test_duplicate_import_ref_and_transaction_roundtrip(self):
        value = self.transaction()
        insert(self.db, value)
        self.assertEqual(get(self.db, Transaction, value.id), value)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, self.transaction())

    def test_transaction_rules_and_currency(self):
        with self.assertRaises(ValidationError):
            self.transaction(quantity=None)
        with self.assertRaises(ValidationError):
            self.transaction(recognized_on="2026-09-29")
        account = self.special_account("ISA")
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, self.transaction(account=account))
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, self.transaction(currency="KRW", fx="1"))

    def test_tax_event_year_is_recognition_year(self):
        transaction = self.transaction(traded_on="2026-12-31", recognized_on="2027-01-04")
        insert(self.db, transaction)
        event = self.event(transaction)
        insert(self.db, event)
        stored = get(self.db, TaxEvent, event.id)
        self.assertEqual(stored, event)
        self.assertEqual(stored.tax_year, 2027)
        self.assertEqual(self.db.execute("SELECT tax_year FROM tax_events").fetchone()[0], 2027)

    def test_tax_event_wrong_account_date_and_kind_rejected(self):
        transaction = self.transaction()
        insert(self.db, transaction)
        account = self.special_account("IRP")
        for changes in ({"account_id": account.id}, {"recognized_on": date(2027, 1, 1)},
                        {"event_type": "DIVIDEND", "treatment": "FINANCIAL_INCOME"}):
            with self.subTest(changes=changes), self.assertRaises(sqlite3.IntegrityError):
                insert(self.db, self.event(transaction, **changes))

    def test_tax_event_account_treatment_and_duplicate_rejected(self):
        account = self.special_account("ISA")
        transaction = self.transaction(account, self.kr)
        insert(self.db, transaction)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, self.event(transaction))
        insert(self.db, self.event(transaction, treatment="ISA"))
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, self.event(transaction, treatment="ISA"))

    def test_loss_and_unknown_basis_remain_distinct(self):
        transaction = self.transaction()
        insert(self.db, transaction)
        event = self.event(transaction, economic_income_krw=-300_000, taxable_income_krw=None,
                           treatment="REVIEW_REQUIRED", applied_rate=None)
        insert(self.db, event)
        self.assertIsNone(get(self.db, TaxEvent, event.id).taxable_income_krw)

    def test_foreign_and_domestic_dividend_withholding_are_separate(self):
        transaction = self.transaction(transaction_type="DIVIDEND", quantity=None, unit_price=None)
        insert(self.db, transaction)
        event = self.event(transaction, event_type="DIVIDEND", treatment="FINANCIAL_INCOME",
                           economic_income_krw=135_000, taxable_income_krw=135_000,
                           foreign_withholding_krw=20_250, domestic_withholding_krw=0, applied_rate="0.15")
        insert(self.db, event)
        self.assertEqual(get(self.db, TaxEvent, event.id), event)

    def test_exempt_unknown_basis_is_rejected_by_python_and_sql(self):
        transaction = self.transaction()
        insert(self.db, transaction)
        with self.assertRaises(ValidationError):
            self.event(transaction, treatment="EXEMPT", taxable_income_krw=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO tax_events (id,transaction_id,account_id,recognized_on,event_type,treatment,"
                "economic_income_krw,domestic_withholding_krw,foreign_withholding_krw,basis_source) "
                "VALUES ('raw',?,?,?,'CAPITAL_GAIN','EXEMPT',1,0,0,'raw')",
                (transaction.id, transaction.account_id, TODAY.isoformat()))

    def test_cash_balance_and_taxpayer_year_roundtrip(self):
        models = [CashBalance(account_id=self.account.id, currency="USD", amount="12.123456", fx="1350", valued_at=NOW),
                  TaxpayerYear(taxpayer_id=self.owner.id, tax_year=2026, salary_krw=None)]
        for model in models:
            insert(self.db, model)
            self.assertEqual(get(self.db, type(model), model.id), model)
        with self.assertRaises(ValidationError):
            CashBalance(account_id=self.account.id, currency="KRW", amount="1", fx="1350", valued_at=NOW)

    def test_pension_contribution_source_and_amount(self):
        account = self.special_account("IRP")
        transaction = self.transaction(account, self.etf, transaction_type="DEPOSIT", instrument_id=None,
                                       quantity=None, unit_price=None, gross_amount="3000000", fees="0")
        insert(self.db, transaction)
        contribution = PensionContribution(transaction_id=transaction.id, account_id=account.id, recognized_on=TODAY,
                                             contribution_source="PERSONAL", amount_krw=3_000_000)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, contribution.model_copy(update={"amount_krw": 2_000_000}))
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, contribution.model_copy(update={"contribution_source": "RETIREMENT_TRANSFER"}))
        insert(self.db, contribution)
        self.assertEqual(get(self.db, PensionContribution, contribution.id), contribution)

    def test_policy_roundtrip_overlap_and_verification_metadata(self):
        policy = TaxPolicy(code="example-baseline", effective_from="2026-01-01", effective_to="2026-12-31")
        insert(self.db, policy)
        self.assertEqual(get(self.db, TaxPolicy, policy.id), policy)
        with self.assertRaises(ValueError):
            insert(self.db, TaxPolicy(code=policy.code, effective_from="2026-12-31", effective_to="2027-12-31"))
        with self.assertRaises(ValidationError):
            TaxPolicy(code="bad", effective_from=TODAY, effective_to=TODAY, status="VERIFIED")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO tax_policies VALUES ('overlap',?,'2026-02-01','2026-03-01',NULL,'DRAFT','[]','{}')", (policy.code,))

    def test_out_of_period_policy_rejected(self):
        policy = TaxPolicy(code="past", effective_from="2025-01-01", effective_to="2025-12-31")
        insert(self.db, policy)
        transaction = self.transaction()
        insert(self.db, transaction)
        with self.assertRaises(sqlite3.IntegrityError):
            insert(self.db, self.event(transaction, policy_id=policy.id))

    def test_historical_rows_cannot_be_silently_changed(self):
        transaction = self.transaction()
        insert(self.db, transaction)
        event = self.event(transaction)
        insert(self.db, event)
        for sql, identifier in (
            ("UPDATE transactions SET gross_amount_micros = 0 WHERE id = ?", transaction.id),
            ("UPDATE tax_events SET taxable_income_krw = 0 WHERE id = ?", event.id),
            ("UPDATE accounts SET account_type = 'IRP' WHERE id = ?", self.account.id),
            ("UPDATE instruments SET market = 'KR' WHERE id = ?", self.us.id),
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, (identifier,))

    def test_raw_sql_invalid_date_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO accounts (id,taxpayer_id,broker,label,account_type,opened_on) "
                            "VALUES ('bad',?,'X','X','GENERAL','2026-02-30')", (self.owner.id,))

    def test_atomic_batch_rolls_back_on_bad_holding(self):
        owner = Taxpayer(name="Rolled back")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db:
                insert(self.db, owner)
                insert(self.db, self.holding().model_copy(update={"account_id": "missing"}))
        self.assertIsNone(get(self.db, Taxpayer, owner.id))

    def test_revalidation_blocks_model_copy_bypass(self):
        invalid = self.holding().model_copy(update={"quantity": Decimal("-1")})
        with self.assertRaises(ValidationError):
            insert(self.db, invalid)

    def test_display_view_contains_requested_columns(self):
        insert(self.db, self.holding())
        row = self.db.execute("SELECT * FROM holdings_detail").fetchone()
        self.assertEqual((row["ticker"], row["market"], row["account_type"]), ("AAPL", "US", "GENERAL"))


if __name__ == "__main__":
    unittest.main()
