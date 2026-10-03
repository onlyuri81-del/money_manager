"""Local Korean input forms over the existing schema and step-2 engine."""
import os
import sqlite3
import hashlib
from datetime import date, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from zoneinfo import ZoneInfo

import streamlit as st

from .db import connect, get, initialize, insert
from .diagnostics import portfolio_diagnostics
from .input_service import (
    all_models, decimal_input, friendly_error, save_account, save_cash, save_holding,
    save_transaction, won_input,
)
from .local_ocr import extract_local_text, parse_candidates
from .models import Account, CashBalance, Holding, Instrument, TaxEvent, Taxpayer, TaxPolicy, Transaction
from .tax_engine import AnnualTaxRequest, ISAContractRequest, TaxEngine

ACCOUNT_LABELS = {"일반 위탁계좌": "GENERAL", "중개형 ISA": "ISA", "연금저축": "PENSION_SAVINGS", "IRP": "IRP"}
ISA_LABELS = {"일반형": "GENERAL", "서민형": "LOW_INCOME", "농어민형": "FARMER"}
ASSET_LABELS = {"주식": "STOCK", "국내주식형 ETF": "ETF_DOMESTIC_EQUITY", "기타 ETF (해외자산·채권 등)": "ETF_OTHER"}
TRADE_LABELS = {"매수": "BUY", "매도": "SELL", "배당": "DIVIDEND", "이자": "INTEREST", "입금": "DEPOSIT", "출금": "WITHDRAWAL"}


def now():
    return datetime.now(ZoneInfo("Asia/Seoul"))


def amount(label, *, key, value="", help=None):
    return st.text_input(label, value=value, key=key, help=help)


def product(*values):
    with localcontext() as context:
        context.prec = 80
        result = Decimal(1)
        for value in values:
            result *= value
        return result


def account_label(account):
    return f"{account.broker} · {account.label}"


def choose_account(accounts, key):
    return st.selectbox("계좌 선택", accounts, format_func=account_label, key=key)


def feedback(action):
    try:
        action()
    except (ValueError, sqlite3.Error, LookupError) as exc:
        st.error(friendly_error(exc))
    else:
        st.session_state["saved_notice"] = "저장했어요. 아래 목록에서 확인할 수 있습니다."
        st.rerun()


def account_page(db, owners, selected_owner):
    st.subheader("1. 계좌 등록")
    st.caption("실제 계좌번호 대신 ‘해외주식’, ‘생활비’ 같은 별칭을 사용하세요.")
    kind = st.selectbox("계좌 유형", list(ACCOUNT_LABELS), key="account_kind")
    with st.form("account_form"):
        owner_name = st.text_input("사용자 이름", key="owner_name") if selected_owner is None else selected_owner.name
        broker = st.text_input("증권사 / 금융기관", placeholder="예: 키움증권", key="broker")
        label = st.text_input("계좌 별칭", placeholder="예: 해외주식 계좌", key="account_label")
        opened = st.date_input("계좌 개설일", value=now().date(), key="opened", min_value=date(1900, 1, 1), max_value=now().date())
        subtype = maturity = None
        if ACCOUNT_LABELS[kind] == "ISA":
            subtype = ISA_LABELS[st.selectbox("ISA 유형", list(ISA_LABELS), key="isa_type")]
            maturity = st.date_input("계약상 만기일", value=date(now().year + 3, 12, 31), key="maturity")
        closed = st.date_input("실제 해지일 (선택)", value=None, key="closed")
        if st.form_submit_button("계좌 저장", type="primary"):
            feedback(lambda: save_account(db, owner_id=selected_owner.id if selected_owner else None,
                owner_name=owner_name, broker=broker, label=label, account_type=ACCOUNT_LABELS[kind],
                opened_on=opened, isa_type=subtype, maturity_on=maturity, closed_on=closed))
    accounts = all_models(db, Account, "accounts")
    if selected_owner:
        accounts = [a for a in accounts if a.taxpayer_id == selected_owner.id]
    if accounts:
        st.dataframe([{"증권사": a.broker, "계좌": a.label,
                       "유형": next(k for k, v in ACCOUNT_LABELS.items() if v == a.account_type),
                       "개설일": str(a.opened_on)} for a in accounts], hide_index=True, width="stretch")


