"""
A beancount plugin to predict tax liabilities in Kanton Zurich, Switzerland.

The plugin:
  1. Sums the current year's taxable income across configured accounts.
  2. Extrapolates linearly to a full year's income.
  3. Queries the ZH web tax calculator API for cantonal + federal tax.
  4. Spreads the result uniformly across the months covered so far.

Config is a Python dict literal passed as the plugin argument. Single quotes
inside the double-quoted beancount string require no escaping:

    plugin "drnukebean.plugins.tax_forecast" "{'year': 2024, 'api_year': 2024,
        'taxable_accounts': ['Income:Jobs:Taxable:Salary', 'Income:Invest:IB:.*:Div'],
        'deductible_accounts': [], 'tax_expenses_main_account': 'Expenses:Taxes',
        'liability_account': 'Liabilities:Tax', 'municipality': 261,
        'marital_status': 'single', 'n_children': 0,
        'tax_day_of_month': 24, 'precision': 2}"

API responses are cached in a diskcache store under {ledger_dir}/.cache with a
TTL that expires at midnight of the current day.
"""

import ast
import collections
import datetime
import http.client as httplib
import json
import re
from pathlib import Path
from typing import cast

import beanquery
import diskcache
from beancount.core import data
from beancount.core.amount import Amount
from beancount.core.number import Decimal
from loguru import logger

__plugins__ = ["tax_forecast"]

TaxForecastError = collections.namedtuple("TaxForecastError", "source message entry")


class APIError(Exception):
    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def tax_forecast(entries, options, config_str):
    errors = []
    config = ast.literal_eval(config_str)
    today = datetime.datetime.today().date()
    year = config.get("year")
    api_year = config.get("api_year")

    # resolve cache directory from ledger file location
    ledger_file = options.get("filename", "")
    cache_dir = Path(ledger_file).parent / ".cache" if ledger_file else Path(".cache")

    # get accounts (via open directives)
    accounts = {entry.account for entry in entries if isinstance(entry, data.Open)}
    taxable_accounts, _deductible_accounts = get_accounts(config, accounts)

    # aggregate positions per currency for the configured year
    rows = get_income_from_accounts(entries, options, taxable_accounts, year)
    if not rows:  # no income for that year
        return entries, errors

    last_month_of_income = max(r[0].month for r in rows)

    # sum positions per currency
    # row layout: (date, account, position: Position, currency: str)
    by_currency: dict[str, float] = collections.defaultdict(float)
    for r in rows:
        by_currency[r[3]] += float(r[2].units.number)

    # convert to base currency and extrapolate to full year
    base_currency = options.get("operating_currency")[0]
    prices = [e for e in entries if isinstance(e, data.Price)]
    taxable_income_float = abs(
        _to_base_currency(by_currency, base_currency, prices, today) / last_month_of_income * 12
    )

    assets = 0
    withholding = 0
    municipality = config.get("municipality")
    marital_status = config.get("marital_status")
    n_children = config.get("n_children")

    # query tax calculator API
    url_staat = "/ZH-Web-Calculators/calculators/INCOME_ASSETS/calculate"
    url_bund = "/ZH-Web-Calculators/calculators/FEDERAL/calculate"

    data_staat = {
        "isLiabilityLessThanAYear": False,
        "hasTaxSeparation": False,
        "hasQualifiedInvestments": False,
        "taxYear": str(api_year),
        "liabilityBegin": None,
        "liabilityEnd": None,
        "name": "",
        "maritalStatus": str(marital_status).lower(),
        "taxScale": "BASIC",
        "religionP1": "OTHERS",
        "religionP2": "OTHERS",
        "municipality": str(municipality),
        "taxableIncome": str(taxable_income_float),
        "ascertainedTaxableIncome": None,
        "qualifiedInvestmentsIncome": None,
        "taxableAssets": str(assets),
        "ascertainedTaxableAssets": None,
        "withholdingTax": str(withholding),
    }

    data_bund = {
        "isLiabilityLessThanAYearOrHasTaxSeparation": False,
        "taxYear": str(api_year),
        "name": "",
        "taxScale": str(marital_status).upper(),
        "childrenNo": str(n_children),
        "taxableIncome": str(taxable_income_float),
        "ascertainedTaxableIncome": None,
    }

    try:
        response_staat = query_zh_tax_api(url_staat, data_staat, cache_dir)
        response_bund = query_zh_tax_api(url_bund, data_bund, cache_dir)
    except APIError:
        logger.info("could not fetch tax info from API. Not providing tax forecast")
        return entries, errors

    # extract relevant info and convert to per-month amounts
    precision = config.get("precision")
    taxes = {
        "Staats": response_staat["cantonalBaseTax"]["value"],
        "Gemeinde": response_staat["municipalityTax"]["value"],
        "Personal": response_staat["personalTax"]["value"],
        "Vermoegen": response_staat["assetsTax"]["value"],
        "Bundes": response_bund["totalFederalTax"]["value"],
    }
    taxes_per_month = {
        kind: (Decimal(str(value)) / 12).quantize(Decimal(10) ** -precision)
        for kind, value in taxes.items()
    }

    # create transactions
    tax_transactions = make_transactions(config, options, taxes_per_month, last_month_of_income)
    entries.extend(tax_transactions)

    return entries, errors


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------


