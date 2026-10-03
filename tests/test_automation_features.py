import unittest
from datetime import date, datetime, timezone

from money_manager.db import connect, initialize
from money_manager.diagnostics import portfolio_diagnostics
from money_manager.input_service import save_account, save_cash, save_holding, save_transaction
from money_manager.local_ocr import parse_candidates
from money_manager.models import CashBalance, Instrument, Taxpayer, Transaction


class LocalOCRParsingTests(unittest.TestCase):
    def test_extracts_unambiguous_date_and_amount_candidates(self):
        candidates = parse_candidates(
            "2026.10.03 카페 결제 8,500원\n2026년 10월 02일 급여 2,500,000원\n"
            "2026-10-01 잔액 100,000원 출금 20,000원"
        )
        self.assertEqual(candidates[0].recognized_on, date(2026, 10, 3))
        self.assertEqual(candidates[0].amount_krw, 8_500)
        self.assertEqual(candidates[1].recognized_on, date(2026, 10, 2))
        self.assertEqual(candidates[1].amount_krw, 2_500_000)
        self.assertIsNone(candidates[2].amount_krw)

    def test_ignores_invalid_dates_and_lines_without_transaction_data(self):
        self.assertEqual(parse_candidates("계좌 잔액\n2026.13.40 거래"), ())


class PortfolioDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        initialize(self.db)
        self.account = save_account(
            self.db, owner_id=None, owner_name="테스트 사용자", broker="토스뱅크",
            label="생활비", account_type="GENERAL", opened_on="2025-01-01",
        )
        self.asset = Instrument(
            ticker="005930", name="삼성전자", market="KR", currency="KRW", asset_type="STOCK"
        )
        save_holding(
            self.db, self.asset, account_id=self.account.id, quantity="2",
            average_buy_price="90", acquisition_cost_krw=180,
            current_price="100", current_fx="1",
            valued_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        save_cash(
            self.db, CashBalance(account_id=self.account.id, currency="KRW", amount="100000",
                                 fx="1", valued_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        )

    def tearDown(self):
        self.db.close()

    def test_reports_snapshot_flows_and_missing_tax_events_separately(self):
        save_transaction(self.db, Transaction(
            account_id=self.account.id, external_ref="deposit-1", transaction_type="DEPOSIT",
            traded_on="2026-10-02", recognized_on="2026-10-02", currency="KRW",
            gross_amount="150000", fx="1",
        ))
        save_transaction(self.db, Transaction(
            account_id=self.account.id, external_ref="withdrawal-1", transaction_type="WITHDRAWAL",
            traded_on="2026-10-03", recognized_on="2026-10-03", currency="KRW",
            gross_amount="20000", fx="1",
        ))
        save_transaction(self.db, Transaction(
            account_id=self.account.id, instrument_id=self.asset.id, external_ref="sale-1",
            transaction_type="SELL", traded_on="2026-10-04", recognized_on="2026-10-04",
            currency="KRW", gross_amount="100", fx="1", quantity="1", unit_price="100",
        ))

        result = portfolio_diagnostics(
            self.db, self.account.taxpayer_id, 2026, date(2026, 10, 4)
        )
        self.assertEqual(result["holdings_value_krw"], 200)
        self.assertEqual(result["cash_value_krw"], 100_000)
        self.assertEqual(result["known_snapshot_value_krw"], 100_200)
        self.assertEqual(result["monthly_flows"], [{
            "월": "2026-10", "입금·이체유입(원)": 150_000,
            "출금·이체유출(원)": 20_000, "순흐름(원)": 130_000,
        }])
        self.assertEqual(result["tax_income_transaction_count"], 1)
        self.assertEqual(result["incomplete_tax_event_count"], 1)

    def test_future_transactions_are_not_included_in_year_to_date_flow(self):
        save_transaction(self.db, Transaction(
            account_id=self.account.id, external_ref="future-deposit",
            transaction_type="DEPOSIT", traded_on="2026-10-05", recognized_on="2026-10-05",
            currency="KRW", gross_amount="999", fx="1",
        ))
        result = portfolio_diagnostics(
            self.db, self.account.taxpayer_id, 2026, date(2026, 10, 4)
        )
        self.assertEqual(result["monthly_flows"], [])

    def test_selected_past_year_excludes_newer_transactions(self):
        for reference, day, amount in (
            ("past-year", "2025-11-01", "1000"),
            ("next-year", "2026-01-02", "2000"),
        ):
            save_transaction(self.db, Transaction(
                account_id=self.account.id, external_ref=reference,
                transaction_type="DEPOSIT", traded_on=day, recognized_on=day,
                currency="KRW", gross_amount=amount, fx="1",
            ))
        result = portfolio_diagnostics(
            self.db, self.account.taxpayer_id, 2025, date(2026, 10, 4)
        )
        self.assertEqual([row["월"] for row in result["monthly_flows"]], ["2025-11"])


if __name__ == "__main__":
    unittest.main()
