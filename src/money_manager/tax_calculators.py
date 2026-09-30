"""Pure, explicit-input tax simulations. No database reads or tax classification.

All inputs are KRW after acquisition FX / eligible costs have been reconciled.
Tax is simulated at the combined national+local rate, rounded DOWN to one KRW.
This is not the separate filing/payment rounding used by a tax authority.
"""
from decimal import Decimal, ROUND_DOWN, localcontext

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import ISAType, Market, NonNegativeKRW, Rate, SignedKRW, TaxParameters


class ValueModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class CapitalGainsInput(ValueModel):
    realized_gains_krw: tuple[SignedKRW, ...]
    parameters: TaxParameters = Field(default_factory=TaxParameters)


class CapitalGainsResult(ValueModel):
    realized_net_gain_krw: int
    deduction_limit_krw: int
    deduction_used_krw: int
    taxable_gain_krw: int
    rate: Decimal
    estimated_tax_krw: int
    net_gain_after_estimated_tax_krw: int


class DividendInput(ValueModel):
    gross_income_krw: NonNegativeKRW
    market: Market
    domestic_withholding_krw: NonNegativeKRW | None = None
    foreign_withholding_krw: NonNegativeKRW | None = None
    # A scenario is explicit. There is deliberately no automatic US 15% default.
    foreign_rate: Rate | None = None
    parameters: TaxParameters = Field(default_factory=TaxParameters)

    @model_validator(mode="after")
    def withholding_shape(self):
        if self.market == Market.KR:
            if self.foreign_withholding_krw not in (None, 0) or self.foreign_rate is not None:
                raise ValueError("KR standard-income calculator does not accept foreign withholding")
        else:
            if self.foreign_withholding_krw is None and self.foreign_rate is None:
                raise ValueError("US income needs actual foreign withholding or an explicit scenario rate")
            if self.foreign_withholding_krw is not None and self.foreign_rate is not None:
                raise ValueError("choose actual foreign withholding OR a scenario rate")
        actual = (self.domestic_withholding_krw or 0) + (self.foreign_withholding_krw or 0)
        if actual > self.gross_income_krw:
            raise ValueError("withholding exceeds gross income; correction/refund is a separate workflow")
        return self


class DividendResult(ValueModel):
    gross_income_krw: int
    domestic_withholding_krw: int | None
    foreign_withholding_krw: int
    total_withholding_krw: int | None
    cash_after_known_withholding_krw: int
    net_cash_krw: int | None
    basis: str  # ACTUAL / STANDARD_RATE_SCENARIO / FOREIGN_RATE_SCENARIO
    warnings: tuple[str, ...]


class ISAInput(ValueModel):
    isa_type: ISAType
    # Full contract-period eligible bases, not a yearly reset or raw portfolio P&L.
    taxable_incomes_krw: tuple[NonNegativeKRW, ...]
    eligible_losses_krw: tuple[NonNegativeKRW, ...]  # Positive magnitudes.
    parameters: TaxParameters = Field(default_factory=TaxParameters)


class ISAResult(ValueModel):
    eligible_income_krw: int
    eligible_loss_krw: int
    net_eligible_income_krw: int
    exemption_limit_krw: int
    exemption_used_krw: int
    taxable_excess_krw: int
    rate: Decimal
    estimated_tax_if_eligible_krw: int


class FinancialIncomeInput(ValueModel):
    ordinary_income_bases_krw: tuple[NonNegativeKRW, ...]
    threshold_krw: NonNegativeKRW = 20_000_000
    # False means unknown/unimported income may remain.
    complete: bool = Field(default=True, strict=True)


class FinancialIncomeResult(ValueModel):
    known_ordinary_income_krw: int
    threshold_krw: int
    threshold_exceeded: bool | None
    complete: bool


def _validated(model, model_class):
    if not isinstance(model, model_class):
        raise TypeError(f"expected {model_class.__name__}")
    # Revalidate copies and nested policy parameters instead of trusting constructors.
    return model_class.model_validate(model.model_dump(mode="python", warnings=False))


