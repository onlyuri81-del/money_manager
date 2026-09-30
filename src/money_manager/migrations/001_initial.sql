-- Monetary totals: INTEGER KRW. Prices, quantities and FX: INTEGER x 1,000,000.
-- Foreign key enforcement must be enabled on EVERY connection, before BEGIN.
CREATE TABLE taxpayers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    birth_date TEXT CHECK(birth_date IS NULL OR (length(birth_date) = 10 AND date(birth_date, '+0 days') IS birth_date AND birth_date >= '1900-01-01')),
    residency TEXT NOT NULL CHECK(residency IN ('KR','OTHER')),
    us_tax_person INTEGER NOT NULL CHECK(us_tax_person IN (0,1))
) STRICT;

CREATE TABLE taxpayer_years (
    id TEXT PRIMARY KEY,
    taxpayer_id TEXT NOT NULL REFERENCES taxpayers(id),
    tax_year INTEGER NOT NULL CHECK(tax_year BETWEEN 1900 AND 9999),
    salary_krw INTEGER CHECK(salary_krw >= 0),
    comprehensive_income_krw INTEGER CHECK(comprehensive_income_krw >= 0),
    UNIQUE(taxpayer_id, tax_year)
) STRICT;

CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    taxpayer_id TEXT NOT NULL REFERENCES taxpayers(id),
    broker TEXT NOT NULL CHECK(length(trim(broker)) > 0),
    label TEXT NOT NULL CHECK(length(trim(label)) > 0),
    account_type TEXT NOT NULL CHECK(account_type IN ('GENERAL','ISA','PENSION_SAVINGS','IRP')),
    isa_type TEXT CHECK(isa_type IN ('GENERAL','LOW_INCOME','FARMER')),
    opened_on TEXT NOT NULL CHECK(length(opened_on) = 10 AND date(opened_on, '+0 days') IS opened_on AND opened_on >= '1900-01-01'),
    maturity_on TEXT CHECK(maturity_on IS NULL OR (length(maturity_on) = 10 AND date(maturity_on, '+0 days') IS maturity_on)),
    closed_on TEXT CHECK(closed_on IS NULL OR (length(closed_on) = 10 AND date(closed_on, '+0 days') IS closed_on)),
    CHECK((account_type = 'ISA' AND isa_type IS NOT NULL AND maturity_on IS NOT NULL)
       OR (account_type <> 'ISA' AND isa_type IS NULL AND maturity_on IS NULL)),
    CHECK(maturity_on IS NULL OR maturity_on > opened_on),
    CHECK(closed_on IS NULL OR closed_on >= opened_on)
) STRICT;
CREATE UNIQUE INDEX one_active_isa ON accounts(taxpayer_id)
    WHERE account_type = 'ISA' AND closed_on IS NULL;

CREATE TABLE instruments (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL CHECK(length(trim(ticker)) > 0),
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    market TEXT NOT NULL CHECK(market IN ('KR','US')),
    currency TEXT NOT NULL CHECK(currency IN ('KRW','USD')),
    asset_type TEXT NOT NULL CHECK(asset_type IN ('STOCK','ETF_DOMESTIC_EQUITY','ETF_OTHER')),
    pension_eligible INTEGER NOT NULL CHECK(pension_eligible IN (0,1)),
    CHECK((market = 'KR' AND currency = 'KRW') OR (market = 'US' AND currency = 'USD')),
    CHECK(asset_type <> 'ETF_DOMESTIC_EQUITY' OR market = 'KR'),
    CHECK(pension_eligible = 0 OR (market = 'KR' AND asset_type <> 'STOCK')),
    UNIQUE(market, ticker)
) STRICT;

CREATE TABLE holdings (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    quantity_micros INTEGER NOT NULL CHECK(quantity_micros > 0),
    average_buy_price_micros INTEGER NOT NULL CHECK(average_buy_price_micros >= 0),
    acquisition_cost_krw INTEGER NOT NULL CHECK(acquisition_cost_krw >= 0),
    current_price_micros INTEGER CHECK(current_price_micros >= 0),
    current_fx_micros INTEGER CHECK(current_fx_micros > 0),
    valued_at TEXT,
    CHECK((current_price_micros IS NULL AND current_fx_micros IS NULL AND valued_at IS NULL)
       OR (current_price_micros IS NOT NULL AND current_fx_micros IS NOT NULL AND valued_at IS NOT NULL)),
    UNIQUE(account_id, instrument_id)
) STRICT;

