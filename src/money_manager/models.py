"""Validated domain objects. No tax computations or automatic classifications."""
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import uuid4

from pydantic import (
    BaseModel, BeforeValidator, ConfigDict, Field, HttpUrl, StringConstraints,
    field_validator, model_validator,
)


def reject_float(value):
    if isinstance(value, (float, bool)):
        raise ValueError("use a Decimal, decimal string, or integer; floats are not accepted")
    return value


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
NonNegativeKRW = Annotated[int, Field(strict=True, ge=0, le=9_000_000_000_000_000)]
PositiveKRW = Annotated[int, Field(strict=True, gt=0, le=9_000_000_000_000)]
SignedKRW = Annotated[int, Field(strict=True, ge=-9_000_000_000_000_000, le=9_000_000_000_000_000)]
NonNegativeDecimal = Annotated[
    Decimal, BeforeValidator(reject_float),
    Field(ge=0, le=Decimal("9000000000000"), decimal_places=6, allow_inf_nan=False),
]
PositiveDecimal = Annotated[
    Decimal, BeforeValidator(reject_float),
    Field(gt=0, le=Decimal("9000000000000"), decimal_places=6, allow_inf_nan=False),
]
Rate = Annotated[Decimal, BeforeValidator(reject_float), Field(ge=0, le=1, decimal_places=6, allow_inf_nan=False)]
Year = Annotated[int, Field(strict=True, ge=1900, le=9999)]


class AccountType(StrEnum):
    GENERAL = "GENERAL"
    ISA = "ISA"
    PENSION_SAVINGS = "PENSION_SAVINGS"
    IRP = "IRP"


class ISAType(StrEnum):
    GENERAL = "GENERAL"
    LOW_INCOME = "LOW_INCOME"
    FARMER = "FARMER"


class Market(StrEnum):
    KR = "KR"
    US = "US"


class Currency(StrEnum):
    KRW = "KRW"
    USD = "USD"


class Residency(StrEnum):
    KR = "KR"
    OTHER = "OTHER"


class AssetType(StrEnum):
    STOCK = "STOCK"
    ETF_DOMESTIC_EQUITY = "ETF_DOMESTIC_EQUITY"
    ETF_OTHER = "ETF_OTHER"