def _tax(amount_krw: int, rate: Decimal) -> int:
    with localcontext() as context:
        context.prec = max(64, len(str(abs(amount_krw))) + 20)
        return int((Decimal(amount_krw) * rate).to_integral_value(rounding=ROUND_DOWN))


def calculate_us_stock_tax(request: CapitalGainsInput) -> CapitalGainsResult:
    request = _validated(request, CapitalGainsInput)
    net = sum(request.realized_gains_krw)
    deduction = min(max(net, 0), request.parameters.stock_basic_deduction_krw)
    taxable = max(net - deduction, 0)
    tax = _tax(taxable, request.parameters.overseas_stock_rate)
    return CapitalGainsResult(
        realized_net_gain_krw=net, deduction_limit_krw=request.parameters.stock_basic_deduction_krw,
        deduction_used_krw=deduction, taxable_gain_krw=taxable, rate=request.parameters.overseas_stock_rate,
        estimated_tax_krw=tax, net_gain_after_estimated_tax_krw=net - tax,
    )


def calculate_dividend(request: DividendInput) -> DividendResult:
    """Cash after withholding, NEVER final tax after comprehensive taxation."""
    request = _validated(request, DividendInput)
    gross = request.gross_income_krw
    warnings = ["원천징수 후 현금이며 종합과세 추가세액·외국납부세액공제는 포함하지 않습니다."]
    foreign = request.foreign_withholding_krw or 0
    domestic = request.domestic_withholding_krw
    basis = "ACTUAL"
    if request.market == Market.KR and domestic is None:
        domestic = _tax(gross, request.parameters.domestic_financial_withholding_rate)
        basis = "STANDARD_RATE_SCENARIO"
        warnings.append("국내 일반 배당·이자 기준 세율을 가정했습니다. 특례 상품에는 그대로 적용하지 않습니다.")
    elif request.market == Market.US:
        if request.foreign_rate is not None:
            foreign = _tax(gross, request.foreign_rate)
            basis = "FOREIGN_RATE_SCENARIO"
            warnings.append("해외 원천징수는 사용자가 지정한 시뮬레이션 세율입니다.")
        if domestic is None:
            warnings.append("국내 원천징수액이 미확정이므로 최종 입금액은 미확정입니다.")
    known_total = foreign + (domestic or 0)
    if known_total > gross:
        raise ValueError("calculated withholding exceeds gross income")
    total = None if domestic is None else known_total
    return DividendResult(
        gross_income_krw=gross, domestic_withholding_krw=domestic, foreign_withholding_krw=foreign,
        total_withholding_krw=total, cash_after_known_withholding_krw=gross-known_total,
        net_cash_krw=None if total is None else gross-total, basis=basis, warnings=tuple(warnings),
    )


def calculate_isa_tax(request: ISAInput) -> ISAResult:
    request = _validated(request, ISAInput)
    income, loss = sum(request.taxable_incomes_krw), sum(request.eligible_losses_krw)
    net = income - loss
    limit = (request.parameters.isa_general_exemption_krw if request.isa_type == ISAType.GENERAL
             else request.parameters.isa_preferential_exemption_krw)
    used = min(max(net, 0), limit)
    excess = max(net-used, 0)
    return ISAResult(
        eligible_income_krw=income, eligible_loss_krw=loss, net_eligible_income_krw=net,
        exemption_limit_krw=limit, exemption_used_krw=used, taxable_excess_krw=excess,
        rate=request.parameters.isa_excess_rate, estimated_tax_if_eligible_krw=_tax(excess, request.parameters.isa_excess_rate),
    )


def assess_financial_income(request: FinancialIncomeInput) -> FinancialIncomeResult:
    request = _validated(request, FinancialIncomeInput)
    total = sum(request.ordinary_income_bases_krw)
    exceeded = True if total > request.threshold_krw else (False if request.complete else None)
    return FinancialIncomeResult(known_ordinary_income_krw=total, threshold_krw=request.threshold_krw,
                                 threshold_exceeded=exceeded, complete=request.complete)