def holding_page(db, accounts):
    st.subheader("2. 보유종목 입력")
    account = choose_account(accounts, "holding_account")
    existing = [h for h in all_models(db, Holding, "holdings") if h.account_id == account.id]
    holding_labels = {h.id: get(db, Instrument, h.instrument_id).ticker + " · 보유정보 수정" for h in existing}
    selection = st.selectbox("등록 / 수정", [None, *existing], key="holding_edit",
        format_func=lambda h: "새 종목 등록" if h is None else holding_labels[h.id])
    instrument = get(db, Instrument, selection.instrument_id) if selection else None
    prefix = f"holding_{selection.id if selection else 'new'}"
    markets = ["한국", "미국"] if account.account_type == "GENERAL" else ["한국"]
    market_name = st.selectbox("상장 시장", markets, index=markets.index("미국") if instrument and instrument.market == "US" else 0,
                               key=prefix + "_market", disabled=instrument is not None)
    market = "US" if market_name == "미국" else "KR"
    currency = "USD" if market == "US" else "KRW"
    asset_choices = list(ASSET_LABELS) if market == "KR" else ["주식", "기타 ETF (해외자산·채권 등)"]
    asset = st.selectbox("상품 종류", asset_choices,
        index=next((i for i, label in enumerate(asset_choices) if instrument and ASSET_LABELS[label] == instrument.asset_type), 0),
        key=prefix + "_asset", disabled=instrument is not None)
    quote_enabled = st.checkbox("현재가도 입력하기", value=selection is not None and selection.current_price is not None,
                                key=prefix + "_quote")
    st.caption("증권사 잔고 화면의 수량·매입금액을 옮겨 적으면 됩니다. 같은 종목을 저장하면 기존 보유정보를 갱신합니다.")
    with st.form("holding_form"):
        left, right = st.columns(2)
        ticker = left.text_input("종목코드", value=instrument.ticker if instrument else "", placeholder="예: BAC / 005930",
                                 key=prefix + "_ticker", disabled=instrument is not None)
        name = right.text_input("종목명", value=instrument.name if instrument else "", placeholder="예: Bank of America",
                                key=prefix + "_name", disabled=instrument is not None)
        quantity = amount("보유수량", key=prefix + "_qty", value=str(selection.quantity) if selection else "")
        average = amount(f"평균매수가 ({currency})", key=prefix + "_average", value=str(selection.average_buy_price) if selection else "")
        cost = amount("총 원화 취득금액 (원)", key=prefix + "_cost", value=str(selection.acquisition_cost_krw) if selection else "",
                      help="남은 보유수량의 실제 원화 취득원가입니다. 매입 당시 환율과 인정 비용을 반영한 자료를 사용하세요.")
        eligible = bool(instrument and instrument.pension_eligible)
        if market == "KR" and ASSET_LABELS[asset] != "STOCK" and instrument is None:
            eligible = st.checkbox("증권사에서 연금계좌 매수 가능 상품으로 확인함", key=prefix + "_eligible")
        price = fx = None
        if quote_enabled:
            price = amount(f"현재가 ({currency})", key=prefix + "_price", value=str(selection.current_price) if selection and selection.current_price is not None else "")
            if market == "US":
                fx = amount("현재 환율 (1달러당 원)", key=prefix + "_fx", value=str(selection.current_fx) if selection and selection.current_fx is not None else "")
        if st.form_submit_button("보유정보 저장·갱신", type="primary"):
            def save():
                asset_model = instrument or Instrument(ticker=ticker, name=name, market=market, currency=currency,
                    asset_type=ASSET_LABELS[asset], pension_eligible=eligible)
                save_holding(db, asset_model, account_id=account.id,
                    quantity=decimal_input(quantity, "보유수량"), average_buy_price=decimal_input(average, "평균매수가"),
                    acquisition_cost_krw=won_input(cost, "총 원화 취득금액"),
                    current_price=decimal_input(price, "현재가") if quote_enabled else None,
                    current_fx=(decimal_input(fx, "환율") if market == "US" else Decimal(1)) if quote_enabled else None,
                    valued_at=now() if quote_enabled else None)
            feedback(save)
    rows = []
    for h in existing:
        i = get(db, Instrument, h.instrument_id)
        rows.append({"종목코드": i.ticker, "종목명": i.name, "수량": str(h.quantity),
                     "평균매수가": str(h.average_buy_price), "통화": i.currency.value,
                     "원화 취득금액": f"{h.acquisition_cost_krw:,}",
                     "현재가": str(h.current_price) if h.current_price is not None else "미입력"})
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")


