"""Step-2 deterministic synthetic examples. Never opens the user's portfolio.db."""
import argparse
import json

from .db import connect, initialize, insert
from .models import Account, Instrument, TaxEvent, TaxPolicy, Taxpayer, Transaction
from .tax_calculators import DividendInput, ISAInput, calculate_dividend, calculate_isa_tax
from .tax_engine import AnnualTaxRequest, TaxEngine


def run_demo() -> dict:
    connection = connect()  # Memory only: no real account or file is changed.
    try:
        initialize(connection)
        owner = Taxpayer(id="demo-owner", name="Demo")
        policy = TaxPolicy(id="demo-policy", code="demo-2026", effective_from="2026-01-01",
                           effective_to="2026-12-31", status="SCENARIO")
        accounts = [Account(id=f"demo-account-{n}", taxpayer_id=owner.id, broker=f"Demo broker {n}",
                            label=f"General {n}", account_type="GENERAL", opened_on="2026-01-01") for n in (1, 2)]
        instrument = Instrument(id="demo-us-stock", ticker="DEMO", name="Simulated US stock",
                                market="US", currency="USD", asset_type="STOCK")
        with connection:
            for item in (owner, policy, *accounts, instrument):
                insert(connection, item)
            for n, gain in enumerate((10_000_000, -2_000_000), start=1):
                transaction = Transaction(id=f"demo-sale-{n}", account_id=accounts[n-1].id,
                    instrument_id=instrument.id, external_ref=f"demo-sale-{n}", transaction_type="SELL",
                    traded_on="2026-06-01", recognized_on="2026-06-01", currency="USD",
                    gross_amount="10000", fx="1350", quantity="10", unit_price="1000")
                event = TaxEvent(id=f"demo-event-{n}", transaction_id=transaction.id,
                    account_id=transaction.account_id, recognized_on=transaction.recognized_on,
                    event_type="CAPITAL_GAIN", treatment="STOCK_CAPITAL_GAIN", economic_income_krw=gain,
                    taxable_income_krw=gain, basis_source="synthetic-demo")
                insert(connection, transaction)
                insert(connection, event)
        annual = TaxEngine(connection).annual(AnnualTaxRequest(taxpayer_id=owner.id, tax_year=2026,
                    policy_id=policy.id, data_coverage_confirmed=True))
        domestic = calculate_dividend(DividendInput(gross_income_krw=1_000_000, market="KR"))
        overseas = calculate_dividend(DividendInput(gross_income_krw=1_000_000, market="US",
                    foreign_rate="0.15", domestic_withholding_krw=0))
        isa = calculate_isa_tax(ISAInput(isa_type="GENERAL", taxable_incomes_krw=(5_000_000,), eligible_losses_krw=(1_000_000,)))
        preferential = calculate_isa_tax(ISAInput(isa_type="LOW_INCOME", taxable_incomes_krw=(5_000_000,), eligible_losses_krw=(1_000_000,)))
        return {
            "note": "가상 자료로 계산한 시뮬레이션입니다. 실제 계좌/DB 파일은 수정하지 않습니다.",
            "us_stock_tax": annual.us_stock_tax.model_dump(mode="json"),
            "domestic_dividend": domestic.model_dump(mode="json"),
            "us_dividend_explicit_15pct_scenario": overseas.model_dump(mode="json"),
            "isa_general": isa.model_dump(mode="json"),
            "isa_low_income": preferential.model_dump(mode="json"),
            "issues": [issue.model_dump(mode="json") for issue in annual.issues],
        }
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description="Run step-2 synthetic tax examples without editing your DB")
    parser.add_argument("--json", action="store_true", help="print complete structured calculation results")
    args = parser.parse_args()
    result = run_demo()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(result["note"])
    print(f"미국주식: 이익 10,000,000원 - 손실 2,000,000원 -> 예상 양도세 {result['us_stock_tax']['estimated_tax_krw']:,}원")
    print(f"국내 일반 배당 1,000,000원 -> 원천징수 후 {result['domestic_dividend']['net_cash_krw']:,}원")
    print(f"미국 배당 1,000,000원 (해외 15%, 국내 0원 가정) -> {result['us_dividend_explicit_15pct_scenario']['net_cash_krw']:,}원")
    print(f"ISA 일반형: 적격 이익 5,000,000원 - 적격 손실 1,000,000원 -> 예상 세금 {result['isa_general']['estimated_tax_if_eligible_krw']:,}원")
    print(f"ISA 서민형: 같은 손익 -> 예상 세금 {result['isa_low_income']['estimated_tax_if_eligible_krw']:,}원")


if __name__ == "__main__":
    main()
