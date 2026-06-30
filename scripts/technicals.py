"""
Technical indicators for equity research.

All functions take a pandas Series of close prices (indexed by date)
and return a pandas Series / DataFrame of the same index.

Install: pip install pandas numpy

Usage from the agent:
    from technicals import summarize
Or CLI:
    python scripts/technicals.py --ticker AAPL --period 1y
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd


# ---------- moving averages ----------

def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return close.rolling(window=window, min_periods=window).mean()


def ema(close: pd.Series, window: int) -> pd.Series:
    """Exponential moving average."""
    return close.ewm(span=window, adjust=False).mean()


# ---------- momentum ----------

def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """
    Relative Strength Index using Wilder's smoothing (standard).
    Returns values 0-100.
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    """
    MACD returns a DataFrame with columns:
        macd    = EMA(fast) - EMA(slow)
        signal  = EMA(macd, signal_window)
        hist    = macd - signal
    """
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame(
        {
            "macd": macd_line,
            "signal": signal_line,
            "hist": macd_line - signal_line,
        }
    )


# ---------- volatility / bands ----------

def bollinger(close: pd.Series, window: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """Bollinger bands: middle (SMA), upper, lower."""
    middle = sma(close, window)
    std = close.rolling(window=window, min_periods=window).std()
    return pd.DataFrame(
        {
            "middle": middle,
            "upper": middle + num_std * std,
            "lower": middle - num_std * std,
        }
    )


def atr(
    high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14
) -> pd.Series:
    """Average True Range (Wilder) — used for stop placement."""
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / window, adjust=False).mean()


# ---------- summary signal bundle ----------

@dataclass
class TechnicalSummary:
    last_price: float
    sma_20: float
    sma_50: float
    sma_200: float
    price_vs_200dma_pct: float
    rsi_14: float
    macd_hist: float
    macd_above_signal: bool
    golden_cross: bool          # sma_50 > sma_200
    death_cross: bool           # sma_50 < sma_200
    bollinger_position: str     # "upper", "middle", "lower" band tag

    def to_dict(self) -> dict:
        return self.__dict__


def summarize(
    close: pd.Series,
    high: pd.Series | None = None,
    low: pd.Series | None = None,
) -> TechnicalSummary:
    """
    One-call summary bundle for a report.
    Pass high/low if you want ATR; otherwise they're ignored.
    """
    s20 = sma(close, 20).iloc[-1]
    s50 = sma(close, 50).iloc[-1]
    s200 = sma(close, 200).iloc[-1]
    last = close.iloc[-1]
    macd_df = macd(close)
    bb = bollinger(close)

    # bollinger band position
    last_upper = bb["upper"].iloc[-1]
    last_lower = bb["lower"].iloc[-1]
    last_mid = bb["middle"].iloc[-1]
    if last >= last_upper:
        bb_pos = "at/above upper band"
    elif last <= last_lower:
        bb_pos = "at/below lower band"
    elif last >= last_mid:
        bb_pos = "upper half"
    else:
        bb_pos = "lower half"

    return TechnicalSummary(
        last_price=float(last),
        sma_20=float(s20),
        sma_50=float(s50),
        sma_200=float(s200),
        price_vs_200dma_pct=float((last / s200 - 1) * 100) if s200 else float("nan"),
        rsi_14=float(rsi(close).iloc[-1]),
        macd_hist=float(macd_df["hist"].iloc[-1]),
        macd_above_signal=bool(macd_df["macd"].iloc[-1] > macd_df["signal"].iloc[-1]),
        golden_cross=bool(s50 > s200),
        death_cross=bool(s50 < s200),
        bollinger_position=bb_pos,
    )


# ---------- CLI ----------

def _cli() -> int:
    parser = argparse.ArgumentParser(description="Technical indicators")
    parser.add_argument("--csv", help="Path to CSV with columns: date,close[,high,low]")
    parser.add_argument(
        "--ticker",
        help="Ticker — uses yfinance directly (requires yfinance installed)",
    )
    parser.add_argument("--period", default="1y", help="yfinance period")
    parser.add_argument("--interval", default="1d", help="yfinance interval")
    args = parser.parse_args()

    if args.csv:
        df = pd.read_csv(args.csv, parse_dates=["date"]).set_index("date")
    elif args.ticker:
        try:
            import yfinance as yf  # type: ignore
        except ImportError:
            print("yfinance not installed. pip install yfinance", file=sys.stderr)
            return 1
        df = yf.download(
            args.ticker, period=args.period, interval=args.interval, progress=False
        )
        df.columns = [c.lower() for c in df.columns]
    else:
        parser.error("Provide --csv or --ticker")

    close = df["close"]
    high = df.get("high")
    low = df.get("low")

    summary = summarize(close, high, low)
    print("Technical summary")
    print("-" * 40)
    for k, v in summary.to_dict().items():
        if isinstance(v, float):
            print(f"  {k:<25} {v:>10.2f}")
        else:
            print(f"  {k:<25} {v!s:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
