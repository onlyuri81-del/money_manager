"""Descriptive portfolio diagnostics from explicitly entered snapshots and transactions."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext

from .db import get
from .models import CashBalance, Holding, Transaction, TransactionType


def _won(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def portfolio_diagnostics(connection, taxpayer_id: str, tax_year: int, as_of: date) -> dict:
    with localcontext() as context:
        context.prec = 80
        return _portfolio_diagnostics(connection, taxpayer_id, tax_year, as_of)


def _portfolio_diagnostics(connection, taxpayer_id: str, tax_year: int, as_of: date) -> dict:
    period_start = date(tax_year, 1, 1)
    period_end = min(as_of, date(tax_year, 12, 31))
    accounts = connection.execute(
        "SELECT id FROM accounts WHERE taxpayer_id=? ORDER BY rowid", (taxpayer_id,)
    ).fetchall()
    account_ids = [row[0] for row in accounts]

    holdings_value = Decimal(0)
    priced_holdings = 0
    unpriced_holdings = 0
    if account_ids:
        placeholders = ",".join("?" for _ in account_ids)
        rows = connection.execute(
            f"""SELECT id FROM holdings WHERE account_id IN ({placeholders}) ORDER BY rowid""",
            account_ids,
        ).fetchall()
        for row in rows:
            holding = get(connection, Holding, row[0])
            if holding.current_price is None:
                unpriced_holdings += 1
                continue
            fx = holding.current_fx if holding.current_fx is not None else Decimal(1)
            holdings_value += holding.quantity * holding.current_price * fx
            priced_holdings += 1

    cash_value = Decimal(0)
    if account_ids:
        placeholders = ",".join("?" for _ in account_ids)
        rows = connection.execute(
            f"""SELECT id FROM cash_balances WHERE account_id IN ({placeholders}) ORDER BY rowid""",
            account_ids,
        ).fetchall()
        for row in rows:
            cash = get(connection, CashBalance, row[0])
            cash_value += cash.amount * cash.fx

    flow_rows = connection.execute(
        """SELECT t.id FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE a.taxpayer_id=? AND t.recognized_on BETWEEN ? AND ?
             AND t.transaction_type IN ('DEPOSIT','WITHDRAWAL','TRANSFER_IN','TRANSFER_OUT')
           ORDER BY t.recognized_on, t.id""",
        (taxpayer_id, period_start.isoformat(), period_end.isoformat()),
    ).fetchall()
    monthly_totals: dict[str, list[Decimal]] = {}
    for row in flow_rows:
        transaction = get(connection, Transaction, row[0])
        month = transaction.recognized_on.strftime("%Y-%m")
        totals = monthly_totals.setdefault(month, [Decimal(0), Decimal(0)])
        index = 0 if transaction.transaction_type in (
            TransactionType.DEPOSIT, TransactionType.TRANSFER_IN
        ) else 1
        totals[index] += transaction.gross_amount * transaction.fx
    monthly_flows = [
        {
            "월": month,
            "입금·이체유입(원)": _won(totals[0]),
            "출금·이체유출(원)": _won(totals[1]),
        }
        for month, totals in sorted(monthly_totals.items())
    ]
    for flow in monthly_flows:
        flow["순흐름(원)"] = flow["입금·이체유입(원)"] - flow["출금·이체유출(원)"]

    tax_row = connection.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN e.id IS NULL OR e.treatment='REVIEW_REQUIRED'
                                 OR e.taxable_income_krw IS NULL THEN 1 ELSE 0 END) AS incomplete
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           LEFT JOIN tax_events e ON e.transaction_id=t.id
           WHERE a.taxpayer_id=? AND t.recognized_on BETWEEN ? AND ?
             AND t.transaction_type IN ('SELL','DIVIDEND','INTEREST')""",
        (taxpayer_id, period_start.isoformat(), period_end.isoformat()),
    ).fetchone()
    return {
        "priced_holdings_count": priced_holdings,
        "unpriced_holdings_count": unpriced_holdings,
        "holdings_value_krw": _won(holdings_value),
        "cash_value_krw": _won(cash_value),
        "known_snapshot_value_krw": _won(holdings_value + cash_value),
        "monthly_flows": monthly_flows,
        "tax_income_transaction_count": tax_row["total"],
        "incomplete_tax_event_count": tax_row["incomplete"] or 0,
        "as_of": as_of,
    }
