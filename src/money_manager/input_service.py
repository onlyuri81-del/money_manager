"""Validated form persistence. No tax classification or cost estimation is inferred."""
import re
import sqlite3
from decimal import Decimal, InvalidOperation

from .db import get, insert, to_scaled
from .models import Account, CashBalance, Holding, Instrument, TaxEvent, Taxpayer, Transaction


def decimal_input(text: str, label: str, *, optional: bool = False) -> Decimal | None:
    value = text.strip()
    if not value and optional:
        return None
    if not re.fullmatch(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value):
        raise ValueError(f"{label}: 숫자를 입력하세요. 예: 10.5 또는 1,250,000")
    try:
        result = Decimal(value.replace(",", ""))
        to_scaled(result)  # exact, finite, six-place / SQLite storage bounds
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label}: 소수점은 최대 6자리이고 저장 가능한 범위여야 합니다.") from None
    return result


def won_input(text: str, label: str, *, optional: bool = False) -> int | None:
    result = decimal_input(text, label, optional=optional)
    if result is None:
        return None
    if result != result.to_integral_value():
        raise ValueError(f"{label}: 원 단위 정수로 입력하세요.")
    return int(result)


def all_models(db, cls, table):
    # Internal callers only; table names are not taken from the UI.
    from .db import TABLES
    if TABLES.get(cls) != table:
        raise ValueError("unsupported model table")
    return [get(db, cls, row[0]) for row in db.execute(f"SELECT id FROM {table} ORDER BY rowid")]


def save_account(db, *, owner_id, owner_name, **values):
    """Create owner/account atomically. Existing owner ID comes from a selection."""
    owner = get(db, Taxpayer, owner_id) if owner_id else Taxpayer(name=owner_name)
    if owner is None:
        raise ValueError("선택한 사용자를 찾을 수 없습니다.")
    account = Account(taxpayer_id=owner.id, **values)
    with db:
        if not owner_id:
            insert(db, owner)
        insert(db, account)
    return account


def resolve_instrument(db, instrument):
    row = db.execute("SELECT id FROM instruments WHERE market=? AND ticker=?",
                     (instrument.market.value, instrument.ticker)).fetchone()
    if row is None:
        insert(db, instrument)
        return instrument
    existing = get(db, Instrument, row[0])
    if (existing.currency, existing.asset_type, existing.pension_eligible) != (
            instrument.currency, instrument.asset_type, instrument.pension_eligible):
        raise ValueError("이미 등록된 종목의 상품 종류·연금 편입 가능 여부와 다릅니다. 기존 분류를 확인하세요.")
    return existing


def save_holding(db, instrument: Instrument, **values):
    """Save a full snapshot; preserve ID and never create a second position."""
    with db:
        instrument = resolve_instrument(db, instrument)
        holding = Holding(instrument_id=instrument.id, **values)
        row = db.execute("SELECT id FROM holdings WHERE account_id=? AND instrument_id=?",
                         (holding.account_id, instrument.id)).fetchone()
        if row is None:
            insert(db, holding)
        else:
            db.execute("""UPDATE holdings SET quantity_micros=?, average_buy_price_micros=?,
                acquisition_cost_krw=?, current_price_micros=?, current_fx_micros=?, valued_at=?
                WHERE id=?""", (
                to_scaled(holding.quantity), to_scaled(holding.average_buy_price), holding.acquisition_cost_krw,
                None if holding.current_price is None else to_scaled(holding.current_price),
                None if holding.current_fx is None else to_scaled(holding.current_fx),
                None if holding.valued_at is None else holding.valued_at.isoformat(), row[0]))
            holding = get(db, Holding, row[0])
    return holding


def save_cash(db, cash: CashBalance):
    # Revalidation blocks model_copy / construct bypass before updating existing rows.
    cash = CashBalance.model_validate(cash.model_dump())
    with db:
        row = db.execute("SELECT id FROM cash_balances WHERE account_id=? AND currency=?",
                         (cash.account_id, cash.currency.value)).fetchone()
        if row is None:
            insert(db, cash)
        else:
            db.execute("UPDATE cash_balances SET amount_micros=?, fx_micros=?, valued_at=? WHERE id=?",
                       (to_scaled(cash.amount), to_scaled(cash.fx), cash.valued_at.isoformat(), row[0]))
            cash = get(db, CashBalance, row[0])
    return cash


def save_transaction(db, transaction: Transaction, event: TaxEvent | None = None):
    """A tax event failure also rolls back its transaction; external_ref deduplicates."""
    with db:
        insert(db, transaction)
        if event is not None:
            insert(db, event)
    return transaction


def friendly_error(exc):
    if isinstance(exc, sqlite3.IntegrityError):
        value = str(exc)
        if "transactions.account_id, transactions.external_ref" in value:
            return "이 계좌에 같은 거래번호가 이미 저장돼 있습니다. 거래번호를 확인하세요."
        if "one_active_isa" in value or "accounts.taxpayer_id" in value:
            return "이 사용자에게 이미 사용 중인 ISA 계좌가 있습니다."
        if "ineligible" in value:
            return "이 계좌에 등록할 수 없는 상품이거나 통화가 맞지 않습니다. 상품 종류와 계좌를 확인하세요."
        return "자료가 계좌·거래 규칙과 맞지 않아 저장하지 않았습니다. 입력값을 확인하세요."
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        field = exc.errors()[0]["loc"]
        labels = {"name": "이름", "broker": "증권사", "label": "계좌 별칭", "ticker": "종목코드",
                  "quantity": "수량", "average_buy_price": "평균매수가", "acquisition_cost_krw": "총 원화 취득금액",
                  "current_price": "현재가", "current_fx": "환율", "amount": "현금 잔액", "fx": "환율",
                  "gross_amount": "거래금액", "fees": "수수료", "unit_price": "거래단가",
                  "external_ref": "거래번호", "basis_source": "과세자료 출처"}
        label = labels.get(field[0], "입력값") if field else "입력값"
        return f"{label}을 확인하세요. 필수 값과 날짜, 금액의 부호·범위를 확인해 주세요."
    return str(exc)
