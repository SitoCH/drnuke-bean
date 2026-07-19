# drnuke-bean

Beancount v3 importers and plugins for Swiss personal finance.

---

## Quickstart

clone and then install drnuke-bean in your python env
```bash
pip install -e .
```

### Plugins

Load both plugins in a ledger and explore them in fava:

```bash
fava ledger_plugins_example.bean
```

Compare the transactions in `ledger_plugins_example.bean` with what fava shows — the plugin-generated child transactions demonstrate each mode.

### Importers

1. Copy the example files and fill in your values:

```bash
cp pipeline_secrets.example.py pipeline_secrets.py
cp run_imports_pipeline.example.py run_imports_pipeline.py
cp transaction_fixes_example.py transaction_fixes.py
```

2. Edit `pipeline_secrets.py` with your local paths and credentials.

3. Run the pipeline (last month by default):

```bash
python run_imports_pipeline.py
# or selectively:
python run_imports_pipeline.py zkb ibkr --from 2026-01
```

---

## Overview

### Plugins

**amortize** — Distributes a single transaction across multiple periods. Two modes:
- `spread` (default): cash flow stays on the original date; income/expense legs are deferred through buffer accounts and recognised gradually.
- `split`: the original transaction is replaced by N equal sub-transactions. No buffer accounts.

**tax_forecast** — Predicts Swiss income tax for the current year. Reads configured income accounts, extrapolates to a full-year figure, queries the official ESTV tax calculator API, and emits monthly tax-accrual transactions. API results are disk-cached and refreshed once per day.

### Importers

**IBKR** — Interactive Brokers FlexQuery XML reports. Handles trades, dividends, withholding tax, interest, fees, deposits and balances. Supports multiple IBKR sub-accounts via an `account_map`. Statements are fetched via the FlexQuery API (`ibkr_flexquery.py`) with disk caching to avoid redundant network calls.

**PostFinance (PFG)** — PostFinance giro account CSV exports. Emits single-legged transactions; the counter-posting is filled by a user-supplied `fixes` function and/or `smart_importer`.

**ZKB** — Zürcher Kantonalbank CAMT.053 XML statements. Supports transfers, mobile-banking batches, Visa Debit card transactions and empty statements. Download is handled separately via EBICS H005 (`zkb_ebics.py`) with disk caching. The importer itself is a pure XML parser.

**Finpension** — Finpension CSV exports (pillar 2 & 3a). Handles buy/sell trades, deposits, fees and dividends. One importer instance covers all portfolios by extracting pillar and portfolio from the filename via a configurable regex.

**SBB** — SBB travel CSV exports. Rows paid via Halbtax PLUS are drawn from a prepaid asset account; all other rows from a configured bank account.

---

## Details

### amortize plugin

Invocation:
```
plugin "drnukebean.plugins.amortize" "{'buffer_acc_base': 'Assets:Prepaid:'}"
```

Transaction meta keys:

| Key | Required | Description |
|---|---|---|
| `p_amortize_start` | yes | `"YYYY-MM-DD"` — start date of first period |
| `p_amortize_frequency` | yes | `D` `W` `M` `Q` `Y` |
| `p_amortize_times` | yes | number of periods as string |
| `p_amortize_mode` | no | `"spread"` (default) or `"split"` |

**Spread mode**: asset/liability postings remain on the original transaction date. Each income/expense posting is redirected into a buffer account (`{buffer_acc_base}{account-without-root}`), which is auto-opened by the plugin. N child transactions drain the buffer pro-rata over the configured period. The last period absorbs any rounding remainder.

**Split mode**: the original template transaction is replaced wholesale by N scaled-down copies. No buffer accounts. Each child transaction carries the full set of postings divided by N.

Both modes support multi-leg transactions (multiple income or expense postings in one transaction).

### tax_forecast plugin

