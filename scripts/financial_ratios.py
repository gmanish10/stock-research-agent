"""
Financial ratio calculations.

Input: financial statement data (dicts pulled from yfinance / the agent adapter).
Output: computed ratios.

The agent pulls income/balance/cashflow (latest column -> dict) and passes them here.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

import pandas as pd


@dataclass
class Ratios:
    gross_margin: float | None
    operating_margin: float | None
    net_margin: float | None
    fcf_margin: float | None
    revenue_growth_yoy: float | None
    eps_growth_yoy: float | None
    roe: float | None
    roa: float | None
    roic: float | None
    debt_to_equity: float | None
    current_ratio: float | None
    net_debt_to_ebitda: float | None
    fcf_conversion: float | None
    fcf_yield: float | None  # if market_cap provided

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _safe_div(num, den):
    try:
        if den in (0, None) or pd.isna(den):
            return None
        return float(num) / float(den)
    except (TypeError, ValueError):
        return None


def compute_ratios(
    income: dict,     # TTM or latest year income statement
    balance: dict,    # latest balance sheet
    cashflow: dict,   # TTM or latest year cash flow
    prev_income: dict | None = None,  # prior year for growth
    market_cap: float | None = None,
) -> Ratios:
    """
    Expects income/balance/cashflow to look like the structured output from
    yfinance (dict of line items). Keys vary by provider — be defensive.
    """
    revenue = income.get("Total Revenue") or income.get("totalRevenue")
    gross_profit = income.get("Gross Profit") or income.get("grossProfit")
    operating_income = income.get("Operating Income") or income.get("operatingIncome")
    net_income = income.get("Net Income") or income.get("netIncome")
    ebitda = income.get("EBITDA")
    prev_revenue = (prev_income or {}).get("Total Revenue") or (prev_income or {}).get("totalRevenue")
    prev_eps = (prev_income or {}).get("Basic EPS") or (prev_income or {}).get("basicEPS")
    eps = income.get("Basic EPS") or income.get("basicEPS")

    total_equity = balance.get("Common Stock Equity") or balance.get("stockholdersEquity")
    total_assets = balance.get("Total Assets") or balance.get("totalAssets")
    total_debt = balance.get("Total Debt") or balance.get("totalDebt") or 0
    cash = balance.get("Cash And Cash Equivalents") or balance.get("cash") or 0
    current_assets = balance.get("Current Assets") or balance.get("totalCurrentAssets")
    current_liab = balance.get("Current Liabilities") or balance.get("totalCurrentLiabilities")

    ocf = cashflow.get("Operating Cash Flow") or cashflow.get("operatingCashflow")
    capex = cashflow.get("Capital Expenditure") or cashflow.get("capitalExpenditures") or 0
    fcf = None
    if ocf is not None:
        fcf = float(ocf) + float(capex)  # capex is usually negative; add to subtract

    net_debt = float(total_debt) - float(cash)
    invested_capital = float(total_debt) + float(total_equity or 0)

    return Ratios(
        gross_margin=_safe_div(gross_profit, revenue),
        operating_margin=_safe_div(operating_income, revenue),
        net_margin=_safe_div(net_income, revenue),
        fcf_margin=_safe_div(fcf, revenue),
        revenue_growth_yoy=_safe_div(
            (revenue - prev_revenue) if (revenue and prev_revenue) else None,
            prev_revenue,
        ),
        eps_growth_yoy=_safe_div(
            (eps - prev_eps) if (eps and prev_eps) else None,
            prev_eps,
        ),
        roe=_safe_div(net_income, total_equity),
        roa=_safe_div(net_income, total_assets),
        roic=_safe_div(operating_income, invested_capital) if invested_capital else None,
        debt_to_equity=_safe_div(total_debt, total_equity),
        current_ratio=_safe_div(current_assets, current_liab),
        net_debt_to_ebitda=_safe_div(net_debt, ebitda),
        fcf_conversion=_safe_div(fcf, net_income),
        fcf_yield=_safe_div(fcf, market_cap) if market_cap else None,
    )


def _cli() -> int:
    """
    CLI for ad-hoc ratio computation.
    Expects --input as JSON with {income, balance, cashflow, prev_income?, market_cap?}.
    """
    import argparse
    parser = argparse.ArgumentParser(description="Compute financial ratios")
    parser.add_argument("--input", required=True, help="Path to JSON bundle")
    args = parser.parse_args()
    with open(args.input) as f:
        data = json.load(f)
    r = compute_ratios(
        income=data["income"],
        balance=data["balance"],
        cashflow=data["cashflow"],
        prev_income=data.get("prev_income"),
        market_cap=data.get("market_cap"),
    )
    json.dump(r.to_dict(), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
