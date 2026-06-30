"""
Two-stage discounted-cash-flow (DCF) valuation.

House-style companion to financial_ratios.py: same defensive key handling,
JSON-in / JSON-out CLI, dataclass result. financial_ratios.py gives FCF *yield*;
this gives an intrinsic *fair value per share*.

Input: the same statement dicts the skill already pulls (yfinance), plus
shares/price/beta. Output: fair value + EVERY assumption echoed back with its
source, so the model and the verifier can audit it.

The skill typically pulls:
    cashflow = get_financials(ticker, "cashflow", "yearly")  # latest column -> dict
    balance  = get_financials(ticker, "balance",  "yearly")
    info     = get_ticker_info(ticker)                        # shares, price, beta
and passes them here.

Install: (stdlib only)
Usage:
    python scripts/dcf.py --input dcf_bundle.json
    python scripts/dcf.py --ticker AAPL          # self-fetch via yfinance (needs internet)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass


# ---------- defensive lookups (mirror financial_ratios.py) ----------

def _get(d: dict, *names):
    for n in names:
        if d and n in d and d[n] is not None:
            return d[n]
    return None


def _base_fcf(cashflow: dict):
    """Most-recent free cash flow. Prefer a reported FCF row; else OCF - |Capex|."""
    fcf = _get(cashflow, "Free Cash Flow", "freeCashflow")
    if fcf is not None:
        return float(fcf), "reported Free Cash Flow"
    ocf = _get(cashflow, "Operating Cash Flow", "operatingCashflow",
               "Total Cash From Operating Activities")
    capex = _get(cashflow, "Capital Expenditure", "capitalExpenditures")
    if ocf is None or capex is None:
        return None, "unavailable"
    # capex is usually stored negative; subtract its magnitude regardless of sign
    return float(ocf) - abs(float(capex)), "OCF - |Capex|"


@dataclass
class DCFResult:
    fair_value_per_share: float | None
    current_price: float | None
    upside_pct: float | None
    base_fcf: float | None
    enterprise_value: float | None
    equity_value: float | None
    assumptions: dict
    sources: dict
    caveat: str

    def to_dict(self) -> dict:
        return self.__dict__


def dcf(
    cashflow: dict,
    balance: dict,
    shares_outstanding: float | None,
    current_price: float | None = None,
    beta: float | None = None,
    growth_rate: float | None = None,
    terminal_growth: float = 0.025,
    discount_rate: float | None = None,
    years: int = 5,
    risk_free: float = 0.043,   # STATED assumption (≈10Y UST proxy), not a live feed
    erp: float = 0.05,          # STATED assumption (equity risk premium)
    fallback_growth: float = 0.08,
) -> DCFResult:
    base_fcf, fcf_src = _base_fcf(cashflow)
    if base_fcf is None:
        return DCFResult(None, current_price, None, None, None, None,
                         assumptions={}, sources={"fcf": "unavailable from statements"},
                         caveat="FCF inputs missing — cannot value.")

    # --- growth assumption ---
    if growth_rate is None:
        growth_rate = fallback_growth
        growth_src = f"default {fallback_growth:.0%} (no override supplied)"
    else:
        growth_src = "user-supplied"
    # keep two-stage sane: clamp 0–25%
    growth_rate = max(min(growth_rate, 0.25), 0.0)

    # --- discount rate: CAPM cost of equity (simple WACC proxy) ---
    if discount_rate is None:
        b = beta if beta is not None else 1.1
        discount_rate = risk_free + b * erp
        disc_src = (f"CAPM: rf {risk_free:.1%} + beta {b:.2f} x ERP {erp:.1%} "
                    f"(rf & ERP are static assumptions, not live)")
    else:
        disc_src = "user-supplied"
    # guard: discount must exceed terminal growth for a finite TV
    if discount_rate <= terminal_growth:
        discount_rate = terminal_growth + 0.03

    # --- project & discount ---
    pv_fcf, f = 0.0, base_fcf
    for yr in range(1, years + 1):
        f *= (1 + growth_rate)
        pv_fcf += f / (1 + discount_rate) ** yr
    terminal_value = f * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_tv = terminal_value / (1 + discount_rate) ** years
    enterprise_value = pv_fcf + pv_tv

    total_debt = float(_get(balance, "Total Debt", "totalDebt") or 0)
    cash = float(_get(balance, "Cash And Cash Equivalents", "cash", "totalCash") or 0)
    equity_value = enterprise_value - total_debt + cash

    fair = equity_value / shares_outstanding if shares_outstanding else None
    upside = ((fair / current_price - 1) * 100) if (fair and current_price) else None

    return DCFResult(
        fair_value_per_share=round(fair, 2) if fair else None,
        current_price=current_price,
        upside_pct=round(upside, 1) if upside is not None else None,
        base_fcf=round(base_fcf, 0),
        enterprise_value=round(enterprise_value, 0),
        equity_value=round(equity_value, 0),
        assumptions={
            "fcf_growth": round(growth_rate, 4),
            "terminal_growth": terminal_growth,
            "discount_rate": round(discount_rate, 4),
            "projection_years": years,
        },
        sources={
            "base_fcf": fcf_src,
            "growth": growth_src,
            "discount": disc_src,
            "debt_cash": "Yahoo Finance balance sheet",
            "rf_erp": "static assumptions in code (auditable, override per-name)",
        },
        caveat=("DCF is highly sensitive: a ±1% change in growth or discount rate can "
                "move fair value 15–25%. Treat as a scenario, not a price target."),
    )


# ---------- CLI ----------

def _cli() -> int:
    p = argparse.ArgumentParser(description="Two-stage DCF valuation")
    p.add_argument("--input", help="JSON bundle: {cashflow, balance, shares_outstanding, "
                                   "current_price?, beta?, assumptions?}")
    p.add_argument("--ticker", help="Self-fetch via yfinance instead of --input")
    p.add_argument("--growth", type=float)
    p.add_argument("--discount", type=float)
    p.add_argument("--terminal", type=float, default=0.025)
    p.add_argument("--years", type=int, default=5)
    args = p.parse_args()

    if args.ticker:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            print("yfinance not installed. pip install yfinance", file=sys.stderr)
            return 1
        t = yf.Ticker(args.ticker)
        cf_df = t.cashflow
        bs_df = t.balance_sheet
        info = t.info
        cashflow = {} if cf_df is None or cf_df.empty else cf_df.iloc[:, 0].to_dict()
        balance = {} if bs_df is None or bs_df.empty else bs_df.iloc[:, 0].to_dict()
        shares = info.get("sharesOutstanding")
        price = info.get("currentPrice")
        beta = info.get("beta")
        a = {}
    elif args.input:
        with open(args.input) as f:
            data = json.load(f)
        cashflow = data["cashflow"]
        balance = data["balance"]
        shares = data.get("shares_outstanding")
        price = data.get("current_price")
        beta = data.get("beta")
        a = data.get("assumptions", {})
    else:
        p.error("Provide --input or --ticker")

    result = dcf(
        cashflow=cashflow, balance=balance, shares_outstanding=shares,
        current_price=price, beta=beta,
        growth_rate=args.growth if args.growth is not None else a.get("growth_rate"),
        terminal_growth=args.terminal if args.terminal != 0.025 else a.get("terminal_growth", 0.025),
        discount_rate=args.discount if args.discount is not None else a.get("discount_rate"),
        years=args.years if args.years != 5 else a.get("years", 5),
    )
    json.dump(result.to_dict(), sys.stdout, indent=2, default=float)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