def cash_page(db, accounts):
    st.subheader("3. 현금 잔액")
    account = choose_account(accounts, "cash_account")
    currency = st.selectbox("통화", ["KRW", "USD"], key="cash_currency")
    existing = next((c for c in all_models(db, CashBalance, "cash_balances")
                     if c.account_id == account.id and c.currency == currency), None)
    prefix = f"cash_{account.id}_{currency}"
    with st.form("cash_form"):
        value = amount("현금 잔액", key=prefix + "_amount", value=str(existing.amount) if existing else "")
        fx = amount("현재 환율 (1달러당 원)", key=prefix + "_fx", value=str(existing.fx) if existing else "") if currency == "USD" else "1"
        if st.form_submit_button("현금 저장·갱신", type="primary"):
            feedback(lambda: save_cash(db, CashBalance(account_id=account.id, currency=currency,
                amount=decimal_input(value, "현금 잔액"), fx=decimal_input(fx, "환율"), valued_at=now())))
    st.caption("이 화면은 현재 잔액을 저장합니다. 거래 입력 시 잔액과 보유수량은 자동 변경되지 않습니다.")
    balances = [c for c in all_models(db, CashBalance, "cash_balances") if c.account_id == account.id]
    if balances:
        st.dataframe([{"통화": c.currency.value, "잔액": str(c.amount), "환율": str(c.fx)} for c in balances], hide_index=True)