class TransactionType(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"
    DEPOSIT = "DEPOSIT"
    WITHDRAWAL = "WITHDRAWAL"
    TRANSFER_IN = "TRANSFER_IN"
    TRANSFER_OUT = "TRANSFER_OUT"


class TaxEventType(StrEnum):
    CAPITAL_GAIN = "CAPITAL_GAIN"
    DIVIDEND = "DIVIDEND"
    INTEREST = "INTEREST"


class TaxTreatment(StrEnum):
    STOCK_CAPITAL_GAIN = "STOCK_CAPITAL_GAIN"
    FINANCIAL_INCOME = "FINANCIAL_INCOME"
    EXEMPT = "EXEMPT"
    ISA = "ISA"
    PENSION_DEFERRED = "PENSION_DEFERRED"
    SEPARATE_DIVIDEND = "SEPARATE_DIVIDEND"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class PolicyStatus(StrEnum):
    DRAFT = "DRAFT"
    VERIFIED = "VERIFIED"
    SCENARIO = "SCENARIO"


class ContributionSource(StrEnum):
    PERSONAL = "PERSONAL"
    RETIREMENT_TRANSFER = "RETIREMENT_TRANSFER"
    ISA_TRANSFER = "ISA_TRANSFER"


class Entity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
    id: Identifier = Field(default_factory=lambda: uuid4().hex)

    @field_validator("*", mode="after")
    @classmethod
    def check_temporal_values(cls, value):
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("timestamps must include a timezone")
            return value.astimezone(timezone.utc)
        if isinstance(value, date) and value.year < 1900:
            raise ValueError("dates before 1900 are out of scope")
        return value


class Taxpayer(Entity):
    name: Label
    birth_date: date | None = None
    residency: Residency = Residency.KR
    us_tax_person: bool = Field(default=False, strict=True)


class TaxpayerYear(Entity):
    taxpayer_id: Identifier
    tax_year: Year
    salary_krw: NonNegativeKRW | None = None
    comprehensive_income_krw: NonNegativeKRW | None = None


class Account(Entity):
    taxpayer_id: Identifier
    broker: Label
    label: Label
    account_type: AccountType
    isa_type: ISAType | None = None
    opened_on: date
    maturity_on: date | None = None
    closed_on: date | None = None

    @model_validator(mode="after")
    def account_dates(self):
        if self.account_type == AccountType.ISA:
            if self.isa_type is None or self.maturity_on is None:
                raise ValueError("ISA needs its subtype and contractual maturity date")
        elif self.isa_type is not None or self.maturity_on is not None:
            raise ValueError("ISA fields are only valid for ISA accounts")
        if self.maturity_on is not None and self.maturity_on <= self.opened_on:
            raise ValueError("maturity must follow opening")
        if self.closed_on is not None and self.closed_on < self.opened_on:
            raise ValueError("closing cannot precede opening")
        return self


class Instrument(Entity):
    ticker: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    name: Label
    market: Market
    currency: Currency
    asset_type: AssetType
    pension_eligible: bool = Field(default=False, strict=True)

    @field_validator("ticker")
    @classmethod
    def uppercase_ticker(cls, value):
        return value.upper()

    @model_validator(mode="after")
    def classification(self):
        expected = Currency.KRW if self.market == Market.KR else Currency.USD
        if self.currency != expected:
            raise ValueError("market/currency mismatch in the KR/US scope")
        if self.asset_type == AssetType.ETF_DOMESTIC_EQUITY and self.market != Market.KR:
            raise ValueError("domestic equity ETF classification requires KR listing")
        if self.pension_eligible and (self.market != Market.KR or self.asset_type == AssetType.STOCK):
            raise ValueError("direct stocks / US listings cannot be pension eligible")
        return self


class Holding(Entity):
    account_id: Identifier
    instrument_id: Identifier
    quantity: PositiveDecimal
    average_buy_price: NonNegativeDecimal
    # Remaining KRW acquisition cost, including acquisition FX and eligible fees.
    # This is authoritative for future KRW gains; average_buy_price is display data.
    acquisition_cost_krw: NonNegativeKRW
    current_price: NonNegativeDecimal | None = None
    current_fx: PositiveDecimal | None = None
    valued_at: datetime | None = None

    @model_validator(mode="after")
    def complete_quote(self):
        fields = (self.current_price, self.current_fx, self.valued_at)
        if any(v is not None for v in fields) and any(v is None for v in fields):
            raise ValueError("quote must include current price, KRW FX and timestamp together")
        return self


class CashBalance(Entity):
    account_id: Identifier
    currency: Currency
    amount: NonNegativeDecimal
    fx: PositiveDecimal
    valued_at: datetime

    @model_validator(mode="after")
    def krw_fx(self):
        if self.currency == Currency.KRW and self.fx != 1:
            raise ValueError("KRW/KRW FX must be exactly 1")
        return self


class Transaction(Entity):
    account_id: Identifier
    instrument_id: Identifier | None = None
    external_ref: Label
    transaction_type: TransactionType
    traded_on: date
    recognized_on: date  # Settlement/payment recognition date, supplied by importer.
    currency: Currency
    gross_amount: NonNegativeDecimal
    fees: NonNegativeDecimal = Decimal("0")
    fx: PositiveDecimal
    quantity: PositiveDecimal | None = None
    unit_price: NonNegativeDecimal | None = None

    @model_validator(mode="after")
    def transaction_shape(self):
        if self.recognized_on < self.traded_on:
            raise ValueError("recognition cannot precede trade date")
        if self.currency == Currency.KRW and self.fx != 1:
            raise ValueError("KRW/KRW FX must be exactly 1")
        if self.transaction_type in (TransactionType.BUY, TransactionType.SELL):
            if self.instrument_id is None or self.quantity is None or self.unit_price is None:
                raise ValueError("BUY/SELL requires an instrument, quantity and unit price")
        elif self.quantity is not None or self.unit_price is not None:
            raise ValueError("non-trade transactions do not have quantity or unit price")
        if self.transaction_type == TransactionType.DIVIDEND and self.instrument_id is None:
            raise ValueError("DIVIDEND requires an instrument")
        return self


class TaxParameters(BaseModel):
    """Requested baseline assumptions, NOT verified operative tax law by themselves."""
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)
    overseas_stock_rate: Rate = Decimal("0.22")  # National + local income tax.
    stock_basic_deduction_krw: NonNegativeKRW = 2_500_000
    domestic_financial_withholding_rate: Rate = Decimal("0.154")
    financial_income_threshold_krw: NonNegativeKRW = 20_000_000
    isa_general_exemption_krw: NonNegativeKRW = 2_000_000
    isa_preferential_exemption_krw: NonNegativeKRW = 4_000_000
    isa_excess_rate: Rate = Decimal("0.099")
    pension_rate_under_70: Rate = Decimal("0.055")
    pension_rate_70_to_79: Rate = Decimal("0.044")
    pension_rate_80_plus: Rate = Decimal("0.033")