Invocation:
```
plugin "drnukebean.plugins.tax_forecast" "{'year': 2024, 'api_year': 2024,
    'taxable_accounts': ['Income:Jobs:Taxable:Salary', 'Income:Invest:.*:Div'],
    'deductible_accounts': [], 'tax_expenses_main_account': 'Expenses:Taxes',
    'liability_account': 'Liabilities:Tax', 'municipality': 261,
    'marital_status': 'single', 'n_children': 0, 'tax_day_of_month': 24}"
```
* `year`: the tax tariff year
* `api_year`: parameter used by the api
* `taxable_accounts`: those accounts will be summed up
* `deductible_accounts`: not yet implemented
* `tax_expenses_main_account`: account to book tax expenses to
* `liability_account`: liability account to book tax liabilities to
* `municipality`: number id of the municipality. must be reverse-engineered from the tax calculator webpage https://swisstaxcalculator.estv.admin.ch/#/calculator/income-wealth-tax api request


`taxable_accounts` accepts regex patterns matched against open account names. Income to date is extrapolated linearly to a full-year figure and passed to the ZH web tax API. The result is spread as equal monthly accruals across all months covered so far, emitted on `tax_day_of_month` each month. API responses are cached under `{ledger_dir}/.cache` and refreshed at midnight.


### IBKR importer

Files are identified by XML content (`<FlexQueryResponse>` root element). When `query_name` is configured, the `queryName` attribute is also validated, which prevents mixing up multiple queries.

The flexquery must be configured in the IBKR portal accordingly.

The `account_map` maps each IBKR account ID to a beancount root account and an optional `deposit_from` account. If `deposit_from` is absent, deposit and withdrawal transactions are flagged `!` for manual annotation. Account IDs not present in `account_map` raise a `RuntimeError` at import time rather than silently dropping data.

Setting `transactionID_labeled_since` enables exact lot matching: lots acquired on or after that date get a CostSpec label equal to IBKR's `transactionID`, so a sell reduces exactly the lot IBKR reports as closed (same-day multi-lot sells are otherwise ambiguous under booking-method tiebreaks). Set it once -- to the date of the first import run with this importer version -- and never change it afterwards. Pre-threshold lots keep the previous priced/date-only cost specs and carry the `transactionID` as posting metadata, usable by a later scripted ledger migration. Leaving the field unset disables labeling and logs a warning on every run.

The network layer (`ibkr_flexquery.py`) caches raw responses to disk to avoid repeated API calls during development.

Required FlexQuery fields are listed in `ibkr.py`'s module docstring; deviating from them raises a descriptive error rather than silently producing wrong output.

### PostFinance importer

The CSV format has a 6-row metadata header before the column headers; the importer skips these rows and validates the IBAN from the header against the configured IBAN.

The `fixes` function receives a dict of transaction fields and may modify any of them — narration, payee, flag, postings, metadata — before the `data.Transaction` is constructed.

### ZKB importer

Parses CAMT.053.001.08 (ISO 20022, Swiss SPS/2.2 variant). The split between `zkb_camt.py` (pure XML parser) and `zkb_ebics.py` (network/credentials) is intentional: the importer can be used without EBICS by pointing it at manually downloaded XML files.

### Finpension importer

Finpension always exports the complete transaction history. The `year` parameter restricts extraction to a single calendar year; set to `None` to import everything.

Holdings use a `price` annotation (current market price in CHF) rather than cost-basis lots, consistent with Finpension not tracking lots.

The `regex` parameter extracts the pillar (e.g. `S3a`) and portfolio (e.g. `Portfolio1`) from the filename. One importer instance handles all files whose names match the pattern; the matched groups are substituted into `root_account` to produce the per-portfolio account path. The default regex is `r"finpension_(S[23][a]?)_(Portfolio\d)"`.

`isin_lookup` is a required dict mapping each ISIN to a beancount commodity ticker. Finpension does not supply ticker symbols, and Yahoo Finance tickers are not valid beancount commodity names, so the mapping must be maintained by the user.

### SBB importer

Identifies files by CSV header fingerprint in both German and English (SBB exports the same format in both languages depending on account locale).

Rows with `Zahlungsmittel` / `Payment methods` matching `Halbtax PLUS` or `Half Fare Card PLUS` are drawn from `account_halbtax`. All other rows use `account_bank`. If `account_bank` is not configured, non-Halbtax rows are emitted with flag `!` and no counter-posting.

### spreading / recurring (v2 legacy, deprecated)

`drnukebean.plugins.spreading` and `drnukebean.plugins.recurring` are v2-era plugins retained for reference only. They are superseded by `drnukebean.plugins.amortize`, which covers both use cases in a single implementation with multi-leg support. Do not use them in new ledgers.