def ocr_import_page(db, accounts):
    st.subheader("6. 사진에서 거래 가져오기")
    st.caption("로컬 Tesseract로 이 기기에서만 읽습니다. 사진·OCR 원문은 DB에 저장하지 않으며, 확인한 거래만 저장합니다.")
    account = choose_account(accounts, "ocr_account")
    uploaded = st.file_uploader("토스뱅크 거래 화면 이미지", type=["png", "jpg", "jpeg"], key="ocr_upload")
    if uploaded is None:
        st.info("거래 한 건이 보이는 화면을 이미지로 올리세요. 계좌번호 등 불필요한 개인정보는 가린 뒤 올리는 것을 권장합니다.")
        return

    image_bytes = uploaded.getvalue()
    if len(image_bytes) > 15 * 1024 * 1024:
        st.error("이미지 크기는 15MB 이하여야 합니다.")
        return
    image_hash = hashlib.sha256(image_bytes).hexdigest()
    if st.session_state.get("ocr_image_hash") != image_hash:
        st.session_state["ocr_image_hash"] = image_hash
        st.session_state.pop("ocr_text", None)
    if st.button("기기에서 OCR 실행", type="primary", key="run_local_ocr"):
        try:
            st.session_state["ocr_text"] = extract_local_text(image_bytes)
        except (RuntimeError, ValueError) as exc:
            st.error(str(exc))
            return

    text = st.session_state.get("ocr_text")
    if text is None:
        st.info("OCR을 실행하면 읽은 내용을 확인하고 거래 저장 후보를 만들 수 있습니다.")
        return
    if not text.strip():
        st.warning("글자를 읽지 못했습니다. 선명한 이미지를 다시 올리거나 직접 입력을 사용하세요.")
        return

    candidates = parse_candidates(text)
    candidate = None
    key_suffix = image_hash[:12]
    st.text_area("인식 결과 (기기에 임시 표시)", value=text, height=150, disabled=True,
                 key=f"ocr_preview_{key_suffix}")
    if candidates:
        candidate = st.selectbox(
            "거래 행 선택",
            [None, *candidates],
            format_func=lambda item: "직접 확인 후 입력" if item is None else item.line,
            key=f"ocr_row_{key_suffix}",
        )
    st.caption("자동 인식은 후보 입력일 뿐입니다. 날짜·금액·입출금 종류를 원본과 대조한 뒤 저장하세요.")
    with st.form(f"ocr_transaction_form_{key_suffix}"):
        kind = st.selectbox("거래 종류", ["입금", "출금"], key=f"ocr_kind_{key_suffix}")
        candidate_date = candidate.recognized_on if candidate and candidate.recognized_on else now().date()
        recognized = st.date_input("거래일 / 인식일", value=candidate_date, key=f"ocr_date_{key_suffix}")
        suggested_amount = str(abs(candidate.amount_krw)) if candidate and candidate.amount_krw is not None else ""
        amount_text = st.text_input("거래 금액 (원)", value=suggested_amount, key=f"ocr_amount_{key_suffix}")
        seed = f"{image_hash}:{candidate.line if candidate else 'manual'}"
        default_ref = "ocr-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
        reference = st.text_input("중복 확인용 거래번호", value=default_ref, key=f"ocr_ref_{key_suffix}")
        if st.form_submit_button("확인한 거래 저장", type="primary"):
            def save():
                transaction = Transaction(
                    account_id=account.id, external_ref=reference,
                    transaction_type="DEPOSIT" if kind == "입금" else "WITHDRAWAL",
                    traded_on=recognized, recognized_on=recognized, currency="KRW",
                    gross_amount=decimal_input(amount_text, "거래 금액"), fx="1",
                )
                save_transaction(db, transaction)
            feedback(save)


def diagnostics_page(db, owner):
    st.subheader("7. 자금 현황 진단")
    today = now().date()
    year = st.number_input("분석 연도", value=today.year, min_value=1900, max_value=9998, step=1, key="diagnostic_year")
    result = portfolio_diagnostics(db, owner.id, int(year), today)
    st.caption("등록된 스냅샷과 거래만 요약합니다. 대출·카드 부채, 누락된 계좌·거래는 반영되지 않아 순자산이나 가처분소득이 아닙니다.")
    first, second, third = st.columns(3)
    first.metric("입력자료 기준 알려진 자산", f"{result['known_snapshot_value_krw']:,}원")
    second.metric("현재가 입력 보유종목", f"{result['priced_holdings_count']}개",
                  help=f"현재가 미입력 종목 {result['unpriced_holdings_count']}개는 평가액에서 제외했습니다.")
    third.metric("현금 스냅샷 환산액", f"{result['cash_value_krw']:,}원")
    st.caption(f"보유종목 평가액 {result['holdings_value_krw']:,}원 + 현금 {result['cash_value_krw']:,}원입니다. 현재가·환율은 사용자가 입력한 값입니다.")
    if result["unpriced_holdings_count"]:
        st.warning(f"현재가가 없는 보유종목 {result['unpriced_holdings_count']}개는 알려진 자산 합계에 포함되지 않았습니다.")

    st.markdown(f"**{year}년 등록된 입출금·이체 흐름 (연초~오늘)**")
    st.caption("투자계좌 간 이체도 포함될 수 있으므로 수입·생활비 지출로 간주하지 않습니다.")
    if result["monthly_flows"]:
        st.dataframe(result["monthly_flows"], hide_index=True, width="stretch")
    else:
        st.info("분석 기간에 등록된 입출금·이체 거래가 없습니다.")

    st.markdown("**세무자료 누락 점검**")
    total = result["tax_income_transaction_count"]
    incomplete = result["incomplete_tax_event_count"]
    if incomplete:
        st.warning(f"매도·배당·이자 {total}건 중 {incomplete}건에 과세자료가 없거나 분류·과세 산입액이 미확정입니다. 거래·과세자료에서 보완하세요.")
    else:
        st.info(f"등록된 매도·배당·이자 {total}건 중 미등록 과세자료는 발견되지 않았습니다. 다른 기관·계좌의 누락 여부까지 확인된 것은 아닙니다.")
    st.caption("이 화면은 자료 점검용 요약이며 투자 권유, 세무 신고 결과 또는 금융기관 전체 데이터의 완전성 확인이 아닙니다.")
    st.info("더 완전한 PB식 진단에는 대출·부채 잔액, 급여·생활비 구분, 재무목표와 위험 선호가 필요합니다. 현재는 이 자료를 수집하거나 저축 여력·투자 적정성을 판단하지 않습니다.")


