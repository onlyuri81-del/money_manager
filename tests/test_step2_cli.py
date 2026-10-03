"""Exercise the public commands and their file-safety/error contracts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from money_manager.db import connect, initialize, insert
from money_manager.models import Taxpayer, TaxPolicy


class CommandTests(unittest.TestCase):
    def command(self, module, *arguments, cwd):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        environment["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run([sys.executable, "-m", module, *arguments], cwd=cwd,
                              env=environment, capture_output=True, text=True, encoding="utf-8", timeout=20)

    def test_demo_outputs_json_without_touching_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.db"
            path.write_bytes(b"existing user database sentinel")
            result = self.command("money_manager.demo", "--json", cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["us_stock_tax"]["estimated_tax_krw"], 1_210_000)
            self.assertEqual(data["isa_general"]["estimated_tax_if_eligible_krw"], 198_000)
            self.assertEqual(path.read_bytes(), b"existing user database sentinel")
            self.assertEqual({p.name for p in Path(directory).iterdir()}, {"portfolio.db"})

    def test_report_command_is_read_only_and_returns_coverage_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio with spaces.db"
            connection = connect(path)
            initialize(connection)
            owner = Taxpayer(id="cli-owner", name="Demo")
            policy = TaxPolicy(id="cli-policy", code="scenario", effective_from="2026-01-01",
                               effective_to="2026-12-31", status="SCENARIO")
            with connection:
                insert(connection, owner)
                insert(connection, policy)
            connection.close()
            original = path.read_bytes()
            result = self.command("money_manager.tax_cli", "annual", str(path), "--taxpayer", owner.id,
                                  "--year", "2026", "--policy", policy.id, cwd=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertIsNone(report["financial_income"]["threshold_exceeded"])
            self.assertIn("COVERAGE_UNCONFIRMED", {issue["code"] for issue in report["issues"]})
            self.assertEqual(path.read_bytes(), original)

    def test_missing_database_errors_without_creating_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            result = self.command("money_manager.tax_cli", "annual", str(path), "--taxpayer", "owner",
                                  "--year", "2026", "--policy", "policy", cwd=directory)
            self.assertEqual(result.returncode, 2)
            self.assertIn("DB", result.stderr)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