CREATE TABLE cash_balances (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    currency TEXT NOT NULL CHECK(currency IN ('KRW','USD')),
    amount_micros INTEGER NOT NULL CHECK(amount_micros >= 0),
    fx_micros INTEGER NOT NULL CHECK(fx_micros > 0),
    valued_at TEXT NOT NULL,
    CHECK(currency <> 'KRW' OR fx_micros = 1000000),
    UNIQUE(account_id, currency)
) STRICT;

CREATE TABLE transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    instrument_id TEXT REFERENCES instruments(id),
    external_ref TEXT NOT NULL CHECK(length(trim(external_ref)) > 0),
    transaction_type TEXT NOT NULL CHECK(transaction_type IN
        ('BUY','SELL','DIVIDEND','INTEREST','DEPOSIT','WITHDRAWAL','TRANSFER_IN','TRANSFER_OUT')),
    traded_on TEXT NOT NULL CHECK(length(traded_on) = 10 AND date(traded_on, '+0 days') IS traded_on AND traded_on >= '1900-01-01'),
    recognized_on TEXT NOT NULL CHECK(length(recognized_on) = 10 AND date(recognized_on, '+0 days') IS recognized_on AND recognized_on >= '1900-01-01'),
    currency TEXT NOT NULL CHECK(currency IN ('KRW','USD')),
    gross_amount_micros INTEGER NOT NULL CHECK(gross_amount_micros >= 0),
    fees_micros INTEGER NOT NULL CHECK(fees_micros >= 0),
    fx_micros INTEGER NOT NULL CHECK(fx_micros > 0),
    quantity_micros INTEGER CHECK(quantity_micros > 0),
    unit_price_micros INTEGER CHECK(unit_price_micros >= 0),
    CHECK(recognized_on >= traded_on),
    CHECK(currency <> 'KRW' OR fx_micros = 1000000),
    CHECK((transaction_type IN ('BUY','SELL') AND instrument_id IS NOT NULL
        AND quantity_micros IS NOT NULL AND unit_price_micros IS NOT NULL)
       OR (transaction_type NOT IN ('BUY','SELL') AND quantity_micros IS NULL AND unit_price_micros IS NULL)),
    CHECK(transaction_type <> 'DIVIDEND' OR instrument_id IS NOT NULL),
    UNIQUE(account_id, external_ref),
    UNIQUE(id, account_id, recognized_on)
) STRICT;

CREATE TABLE tax_policies (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL CHECK(length(trim(code)) > 0),
    effective_from TEXT NOT NULL CHECK(length(effective_from) = 10 AND date(effective_from, '+0 days') IS effective_from AND effective_from >= '1900-01-01'),
    effective_to TEXT NOT NULL CHECK(length(effective_to) = 10 AND date(effective_to, '+0 days') IS effective_to),
    verified_on TEXT CHECK(verified_on IS NULL OR (length(verified_on) = 10 AND date(verified_on, '+0 days') IS verified_on AND verified_on >= '1900-01-01')),
    status TEXT NOT NULL CHECK(status IN ('DRAFT','VERIFIED','SCENARIO')),
    source_urls_json TEXT NOT NULL CHECK(json_valid(source_urls_json) AND json_type(source_urls_json) = 'array'),
    parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json) AND json_type(parameters_json) = 'object'),
    CHECK(effective_to >= effective_from),
    CHECK(status <> 'VERIFIED' OR (verified_on IS NOT NULL AND json_array_length(source_urls_json) > 0)),
    UNIQUE(code, effective_from)
) STRICT;