def treatment_choices(account, kind, instrument):
    if account.account_type == "ISA":
        return {"ISA 통산 대상": "ISA", "비과세 (증권사 확인)": "EXEMPT", "확인 필요": "REVIEW_REQUIRED"}
    if account.account_type in ("IRP", "PENSION_SAVINGS"):
        return {"연금 과세이연": "PENSION_DEFERRED"}
    if kind == "SELL" and instrument.market == "US":
        return {"해외주식 양도소득": "STOCK_CAPITAL_GAIN", "확인 필요": "REVIEW_REQUIRED"}
    if kind == "SELL":
        return {"확인 필요": "REVIEW_REQUIRED", "비과세 (증권사 확인)": "EXEMPT",
                "금융소득 (ETF 등)": "FINANCIAL_INCOME", "국내 과세 주식": "STOCK_CAPITAL_GAIN"}
    return {"일반 금융소득": "FINANCIAL_INCOME", "확인 필요": "REVIEW_REQUIRED"}


def complete_tax_page(db, account):
    ids = db.execute("""SELECT t.id FROM transactions t LEFT JOIN tax_events e ON e.transaction_id=t.id
        WHERE t.account_id=? AND t.transaction_type IN ('SELL','DIVIDEND','INTEREST') AND e.id IS NULL
        ORDER BY t.rowid DESC""", (account.id,)).fetchall()
    if not ids:
        st.info("보완할 과세자료가 없습니다.")
        return
    transaction = st.selectbox("과세자료를 보완할 거래", [get(db, Transaction, row[0]) for row in ids],
        format_func=lambda t: f"{t.recognized_on} · {t.external_ref}", key="pending_transaction")
    instrument = get(db, Instrument, transaction.instrument_id) if transaction.instrument_id else None
    prefix = f"complete_{transaction.id}"
    st.caption("원거래는 그대로 두고 과세자료를 연결합니다. 경제적 손익과 산입액은 증권사 원화 자료를 사용하세요.")
    with st.form("complete_tax_form"):
        choices = treatment_choices(account, transaction.transaction_type, instrument)
        treatment = choices[st.selectbox("과세 분류", list(choices), key=prefix + "_treatment")]
        economic = amount("실현손익 / 세전 배당·이자 (원)", key=prefix + "_economic")
        taxable = amount("과세 산입액 / 적격 통산금액 (원)", key=prefix + "_basis")
        domestic = foreign = None
        if transaction.transaction_type in ("DIVIDEND", "INTEREST"):
            domestic = amount("국내 원천징수액 (원)", key=prefix + "_domestic")
            foreign = amount("해외 원천징수액 (원)", key=prefix + "_foreign")
        source = st.text_input("과세자료 출처", key=prefix + "_source")
        if st.form_submit_button("과세자료 연결", type="primary"):
            def save():
                event = TaxEvent(transaction_id=transaction.id, account_id=account.id,
                    recognized_on=transaction.recognized_on,
                    event_type="CAPITAL_GAIN" if transaction.transaction_type == "SELL" else transaction.transaction_type,
                    treatment=treatment, economic_income_krw=won_input(economic, "실현손익 / 세전금액"),
                    taxable_income_krw=won_input(taxable, "과세 산입액"), basis_source=source,
                    domestic_withholding_krw=won_input(domestic, "국내 원천징수액") if domestic is not None else 0,
                    foreign_withholding_krw=won_input(foreign, "해외 원천징수액") if foreign is not None else 0)
                with db:
                    insert(db, event)
            feedback(save)


