# Money Manager

Python / SQLite / Pydantic 기반 자산·세무 관리 프로그램입니다.

1단계 스키마와 모델 구현은 검토용 브랜치 `feat/tax-schema-step1`에서 확인하세요.

```bash
git clone --branch feat/tax-schema-step1 https://github.com/onlyuri81-del/money_manager.git
cd money_manager
python -m pip install -e .
python -m money_manager.db portfolio.db
python -m unittest discover -s tests -v
```

현재 개발 범위는 1단계 데이터 기반이며 세금 계산 엔진과 대시보드는 후속 단계입니다.
