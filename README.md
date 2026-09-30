# Money Manager

한국 거주 개인의 한국·미국 투자자산을 관리하기 위한 Python 백엔드입니다.
현재 구현 범위는 **1단계: SQLite 스키마와 Pydantic 모델**입니다.
세금 계산, 손익통산 추천, Streamlit 화면은 아직 구현하지 않았습니다.

## 실행

Python 3.11 이상, SQLite 3.37 이상이 필요합니다.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e .
python -m money_manager.db portfolio.db
python -m unittest discover -s tests -v
```

Streamlit은 4단계에서 `python -m pip install -e '.[ui]'`로 설치합니다.
저장소 루트에서 설치 없이 확인하려면 `PYTHONPATH=src python -m unittest discover -s tests -v`를 사용합니다.
DB 파일, 계좌 원본 데이터와 인증정보는 Git에 올리지 않습니다.

## 첫 번째 데이터 저장

```python
from datetime import datetime, timezone

from money_manager.db import connect, initialize, insert, get
from money_manager.models import Account, Holding, Instrument, Taxpayer

db = connect("portfolio.db")
try:
    initialize(db)
    owner = Taxpayer(name="예시 사용자", residency="KR", us_tax_person=False)
    account = Account(
        taxpayer_id=owner.id, broker="예시 증권사", label="해외주식 계좌",
        account_type="GENERAL", opened_on="2026-01-01",
    )
    stock = Instrument(
        ticker="AAPL", name="Apple", market="US", currency="USD", asset_type="STOCK",
    )
    holding = Holding(
        account_id=account.id, instrument_id=stock.id,
        quantity="10.5", average_buy_price="180.25",
        acquisition_cost_krw=2_555_000,
        current_price="200", current_fx="1350",
        valued_at=datetime.now(timezone.utc),
    )
    with db:  # 실패하면 이 묶음 전체를 롤백합니다.
        for item in (owner, account, stock, holding):
            insert(db, item)
    print(get(db, Holding, holding.id).model_dump_json(indent=2))
finally:
    db.close()
```

이 예시는 빈 DB에 한 번 실행하는 예입니다. 시세와 환율은 예시 입력값입니다.
수량, 가격, 환율에는 `Decimal`, 문자열 또는 정수를 사용하세요. Python `float`는 거부합니다.

## 구성

| 파일 | 역할 |
|---|---|
| `src/money_manager/migrations/001_initial.sql` | 10개 테이블, 조회 뷰, 무결성 제약과 트리거 |
| `src/money_manager/models.py` | 계좌·자산·거래·세금·정책의 Pydantic 모델 |
| `src/money_manager/db.py` | DB 연결, 원자적 초기화, 저장·읽기 및 정확한 단위 변환 |
| `tests/test_step1.py` | 데이터 왕복, 세금 귀속연도, 계좌 제한, 중복 및 롤백 검증 |
| `docs/step1_design.md` | 데이터 관계, 세무 처리 범위와 이후 단계의 계약 |

`initialize()`는 새 DB만 초기화합니다. 기존 미관리 DB를 발견하면 명시적 마이그레이션을 요구하며,
기존 테이블을 지우거나 스키마를 덮어쓰지 않습니다. `insert()`는 커밋하지 않습니다.
트랜잭션 묶음과 커밋/롤백의 책임은 호출자에게 있습니다.