def transaction_page(db, accounts):
    st.subheader("4. 거래·과세자료")
    account = choose_account(accounts, "transaction_account")
    entry_mode = st.radio("등록 방식", ["새 거래 입력", "미등록 과세자료 보완"], horizontal=True, key="entry_mode")
    if entry_mode == "미등록 과세자료 보완":
        complete_tax_page(db, account)
        return
    kind = st.selectbox("거래 종류", list(TRADE_LABELS), key="transaction_kind")
    trade_kind = TRADE_LABELS[kind]
    is_trade = trade_kind in ("BUY", "SELL")
    is_income = trade_kind in ("SELL", "DIVIDEND", "INTEREST")
    instrument = None
    if trade_kind in ("BUY", "SELL", "DIVIDEND"):
        assets = [i for i in all_models(db, Instrument, "instruments")
                  if (account.account_type == "GENERAL" or i.market == "KR")
                  and (account.account_type not in ("IRP", "PENSION_SAVINGS") or i.pension_eligible)]
        if not assets:
            st.info("먼저 보유종목 화면에서 거래할 종목을 등록하세요.")
            return
        instrument = st.selectbox("종목", assets, format_func=lambda i: f"{i.ticker} · {i.name}", key="transaction_instrument")
    currency = instrument.currency.value if instrument else st.selectbox("거래 통화", ["KRW", "USD"], key="transaction_currency")
    tax_confirmed = st.checkbox("증권사 과세자료를 확인했어요", key="tax_confirmed") if is_income else False
    st.caption("거래번호로 중복 등록을 막습니다. 원화 과세자료가 없으면 거래만 저장하고 세금 계산은 미확정으로 표시합니다.")
    with st.form("transaction_form"):
        reference = st.text_input("증권사 거래번호 / 직접 정한 고유번호", placeholder="예: broker-20261001-001", key="trade_ref")
        a, b = st.columns(2)
        traded = a.date_input("거래일", value=now().date(), key="trade_date")
        recognized = b.date_input("결제일 / 지급일", value=now().date(), key="recognition_date")
        qty = price = None
        if is_trade:
            qty = amount("거래수량", key="trade_qty")
            price = amount(f"거래단가 ({currency})", key="trade_price")
            st.caption("거래금액은 수량 × 단가로 저장합니다.")
            gross = None
        else:
            gross = amount(f"세전 거래금액 ({currency})", key="trade_gross")
        fees = amount(f"수수료 ({currency})", key="trade_fees", value="0")
        fx = amount("결제·지급 기준 환율 (1달러당 원)", key="trade_fx") if currency == "USD" else "1"
        treatment = source = economic = taxable = domestic = foreign = None
        if tax_confirmed:
            st.markdown("**증권사 과세자료의 원화 금액**")
            economic = amount("실현손익 (원)" if trade_kind == "SELL" else "세전 배당·이자 금액 (원)", key="tax_economic")
            taxable = amount("과세 산입액 / 적격 통산금액 (원)", key="tax_basis",
                help="손실은 음수입니다. 경제적 실현손익과 다른 경우 증권사 과세자료의 산입액을 사용하세요.")
            choices = treatment_choices(account, trade_kind, instrument)
            treatment = choices[st.selectbox("과세 분류", list(choices), key="tax_treatment")]
            if trade_kind in ("DIVIDEND", "INTEREST"):
                domestic = amount("국내 원천징수액 (원)", key="tax_domestic", help="실제 0원이면 0을 입력하세요.")
                foreign = amount("해외 원천징수액 (원)", key="tax_foreign", help="실제 0원이면 0을 입력하세요.")
            source = st.text_input("과세자료 출처", placeholder="예: 증권사 2026년 해외주식 양도소득 조회", key="tax_source")
        if st.form_submit_button("거래 저장", type="primary"):
            def save():
                parsed_qty = decimal_input(qty, "거래수량") if is_trade else None
                parsed_price = decimal_input(price, "거래단가") if is_trade else None
                transaction = Transaction(account_id=account.id, instrument_id=instrument.id if instrument else None,
                    external_ref=reference, transaction_type=trade_kind, traded_on=traded, recognized_on=recognized,
                    currency=currency, gross_amount=product(parsed_qty, parsed_price) if is_trade else decimal_input(gross, "거래금액"),
                    fees=decimal_input(fees, "수수료"), fx=decimal_input(fx, "환율"), quantity=parsed_qty, unit_price=parsed_price)
                event = None
                if tax_confirmed:
                    event = TaxEvent(transaction_id=transaction.id, account_id=account.id, recognized_on=recognized,
                        event_type="CAPITAL_GAIN" if trade_kind == "SELL" else trade_kind, treatment=treatment,
                        economic_income_krw=won_input(economic, "실현손익 / 세전금액"),
                        taxable_income_krw=won_input(taxable, "과세 산입액"), basis_source=source,
                        domestic_withholding_krw=won_input(domestic, "국내 원천징수액") if domestic is not None else 0,
                        foreign_withholding_krw=won_input(foreign, "해외 원천징수액") if foreign is not None else 0)
                save_transaction(db, transaction, event)
            feedback(save)
    st.info("보유정보·현금은 별도로 갱신하세요. 저장한 거래는 원자료를 보존하며 화면에서 덮어쓰지 않습니다.")
    rows = db.execute("""SELECT t.recognized_on AS '결제·지급일', t.external_ref AS '거래번호',
        t.transaction_type AS '종류', COALESCE(i.ticker, '') AS '종목',
        CASE WHEN e.id IS NULL THEN '미등록' ELSE e.treatment END AS '과세자료'
        FROM transactions t LEFT JOIN instruments i ON i.id=t.instrument_id
        LEFT JOIN tax_events e ON e.transaction_id=t.id WHERE t.account_id=? ORDER BY t.rowid DESC LIMIT 50""", (account.id,)).fetchall()
    if rows:
        st.dataframe([dict(r) for r in rows], hide_index=True, width="stretch")