def get_accounts(config, accounts):
    """Return (taxable_accounts, deductible_accounts) matched against open accounts."""

    def _match(patterns):
        if not patterns:
            return []
        combined = "(" + ")|(".join(patterns) + ")"
        return [acc for acc in accounts if re.search(combined, acc, re.IGNORECASE)]

    taxable_accounts = _match(config.get("taxable_accounts", []))
    deductible_accounts = _match(config.get("deductible_accounts", []))
    return taxable_accounts, deductible_accounts


# ---------------------------------------------------------------------------
# Income aggregation
# ---------------------------------------------------------------------------


def get_income_from_accounts(entries, options, taxable_accounts, year):
    """Return a flat list of row namedtuples (date, account, position, currency)."""
    if not taxable_accounts:
        return []

    conn = beanquery.connect("beancount:", entries=entries, errors=[], options=options)
    rows = []
    for acc in taxable_accounts:
        cursor = conn.execute(
            f'SELECT date, account, position, currency WHERE account = "{acc}" AND year = {year}'
        )
        rows.extend(cursor.fetchall())
    return rows


# ---------------------------------------------------------------------------
# FX conversion
# ---------------------------------------------------------------------------


def _to_base_currency(by_currency, base_currency, prices, today):
    """Convert a {currency: float} dict to a single base-currency float."""
    total = 0.0
    for currency, amount in by_currency.items():
        if currency == base_currency:
            total += amount
        else:
            prices_curr = [p for p in prices if p.currency == currency]
            if not prices_curr:
                logger.warning(f"No price entries found for currency {currency}, skipping")
                continue
            best = min(prices_curr, key=lambda p: abs(p.date - today))
            total += amount * float(best.amount.number)
    return total


# ---------------------------------------------------------------------------
# API cache helpers
# ---------------------------------------------------------------------------


def _seconds_until_midnight() -> float:
    """Return seconds remaining until the next calendar-day boundary."""
    now = datetime.datetime.now()
    midnight = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - now).total_seconds()


def _cache_key(payload: dict) -> str:
    return str(sorted(payload.items()))


def query_zh_tax_api(url: str, payload: dict, cache_dir: Path) -> dict:
    key = _cache_key(payload)
    with diskcache.Cache(cache_dir) as cache:
        if key in cache:
            logger.info("tax API: returning cached response")
            return cast(dict, cache[key])

    logger.info("tax API: cache miss, querying API")
    host = "webcalc.services.zh.ch"
    headers = {"Content-Type": "application/json"}
    conn = httplib.HTTPSConnection(host)
    conn.request("POST", url, json.dumps(payload), headers)
    response = conn.getresponse()

    if response.status != 200:
        logger.info(f"tax API returned {response.status}")
        raise APIError(f"Tax API did not return 200 but {response.status}")

    logger.info("tax API: query successful")
    answer = json.loads(response.read())
    ttl = _seconds_until_midnight()
    with diskcache.Cache(cache_dir) as cache:
        cache.set(key, answer, expire=ttl)
    return answer


# ---------------------------------------------------------------------------
# Transaction builder
# ---------------------------------------------------------------------------


def make_transactions(config, options, taxes_per_month, last_month_of_income):
    year = config.get("year")
    base_currency = options.get("operating_currency")[0]
    day = config.get("tax_day_of_month")
    tax_base_account = config.get("tax_expenses_main_account")

    postings = [
        data.Posting(
            f"{tax_base_account}:{tax_type}",
            Amount(value, base_currency),
            None,
            None,
            None,
            None,
        )
        for tax_type, value in taxes_per_month.items()
    ]
    postings.append(
        data.Posting(
            config.get("liability_account"),
            Amount(-sum(taxes_per_month.values(), Decimal(0)), base_currency),
            None,
            None,
            None,
            None,
        )
    )

    tax_transactions = []
    for month in range(1, last_month_of_income + 1):
        month_name = datetime.date(1900, month, 1).strftime("%b")
        trans = data.Transaction(
            data.new_metadata("<tax_forecast>", 0),
            datetime.date(year, month, day),
            "*",
            "Tax Authority",
            f"Tax forecast {month_name} {year}",
            data.EMPTY_SET,
            data.EMPTY_SET,
            list(postings),
        )
        tax_transactions.append(trans)
    return tax_transactions