class TaxPolicy(Entity):
    code: Label
    effective_from: date
    effective_to: date  # Explicit inclusive period; no implicit infinite validity.
    status: PolicyStatus = PolicyStatus.DRAFT
    verified_on: date | None = None
    source_urls: tuple[HttpUrl, ...] = ()
    parameters: TaxParameters = Field(default_factory=TaxParameters)

    @model_validator(mode="after")
    def validity(self):
        if self.effective_to < self.effective_from:
            raise ValueError("invalid policy period")
        if self.status == PolicyStatus.VERIFIED and (self.verified_on is None or not self.source_urls):
            raise ValueError("verified policies need verification date and source URLs")
        return self


class TaxEvent(Entity):
    transaction_id: Identifier
    account_id: Identifier
    recognized_on: date
    event_type: TaxEventType
    treatment: TaxTreatment = TaxTreatment.REVIEW_REQUIRED
    economic_income_krw: SignedKRW
    # None means unclassified/unknown, NOT zero. ISA values use eligible netting basis.
    taxable_income_krw: SignedKRW | None = None
    domestic_withholding_krw: NonNegativeKRW = 0
    foreign_withholding_krw: NonNegativeKRW = 0
    applied_rate: Rate | None = None
    policy_id: Identifier | None = None
    basis_source: Label  # Broker statement / import batch identifier.

    @property
    def tax_year(self) -> int:
        return self.recognized_on.year

    @model_validator(mode="after")
    def income_shape(self):
        if self.event_type != TaxEventType.CAPITAL_GAIN:
            if self.economic_income_krw < 0 or (self.taxable_income_krw is not None and self.taxable_income_krw < 0):
                raise ValueError("negative dividends/interest require an explicit reversal workflow")
        if self.treatment == TaxTreatment.EXEMPT and self.taxable_income_krw != 0:
            raise ValueError("EXEMPT income requires explicit zero taxable basis")
        if self.treatment == TaxTreatment.STOCK_CAPITAL_GAIN and self.event_type != TaxEventType.CAPITAL_GAIN:
            raise ValueError("stock capital-gain treatment requires CAPITAL_GAIN")
        if self.treatment == TaxTreatment.SEPARATE_DIVIDEND and self.event_type != TaxEventType.DIVIDEND:
            raise ValueError("separate-dividend treatment requires DIVIDEND")
        return self


class PensionContribution(Entity):
    transaction_id: Identifier
    account_id: Identifier
    recognized_on: date
    contribution_source: ContributionSource
    amount_krw: PositiveKRW
    credit_claimed_krw: NonNegativeKRW | None = None  # Claimed contribution basis, not credit amount.

    @property
    def tax_year(self) -> int:
        return self.recognized_on.year

    @model_validator(mode="after")
    def credit_basis(self):
        if self.credit_claimed_krw is not None and self.credit_claimed_krw > self.amount_krw:
            raise ValueError("claimed contribution basis exceeds payment")
        if self.contribution_source == ContributionSource.RETIREMENT_TRANSFER and self.credit_claimed_krw not in (None, 0):
            raise ValueError("retirement transfer is not a personal contribution credit basis")
        return self