def report_page(db, owner, accounts):
    st.subheader("5. 세금 계산 확인")
    mode = st.radio("계산 범위", ["연간 미국주식·금융소득", "ISA 계약기간"], horizontal=True)
    period = st.number_input("귀속연도", value=now().year, min_value=1900, max_value=9998, step=1) if mode.startswith("연간") else None
    account = None
    as_of = None
    if period is None:
        isa_accounts = [a for a in accounts if a.account_type == "ISA"]
        if not isa_accounts:
            st.info("ISA 계좌를 먼저 등록하세요.")
            return
        account = choose_account(isa_accounts, "report_isa")
        as_of = st.date_input("정산 가정일", value=now().date())
    covered = st.checkbox("다른 증권사·은행까지 해당 기간 자료가 모두 반영됐어요", key="coverage")
    st.caption("입력한 자료와 기본 가정 정책으로 계산합니다. 과세자료가 빠진 항목은 0원으로 확정하지 않습니다.")
    if st.button("입력한 자료로 계산", type="primary"):
        try:
            year = period or as_of.year
            policy_id = f"ui-scenario-{year}"
            policy = get(db, TaxPolicy, policy_id)
            if policy is None:
                policy = TaxPolicy(id=policy_id, code=f"ui-default-{year}", effective_from=date(year, 1, 1),
                    effective_to=date(year, 12, 31), status="SCENARIO")
                with db:
                    insert(db, policy)
            engine = TaxEngine(db)
            if period:
                report = engine.annual(AnnualTaxRequest(taxpayer_id=owner.id, tax_year=period,
                    policy_id=policy.id, data_coverage_confirmed=covered))
                if report.imported_income_transaction_count == 0:
                    st.info("해당 연도에 저장한 매도·배당·이자 거래가 없습니다. 실제 소득이 0원이라는 뜻은 아닙니다.")
                else:
                    st.metric("입력자료 기준 예상 미국주식 양도세", "미확정" if report.us_stock_tax is None else f"{report.us_stock_tax.estimated_tax_krw:,}원")
                    flag = report.financial_income.threshold_exceeded
                    st.metric("금융소득 기준금액 초과 여부", "확인 필요" if flag is None else "초과" if flag else "기준 이하")
            else:
                report = engine.isa_contract(ISAContractRequest(account_id=account.id, as_of=as_of,
                    policy_id=policy.id, data_coverage_confirmed=covered))
                if report.imported_income_transaction_count == 0:
                    st.info("ISA 계약기간에 저장한 매도·배당·이자 거래가 없습니다. 실제 소득이 0원이라는 뜻은 아닙니다.")
                else:
                    st.metric("ISA 요건 충족 가정 세금", "미확정" if report.projection is None else f"{report.projection.estimated_tax_if_eligible_krw:,}원")
            for issue in report.issues:
                (st.error if issue.severity == "BLOCKER" else st.warning)(issue.message)
            with st.expander("계산 상세"):
                st.json(report.model_dump(mode="json"))
        except (ValueError, LookupError, sqlite3.Error, RuntimeError) as exc:
            st.error(friendly_error(exc))


