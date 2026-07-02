"""
Multi-year fundamental trend analysis.

House-style companion to financial_ratios.py / dcf.py: same defensive key
handling, JSON-in / JSON-out CLI, dataclass result. financial_ratios.py gives
a single-period snapshot (+YoY); this gives the multi-year TRAJECTORY a
long-term holder cares about: revenue/EPS/FCF CAGR, margin and ROIC paths,
share count (dilution vs buybacks) and debt trends.

Input: fiscal-year statement dicts OLDEST -> NEWEST, each carrying a "period"
key (ISO date) alongside the line items (main.py's _series() adapter adds it).
Output: per-year series + derived scalars, every one computed here — the model
never does arithmetic.

Worked example (hand-verified; there is no automated test suite):
    revenue [100, 110, 121] over 3 fiscal years -> revenue_cagr = 0.10
    shares  [100, 95]                           -> share_count_change_pct = -5.0

Install: (stdlib only)
Usage:
    python scripts/fundamental_trends.py --input trends_bundle.json
    python scripts/fundamental_trends.py --ticker AAPL   # self-fetch via yfinance
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass


# ---------- defensive lookups (mirror financial_ratios.py / dcf.py) ----------

def _num(v):
    """Coerce to float; None for missing / non-numeric / NaN (yfinance emits NaN)."""
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _g(d: dict, *names):
    for n in names:
        if d and n in d:
            v = _num(d[n])
            if v is not None:
                return v
    return None


def _fcf(cf: dict):
    """Free cash flow for one year. Prefer a reported FCF row; else OCF - |Capex|
    (mirrors dcf._base_fcf)."""
    fcf = _g(cf, "Free Cash Flow", "freeCashflow")
    if fcf is not None:
        return fcf
    ocf = _g(cf, "Operating Cash Flow", "operatingCashflow",
             "Total Cash From Operating Activities")
    capex = _g(cf, "Capital Expenditure", "capitalExpenditures")
    if ocf is None or capex is None:
        return None
    return ocf - abs(capex)


# ---------- result ----------

@dataclass
class Trends:
    periods: list          # fiscal year-end dates, oldest -> newest
    revenue: list          # per-year series aligned to periods (None = missing)
    net_income: list
    eps: list
    fcf: list
    gross_margin: list
    operating_margin: list
    net_margin: list
    roic: list
    shares_outstanding: list
    total_debt: list
    net_debt: list
    revenue_cagr: float | None
    eps_cagr: float | None
    fcf_cagr: float | None
    operating_margin_change_pp: float | None  # last - first, percentage points
    share_count_change_pct: float | None      # + = dilution, - = buybacks
    net_debt_change: float | None             # last - first, absolute currency
    caveats: list

    def to_dict(self) -> dict:
        # Drop None SCALARS (like Ratios.to_dict) but keep the series intact —
        # a None inside a series is informative (that year is missing).
        out = {}
        for k, v in self.__dict__.items():
            if v is None:
                continue
            out[k] = v
        return out


# ---------- core ----------

def _endpoints(series: list):
    """(first_idx, first_val, last_idx, last_val) over non-None entries, or None."""
    pts = [(i, v) for i, v in enumerate(series) if v is not None]
    if len(pts) < 2:
        return None
    (i0, v0), (i1, v1) = pts[0], pts[-1]
    return i0, v0, i1, v1


def _cagr(series: list, label: str, caveats: list):
    pts = _endpoints(series)
    if pts is None:
        caveats.append(f"{label} CAGR unavailable: fewer than 2 periods with data")
        return None
    i0, v0, i1, v1 = pts
    if v0 <= 0 or v1 <= 0:
        caveats.append(f"{label} CAGR undefined: non-positive endpoint value")
        return None
    return round((v1 / v0) ** (1.0 / (i1 - i0)) - 1.0, 4)


def _delta(series: list, scale: float = 1.0, relative: bool = False):
    pts = _endpoints(series)
    if pts is None:
        return None
    _, v0, _, v1 = pts
    if relative:
        return round((v1 / v0 - 1.0) * scale, 2) if v0 else None
    return round((v1 - v0) * scale, 2)


def compute_trends(income: list, balance: list, cashflow: list) -> Trends:
    """
    Each argument is a list of fiscal-year statement dicts OLDEST -> NEWEST,
    each with a "period" ISO-date key. Frames may cover different year sets
    (common for non-US listings) — years are aligned by period; anything a
    frame lacks becomes None for that year, never an error.
    """
    caveats: list = []
    by_period = {}
    for frame in (income, balance, cashflow):
        for row in frame or []:
            p = row.get("period")
            if p:
                by_period.setdefault(p, {}).update(
                    {k: v for k, v in row.items() if k != "period"})
    periods = sorted(by_period)
    if not periods:
        caveats.append("no fiscal periods available from statements")
    elif len(periods) == 1:
        caveats.append("only 1 fiscal period available — trends cannot be computed")

    revenue, net_income, eps, fcf = [], [], [], []
    gross_margin, operating_margin, net_margin, roic = [], [], [], []
    shares, total_debt, net_debt = [], [], []
    for p in periods:
        d = by_period[p]
        rev = _g(d, "Total Revenue", "totalRevenue")
        gp = _g(d, "Gross Profit", "grossProfit")
        op = _g(d, "Operating Income", "operatingIncome")
        ni = _g(d, "Net Income", "netIncome")
        debt = _g(d, "Total Debt", "totalDebt")
        cash = _g(d, "Cash And Cash Equivalents", "cash", "totalCash")
        equity = _g(d, "Common Stock Equity", "stockholdersEquity")
        invested = (debt or 0) + (equity or 0)

        revenue.append(rev)
        net_income.append(ni)
        eps.append(_g(d, "Basic EPS", "basicEPS"))
        fcf.append(_fcf(d))
        gross_margin.append(round(gp / rev, 4) if (gp is not None and rev) else None)
        operating_margin.append(round(op / rev, 4) if (op is not None and rev) else None)
        net_margin.append(round(ni / rev, 4) if (ni is not None and rev) else None)
        roic.append(round(op / invested, 4) if (op is not None and invested) else None)
        shares.append(_g(d, "Ordinary Shares Number", "Share Issued",
                         "Basic Average Shares", "sharesOutstanding"))
        total_debt.append(debt)
        net_debt.append(round(debt - cash, 0) if (debt is not None and cash is not None)
                        else None)

    return Trends(
        periods=periods,
        revenue=revenue, net_income=net_income, eps=eps, fcf=fcf,
        gross_margin=gross_margin, operating_margin=operating_margin,
        net_margin=net_margin, roic=roic,
        shares_outstanding=shares, total_debt=total_debt, net_debt=net_debt,
        revenue_cagr=_cagr(revenue, "revenue", caveats),
        eps_cagr=_cagr(eps, "EPS", caveats),
        fcf_cagr=_cagr(fcf, "FCF", caveats),
        operating_margin_change_pp=_delta(operating_margin, scale=100.0),
        share_count_change_pct=_delta(shares, scale=100.0, relative=True),
        net_debt_change=_delta(net_debt),
        caveats=caveats,
    )


# ---------- CLI ----------

def _cli() -> int:
    p = argparse.ArgumentParser(description="Multi-year fundamental trends")
    p.add_argument("--input", help="JSON bundle: {income: [...], balance: [...], "
                                   "cashflow: [...]} — fiscal years oldest->newest, "
                                   "each dict with a 'period' ISO date")
    p.add_argument("--ticker", help="Self-fetch via yfinance instead of --input")
    args = p.parse_args()

    if args.ticker:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            print("yfinance not installed. pip install yfinance", file=sys.stderr)
            return 1

        def series(df):
            if df is None or df.empty:
                return []
            out = []
            for col in reversed(list(df.columns)):  # yfinance is newest-first
                d = {k: (None if v is None or (isinstance(v, float) and math.isnan(v))
                         else v) for k, v in df[col].to_dict().items()}
                d["period"] = str(col)[:10]
                out.append(d)
            return out

        t = yf.Ticker(args.ticker)
        income, balance, cashflow = (series(t.income_stmt), series(t.balance_sheet),
                                     series(t.cashflow))
    elif args.input:
        with open(args.input) as f:
            data = json.load(f)
        income, balance, cashflow = (data.get("income") or [],
                                     data.get("balance") or [],
                                     data.get("cashflow") or [])
    else:
        p.error("Provide --input or --ticker")

    result = compute_trends(income=income, balance=balance, cashflow=cashflow)
    json.dump(result.to_dict(), sys.stdout, indent=2, default=float)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