CREATE TABLE tax_events (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    recognized_on TEXT NOT NULL,
    tax_year INTEGER GENERATED ALWAYS AS (CAST(substr(recognized_on, 1, 4) AS INTEGER)) STORED,
    event_type TEXT NOT NULL CHECK(event_type IN ('CAPITAL_GAIN','DIVIDEND','INTEREST')),
    treatment TEXT NOT NULL CHECK(treatment IN
        ('STOCK_CAPITAL_GAIN','FINANCIAL_INCOME','EXEMPT','ISA','PENSION_DEFERRED','SEPARATE_DIVIDEND','REVIEW_REQUIRED')),
    economic_income_krw INTEGER NOT NULL,
    taxable_income_krw INTEGER,
    domestic_withholding_krw INTEGER NOT NULL CHECK(domestic_withholding_krw >= 0),
    foreign_withholding_krw INTEGER NOT NULL CHECK(foreign_withholding_krw >= 0),
    applied_rate_ppm INTEGER CHECK(applied_rate_ppm BETWEEN 0 AND 1000000),
    policy_id TEXT REFERENCES tax_policies(id),
    basis_source TEXT NOT NULL CHECK(length(trim(basis_source)) > 0),
    CHECK(event_type = 'CAPITAL_GAIN' OR economic_income_krw >= 0),
    CHECK(event_type = 'CAPITAL_GAIN' OR taxable_income_krw IS NULL OR taxable_income_krw >= 0),
    CHECK(treatment <> 'EXEMPT' OR (taxable_income_krw IS NOT NULL AND taxable_income_krw = 0)),
    FOREIGN KEY(transaction_id, account_id, recognized_on)
        REFERENCES transactions(id, account_id, recognized_on)
) STRICT;
CREATE INDEX tax_events_year_account ON tax_events(tax_year, account_id, treatment);
CREATE INDEX transactions_account_date ON transactions(account_id, recognized_on);

CREATE TRIGGER policy_no_overlap BEFORE INSERT ON tax_policies
WHEN EXISTS (SELECT 1 FROM tax_policies p WHERE p.code = NEW.code
    AND p.effective_from <= NEW.effective_to AND p.effective_to >= NEW.effective_from)
BEGIN SELECT RAISE(ABORT, 'policy periods overlap'); END;

CREATE TRIGGER event_policy_period BEFORE INSERT ON tax_events
WHEN NEW.policy_id IS NOT NULL AND EXISTS (SELECT 1 FROM tax_policies p WHERE p.id = NEW.policy_id
    AND (NEW.recognized_on < p.effective_from OR NEW.recognized_on > p.effective_to))
BEGIN SELECT RAISE(ABORT, 'tax event outside policy period'); END;

CREATE TABLE pension_contributions (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL UNIQUE,
    account_id TEXT NOT NULL,
    recognized_on TEXT NOT NULL,
    tax_year INTEGER GENERATED ALWAYS AS (CAST(substr(recognized_on, 1, 4) AS INTEGER)) STORED,
    contribution_source TEXT NOT NULL CHECK(contribution_source IN ('PERSONAL','RETIREMENT_TRANSFER','ISA_TRANSFER')),
    amount_krw INTEGER NOT NULL CHECK(amount_krw > 0),
    credit_claimed_krw INTEGER CHECK(credit_claimed_krw >= 0 AND credit_claimed_krw <= amount_krw),
    CHECK(contribution_source <> 'RETIREMENT_TRANSFER' OR credit_claimed_krw IS NULL OR credit_claimed_krw = 0),
    FOREIGN KEY(transaction_id, account_id, recognized_on)
        REFERENCES transactions(id, account_id, recognized_on)
) STRICT;

-- These triggers also run on direct SQL imports, not just Pydantic writes.
CREATE TRIGGER holding_eligibility_insert BEFORE INSERT ON holdings
WHEN EXISTS (
    SELECT 1 FROM accounts a, instruments i WHERE a.id = NEW.account_id AND i.id = NEW.instrument_id
    AND ((a.account_type <> 'GENERAL' AND i.market = 'US')
      OR (a.account_type IN ('PENSION_SAVINGS','IRP') AND i.pension_eligible = 0)
      OR (i.currency = 'KRW' AND NEW.current_fx_micros IS NOT NULL AND NEW.current_fx_micros <> 1000000))
)
BEGIN SELECT RAISE(ABORT, 'ineligible holding or invalid KRW FX'); END;
CREATE TRIGGER holding_eligibility_update BEFORE UPDATE ON holdings
WHEN EXISTS (
    SELECT 1 FROM accounts a, instruments i WHERE a.id = NEW.account_id AND i.id = NEW.instrument_id
    AND ((a.account_type <> 'GENERAL' AND i.market = 'US')
      OR (a.account_type IN ('PENSION_SAVINGS','IRP') AND i.pension_eligible = 0)
      OR (i.currency = 'KRW' AND NEW.current_fx_micros IS NOT NULL AND NEW.current_fx_micros <> 1000000))
)
BEGIN SELECT RAISE(ABORT, 'ineligible holding or invalid KRW FX'); END;

