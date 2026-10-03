"""Read-only JSON tax reports for an already populated portfolio database."""
import argparse
import sqlite3
from datetime import date

from .db import connect
from .tax_engine import AnnualTaxRequest, ISAContractRequest, TaxEngine


def main():
    parser = argparse.ArgumentParser(description="Read-only tax simulations over a populated SQLite DB")
    sub = parser.add_subparsers(dest="command", required=True)
    annual = sub.add_parser("annual", help="taxpayer-wide annual US gains and financial-income alert")
    annual.add_argument("database")
    annual.add_argument("--taxpayer", required=True)
    annual.add_argument("--year", required=True, type=int)
    annual.add_argument("--policy", required=True)
    annual.add_argument("--confirm-coverage", action="store_true")
    isa = sub.add_parser("isa", help="one ISA contract from opening through the selected date")
    isa.add_argument("database")
    isa.add_argument("--account", required=True)
    isa.add_argument("--as-of", required=True, type=date.fromisoformat)
    isa.add_argument("--policy", required=True)
    isa.add_argument("--confirm-coverage", action="store_true")
    args = parser.parse_args()
    connection = None
    try:
        connection = connect(args.database, read_only=True)
        engine = TaxEngine(connection)
        if args.command == "annual":
            report = engine.annual(AnnualTaxRequest(taxpayer_id=args.taxpayer, tax_year=args.year,
                policy_id=args.policy, data_coverage_confirmed=args.confirm_coverage))
        else:
            report = engine.isa_contract(ISAContractRequest(account_id=args.account, as_of=args.as_of,
                policy_id=args.policy, data_coverage_confirmed=args.confirm_coverage))
        print(report.model_dump_json(indent=2))
        if report.needs_review:
            parser.exit(2, "미확정/미지원 자료가 있습니다. JSON의 issues를 확인하세요.\n")
    except (ValueError, LookupError, RuntimeError) as exc:
        parser.exit(2, f"계산할 수 없습니다: {exc}\n")
    except sqlite3.Error as exc:
        parser.exit(2, f"DB를 읽을 수 없습니다: {exc}\n")
    finally:
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    main()
