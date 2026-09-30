# Money Manager

한국 거주 개인의 한국·미국 투자자산을 관리하기 위한 Python 백엔드입니다.
현재 **1단계: SQLite 스키마·Pydantic 모델**과 **2단계: 세금 시뮬레이션 엔진**을 구현했습니다.
미국주식 연간 양도세, 배당 원천징수 후 현금, ISA 계약기간 손익통산,
일반 금융소득 기준금액 초과 여부를 계산합니다. 손익통산 추천과 Streamlit 화면은 이후 단계입니다.

## 실행

Python 3.11 이상, SQLite 3.37 이상이 필요합니다.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e .
python -m money_manager.db portfolio.db
python -m unittest discover -s tests -v
python -m money_manager.demo
```

데모는 메모리의 가상 데이터를 사용하며 기존 `portfolio.db`를 읽거나 수정하지 않습니다.
1단계 DB 스키마 버전은 그대로 `1`입니다. DB 재생성이나 삭제는 필요하지 않습니다.

| 데모 시나리오 | 계산 결과 |
|---|---:|
| 미국주식 실현이익 1,000만원, 실현손실 200만원 | 추정 양도세 1,210,000원 |
| 국내 일반 배당 100만원, 기본 원천징수 가정 | 입금액 846,000원 |
| 미국 배당 100만원, 해외 15%·국내 0원 명시적 가정 | 입금액 850,000원 |
| ISA 적격 이익 500만원·적격 손실 100만원, 일반형 | 요건 충족 가정 세금 198,000원 |
| 같은 ISA 손익, 서민형 | 요건 충족 가정 세금 0원 |

전체 결과와 경고는 `python -m money_manager.demo --json`으로 확인합니다.
새 버전을 별도 폴더에 설치하려면 아래 명령을 사용하세요. 기존 폴더의 DB는 보존됩니다.

```bash
git clone --branch feat/tax-engine-step2 https://github.com/onlyuri81-del/money_manager.git money_manager_step2
cd money_manager_step2
python -m pip install -e .
python -m unittest discover -s tests -v
python -m money_manager.demo
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

## 기존 DB의 세금 보고서

계산에는 `Taxpayer`, 계좌·종목·거래, 연결된 `TaxEvent`, 명시적으로 선택한 `TaxPolicy`가 필요합니다.
위 보유종목 예시나 빈 DB만으로 과세 손익을 추론하지 않습니다.
적재 방법은 [2단계 설계](docs/step2_design.md)와 `demo.py`의 예시를 참고하세요.

```bash
python -m money_manager.tax_cli annual portfolio.db --taxpayer OWNER_ID --year 2026 --policy POLICY_ID
python -m money_manager.tax_cli isa portfolio.db --account ISA_ACCOUNT_ID --as-of 2026-09-30 --policy POLICY_ID
```

CLI는 SQLite를 읽기 전용으로 열고 JSON을 출력합니다. `OWNER_ID` 등은 실제 저장된 ID로 바꿉니다.
자료 누락이나 미지원 분류가 있으면 해당 계산을 `null`로 표시하고 `issues`에 사유를 기록합니다.
계산을 막는 사유가 있으면 종료코드 `2`입니다. 자료 범위 미확인 경고만 있으면 종료코드는 `0`입니다.
모든 증권사·은행의 해당 기간 자료가 빠짐없이 반영됐다고 확인한 경우에만 `--confirm-coverage`를 붙입니다.
그 전에는 등록 소득이 기준금액 이하더라도 금융소득 초과 여부가 `null`일 수 있습니다.

배당 입금액은 원천징수 후 현금이며 최종 종합과세 후 금액과 다릅니다.
ISA는 적격 과세 산입액·손실을 계약기간 전체로 계산하며, 일반적인 중도해지 추징은 아직 계산하지 않습니다.
정책 상태 `DRAFT`·`SCENARIO`는 검증된 세법으로 자동 승격하지 않습니다.

## 구성

| 파일 | 역할 |
|---|---|
| `src/money_manager/migrations/001_initial.sql` | 10개 테이블, 조회 뷰, 무결성 제약과 트리거 |
| `src/money_manager/models.py` | 계좌·자산·거래·세금·정책의 Pydantic 모델 |
| `src/money_manager/db.py` | DB 연결, 원자적 초기화, 저장·읽기 및 정확한 단위 변환 |
| `src/money_manager/tax_calculators.py` | DB와 독립된 양도세·배당·ISA·금융소득 계산기 |
| `src/money_manager/tax_engine.py` | 납세자 연간 집계, ISA 계약 집계, 누락·분류·정책 경고 |
| `src/money_manager/tax_cli.py` | 실제 적재 DB의 읽기 전용 JSON 보고서 |
| `src/money_manager/demo.py` | 실제 DB를 사용하지 않는 실행 예시 |
| `tests/test_step1.py` | 데이터 왕복, 세금 귀속연도, 계좌 제한, 중복 및 롤백 검증 |
| `tests/test_step2.py`, `tests/test_step2_cli.py` | 계산 경계, 계좌별 과세, 누락 자료와 명령 실행 검증 |
| `docs/step1_design.md` | 데이터 관계, 세무 처리 범위와 이후 단계의 계약 |
| `docs/step2_design.md` | 계산 계약, 세법 확인 자료, 지원 범위와 제한 |

`initialize()`는 새 DB만 초기화합니다. 기존 미관리 DB를 발견하면 명시적 마이그레이션을 요구하며,
기존 테이블을 지우거나 스키마를 덮어쓰지 않습니다. `insert()`는 커밋하지 않습니다.
트랜잭션 묶음과 커밋/롤백의 책임은 호출자에게 있습니다.