def main():
    st.set_page_config(page_title="Money Manager · 간편 입력", page_icon="💰", layout="wide")
    st.title("Money Manager")
    st.caption("내 계좌와 자산을 입력하고, 등록한 자료로 세금을 확인하세요.")
    database = st.sidebar.text_input("사용할 DB 파일", value=os.environ.get("MONEY_MANAGER_DB", "portfolio.db"), key="database_path")
    st.sidebar.caption("기존 portfolio.db의 경로를 입력하면 그 자료를 계속 사용할 수 있습니다.")
    if not database.strip() or database.strip() == ":memory:":
        st.error("자료를 저장할 DB 파일 경로를 입력하세요.")
        return
    db = None
    try:
        db = connect(database.strip())
        initialize(db)
        st.sidebar.caption(f"저장 위치: {Path(database.strip()).resolve()}")
        owners = all_models(db, Taxpayer, "taxpayers")
        owner = st.sidebar.selectbox("사용자", [*owners, None],
            format_func=lambda p: p.name if p else "새 사용자 등록", key="selected_owner") if owners else None
        accounts = [a for a in all_models(db, Account, "accounts") if owner and a.taxpayer_id == owner.id]
        route = st.sidebar.radio("메뉴", ["계좌 등록", "보유종목", "현금 잔액", "거래·과세자료", "세금 계산",
                                          "사진 거래 가져오기", "자금 진단"], key="page")
        notice = st.session_state.pop("saved_notice", None)
        if notice:
            st.success(notice)
        if route == "계좌 등록":
            account_page(db, owners, owner)
        elif not accounts:
            st.info("‘계좌 등록’에서 사용자와 계좌를 먼저 추가하세요.")
        elif route == "보유종목":
            holding_page(db, accounts)
        elif route == "현금 잔액":
            cash_page(db, accounts)
        elif route == "거래·과세자료":
            transaction_page(db, accounts)
        elif route == "세금 계산":
            report_page(db, owner, accounts)
        elif route == "사진 거래 가져오기":
            ocr_import_page(db, accounts)
        else:
            diagnostics_page(db, owner)
    except (sqlite3.Error, RuntimeError, ValueError) as exc:
        st.error("DB를 열 수 없습니다. 파일 경로와 기존 DB 형식을 확인하세요. " + friendly_error(exc))
    finally:
        if db is not None:
            db.close()
