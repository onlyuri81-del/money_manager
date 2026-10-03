import unittest
import sqlite3
from datetime import date
from decimal import Decimal, localcontext

from pydantic import ValidationError

from money_manager.db import connect, get, initialize, insert
from money_manager.models import Account, Instrument, TaxEvent, TaxParameters, TaxPolicy, Taxpayer, Transaction
from money_manager.tax_calculators import (
    CapitalGainsInput, DividendInput, FinancialIncomeInput, ISAInput,
    assess_financial_income, calculate_dividend, calculate_isa_tax, calculate_us_stock_tax,
)
from money_manager.tax_engine import AnnualTaxRequest, ISAContractRequest, TaxEngine, UnsupportedTaxCase


class CalculatorTests(unittest.TestCase):
    def test_capital_gains_net_once_before_deduction(self):
        result = calculate_us_stock_tax(CapitalGainsInput(realized_gains_krw=(10_000_000, -2_000_000)))
        self.assertEqual((result.realized_net_gain_krw, result.taxable_gain_krw, result.estimated_tax_krw),
                         (8_000_000, 5_500_000, 1_210_000))

    def test_capital_gains_empty_loss_and_deduction_boundaries(self):
        for gains, expected in (((), 0), ((-1_000_000,), 0), ((2_499_999,), 0), ((2_500_000,), 0),
                                ((2_500_001,), 0), ((2_500_005,), 1)):
            with self.subTest(gains=gains):
                self.assertEqual(calculate_us_stock_tax(CapitalGainsInput(realized_gains_krw=gains)).estimated_tax_krw, expected)
        loss = calculate_us_stock_tax(CapitalGainsInput(realized_gains_krw=(-1_000_000,)))
        self.assertEqual(loss.net_gain_after_estimated_tax_krw, -1_000_000)

    def test_policy_parameters_override_without_mutating_defaults(self):
        custom = TaxParameters(overseas_stock_rate="0.30", stock_basic_deduction_krw=1_000_000)
        result = calculate_us_stock_tax(CapitalGainsInput(realized_gains_krw=(5_000_000,), parameters=custom))
        self.assertEqual(result.estimated_tax_krw, 1_200_000)
        self.assertEqual(TaxParameters().overseas_stock_rate, Decimal("0.22"))

    def test_invalid_and_unvalidated_inputs_rejected(self):
        for gain in (True, 0.1, "100", Decimal("100")):
            with self.subTest(gain=gain), self.assertRaises(ValidationError):
                CapitalGainsInput(realized_gains_krw=(gain,))
        invalid = CapitalGainsInput(realized_gains_krw=(1,)).model_copy(update={"realized_gains_krw": (True,)})
        with self.assertRaises(ValidationError):
            calculate_us_stock_tax(invalid)
        invalid_parameters = TaxParameters().model_copy(update={"overseas_stock_rate": -1})
        invalid = CapitalGainsInput(realized_gains_krw=(1,)).model_copy(update={"parameters": invalid_parameters})
        with self.assertRaises(ValidationError):
            calculate_us_stock_tax(invalid)

    def test_decimal_context_and_large_sums_are_exact(self):
        with localcontext() as context:
            context.prec = 3
            result = calculate_us_stock_tax(CapitalGainsInput(realized_gains_krw=(9_000_000_000_000_000,)*10))
        expected = (90_000_000_000_000_000-2_500_000)*22//100
        self.assertEqual(result.estimated_tax_krw, expected)

    def test_domestic_dividend_standard_and_observed(self):
        standard = calculate_dividend(DividendInput(gross_income_krw=1_000_000, market="KR"))
        self.assertEqual((standard.total_withholding_krw, standard.net_cash_krw), (154_000, 846_000))
        self.assertEqual(standard.basis, "STANDARD_RATE_SCENARIO")
        actual = calculate_dividend(DividendInput(gross_income_krw=1_000_000, market="KR", domestic_withholding_krw=140_000))
        self.assertEqual((actual.total_withholding_krw, actual.basis), (140_000, "ACTUAL"))

    def test_us_dividend_is_not_double_taxed(self):
        result = calculate_dividend(DividendInput(gross_income_krw=1_000_000, market="US",
                                  foreign_withholding_krw=150_000, domestic_withholding_krw=0))
        self.assertEqual((result.total_withholding_krw, result.net_cash_krw), (150_000, 850_000))

    def test_foreign_rate_is_explicit_and_zero_is_valid(self):
        for rate, net in (("0.15", 850_000), ("0", 1_000_000), ("0.30", 700_000)):
            result = calculate_dividend(DividendInput(gross_income_krw=1_000_000, market="US",
                                      foreign_rate=rate, domestic_withholding_krw=0))
            self.assertEqual(result.net_cash_krw, net)
        with self.assertRaises(ValidationError):
            DividendInput(gross_income_krw=1_000_000, market="US")

    def test_unknown_domestic_withholding_is_not_zero(self):
        result = calculate_dividend(DividendInput(gross_income_krw=1_000_000, market="US", foreign_withholding_krw=150_000))
        self.assertIsNone(result.total_withholding_krw)
        self.assertIsNone(result.net_cash_krw)
        self.assertEqual(result.cash_after_known_withholding_krw, 850_000)

    def test_dividend_conflicting_excess_negative_and_float_inputs(self):
        for changes in (
            {"foreign_withholding_krw": 1, "foreign_rate": "0.15"},
            {"foreign_withholding_krw": 900_000, "domestic_withholding_krw": 200_000},
            {"foreign_withholding_krw": -1}, {"foreign_rate": 0.15},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                DividendInput(gross_income_krw=1_000_000, market="US", **changes)
        with self.assertRaises(ValueError):
            calculate_dividend(DividendInput(gross_income_krw=1_000_000, market="US", foreign_rate="1", domestic_withholding_krw=1))

    def test_isa_general_low_income_and_farmer(self):
        for kind, expected in (("GENERAL", 198_000), ("LOW_INCOME", 0), ("FARMER", 0)):
            result = calculate_isa_tax(ISAInput(isa_type=kind, taxable_incomes_krw=(5_000_000,), eligible_losses_krw=(1_000_000,)))
            self.assertEqual(result.estimated_tax_if_eligible_krw, expected)

    def test_isa_threshold_losses_zero_and_custom_policy(self):
        for income, loss, expected in ((0, 0, 0), (1_000_000, 2_000_000, 0), (2_000_000, 0, 0), (2_000_011, 0, 1)):
            result = calculate_isa_tax(ISAInput(isa_type="GENERAL", taxable_incomes_krw=(income,), eligible_losses_krw=(loss,)))
            self.assertEqual(result.estimated_tax_if_eligible_krw, expected)
        custom = TaxParameters(isa_general_exemption_krw=0, isa_excess_rate="0.1")
        self.assertEqual(calculate_isa_tax(ISAInput(isa_type="GENERAL", taxable_incomes_krw=(100,),
                        eligible_losses_krw=(), parameters=custom)).estimated_tax_if_eligible_krw, 10)

    def test_financial_income_threshold_is_strictly_greater(self):
        for total, exceeded in ((19_999_999, False), (20_000_000, False), (20_000_001, True)):
            self.assertEqual(assess_financial_income(FinancialIncomeInput(ordinary_income_bases_krw=(total,))).threshold_exceeded, exceeded)

    def test_incomplete_income_cannot_return_false_safety(self):
        for total, expected in ((0, None), (20_000_000, None), (20_000_001, True)):
            result = assess_financial_income(FinancialIncomeInput(ordinary_income_bases_krw=(total,), complete=False))
            self.assertIs(result.threshold_exceeded, expected)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize(self.db)
        self.owner = Taxpayer(name="Simulated person")
        self.other = Taxpayer(name="Other simulated person")
        self.policy = TaxPolicy(code="2026-scenario", effective_from="2026-01-01", effective_to="2026-12-31", status="SCENARIO")
        self.policy27 = TaxPolicy(code="2027-scenario", effective_from="2027-01-01", effective_to="2027-12-31", status="SCENARIO")
        self.us = Instrument(ticker="AAPL", name="Apple", market="US", currency="USD", asset_type="STOCK")
        self.kr = Instrument(ticker="005930", name="KR stock", market="KR", currency="KRW", asset_type="STOCK")
        self.etf = Instrument(ticker="SIM-ETF", name="KR overseas ETF", market="KR", currency="KRW", asset_type="ETF_OTHER", pension_eligible=True)
        self.domestic_etf = Instrument(ticker="SIM-KR-ETF", name="KR equity ETF", market="KR", currency="KRW", asset_type="ETF_DOMESTIC_EQUITY", pension_eligible=True)
        with self.db:
            for model in (self.owner, self.other, self.policy, self.policy27, self.us, self.kr, self.etf, self.domestic_etf):
                insert(self.db, model)
        self.a = self.account("GENERAL", "Broker A")
        self.b = self.account("GENERAL", "Broker B")
        self.isa = self.account("ISA", "ISA", opened_on="2023-01-01", maturity_on="2026-01-01")
        self.pension = self.account("IRP", "IRP")
        self.engine = TaxEngine(self.db)
        self.sequence = 0

    def tearDown(self):
        self.db.close()

    def account(self, kind, label, owner=None, **changes):
        values = dict(taxpayer_id=(owner or self.owner).id, broker=label, label=label, account_type=kind, opened_on="2023-01-01")
        if kind == "ISA":
            values.update(isa_type="GENERAL", maturity_on="2026-01-01")
        values.update(changes)
        model = Account(**values)
        with self.db:
            insert(self.db, model)
        return model

    def record(self, account, instrument, economic, basis, treatment, day="2026-06-01", kind="SELL", event=True, **extra):
        self.sequence += 1
        transaction = Transaction(account_id=account.id, instrument_id=instrument.id if instrument else None,
            external_ref=f"sim-{self.sequence}", transaction_type=kind, traded_on=day, recognized_on=day,
            currency=instrument.currency if instrument else "KRW", gross_amount=str(max(economic, 0)),
            fx="1350" if instrument and instrument.market == "US" else "1",
            quantity="1" if kind == "SELL" else None, unit_price="1" if kind == "SELL" else None)
        tax_event = TaxEvent(transaction_id=transaction.id, account_id=account.id, recognized_on=day,
            event_type={"SELL":"CAPITAL_GAIN", "DIVIDEND":"DIVIDEND", "INTEREST":"INTEREST"}[kind],
            economic_income_krw=economic, taxable_income_krw=basis, treatment=treatment, basis_source="synthetic-test", **extra)
        with self.db:
            insert(self.db, transaction)
            if event:
                insert(self.db, tax_event)
        return transaction, tax_event

    def annual(self, **changes):
        values = dict(taxpayer_id=self.owner.id, tax_year=2026, policy_id=self.policy.id, data_coverage_confirmed=True)
        values.update(changes)
        return self.engine.annual(AnnualTaxRequest(**values))

    def isa_report(self, **changes):
        values = dict(account_id=self.isa.id, as_of="2026-09-30", policy_id=self.policy.id, data_coverage_confirmed=True)
        values.update(changes)
        return self.engine.isa_contract(ISAContractRequest(**values))

    def test_gains_aggregate_across_brokers_with_one_deduction(self):
        self.record(self.a, self.us, 5_000_000, 5_000_000, "STOCK_CAPITAL_GAIN")
        self.record(self.b, self.us, 5_000_000, 5_000_000, "STOCK_CAPITAL_GAIN")
        self.record(self.b, self.us, -2_000_000, -2_000_000, "STOCK_CAPITAL_GAIN")
        self.assertEqual(self.annual().us_stock_tax.estimated_tax_krw, 1_210_000)

    def test_other_people_years_and_exempt_kr_losses_do_not_offset(self):
        other_account = self.account("GENERAL", "Other", owner=self.other)
        self.record(self.a, self.us, 5_000_000, 5_000_000, "STOCK_CAPITAL_GAIN")
        self.record(other_account, self.us, -5_000_000, -5_000_000, "STOCK_CAPITAL_GAIN")
        self.record(self.a, self.us, -5_000_000, -5_000_000, "STOCK_CAPITAL_GAIN", day="2025-06-01")
        self.record(self.a, self.kr, -9_000_000, 0, "EXEMPT")
        self.assertEqual(self.annual().us_stock_tax.estimated_tax_krw, 550_000)

    def test_financial_income_excludes_isa_pension_and_elected_special_dividend(self):
        self.record(self.a, self.kr, 12_000_000, 12_000_000, "FINANCIAL_INCOME", kind="DIVIDEND")
        self.record(self.b, None, 8_000_001, 8_000_001, "FINANCIAL_INCOME", kind="INTEREST")
        self.record(self.isa, self.kr, 30_000_000, 30_000_000, "ISA", kind="DIVIDEND")
        self.record(self.pension, self.etf, 30_000_000, 30_000_000, "PENSION_DEFERRED", kind="DIVIDEND")
        self.record(self.a, self.kr, 30_000_000, 30_000_000, "SEPARATE_DIVIDEND", kind="DIVIDEND")
        result = self.annual()
        self.assertEqual(result.financial_income.known_ordinary_income_krw, 20_000_001)
        self.assertTrue(result.financial_income.threshold_exceeded)
        self.assertTrue(any(issue.code == "SEPARATE_DIVIDEND_SELECTION" for issue in result.issues))

    def test_kr_overseas_etf_sale_is_financial_income_not_stock_gains(self):
        self.record(self.a, self.etf, 30_000_000, 3_000_000, "FINANCIAL_INCOME")
        result = self.annual()
        self.assertEqual(result.us_stock_tax.estimated_tax_krw, 0)
        self.assertEqual(result.financial_income.known_ordinary_income_krw, 3_000_000)

    def test_missing_capital_event_and_unknown_basis_block_tax(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                transaction, event = self.record(self.a, self.us, 3_000_000, None, "STOCK_CAPITAL_GAIN", event=not missing)
                result = self.annual()
                self.assertIsNone(result.us_stock_tax)
                self.assertTrue(result.needs_review)

    def test_missing_financial_event_and_unconfirmed_coverage_return_unknown_alert(self):
        self.record(self.a, self.kr, 100, None, "REVIEW_REQUIRED", kind="DIVIDEND", event=False)
        self.assertIsNone(self.annual().financial_income.threshold_exceeded)
        self.assertIsNone(self.annual(data_coverage_confirmed=False).financial_income.threshold_exceeded)

    def test_unsupported_domestic_taxable_stock_blocks_shared_deduction(self):
        self.record(self.a, self.kr, 100_000, 100_000, "STOCK_CAPITAL_GAIN")
        self.assertIsNone(self.annual().us_stock_tax)

    def test_misclassified_us_sale_and_negative_financial_income_are_blocked(self):
        self.record(self.a, self.us, 1_000_000, 0, "EXEMPT")
        self.record(self.a, self.etf, -10_000, -10_000, "FINANCIAL_INCOME")
        result = self.annual()
        self.assertIsNone(result.us_stock_tax)
        self.assertIsNone(result.financial_income.threshold_exceeded)

    def test_actual_us_dividend_withholding_is_used_once(self):
        self.record(self.a, self.us, 1_000_000, 1_000_000, "FINANCIAL_INCOME", kind="DIVIDEND",
                    foreign_withholding_krw=150_000, domestic_withholding_krw=0)
        self.assertEqual(self.annual().dividends[0].cash.net_cash_krw, 850_000)

    def test_nonresident_and_us_tax_person_are_rejected(self):
        for owner in (Taxpayer(name="US", us_tax_person=True), Taxpayer(name="Nonresident", residency="OTHER")):
            with self.db:
                insert(self.db, owner)
            with self.assertRaises(UnsupportedTaxCase):
                self.annual(taxpayer_id=owner.id)

    def test_missing_policy_period_and_invalid_year_are_rejected(self):
        with self.assertRaises(LookupError):
            self.annual(policy_id="missing")
        with self.assertRaises(ValueError):
            self.annual(tax_year=2027)
        with self.assertRaises(ValidationError):
            self.annual(tax_year="2026")

    def test_isa_uses_whole_contract_and_single_exemption(self):
        self.record(self.isa, self.etf, 3_000_000, 3_000_000, "ISA", day="2024-06-01")
        self.record(self.isa, self.etf, 2_000_000, 2_000_000, "ISA", day="2025-06-01")
        self.record(self.isa, self.kr, -1_000_000, -1_000_000, "ISA")
        self.record(self.isa, self.kr, 10_000_000, 0, "EXEMPT")
        self.record(self.a, self.kr, -10_000_000, 0, "EXEMPT")
        report = self.isa_report()
        self.assertEqual(report.projection.estimated_tax_if_eligible_krw, 198_000)
        self.assertEqual(report.excluded_net_income_krw, 10_000_000)
        self.assertTrue(report.minimum_holding_period_met)

    def test_isa_excludes_domestic_equity_etf_capital_loss(self):
        self.record(self.isa, self.domestic_etf, -5_000_000, 0, "EXEMPT")
        self.record(self.isa, self.etf, 5_000_000, 5_000_000, "ISA")
        self.assertEqual(self.isa_report().projection.estimated_tax_if_eligible_krw, 297_000)

    def test_isa_invalid_exempt_gain_basis_and_unconfirmed_loss_are_blocked(self):
        self.record(self.isa, self.kr, 1_000_000, 1_000_000, "ISA")
        self.record(self.isa, self.kr, -1_000_000, None, "ISA")
        result = self.isa_report()
        self.assertIsNone(result.projection)
        self.assertTrue(result.needs_review)

    def test_isa_missing_event_not_counted_as_zero(self):
        self.record(self.isa, self.etf, 3_000_000, 3_000_000, "ISA", event=False)
        self.assertIsNone(self.isa_report().projection)

    def test_isa_future_preview_and_actual_early_closure(self):
        policy = TaxPolicy(code="2024-preview", effective_from="2024-01-01", effective_to="2024-12-31")
        with self.db:
            insert(self.db, policy)
        with self.db:
            self.db.execute("UPDATE accounts SET closed_on='2024-01-01' WHERE id=?", (self.isa.id,))
        self.assertIsNone(self.isa_report(policy_id=policy.id).projection)
        self.assertTrue(any(i.code == "EARLY_ISA_CLOSURE" for i in self.isa_report(policy_id=policy.id).issues))
        # A still-open contract before year 3 gets a labelled projection, not a due tax.
        with self.db:
            self.db.execute("UPDATE accounts SET closed_on=NULL WHERE id=?", (self.isa.id,))
        report = self.isa_report(as_of="2024-06-01", policy_id=policy.id)
        self.assertIsNotNone(report.projection)
        self.assertFalse(report.minimum_holding_period_met)

    def test_isa_non_isa_account_and_preopening_date_rejected(self):
        with self.assertRaises(UnsupportedTaxCase):
            self.isa_report(account_id=self.a.id)
        with self.assertRaises(ValueError):
            self.isa_report(as_of="2022-12-31")

    def test_isa_data_before_opening_is_detected(self):
        self.record(self.isa, self.etf, 1_000_000, 1_000_000, "ISA", day="2022-12-31")
        self.assertIsNone(self.isa_report().projection)

    def test_engine_does_not_write_or_commit_callers_data(self):
        before = self.db.total_changes
        self.annual()
        self.isa_report()
        self.assertEqual(self.db.total_changes, before)
        pending = Taxpayer(name="Pending write")
        insert(self.db, pending)
        self.annual()
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertIsNone(get(self.db, Taxpayer, pending.id))

    def test_errors_keep_outer_transaction_intact(self):
        pending = Taxpayer(name="Pending after error")
        insert(self.db, pending)
        with self.assertRaises(LookupError):
            self.annual(policy_id="missing")
        self.assertIsNotNone(get(self.db, Taxpayer, pending.id))
        self.db.rollback()

    def test_unknown_financial_event_still_alerts_if_known_income_already_exceeds(self):
        self.record(self.a, None, 21_000_000, 21_000_000, "FINANCIAL_INCOME", kind="INTEREST")
        self.record(self.a, self.kr, 1, None, "REVIEW_REQUIRED", kind="DIVIDEND")
        result = self.annual()
        self.assertTrue(result.financial_income.threshold_exceeded)
        self.assertFalse(result.financial_income.complete)

    def test_isa_future_transactions_do_not_enter_as_of_result(self):
        self.record(self.isa, self.etf, 3_000_000, 3_000_000, "ISA", day="2026-12-01")
        self.assertEqual(self.isa_report().projection.estimated_tax_if_eligible_krw, 0)

    def test_separate_dividend_tag_on_us_assets_is_not_trusted(self):
        self.record(self.a, self.us, 3_000_000, 3_000_000, "SEPARATE_DIVIDEND", kind="DIVIDEND")
        report = self.annual()
        self.assertIsNone(report.financial_income.threshold_exceeded)
        self.assertTrue(any(i.code == "INVALID_SEPARATE_DIVIDEND" for i in report.issues))

    def test_annual_account_date_mismatch_blocks_estimate(self):
        future = self.account("GENERAL", "Future", opened_on="2027-01-01")
        self.record(future, self.us, 3_000_000, 3_000_000, "STOCK_CAPITAL_GAIN")
        self.assertIsNone(self.annual().us_stock_tax)

    def test_feb29_isa_anniversary_uses_feb28_in_nonleap_year(self):
        account = self.account("ISA", "Leap", owner=self.other, opened_on="2024-02-29", maturity_on="2027-02-28")
        before = self.isa_report(account_id=account.id, policy_id=self.policy27.id, as_of="2027-02-27")
        on = self.isa_report(account_id=account.id, policy_id=self.policy27.id, as_of="2027-02-28")
        self.assertFalse(before.minimum_holding_period_met)
        self.assertTrue(on.minimum_holding_period_met)

    def test_read_only_database_connection_can_run_engine(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "read only.db"
            writer = connect(path)
            initialize(writer)
            with writer:
                insert(writer, self.owner)
                insert(writer, self.policy)
            writer.close()
            reader = connect(path, read_only=True)
            try:
                report = TaxEngine(reader).annual(AnnualTaxRequest(taxpayer_id=self.owner.id, tax_year=2026,
                    policy_id=self.policy.id, data_coverage_confirmed=True))
                self.assertEqual(report.us_stock_tax.estimated_tax_krw, 0)
                with self.assertRaises(sqlite3.OperationalError) as error:
                    insert(reader, self.other)
                self.assertIn("readonly", str(error.exception).lower())
            finally:
                reader.close()


if __name__ == "__main__":
    unittest.main()
