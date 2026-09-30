"""SQLite connection, atomic version-1 migration and lossless model serialization."""
import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal, localcontext
from importlib.resources import files
from pathlib import Path

from .models import (
    Account, CashBalance, Entity, Holding, Instrument, PensionContribution,
    TaxEvent, Taxpayer, TaxpayerYear, TaxPolicy, Transaction,
)

SCALE = 1_000_000
MAX_SQLITE_INT = 2**63 - 1
MIN_SQLITE_INT = -(2**63)

# Fixed internal allowlist: user input never becomes a table or column name.
TABLES = {
    Taxpayer: "taxpayers", TaxpayerYear: "taxpayer_years", Account: "accounts",
    Instrument: "instruments", Holding: "holdings", CashBalance: "cash_balances",
    Transaction: "transactions", TaxPolicy: "tax_policies", TaxEvent: "tax_events",
    PensionContribution: "pension_contributions",
}
SCALED_FIELDS = {
    Holding: {"quantity": "quantity_micros", "average_buy_price": "average_buy_price_micros",
              "current_price": "current_price_micros", "current_fx": "current_fx_micros"},
    CashBalance: {"amount": "amount_micros", "fx": "fx_micros"},
    Transaction: {"gross_amount": "gross_amount_micros", "fees": "fees_micros",
                  "fx": "fx_micros", "quantity": "quantity_micros", "unit_price": "unit_price_micros"},
    TaxEvent: {"applied_rate": "applied_rate_ppm"},
}


def to_scaled(value: Decimal) -> int:
    """Exactly encode six decimal places; never silently round input."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("expected finite Decimal")
    with localcontext() as context:
        context.prec = max(64, len(value.as_tuple().digits) + 10)
        scaled = value * SCALE
        if scaled != scaled.to_integral_value():
            raise ValueError("more than six decimal places")
        if not MIN_SQLITE_INT <= scaled <= MAX_SQLITE_INT:
            raise ValueError("SQLite integer overflow")
        return int(scaled)


def from_scaled(value: int) -> Decimal:
    with localcontext() as context:
        context.prec = 64
        return Decimal(value) / SCALE


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise RuntimeError("SQLite 3.37+ is required for STRICT tables")
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("foreign key enforcement unavailable")
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    """Apply v1 once. Refuse unmanaged, newer or partly initialized databases."""
    if connection.in_transaction:
        raise RuntimeError("initialize before starting any write transaction")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("foreign_keys must be ON")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 1:
        actual = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES.values()) <= actual:
            raise RuntimeError("version 1 schema is incomplete")
        return
    if version != 0:
        raise RuntimeError(f"unsupported schema version {version}")
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
        raise RuntimeError("existing unmanaged database: explicit migration required")
    sql = files("money_manager").joinpath("migrations/001_initial.sql").read_text(encoding="utf-8")
    try:
        connection.executescript("BEGIN IMMEDIATE;\n" + sql + "\nPRAGMA user_version = 1;\nCOMMIT;")
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def insert(connection: sqlite3.Connection, model: Entity) -> str:
    """Insert a validated object. Caller owns commit/rollback (use `with connection`)."""
    model_class = type(model)
    if model_class not in TABLES:
        raise TypeError("unsupported model type")
    # Revalidate even if the caller used model_construct()/model_copy(update=...).
    model = model_class.model_validate(model.model_dump(mode="python", warnings=False))
    values = model.model_dump(mode="python")
    for source, target in SCALED_FIELDS.get(model_class, {}).items():
        value = values.pop(source)
        values[target] = None if value is None else to_scaled(value)
    if isinstance(model, TaxPolicy):
        values["source_urls_json"] = json.dumps([str(url) for url in values.pop("source_urls")])
        values.pop("parameters")
        values["parameters_json"] = model.parameters.model_dump_json()
        overlap = connection.execute(
            "SELECT 1 FROM tax_policies WHERE code = ? AND effective_from <= ? AND effective_to >= ?",
            (model.code, model.effective_to.isoformat(), model.effective_from.isoformat()),
        ).fetchone()
        if overlap:
            raise ValueError("policy periods with the same code overlap")
    for key, value in values.items():
        if isinstance(value, bool):
            values[key] = int(value)
        elif isinstance(value, (date, datetime)):
            values[key] = value.isoformat()
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {TABLES[model_class]} ({columns}) VALUES ({placeholders})", tuple(values.values()))
    return model.id


def get(connection: sqlite3.Connection, model_class: type[Entity], identifier: str) -> Entity | None:
    if model_class not in TABLES:
        raise TypeError("unsupported model type")
    row = connection.execute(f"SELECT * FROM {TABLES[model_class]} WHERE id = ?", (identifier,)).fetchone()
    if row is None:
        return None
    values = dict(row)
    if model_class in (TaxEvent, PensionContribution):
        values.pop("tax_year")  # Derived from recognized_on by SQL and Python alike.
    for source, target in SCALED_FIELDS.get(model_class, {}).items():
        value = values.pop(target)
        values[source] = None if value is None else from_scaled(value)
    if model_class == TaxPolicy:
        values["parameters"] = json.loads(values.pop("parameters_json"))
        values["source_urls"] = tuple(json.loads(values.pop("source_urls_json")))
    for key in ("us_tax_person", "pension_eligible"):
        if key in values:
            values[key] = bool(values[key])
    return model_class.model_validate(values)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Create/validate the step-1 portfolio database")
    parser.add_argument("path", help="SQLite database path (existing unmanaged DBs are rejected)")
    args = parser.parse_args()
    connection = connect(args.path)
    try:
        initialize(connection)
        print(f"Schema version 1 ready: {args.path}")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
