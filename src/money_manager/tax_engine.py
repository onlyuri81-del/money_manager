"""Read-only simulations over imported, classified tax events.

The engine does not infer tax cost from average prices or invent missing events.
It keeps ordinary financial income, US gains, ISA and pension income separate.
"""
import calendar
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from pydantic import Field

from .db import get
from .models import (
    Account, AccountType, AssetType, Identifier, Instrument, Market, PolicyStatus,
    Residency, TaxEvent, TaxEventType, TaxPolicy, TaxTreatment, Taxpayer,
    Transaction, TransactionType, Year,
)
from .tax_calculators import (
    CapitalGainsInput, CapitalGainsResult, DividendInput, DividendResult,
    FinancialIncomeInput, FinancialIncomeResult, ISAInput, ISAResult, ValueModel,
    assess_financial_income, calculate_dividend, calculate_isa_tax, calculate_us_stock_tax,
)


class AnnualTaxRequest(ValueModel):
    taxpayer_id: Identifier
    tax_year: Year
    policy_id: Identifier
    data_coverage_confirmed: bool = Field(default=False, strict=True)


class ISAContractRequest(ValueModel):
    account_id: Identifier
    as_of: date
    policy_id: Identifier
    data_coverage_confirmed: bool = Field(default=False, strict=True)


class CalculationIssue(ValueModel):
    code: str
    scope: str
    severity: str  # WARNING / BLOCKER
    message: str
    transaction_id: str | None = None


class PostedDividend(ValueModel):
    transaction_id: str
    account_id: str
    ticker: str
    treatment: TaxTreatment
    cash: DividendResult


class AnnualTaxReport(ValueModel):
    taxpayer_id: str
    tax_year: int
    policy_id: str
    policy_status: PolicyStatus
    imported_income_transaction_count: int
    us_stock_tax: CapitalGainsResult | None
    financial_income: FinancialIncomeResult
    dividends: tuple[PostedDividend, ...]
    issues: tuple[CalculationIssue, ...]

    @property
    def needs_review(self) -> bool:
        return any(issue.severity == "BLOCKER" for issue in self.issues)


class ISAContractReport(ValueModel):
    account_id: str
    as_of: date
    period_start: date
    period_end: date
    policy_id: str
    policy_status: PolicyStatus
    imported_income_transaction_count: int
    economic_net_income_krw: int
    excluded_net_income_krw: int
    projection: ISAResult | None
    minimum_holding_period_met: bool
    contract_maturity_reached: bool
    issues: tuple[CalculationIssue, ...]

    @property
    def needs_review(self) -> bool:
        return any(issue.severity == "BLOCKER" for issue in self.issues)


class UnsupportedTaxCase(ValueError):
    pass


@dataclass(frozen=True)
class _Record:
    transaction: Transaction
    account: Account
    instrument: Instrument | None
    event: TaxEvent | None


def _anniversary(value: date, years: int) -> date:
    target_year = value.year + years
    day = min(value.day, calendar.monthrange(target_year, value.month)[1])
    return date(target_year, value.month, day)