CREATE TRIGGER transaction_eligibility BEFORE INSERT ON transactions
WHEN EXISTS (
    SELECT 1 FROM accounts a, instruments i WHERE a.id = NEW.account_id AND i.id = NEW.instrument_id
    AND ((a.account_type <> 'GENERAL' AND i.market = 'US')
      OR (a.account_type IN ('PENSION_SAVINGS','IRP') AND i.pension_eligible = 0)
      OR i.currency <> NEW.currency)
)
BEGIN SELECT RAISE(ABORT, 'ineligible transaction or currency mismatch'); END;

CREATE TRIGGER tax_event_kind BEFORE INSERT ON tax_events
WHEN NOT EXISTS (
    SELECT 1 FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE t.id = NEW.transaction_id
      AND ((NEW.event_type = 'CAPITAL_GAIN' AND t.transaction_type = 'SELL')
        OR (NEW.event_type = 'DIVIDEND' AND t.transaction_type = 'DIVIDEND')
        OR (NEW.event_type = 'INTEREST' AND t.transaction_type = 'INTEREST'))
      AND (NEW.treatment = 'REVIEW_REQUIRED'
        OR (a.account_type = 'GENERAL' AND NEW.treatment IN
            ('STOCK_CAPITAL_GAIN','FINANCIAL_INCOME','EXEMPT','SEPARATE_DIVIDEND'))
        OR (a.account_type = 'ISA' AND NEW.treatment IN ('ISA','EXEMPT'))
        OR (a.account_type IN ('PENSION_SAVINGS','IRP') AND NEW.treatment = 'PENSION_DEFERRED'))
      AND (NEW.treatment <> 'STOCK_CAPITAL_GAIN' OR NEW.event_type = 'CAPITAL_GAIN')
      AND (NEW.treatment <> 'SEPARATE_DIVIDEND' OR NEW.event_type = 'DIVIDEND')
)
BEGIN SELECT RAISE(ABORT, 'tax event kind or account treatment mismatch'); END;

CREATE TRIGGER contribution_kind BEFORE INSERT ON pension_contributions
WHEN NOT EXISTS (
    SELECT 1 FROM transactions t JOIN accounts a ON a.id = t.account_id
    WHERE t.id = NEW.transaction_id AND a.account_type IN ('PENSION_SAVINGS','IRP')
      AND t.currency = 'KRW' AND t.gross_amount_micros = NEW.amount_krw * 1000000
      AND ((NEW.contribution_source = 'PERSONAL' AND t.transaction_type = 'DEPOSIT')
        OR (NEW.contribution_source IN ('RETIREMENT_TRANSFER','ISA_TRANSFER') AND t.transaction_type = 'TRANSFER_IN'))
)
BEGIN SELECT RAISE(ABORT, 'invalid pension contribution source or amount'); END;

-- Historical identifiers/tax classifications are immutable in step 1.
-- Correct imports by reverting and reimporting within an explicit transaction.
CREATE TRIGGER transaction_immutable BEFORE UPDATE ON transactions
BEGIN SELECT RAISE(ABORT, 'transactions are immutable'); END;
CREATE TRIGGER tax_event_immutable BEFORE UPDATE ON tax_events
BEGIN SELECT RAISE(ABORT, 'tax events are immutable'); END;
CREATE TRIGGER contribution_immutable BEFORE UPDATE ON pension_contributions
BEGIN SELECT RAISE(ABORT, 'contributions are immutable'); END;
CREATE TRIGGER instrument_classification_immutable BEFORE UPDATE OF market, currency, asset_type, pension_eligible ON instruments
BEGIN SELECT RAISE(ABORT, 'instrument classification is immutable'); END;
CREATE TRIGGER account_identity_immutable BEFORE UPDATE OF taxpayer_id, account_type, opened_on, maturity_on, isa_type ON accounts
BEGIN SELECT RAISE(ABORT, 'account identity is immutable'); END;
CREATE TRIGGER policy_immutable BEFORE UPDATE ON tax_policies
BEGIN SELECT RAISE(ABORT, 'create a new policy version'); END;

CREATE VIEW holdings_detail AS
SELECT h.*, i.ticker, i.name AS instrument_name, i.market, i.currency, i.asset_type,
       a.taxpayer_id, a.broker, a.account_type
FROM holdings h JOIN instruments i ON i.id = h.instrument_id JOIN accounts a ON a.id = h.account_id;
