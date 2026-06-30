"""
Options analytics: IV rank, max pain, put/call ratios, implied move.

Inputs are options chain dicts shaped like:
    { "calls": [ {strike, volume, openInterest, lastPrice, ...}, ... ],
      "puts":  [ ... ] }
(The agent builds these from yfinance option_chain(...).calls/.puts.)
"""

from __future__ import annotations

import argparse
import json
import sys


def put_call_ratios(chain: dict) -> dict:
    """Compute put/call volume and open interest ratios from a chain."""
    call_vol = sum(c.get("volume", 0) or 0 for c in chain.get("calls", []))
    put_vol = sum(p.get("volume", 0) or 0 for p in chain.get("puts", []))
    call_oi = sum(c.get("openInterest", 0) or 0 for c in chain.get("calls", []))
    put_oi = sum(p.get("openInterest", 0) or 0 for p in chain.get("puts", []))
    return {
        "pc_volume_ratio": put_vol / call_vol if call_vol else None,
        "pc_oi_ratio": put_oi / call_oi if call_oi else None,
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_oi": call_oi,
        "put_oi": put_oi,
    }


def max_pain(chain: dict) -> dict:
    """
    Compute the max-pain strike: the strike where total option value
    (for option holders) is minimized at expiry.
    """
    strikes = sorted(
        {c["strike"] for c in chain.get("calls", [])}
        | {p["strike"] for p in chain.get("puts", [])}
    )
    if not strikes:
        return {"max_pain_strike": None, "pain_by_strike": {}}

    pain_by_strike = {}
    for K in strikes:
        total = 0.0
        for c in chain.get("calls", []):
            oi = c.get("openInterest", 0) or 0
            if K > c["strike"]:
                total += (K - c["strike"]) * oi
        for p in chain.get("puts", []):
            oi = p.get("openInterest", 0) or 0
            if K < p["strike"]:
                total += (p["strike"] - K) * oi
        pain_by_strike[K] = total

    max_pain_strike = min(pain_by_strike, key=pain_by_strike.get)
    return {"max_pain_strike": max_pain_strike, "pain_by_strike": pain_by_strike}


def magnet_strikes(chain: dict, top_n: int = 5) -> list[dict]:
    """
    Identify top-N strikes with highest total open interest (calls + puts).
    These often act as S/R magnets.
    """
    by_strike: dict[float, float] = {}
    for c in chain.get("calls", []):
        by_strike[c["strike"]] = by_strike.get(c["strike"], 0) + (c.get("openInterest", 0) or 0)
    for p in chain.get("puts", []):
        by_strike[p["strike"]] = by_strike.get(p["strike"], 0) + (p.get("openInterest", 0) or 0)
    sorted_strikes = sorted(by_strike.items(), key=lambda x: x[1], reverse=True)
    return [{"strike": s, "total_oi": oi} for s, oi in sorted_strikes[:top_n]]


def implied_move(chain: dict, underlying_price: float) -> dict | None:
    """
    Implied move = ATM straddle price / underlying price.
    Approximates expected +/-% move by expiry.
    """
    calls = chain.get("calls", [])
    puts = chain.get("puts", [])
    if not calls or not puts:
        return None

    atm_call = min(calls, key=lambda c: abs(c["strike"] - underlying_price))
    atm_put = min(puts, key=lambda p: abs(p["strike"] - underlying_price))
    straddle = (atm_call.get("lastPrice", 0) or 0) + (atm_put.get("lastPrice", 0) or 0)
    if not straddle or not underlying_price:
        return None
    return {
        "atm_strike_call": atm_call["strike"],
        "atm_strike_put": atm_put["strike"],
        "straddle_price": straddle,
        "implied_move_pct": straddle / underlying_price * 100,
    }


def unusual_activity(chain: dict, min_ratio: float = 3.0) -> list[dict]:
    """Strikes where volume > min_ratio * open interest."""
    out = []
    for side in ("calls", "puts"):
        for o in chain.get(side, []):
            vol = o.get("volume", 0) or 0
            oi = o.get("openInterest", 0) or 0
            if oi and vol and vol / oi >= min_ratio:
                out.append(
                    {
                        "type": side[:-1],  # "call" or "put"
                        "strike": o["strike"],
                        "volume": vol,
                        "oi": oi,
                        "vol_oi_ratio": vol / oi,
                    }
                )
    return sorted(out, key=lambda x: x["vol_oi_ratio"], reverse=True)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Options analytics")
    parser.add_argument("--chain", required=True, help="Path to chain JSON")
    parser.add_argument("--price", type=float, help="Underlying price for implied move")
    args = parser.parse_args()
    with open(args.chain) as f:
        chain = json.load(f)
    result = {
        "ratios": put_call_ratios(chain),
        "max_pain": max_pain(chain),
        "magnet_strikes": magnet_strikes(chain),
        "unusual": unusual_activity(chain),
    }
    if args.price:
        result["implied_move"] = implied_move(chain, args.price)
    json.dump(result, sys.stdout, indent=2, default=float)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
