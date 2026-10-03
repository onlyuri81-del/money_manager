"""Real widget submission tests; optional when UI extras are not installed."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from money_manager.db import connect


@unittest.skipUnless(importlib.util.find_spec("streamlit"), "install .[ui] for UI tests")
class FormTests(unittest.TestCase):
    def setUp(self):
        from streamlit.testing.v1 import AppTest
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / "portfolio.db")
        self.env = patch.dict(os.environ, {"MONEY_MANAGER_DB": self.path})
        self.env.start()
        self.app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=10).run()
        self.assertFalse(self.app.exception)

    def tearDown(self):
        self.env.stop()
        self.folder.cleanup()

    def row_count(self, table):
        db = connect(self.path)
        try:
            return db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        finally:
            db.close()

    def button(self, label):
        return next(b for b in self.app.button if b.label == label)

    def create_account(self):
        self.app.text_input(key="owner_name").input("테스트 사용자")
        self.app.text_input(key="broker").input("테스트 증권")
        self.app.text_input(key="account_label").input("미국주식")
        self.button("계좌 저장").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.row_count("accounts"), 1)

    def create_holding(self):
        self.create_account()
        self.app.radio(key="page").set_value("보유종목").run()
        self.app.selectbox(key="holding_new_market").select("미국").run()
        for key, value in {"ticker": "BAC", "name": "Bank of America", "qty": "10.5",
                           "average": "40.12", "cost": "550,000"}.items():
            self.app.text_input(key=f"holding_new_{key}").input(value)
        self.button("보유정보 저장·갱신").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.row_count("holdings"), 1)

    def test_onboarding_and_holding_survive_app_restart(self):
        from streamlit.testing.v1 import AppTest
        self.create_holding()
        fresh = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py")).run()
        self.assertFalse(fresh.exception)
        self.assertEqual(self.row_count("holdings"), 1)
        self.assertEqual(fresh.selectbox(key="selected_owner").value.name, "테스트 사용자")

    def test_invalid_input_is_visible_and_does_not_save(self):
        self.create_account()
        self.app.radio(key="page").set_value("보유종목").run()
        for key, value in {"ticker": "005930", "name": "삼성전자", "qty": "틀린 수량", "average": "70000", "cost": "700000"}.items():
            self.app.text_input(key=f"holding_new_{key}").input(value)
        self.button("보유정보 저장·갱신").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(self.app.error)
        self.assertEqual(self.row_count("holdings"), 0)
        self.assertEqual(self.row_count("instruments"), 0)

    def test_cash_save_and_report_empty_data_message(self):
        self.create_account()
        self.app.radio(key="page").set_value("현금 잔액").run()
        field = next(i for i in self.app.text_input if i.label == "현금 잔액")
        field.input("1,234,567")
        self.button("현금 저장·갱신").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.row_count("cash_balances"), 1)
        self.app.radio(key="page").set_value("세금 계산").run()
        self.button("입력한 자료로 계산").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any("거래가 없습니다" in i.value for i in self.app.info))

    def test_local_ocr_page_renders_without_sending_data_to_an_ocr_service(self):
        self.create_account()
        self.app.radio(key="page").set_value("사진 거래 가져오기").run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any(i.label == "토스뱅크 거래 화면 이미지" for i in self.app.file_uploader))
        self.assertTrue(any("기기에서만 읽습니다" in c.value for c in self.app.caption))

    def test_diagnostics_page_shows_scope_and_empty_year_to_date_message(self):
        self.create_account()
        self.app.radio(key="page").set_value("자금 진단").run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any("순자산이나 가처분소득이 아닙니다" in c.value for c in self.app.caption))
        self.assertTrue(any("등록된 입출금·이체 거래가 없습니다" in i.value for i in self.app.info))

    def test_sale_and_later_tax_completion_calculates(self):
        self.create_holding()
        self.app.radio(key="page").set_value("거래·과세자료").run()
        self.app.selectbox(key="transaction_kind").select("매도").run()
        for key, value in {"trade_ref": "sale-001", "trade_qty": "2", "trade_price": "45", "trade_fx": "1350"}.items():
            self.app.text_input(key=key).input(value)
        self.button("거래 저장").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.row_count("transactions"), 1)
        self.assertEqual(self.row_count("tax_events"), 0)
        self.app.radio(key="entry_mode").set_value("미등록 과세자료 보완").run()
        for label, value in {"실현손익 / 세전 배당·이자 (원)": "3,000,000", "과세 산입액 / 적격 통산금액 (원)": "3,000,000",
                             "과세자료 출처": "테스트 증권 과세자료"}.items():
            next(i for i in self.app.text_input if i.label == label).input(value)
        self.button("과세자료 연결").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.row_count("tax_events"), 1)
        self.app.radio(key="page").set_value("세금 계산").run()
        self.button("입력한 자료로 계산").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.app.metric[0].value, "110,000원")

    def test_edit_holding_prefills_and_updates_quote(self):
        self.create_holding()
        selection = self.app.selectbox(key="holding_edit")
        selection.set_value(selection.options[1]).run()
        self.assertFalse(self.app.exception)
        prefix = next(i.key.removesuffix("_qty") for i in self.app.text_input if i.label == "보유수량")
        self.assertEqual(self.app.text_input(key=prefix + "_qty").value, "10.5")
        self.app.checkbox(key=prefix + "_quote").check().run()
        for key, value in {"qty": "12.123456", "price": "45.5", "fx": "1350.12"}.items():
            self.app.text_input(key=prefix + "_" + key).input(value)
        self.button("보유정보 저장·갱신").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.row_count("holdings"), 1)
        db = connect(self.path)
        try:
            self.assertEqual(db.execute("SELECT quantity_micros,current_fx_micros FROM holdings").fetchone()[:], (12_123_456, 1_350_120_000))
        finally:
            db.close()

    def test_foreign_dividend_confirmed_data_and_duplicate(self):
        self.create_holding()
        self.app.radio(key="page").set_value("거래·과세자료").run()
        self.app.selectbox(key="transaction_kind").select("배당").run()
        self.app.checkbox(key="tax_confirmed").check().run()
        for key, value in {"trade_ref": "dividend-001", "trade_gross": "100", "trade_fx": "1350",
            "tax_economic": "135000", "tax_basis": "135000", "tax_domestic": "0", "tax_foreign": "20250",
            "tax_source": "증권사 배당명세"}.items():
            self.app.text_input(key=key).input(value)
        self.button("거래 저장").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(self.row_count("tax_events"), 1)
        self.button("거래 저장").click().run()
        self.assertTrue(self.app.error)
        self.assertEqual(self.row_count("transactions"), 1)

    def test_isa_account_and_contract_report(self):
        self.app.selectbox(key="account_kind").select("중개형 ISA").run()
        self.app.text_input(key="owner_name").input("ISA 사용자")
        self.app.text_input(key="broker").input("테스트 증권")
        self.app.text_input(key="account_label").input("절세 계좌")
        self.button("계좌 저장").click().run()
        self.assertFalse(self.app.exception)
        self.app.radio(key="page").set_value("세금 계산").run()
        next(r for r in self.app.radio if r.label == "계산 범위").set_value("ISA 계약기간").run()
        self.button("입력한 자료로 계산").click().run()
        self.assertFalse(self.app.exception)
        self.assertTrue(any("거래가 없습니다" in i.value for i in self.app.info))


if __name__ == "__main__":
    unittest.main()