class TaxEngine:
    def __init__(self, connection: sqlite3.Connection):
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise RuntimeError("initialize schema version 1 before using TaxEngine")
        self.db = connection

    @contextmanager
    def _snapshot(self):
        # Stable multi-query read snapshot; respects a caller's outer transaction.
        self.db.execute("SAVEPOINT money_manager_tax_snapshot")
        try:
            yield
        except Exception:
            self.db.execute("ROLLBACK TO money_manager_tax_snapshot")
            raise
        finally:
            self.db.execute("RELEASE money_manager_tax_snapshot")

    def _require(self, model_class, identifier):
        result = get(self.db, model_class, identifier)
        if result is None:
            raise LookupError(f"{model_class.__name__} not found: {identifier}")
        return result

    def _taxpayer(self, identifier):
        owner = self._require(Taxpayer, identifier)
        if owner.residency != Residency.KR or owner.us_tax_person:
            raise UnsupportedTaxCase("this engine supports Korean residents who are not US tax persons")
        return owner

    def _policy(self, identifier, start, end):
        policy = self._require(TaxPolicy, identifier)
        if start < policy.effective_from or end > policy.effective_to:
            raise ValueError("requested calculation period is outside the selected policy")
        return policy

    def _base_issues(self, policy, coverage):
        result = [CalculationIssue(
            code="SIMULATION_ROUNDING", scope="GENERAL", severity="WARNING",
            message="합산세율로 계산 후 원 미만을 절사한 추정치입니다. 신고·납부 단수처리와 추가 종합과세는 별도입니다.",
        )]
        if policy.status != PolicyStatus.VERIFIED:
            result.append(CalculationIssue(
                code="UNVERIFIED_POLICY", scope="GENERAL", severity="WARNING",
                message=f"정책 상태 {policy.status.value}: 법적 확정세액이 아닌 가정 기반 시뮬레이션입니다.",
            ))
        if not coverage:
            result.append(CalculationIssue(
                code="COVERAGE_UNCONFIRMED", scope="GENERAL", severity="WARNING",
                message="모든 증권사·은행 및 해당 기간 거래가 반영됐는지 미확인입니다. 등록 자료만 계산합니다.",
            ))
        return result

    def _records(self, taxpayer_id, start, end, account_id=None):
        sql = """SELECT t.id AS transaction_id, t.account_id, t.instrument_id, e.id AS event_id
                 FROM transactions t JOIN accounts a ON a.id=t.account_id
                 LEFT JOIN tax_events e ON e.transaction_id=t.id
                 WHERE a.taxpayer_id=? AND t.recognized_on BETWEEN ? AND ?
                 AND t.transaction_type IN ('SELL','DIVIDEND','INTEREST')"""
        parameters = [taxpayer_id, start.isoformat(), end.isoformat()]
        if account_id is not None:
            sql += " AND a.id=?"
            parameters.append(account_id)
        sql += " ORDER BY t.recognized_on, t.id"
        rows = self.db.execute(sql, parameters).fetchall()
        return [
            _Record(self._require(Transaction, row["transaction_id"]), self._require(Account, row["account_id"]),
                    self._require(Instrument, row["instrument_id"]) if row["instrument_id"] else None,
                    self._require(TaxEvent, row["event_id"]) if row["event_id"] else None)
            for row in rows
        ]

    @staticmethod
    def _scope(record):
        if record.transaction.transaction_type == TransactionType.SELL:
            if record.instrument and record.instrument.market == Market.KR and record.instrument.asset_type != AssetType.STOCK:
                return "FINANCIAL_INCOME"
            return "US_STOCK"
        return "FINANCIAL_INCOME"

    @staticmethod
    def _block(issues, code, scope, message, record):
        issues.append(CalculationIssue(code=code, scope=scope, severity="BLOCKER", message=message,
                                       transaction_id=record.transaction.id))

    @staticmethod
    def _outside_account_period(record):
        day = record.transaction.recognized_on
        return day < record.account.opened_on or (record.account.closed_on is not None and day > record.account.closed_on)

    def annual(self, request: AnnualTaxRequest) -> AnnualTaxReport:
        if not isinstance(request, AnnualTaxRequest):
            raise TypeError("expected AnnualTaxRequest")
        request = AnnualTaxRequest.model_validate(request.model_dump())
        with self._snapshot():
            self._taxpayer(request.taxpayer_id)
            policy = self._policy(request.policy_id, date(request.tax_year, 1, 1), date(request.tax_year, 12, 31))
            records = self._records(request.taxpayer_id, date(request.tax_year, 1, 1), date(request.tax_year, 12, 31))
            issues = self._base_issues(policy, request.data_coverage_confirmed)
            gains, financial, dividends = [], [], []
            general_records = [record for record in records if record.account.account_type == AccountType.GENERAL]
            for record in general_records:
                scope, event = self._scope(record), record.event
                if self._outside_account_period(record):
                    self._block(issues, "ACCOUNT_PERIOD_MISMATCH", scope, "원거래가 계좌 개설·해지 기간 밖에 있습니다.", record)
                    continue
                if event is None or event.treatment == TaxTreatment.REVIEW_REQUIRED:
                    self._block(issues, "MISSING_TAX_EVENT" if event is None else "UNCLASSIFIED_EVENT", scope,
                                "세금 이벤트가 없거나 과세 분류가 미확정입니다.", record)
                    continue
                if event.taxable_income_krw is None:
                    self._block(issues, "UNKNOWN_TAX_BASIS", scope, "과세 산입액이 미확정입니다.", record)
                    continue
                if event.policy_id and event.policy_id != policy.id:
                    issues.append(CalculationIssue(code="POLICY_OVERRIDE", scope=scope, severity="WARNING",
                        message="원자료 정책과 선택한 시뮬레이션 정책이 다릅니다. 선택한 정책을 사용합니다.", transaction_id=record.transaction.id))
                us_sale = (record.instrument is not None and record.instrument.market == Market.US
                           and record.transaction.transaction_type == TransactionType.SELL)
                if us_sale and event.treatment != TaxTreatment.STOCK_CAPITAL_GAIN:
                    self._block(issues, "US_SALE_TREATMENT", "US_STOCK", "미국 주식/ETF 매도는 주식 양도 과세분류가 필요합니다.", record)
                    continue
                if event.treatment == TaxTreatment.STOCK_CAPITAL_GAIN:
                    if not us_sale:
                        self._block(issues, "DOMESTIC_TAXABLE_STOCK", "US_STOCK",
                            "과세대상 국내주식이 있어 공제 공유·혼합세율 계산이 필요합니다. 미국 세금만 확정하지 않습니다.", record)
                    else:
                        gains.append(event.taxable_income_krw)
                elif event.treatment == TaxTreatment.FINANCIAL_INCOME:
                    if (event.taxable_income_krw < 0
                        or (record.transaction.transaction_type == TransactionType.SELL and record.instrument
                            and record.instrument.asset_type in (AssetType.STOCK, AssetType.ETF_DOMESTIC_EQUITY))):
                        self._block(issues, "INVALID_FINANCIAL_BASIS", "FINANCIAL_INCOME",
                                    "일반 금융소득의 음수 통산 또는 자산 과세분류를 확인해야 합니다.", record)
                        continue
                    financial.append(event.taxable_income_krw)
                elif event.treatment == TaxTreatment.SEPARATE_DIVIDEND:
                    if record.instrument is None or record.instrument.market != Market.KR or record.instrument.asset_type != AssetType.STOCK:
                        self._block(issues, "INVALID_SEPARATE_DIVIDEND", "FINANCIAL_INCOME", "적격 국내 주식 배당 분류인지 확인해야 합니다.", record)
                        continue
                    issues.append(CalculationIssue(code="SEPARATE_DIVIDEND_SELECTION", scope="FINANCIAL_INCOME", severity="WARNING",
                        message="고배당 분리과세는 적격성·신청이 확인된 분류로 가정해 기준금액에서 제외합니다. 별도 세액은 미계산입니다.",
                        transaction_id=record.transaction.id))
                elif event.treatment != TaxTreatment.EXEMPT:
                    self._block(issues, "ACCOUNT_TREATMENT_MISMATCH", scope, "일반계좌의 과세분류가 일치하지 않습니다.", record)
                    continue
                if event.event_type == TaxEventType.DIVIDEND:
                    if event.treatment == TaxTreatment.EXEMPT and (event.domestic_withholding_krw or event.foreign_withholding_krw):
                        self._block(issues, "EXEMPT_WITHHOLDING_REVIEW", "DIVIDEND", "비과세 배당의 원천징수 정정·환급을 확인해야 합니다.", record)
                        continue
                    try:
                        cash = calculate_dividend(DividendInput(
                            gross_income_krw=event.economic_income_krw, market=record.instrument.market,
                            domestic_withholding_krw=event.domestic_withholding_krw,
                            foreign_withholding_krw=event.foreign_withholding_krw, parameters=policy.parameters,
                        ))
                    except ValueError as exc:
                        self._block(issues, "DIVIDEND_WITHHOLDING_REVIEW", "DIVIDEND", str(exc), record)
                        continue
                    dividends.append(PostedDividend(transaction_id=record.transaction.id, account_id=record.account.id,
                        ticker=record.instrument.ticker, treatment=event.treatment, cash=cash))
            cg_blocked = any(i.severity == "BLOCKER" and i.scope == "US_STOCK" for i in issues)
            fi_blocked = any(i.severity == "BLOCKER" and i.scope == "FINANCIAL_INCOME" for i in issues)
            result = None if cg_blocked else calculate_us_stock_tax(CapitalGainsInput(realized_gains_krw=tuple(gains), parameters=policy.parameters))
            alert = assess_financial_income(FinancialIncomeInput(ordinary_income_bases_krw=tuple(financial),
                threshold_krw=policy.parameters.financial_income_threshold_krw,
                complete=request.data_coverage_confirmed and not fi_blocked))
            return AnnualTaxReport(taxpayer_id=request.taxpayer_id, tax_year=request.tax_year, policy_id=policy.id,
                policy_status=policy.status, imported_income_transaction_count=len(general_records),
                us_stock_tax=result, financial_income=alert, dividends=tuple(dividends), issues=tuple(issues))

    def isa_contract(self, request: ISAContractRequest) -> ISAContractReport:
        if not isinstance(request, ISAContractRequest):
            raise TypeError("expected ISAContractRequest")
        request = ISAContractRequest.model_validate(request.model_dump())
        with self._snapshot():
            account = self._require(Account, request.account_id)
            self._taxpayer(account.taxpayer_id)
            if account.account_type != AccountType.ISA:
                raise UnsupportedTaxCase("ISA contract calculation requires an ISA account")
            if request.as_of < account.opened_on:
                raise ValueError("as_of precedes ISA opening")
            end = min(request.as_of, account.closed_on) if account.closed_on else request.as_of
            # Policy applies on the hypothetical settlement date, not every past event date.
            policy = self._policy(request.policy_id, end, end)
            issues = self._base_issues(policy, request.data_coverage_confirmed)
            minimum_date = _anniversary(account.opened_on, 3)
            minimum_met = end >= minimum_date
            if account.maturity_on < minimum_date or (account.closed_on and account.closed_on <= request.as_of and not minimum_met):
                issues.append(CalculationIssue(code="EARLY_ISA_CLOSURE", scope="ISA", severity="BLOCKER",
                    message="3년 미만 계약 또는 중도해지입니다. 예외 사유·추징 판정 없이 특례세액을 계산할 수 없습니다."))
            elif not minimum_met:
                issues.append(CalculationIssue(code="ISA_FUTURE_ELIGIBILITY", scope="ISA", severity="WARNING",
                    message="3년 유지조건 전입니다. 계산값은 요건을 충족해 정산한다는 가정의 예상 세금입니다."))
            records = self._records(account.taxpayer_id, date(1900, 1, 1), end, account.id)
            incomes, losses, economic, excluded = [], [], 0, 0
            for record in records:
                event = record.event
                if self._outside_account_period(record):
                    self._block(issues, "ACCOUNT_PERIOD_MISMATCH", "ISA", "거래가 ISA 계약기간 밖에 있습니다.", record)
                    continue
                if event is None or event.treatment == TaxTreatment.REVIEW_REQUIRED or event.taxable_income_krw is None:
                    self._block(issues, "ISA_BASIS_UNCONFIRMED", "ISA", "ISA 과세분류 또는 통산 산입액이 미확정입니다.", record)
                    continue
                if event.treatment not in (TaxTreatment.ISA, TaxTreatment.EXEMPT):
                    self._block(issues, "ISA_TREATMENT_MISMATCH", "ISA", "ISA 이벤트의 과세분류가 일치하지 않습니다.", record)
                    continue
                basis = event.taxable_income_krw
                if record.instrument and record.instrument.market != Market.KR:
                    self._block(issues, "ISA_INELIGIBLE_ASSET", "ISA", "미국 직접투자 자산은 ISA 계산 대상이 아닙니다.", record)
                    continue
                if event.event_type == TaxEventType.CAPITAL_GAIN and record.instrument:
                    asset_type = record.instrument.asset_type
                    if (asset_type == AssetType.ETF_DOMESTIC_EQUITY and basis != 0
                        or asset_type == AssetType.STOCK and not min(event.economic_income_krw, 0) <= basis <= 0):
                        self._block(issues, "ISA_EXEMPT_CAPITAL_BASIS", "ISA",
                            "국내 주식/국내주식형 ETF의 비과세 차익과 적격 손실을 구분해야 합니다.", record)
                        continue
                if event.economic_income_krw * basis < 0:
                    self._block(issues, "ISA_BASIS_SIGN", "ISA", "경제적 손익과 산입액 부호가 다릅니다.", record)
                    continue
                economic += event.economic_income_krw
                excluded += event.economic_income_krw - basis
                if basis >= 0:
                    incomes.append(basis)
                else:
                    losses.append(-basis)
            blocked = any(i.severity == "BLOCKER" for i in issues)
            projection = None if blocked else calculate_isa_tax(ISAInput(isa_type=account.isa_type,
                taxable_incomes_krw=tuple(incomes), eligible_losses_krw=tuple(losses), parameters=policy.parameters))
            return ISAContractReport(account_id=account.id, as_of=request.as_of, period_start=account.opened_on,
                period_end=end, policy_id=policy.id, policy_status=policy.status,
                imported_income_transaction_count=len(records), economic_net_income_krw=economic,
                excluded_net_income_krw=excluded, projection=projection, minimum_holding_period_met=minimum_met,
                contract_maturity_reached=end >= account.maturity_on, issues=tuple(issues))
