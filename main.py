"""
Stock-research agent core.

GLM-5.2 (via Ollama Cloud, OpenAI-compatible) ORCHESTRATES and reasons; the
deterministic math lives in scripts/ (your equity-research scripts + dcf.py).
News/sentiment via Brave Search. The model never does arithmetic itself.

One comprehensive deep dive that always integrates options OPEN INTEREST and
sector context, then self-verifies and iterates until the review passes.

Run a one-off from the terminal:
    python main.py "Deep dive on NVDA"
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import logging
import datetime
import threading

import requests
from dotenv import load_dotenv

load_dotenv()
sys.path.append(os.path.join(os.path.dirname(__file__), "scripts"))  # your scripts/ + dcf.py

import yfinance as yf
logging.getLogger("yfinance").setLevel(logging.CRITICAL)   # silence noisy "HTTP Error 404" lines
from llm import OllamaClient   # requests-based OpenAI-compatible client (no Rust deps; Termux-friendly)

import technicals          # noqa: E402  (your script, reused as-is)
import financial_ratios    # noqa: E402  (your script, reused as-is)
import options_analytics   # noqa: E402  (your script, reused as-is)
import dcf as dcf_mod      # noqa: E402  (new, unit-tested)

client = OllamaClient(
    base_url=os.environ.get("OLLAMA_BASE_URL", "https://ollama.com/v1"),
    api_key=os.environ["OLLAMA_API_KEY"],
)
BRAVE_KEY = os.environ["BRAVE_API_KEY"]
MODEL = os.environ.get("MODEL", "glm-5.2:cloud")


# ---------- shared per-ticker fetch: cache + retry (thread-local, reset per research run) ----------
#
# Every tool used to build its own yf.Ticker and re-scrape .info, so a single research run hit
# Yahoo 5+ times for the same symbol — slow and a rate-limit magnet. We cache one Ticker per
# symbol for the duration of a run (yfinance memoises .info / statements on the instance) and
# wrap each network access in retry/backoff. The cache is thread-local because the bot runs
# concurrent research() calls via asyncio.to_thread; a shared global would let one user's run
# clobber another's. research() clears the current thread's cache on entry, so each run is fresh.

_tls = threading.local()


def _cache():
    c = getattr(_tls, "tickers", None)
    if c is None:
        c = _tls.tickers = {}
    return c


def _ticker(ticker):
    c = _cache()
    t = c.get(ticker)
    if t is None:
        t = c[ticker] = yf.Ticker(ticker)
    return t


def _retry(fn, tries=3, base=0.8):
    """Call fn() with exponential backoff; yfinance is an unofficial scraper that flakes."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:                             # transient scrape/network errors
            last = e
            if i < tries - 1:
                time.sleep(base * (2 ** i))
    raise last


def _info(t):
    return _retry(lambda: t.info)


# ---------- yfinance -> dict adapter (scripts are defensive about key names) ----------

def _latest(df):
    return {} if df is None or df.empty else df.iloc[:, 0].to_dict()


def _prev(df):
    return {} if df is None or df.shape[1] < 2 else df.iloc[:, 1].to_dict()


# ---------- tools: thin wrappers around your scripts (DATA=yfinance, MATH=scripts) ----------

def tool_lookup(query: str) -> str:                        # name/ticker -> real Yahoo symbols
    """Resolve a company/fund/index NAME (or guessed ticker) to its actual Yahoo symbol(s) via
    Search. Use this before pulling data so you never guess exchange suffixes (e.g. TATAMOTORS.BO
    is delisted — the real symbols are TMCV.NS / TMPV.NS)."""
    try:
        quotes = _retry(lambda: yf.Search(query).quotes) or []
    except Exception as e:
        return json.dumps({"source": "yfinance Search", "query": query, "error": str(e)})
    matches = [{"symbol": q.get("symbol"), "quote_type": q.get("quoteType"),
                "name": q.get("shortname") or q.get("longname"), "exchange": q.get("exchange")}
               for q in quotes if q.get("symbol")][:8]
    return json.dumps({"source": "yfinance Search", "query": query, "matches": matches})


def tool_snapshot(ticker: str) -> str:
    i = _info(_ticker(ticker))
    keys = ["longName", "sector", "industry", "marketCap", "currentPrice", "beta",
            "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "trailingPE", "forwardPE"]
    data = {k: i.get(k) for k in keys}
    # invalid/delisted symbol -> yfinance returns an empty .info; flag it so the model uses
    # tool_lookup instead of treating empty fields as real, and the coverage footer catches it.
    if not any(data.get(k) for k in ("longName", "currentPrice", "marketCap")):
        return json.dumps({"source": "yfinance .info", "ticker": ticker,
                           "error": f"no data for {ticker} (invalid/delisted symbol? use tool_lookup)"})
    return json.dumps({"source": "yfinance .info", "data": data})


def tool_ratios(ticker: str) -> str:                       # -> financial_ratios.py
    t = _ticker(ticker)
    income = _retry(lambda: t.income_stmt)                 # fetch once, used for latest + prev
    r = financial_ratios.compute_ratios(
        income=_latest(income), balance=_latest(_retry(lambda: t.balance_sheet)),
        cashflow=_latest(_retry(lambda: t.cashflow)), prev_income=_prev(income),
        market_cap=_info(t).get("marketCap"))
    return json.dumps({"source": "yfinance + scripts/financial_ratios.py", "data": r.to_dict()})


def tool_technicals(ticker: str) -> str:                   # -> technicals.py
    h = _retry(lambda: _ticker(ticker).history(period="1y"))   # ~200+ rows for SMA200
    s = technicals.summarize(h["Close"], h.get("High"), h.get("Low"))
    return json.dumps({"source": "yfinance + scripts/technicals.py", "data": s.to_dict()})


def _estimate_growth(info):
    """Forward-growth seed for the DCF from a real signal, faded/capped. dcf.py's flat 8% default
    is wildly off for high-growth names (NVDA grows ~65%), so when the model gives no override we
    seed from trailing revenue (or earnings) growth, capped at 25% — a 5y projection shouldn't
    extrapolate hyper-growth. Returns (rate, source_label) or (None, None) if no signal."""
    rg = info.get("revenueGrowth")
    eg = info.get("earningsGrowth")
    cand, which = (rg, "revenue") if rg is not None else (eg, "earnings")
    if cand is None:
        return None, None
    rate = max(min(float(cand), 0.25), 0.0)
    return rate, (f"data-derived: trailing {which} growth {float(cand):.0%}, "
                  f"faded/capped to {rate:.0%} for the {{years}}y projection")


def tool_dcf(ticker: str, growth_rate: float = None,
             discount_rate: float = None, years: int = 5) -> str:   # -> dcf.py
    t = _ticker(ticker)
    i = _info(t)
    growth_src = None
    if growth_rate is None:                                # no model override -> seed from real data
        growth_rate, growth_src = _estimate_growth(i)
    common = dict(cashflow=_latest(_retry(lambda: t.cashflow)),
                  balance=_latest(_retry(lambda: t.balance_sheet)),
                  shares_outstanding=i.get("sharesOutstanding"),
                  current_price=i.get("currentPrice"), beta=i.get("beta"), years=years)
    base = dcf_mod.dcf(growth_rate=growth_rate, discount_rate=discount_rate, **common)
    data = base.to_dict()
    if growth_src and data.get("sources"):                 # relabel: it's data-derived, not user-supplied
        data["sources"]["growth"] = growth_src.format(years=years)

    # Sensitivity grid: re-run the (pure, instant) DCF across growth x discount around the
    # RESOLVED base assumptions. A single point estimate is fragile — dcf.py itself warns a
    # ±1% move swings fair value 15-25% — so we surface the bear/bull span instead.
    a = base.assumptions
    if a:                                                  # base valued; FCF inputs were present
        gb, db, tg = a["fcf_growth"], a["discount_rate"], a["terminal_growth"]
        growths = sorted({round(max(gb - 0.02, 0.0), 4), round(gb, 4), round(gb + 0.02, 4)})
        discounts = sorted({round(max(db - 0.01, tg + 0.01), 4), round(db, 4), round(db + 0.01, 4)})
        grid = []
        for g in growths:
            for d in discounts:
                r = dcf_mod.dcf(growth_rate=g, discount_rate=d, **common)
                ra = r.assumptions                         # effective values dcf actually used
                grid.append({"growth": ra.get("fcf_growth", g), "discount": ra.get("discount_rate", d),
                             "fair_value": r.fair_value_per_share, "upside_pct": r.upside_pct})
        priced = [x for x in grid if x["fair_value"] is not None]
        data["sensitivity"] = {
            "note": "fair value/share across growth x discount; bear = low growth + high discount, "
                    "bull = high growth + low discount",
            "grid": grid,
            "bear": min(priced, key=lambda x: x["fair_value"], default=None),
            "bull": max(priced, key=lambda x: x["fair_value"], default=None),
        }
    return json.dumps({"source": "yfinance + scripts/dcf.py (unit-tested)", "data": data})


def _pick_expiries(exps):
    """Nearest expiry + the first one ~>=25 days out (a monthly/quarterly). Weeklies show
    near-term pin risk; the monthly is where the structural OI walls sit. Falls back to just
    the nearest if nothing further out is listed."""
    today = datetime.date.today()
    picks = [exps[0]]
    for e in exps[1:]:
        try:
            if (datetime.date.fromisoformat(e) - today).days >= 25:
                picks.append(e)
                break
        except ValueError:
            continue
    return picks


def _options_block(t, exp, px):                            # one expiry's OI analytics
    oc = _retry(lambda: t.option_chain(exp))
    chain = {"calls": oc.calls.to_dict("records"), "puts": oc.puts.to_dict("records")}
    pcr = options_analytics.put_call_ratios(chain)         # call_oi/put_oi/pc_oi_ratio/volumes
    block = {
        "expiry": exp,
        "open_interest": {"call_oi": pcr["call_oi"], "put_oi": pcr["put_oi"],
                          "pc_oi_ratio": pcr["pc_oi_ratio"]},
        "pc_volume_ratio": pcr["pc_volume_ratio"],
        "implied_move": options_analytics.implied_move(chain, px),   # from prices, not OI
    }
    # When total OI is ~0 (stale/off-hours feed), max-pain/magnets/unusual are meaningless
    # artifacts — max_pain() just returns the first strike. Omit them rather than emit numbers
    # that look real; the "unavailable" note then trips the coverage footer.
    if (pcr["call_oi"] or 0) + (pcr["put_oi"] or 0) > 0:
        block["max_pain_strike"] = options_analytics.max_pain(chain)["max_pain_strike"]  # OI-derived
        block["oi_magnet_strikes"] = options_analytics.magnet_strikes(chain, top_n=5)    # top OI walls
        block["unusual_activity"] = options_analytics.unusual_activity(chain)[:5]         # vol/OI spikes
    else:
        block["max_pain_strike"] = None
        block["oi_magnet_strikes"] = []
        block["unusual_activity"] = []
        block["note"] = ("open interest unavailable (zero OI — likely stale/off-hours feed); "
                         "OI-derived levels omitted")
    return block


def tool_options(ticker: str) -> str:                      # -> options_analytics.py
    t = _ticker(ticker)
    exps = _retry(lambda: t.options)
    if not exps:
        return json.dumps({"source": "yfinance", "data": {"note": "no options listed"}})
    px = _info(t).get("currentPrice")
    term = []
    for e in _pick_expiries(exps):                         # near expiry + a ~monthly
        try:
            term.append(_options_block(t, e, px))
        except Exception as ex:
            term.append({"expiry": e, "note": f"unavailable: {ex}"})
    near = term[0]
    data = {
        "nearest_expiry": near["expiry"],
        "open_interest": near.get("open_interest"),        # <- OI, front and center (nearest expiry)
        "expiries_analysed": [b["expiry"] for b in term],
        "term_structure": term,                            # full OI analytics per expiry
    }
    return json.dumps({"source": "yfinance + scripts/options_analytics.py", "data": data})


def _sector_payload(skey, out):
    """Fill `out` with yfinance Sector overview / top peers / top ETFs for sector key `skey`."""
    try:
        s = yf.Sector(skey)
        ov = s.overview or {}
        out["sector_overview"] = {k: ov.get(k) for k in
                                  ("market_cap", "market_weight", "companies_count",
                                   "employee_count") if k in ov}
        tc = s.top_companies
        out["top_companies"] = (tc.head(8).reset_index().to_dict("records")
                                if tc is not None else [])     # sector peers + ratings
        out["top_etfs"] = dict(list((s.top_etfs or {}).items())[:5])
    except Exception as e:
        out["note"] = f"sector detail unavailable: {e}"
    return out


def tool_sector(ticker: str) -> str:                       # -> yfinance Sector/Industry (from a ticker)
    i = _info(_ticker(ticker))
    skey = i.get("sectorKey") or (i.get("sector", "").lower().replace(" ", "-") or None)
    out = _sector_payload(skey, {"sector": i.get("sector"), "industry": i.get("industry")})
    return json.dumps({"source": "yfinance Sector/Industry", "data": out})


def tool_sector_overview(sector_key: str) -> str:          # sector brief without a ticker
    out = _sector_payload(sector_key, {"sector_key": sector_key})
    return json.dumps({"source": "yfinance Sector", "data": out}, default=str)


def tool_etf(ticker: str) -> str:                          # ETF/fund: holdings, cost, exposure
    t = _ticker(ticker)
    i = _info(t)
    out = {"name": i.get("longName") or i.get("shortName"), "category": i.get("category"),
           "aum_total_assets": i.get("totalAssets"),
           "expense_ratio": i.get("netExpenseRatio") or i.get("annualReportExpenseRatio"),
           "yield": i.get("yield"), "nav_price": i.get("navPrice"),
           "price": i.get("currentPrice") or i.get("regularMarketPrice"),
           "fifty_two_week": {"high": i.get("fiftyTwoWeekHigh"), "low": i.get("fiftyTwoWeekLow")}}
    nav, px = out["nav_price"], out["price"]
    if nav and px:
        out["premium_discount_pct"] = round((px / nav - 1) * 100, 2)   # vs NAV
    try:
        fd = _retry(lambda: t.funds_data)
        th = fd.top_holdings
        out["top_holdings"] = (th.reset_index().to_dict("records")[:10] if th is not None else [])
        out["sector_weightings"] = fd.sector_weightings or {}
    except Exception as e:
        out["holdings_note"] = f"holdings unavailable: {e}"
    return json.dumps({"source": "yfinance .info + .funds_data", "data": out}, default=str)


def tool_analyst(ticker: str) -> str:                      # earnings catalyst + street estimates
    # Each block is fetched independently and degrades to a note on failure — yfinance shapes
    # vary by version (.calendar can be a dict or a DataFrame), so we stay defensive. default=str
    # on the final dump serialises pandas Timestamps / dates that show up in these fields.
    t = _ticker(ticker)
    out = {}
    try:
        cal = _retry(lambda: t.calendar) or {}
        out["calendar"] = cal.to_dict() if hasattr(cal, "to_dict") else cal   # earnings/ex-div dates
    except Exception as e:
        out["calendar_note"] = f"unavailable: {e}"
    try:
        out["price_targets"] = _retry(lambda: t.analyst_price_targets) or {}  # current/low/high/mean/median
    except Exception as e:
        out["price_targets_note"] = f"unavailable: {e}"
    try:
        rec = _retry(lambda: t.recommendations)
        if rec is None:
            out["recommendation_trend"] = []
        elif hasattr(rec, "empty"):                        # DataFrame: strongBuy/buy/hold/sell/strongSell
            out["recommendation_trend"] = [] if rec.empty else rec.tail(4).to_dict("records")
        else:
            out["recommendation_trend"] = rec
    except Exception as e:
        out["recommendation_trend_note"] = f"unavailable: {e}"
    return json.dumps({"source": "yfinance .calendar/.analyst_price_targets/.recommendations",
                       "data": out}, default=str)


def _too_old(page_age, cutoff):
    """True if a Brave result's page_age parses to a date older than cutoff. Undated -> keep."""
    if not page_age:
        return False
    try:
        dt = datetime.datetime.fromisoformat(str(page_age).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt < cutoff
    except ValueError:
        return False


def tool_web_search(query: str) -> str:                    # news/sentiment via Brave; returns URLs
    # Brave's `freshness` filter is loose (a 3-month-old item slipped through `pm`), so we ALSO
    # hard-drop results whose page_age parses older than BRAVE_MAX_AGE_DAYS, request extra to
    # backfill after filtering, and surface each result's `age` so staleness stays visible.
    freshness = os.environ.get("BRAVE_FRESHNESS", "pw")    # tightened default: past week
    max_age = int(os.environ.get("BRAVE_MAX_AGE_DAYS", "30"))
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age)
    resp = requests.get("https://api.search.brave.com/res/v1/web/search",
                        headers={"X-Subscription-Token": BRAVE_KEY, "Accept": "application/json"},
                        params={"q": query, "count": 12, "freshness": freshness,
                                "result_filter": "web"}, timeout=20)
    results = (resp.json().get("web") or {}).get("results", [])
    out = []
    for x in results:
        if _too_old(x.get("page_age"), cutoff):
            continue
        out.append({"title": x.get("title"), "url": x.get("url"),
                    "age": x.get("age") or x.get("page_age"),     # human/ISO recency, visible to model
                    "content": (x.get("description") or "")[:500]})
        if len(out) >= 5:
            break
    return json.dumps({"source": f"Brave Search (freshness={freshness}, <={max_age}d)", "results": out})


TOOLS_IMPL = {"tool_snapshot": tool_snapshot, "tool_ratios": tool_ratios,
              "tool_technicals": tool_technicals, "tool_dcf": tool_dcf,
              "tool_options": tool_options, "tool_sector": tool_sector,
              "tool_sector_overview": tool_sector_overview, "tool_etf": tool_etf,
              "tool_lookup": tool_lookup,
              "tool_analyst": tool_analyst, "tool_web_search": tool_web_search}


def _s(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


TOOLS_SPEC = [
    _s("tool_snapshot", "Business/sector/price snapshot (Yahoo Finance)",
       {"ticker": {"type": "string"}}, ["ticker"]),
    _s("tool_ratios", "Margins/ROE/ROIC/FCF-yield via financial_ratios.py",
       {"ticker": {"type": "string"}}, ["ticker"]),
    _s("tool_technicals", "SMA/RSI/MACD/crosses via technicals.py",
       {"ticker": {"type": "string"}}, ["ticker"]),
    _s("tool_dcf", "Two-stage DCF fair value via dcf.py; returns explicit assumptions. "
       "Optional growth_rate/discount_rate/years overrides.",
       {"ticker": {"type": "string"}, "growth_rate": {"type": "number"},
        "discount_rate": {"type": "number"}, "years": {"type": "integer"}}, ["ticker"]),
    _s("tool_options", "Options positioning incl. open interest (call/put OI, P/C OI ratio), "
       "max-pain, OI magnet strikes, unusual activity, implied move — via options_analytics.py",
       {"ticker": {"type": "string"}}, ["ticker"]),
    _s("tool_sector", "Sector/industry context for a STOCK: trend, top peer companies, top ETFs "
       "(yfinance Sector, keyed off the ticker)", {"ticker": {"type": "string"}}, ["ticker"]),
    _s("tool_sector_overview", "Sector brief WITHOUT a ticker: trend, top peer companies, top ETFs "
       "for a sector key (technology, healthcare, energy, ...)",
       {"sector_key": {"type": "string"}}, ["sector_key"]),
    _s("tool_etf", "ETF/fund profile: expense ratio, AUM, yield, top holdings, sector weightings, "
       "premium/discount to NAV (yfinance .info + .funds_data)", {"ticker": {"type": "string"}}, ["ticker"]),
    _s("tool_lookup", "Resolve a company/fund/index NAME (or a guessed ticker) to its REAL Yahoo "
       "symbol(s) via Search. Use before pulling data — don't guess exchange suffixes like .NS/.BO.",
       {"query": {"type": "string"}}, ["query"]),
    _s("tool_analyst", "Earnings catalyst + street view: next earnings/ex-div date, analyst price "
       "targets (current/low/high/mean), and recommendation trend (yfinance)",
       {"ticker": {"type": "string"}}, ["ticker"]),
    _s("tool_web_search", "Recent news/sentiment; returns source URLs to cite",
       {"query": {"type": "string"}}, ["query"]),
]

_RATINGS_TAIL = """End with SHORT/MID/LONG ratings; each needs Rating, Conviction, Target, Entry,
Stop, 'WHAT KILLS THIS TRADE' (with % loss), position size. Append:
'Educational only, not financial advice.'"""

_HARD_RULES = """HARD RULES:
- Tools compute every number. NEVER do arithmetic yourself or recall figures from memory.
- Attribute each datum (Yahoo Finance, a named script, or a web_search URL). If a tool
  errors/returns empty, say 'unavailable' for that piece and continue."""

SYSTEM_EQUITY = """You are an equity research analyst. For the ticker, produce ONE comprehensive,
integrated deep dive — synthesize the phases together, do not output disconnected sections.
Always run ALL of these (none optional):
 - Business + sector context -> tool_snapshot, tool_sector (sector trend, peer names, top ETFs)
 - Fundamentals -> tool_ratios
 - Valuation -> tool_dcf (state assumptions + implied upside/downside; present the bear/base/bull
   span from its sensitivity grid, not just one point estimate)
 - Technicals/trend -> tool_technicals
 - Options positioning -> tool_options. If options ARE listed, EXPLICITLY factor in OPEN INTEREST:
   call vs put OI, P/C OI ratio, max-pain, OI magnet strikes, and unusual activity — and say what
   that implies for the likely move and your price levels. If the tool reports no options (common
   for non-US listings like .KS/.NS/.HK — Yahoo has no chain), note that in ONE line and move on;
   derive levels from technicals instead. Do not dwell on it or treat it as a weakness.
 - Earnings catalyst + street view -> tool_analyst. Note the NEXT EARNINGS DATE (a near-term
   catalyst/risk that should shape the SHORT-term rating and stop), and compare the analyst
   price-target range and buy/hold/sell trend against your own DCF target.
 - Catalysts/news/sentiment -> tool_web_search (cite source URLs, last ~30 days)
The verdict must REFLECT the synthesis: tie targets to DCF and — where options exist — to max-pain/
OI magnet levels; tie stops to technicals (ATR/SMA); and state how the sector trend and (if present)
options skew support or threaten the thesis.
""" + _HARD_RULES + "\n" + _RATINGS_TAIL

SYSTEM_ETF = """You are a markets analyst. INSTRUMENT: {name} [{symbol}] — an ETF/FUND, not a single
company. Do NOT run a DCF, single-company ratios, or earnings/analyst tools (they don't apply).
Run these:
 - Fund profile -> tool_etf: expense ratio, AUM, yield, TOP HOLDINGS, SECTOR WEIGHTINGS, and
   premium/discount to NAV. Explain what it holds, how concentrated it is, and its cost.
 - Technicals/trend -> tool_technicals (trend, RSI, support/resistance levels).
 - Options positioning -> tool_options IF the fund has listed options (OI, implied move); skip if none.
 - News/flows -> tool_web_search (fund flows, holdings news, last ~30 days; cite URLs).
Tie the view to the holdings/sector exposure and concentration, and to technical levels.
""" + _HARD_RULES + "\n" + _RATINGS_TAIL

SYSTEM_INDEX = """You are a markets analyst. INSTRUMENT: {name} [{symbol}] — a market INDEX. Do NOT
run a DCF, company ratios, options, or earnings/analyst tools. Live constituents are unavailable —
describe exposure by sector/theme instead. Run these:
 - Level/range -> tool_snapshot (current level, 52-week range).
 - Technicals/trend -> tool_technicals (trend, RSI, SMA, support/resistance levels).
 - Macro/news drivers -> tool_web_search (rate/macro/policy catalysts, last ~30 days; cite URLs).
Frame targets/stops as index LEVELS (note they're tradable via a tracking ETF/futures).
""" + _HARD_RULES + "\n" + _RATINGS_TAIL

SYSTEM_SECTOR = """You are a markets analyst. Produce a SECTOR OUTLOOK for the '{sector_key}' sector —
NOT a trade ticket. Do NOT output SHORT/MID/LONG ratings or entry/stop levels. Run these:
 - Sector data -> tool_sector_overview('{sector_key}'): trend, top peer companies, top ETFs.
 - Trend context -> tool_technicals on the leading sector ETF (pick one from top_etfs) for relative strength.
 - Catalysts/news -> tool_web_search (sector catalysts/risks, last ~30 days; cite URLs).
Cover: sector trend & relative strength, leaders vs laggards, the main ETFs for exposure, key
catalysts and risks, and a balanced bull/bear view grounded in the data and cited news.
""" + _HARD_RULES + """
Append: 'Educational only, not financial advice.'"""

SYSTEM_THEME = """You are a markets analyst. The user asked about a THEME / BASKET: '{name}'. There is
NO single ticker for this and it is not one of the 11 standard sectors — your job is to find the best
ways to get exposure and brief them. Do NOT invent tickers or pick one random stock. Steps:
 - Discover instruments -> tool_web_search. Run a few searches (the theme itself, '{name} ETF',
   '{name} index', 'top {name} stocks') to identify the concrete vehicles — ETFs, indices, and 2-5
   leading stocks. Cite source URLs.
 - Get the REAL ticker -> tool_lookup(name) for each instrument/company. DO NOT guess exchange
   suffixes (.NS/.BO/etc.) — tickers change (e.g. TATAMOTORS is delisted; the real ones are
   TMCV.NS / TMPV.NS). Use the symbol tool_lookup returns.
 - Add live data -> with that verified symbol, call tool_snapshot, tool_etf, or tool_technicals
   (price, expense ratio, trend). If a tool returns an "error"/no-data, SKIP that instrument;
   never state a ticker that did not come from a tool result.
 - PRIORITISE THE LOCAL MARKET for a country/regional theme. Research the actual local-exchange
   instruments, not just US-listed proxies: for India use NSE '.NS' / BSE '.BO' tickers (e.g.
   RELIANCE.NS, LT.NS) and the local benchmark/sector index; analogous suffixes for other markets.
   The tools accept these symbols, so pull live data on the leading LOCAL companies. Treat US-listed
   ADRs/ETFs (e.g. INDA) as a SECONDARY access route, clearly labelled as such — not the headline.
Produce a THEMATIC BRIEF (outlook, not a trade ticket): what the theme is, the leading LOCAL stocks
and indices (with symbols), the main ETFs/funds to play it, key drivers/catalysts, and the main
risks. Give a balanced view. Do NOT output SHORT/MID/LONG ratings, entry/stop levels, or fabricated
price targets.
""" + _HARD_RULES + """
Append: 'Educational only, not financial advice.'"""

_VERIFIER_HEAD = """You are a skeptical reviewer. Check the DRAFT against the TRANSCRIPT of
tool results. Reply JSON only: {"passed": boolean, "issues": [string,...]}.
Fail (passed=false) and list each violation if ANY of these is true:
1. A number in the draft is NOT present in the transcript (invented/unsourced).
2. A news/sentiment claim lacks a source URL from tool_web_search."""

VERIFIER_EQUITY = _VERIFIER_HEAD + """
3. The DCF assumptions (growth, discount, terminal) are missing or unreasonable.
4. Any rating lacks a target, a stop, or a quantified 'WHAT KILLS THIS TRADE'.
5. A rating contradicts the data (e.g. 'Buy' despite large DCF downside) with no rationale.
6. Options OPEN INTEREST is not analysed (call/put OI or P/C OI ratio, plus max-pain or OI magnets), when available.
7. Sector/industry context is missing (sector trend or peer comparison).
8. The verdict doesn't tie targets/stops back to the DCF, OI levels, or technicals (sections feel disconnected).
9. A next earnings date or analyst price targets are present in the transcript but the draft ignores them."""

VERIFIER_ETF = _VERIFIER_HEAD + """
3. The fund's top holdings or sector weightings are not discussed (tool_etf data present but ignored).
4. The expense ratio / cost is not mentioned when present in the transcript.
5. Any rating lacks a target, a stop, or a quantified 'WHAT KILLS THIS TRADE'.
6. Targets/stops aren't tied to technical levels or the holdings/sector exposure.
DO NOT require a DCF or single-company ratios — this is a fund."""

VERIFIER_INDEX = _VERIFIER_HEAD + """
3. Technical levels (trend, support/resistance) are missing.
4. Any rating lacks a target level, a stop, or a quantified 'WHAT KILLS THIS TRADE'.
5. Macro/news drivers are not discussed.
DO NOT require a DCF, company ratios, or options — this is an index."""

VERIFIER_SECTOR = _VERIFIER_HEAD + """
3. Sector trend or top peer companies are missing (tool_sector_overview data present but ignored).
4. The main ETFs for exposure are not mentioned.
5. The outlook is one-sided (no balanced bull/bear view).
DO NOT require SHORT/MID/LONG ratings, entry/stop levels, a DCF, or options — this is an outlook."""

VERIFIER_THEME = _VERIFIER_HEAD + """
3. No concrete instruments (ETFs / indices / stocks WITH ticker symbols) are identified for the theme.
4. A ticker is stated in the draft but does NOT appear anywhere in the transcript (invented symbol).
5. The brief names no drivers/catalysts or no risks (one-sided).
DO NOT require a DCF, single-company ratios, options, or SHORT/MID/LONG ratings — this is a thematic outlook."""

# ---------- kind registry: the one place each instrument kind is configured ----------
# To support a new query type, add a SYSTEM_*/VERIFIER_* pair and one row here — research()
# is generic and reads everything it needs from this table. `structural` = run the local
# structural check (only meaningful for the rating-style equity report). `task` is the opening
# user turn (str.format fields: name, symbol, sector_key, q).
KINDS = {
    "equity": {"system": SYSTEM_EQUITY, "verifier": VERIFIER_EQUITY, "structural": True,
               "task": "Research {name} [{symbol}] — type equity. Original request: {q!r}."},
    "etf": {"system": SYSTEM_ETF, "verifier": VERIFIER_ETF, "structural": False,
            "task": "Research {name} [{symbol}] — type etf. Original request: {q!r}."},
    "index": {"system": SYSTEM_INDEX, "verifier": VERIFIER_INDEX, "structural": False,
              "task": "Research {name} [{symbol}] — type index. Original request: {q!r}."},
    "sector": {"system": SYSTEM_SECTOR, "verifier": VERIFIER_SECTOR, "structural": False,
               "task": "Produce a sector outlook for: {sector_key}. Original request: {q!r}."},
    "theme": {"system": SYSTEM_THEME, "verifier": VERIFIER_THEME, "structural": False,
              "task": "Produce a thematic exposure brief for: {name!r}. Original request: {q!r}."},
}


def _kind_spec(kind):
    return KINDS.get(kind, KINDS["equity"])


def _prompts_for(intent):
    """Return (system_prompt, verifier_prompt) for a resolved intent, formatted with its fields."""
    spec = _kind_spec(intent.get("kind", "equity"))
    sys_p = spec["system"].format(name=intent.get("name") or "", symbol=intent.get("symbol") or "",
                                  sector_key=intent.get("sector_key") or "")
    return sys_p, spec["verifier"]


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _stamp(out):
    """Add an `as_of` fetch timestamp to a tool result so the report can date its data."""
    try:
        d = json.loads(out)
        if isinstance(d, dict):
            d.setdefault("as_of", _now_iso())
            return json.dumps(d, default=str)
    except Exception:
        pass
    return out


def _agent_loop(msgs, max_rounds=16):
    for _ in range(max_rounds):
        m = client.chat.completions.create(model=MODEL, messages=msgs,
                                           tools=TOOLS_SPEC).choices[0].message
        if not m.tool_calls:
            return m.content, msgs
        msgs.append(m)
        for tc in m.tool_calls:
            try:
                out = _stamp(TOOLS_IMPL[tc.function.name](**json.loads(tc.function.arguments)))
            except Exception as e:
                out = json.dumps({"error": str(e), "as_of": _now_iso()})
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    return "Stopped after max tool rounds.", msgs


# ---------- history compaction (keep msgs bounded across verify/fix passes) ----------
#
# msgs mixes SDK message objects (assistant turns, from the model) with plain dicts
# (our tool/user/system turns), and each tool message MUST stay paired with the
# assistant tool_call that produced it or the API rejects the request. So we never
# delete messages surgically. Instead we accumulate the latest result per distinct
# tool call into `facts`, and each fix pass starts from a freshly rebuilt, structurally
# valid history: system + user + facts digest + latest draft + reviewer issues. That
# bounds context (re-running a tool overwrites its fact, it doesn't pile up) and also
# feeds the verifier every tool's freshest output instead of a front-truncated slice.

def _mget(m, key):
    return m.get(key) if isinstance(m, dict) else getattr(m, key, None)


def _merge_tool_results(facts, msgs):
    """Update `facts` ((name, args) -> {name, args, content}) with the tool results in msgs."""
    calls = {}                                             # tool_call_id -> (name, arguments)
    for m in msgs:
        for tc in (_mget(m, "tool_calls") or []):
            calls[tc.id] = (tc.function.name, tc.function.arguments or "")
    for m in msgs:
        if _mget(m, "role") == "tool":
            key = calls.get(_mget(m, "tool_call_id"))
            if key:                                        # latest call with these args wins
                facts[key] = {"name": key[0], "args": key[1], "content": _mget(m, "content") or ""}


def _facts_block(facts):
    out = ["TOOL RESULTS GATHERED SO FAR (latest per tool — authoritative; base every "
           "number on these and do not invent figures):"]
    for f in facts.values():
        args = f["args"] if f["args"] not in ("", "{}") else ""
        out.append(f"\n### {f['name']} {args}".rstrip() + f"\n{f['content']}")
    return "\n".join(out)


def _approx_tokens(msgs):
    """Rough char/4 token estimate over message content + tool-call arguments."""
    n = 0
    for m in msgs:
        c = _mget(m, "content")
        if c:
            n += len(str(c))
        for tc in (_mget(m, "tool_calls") or []):
            n += len(tc.function.arguments or "")
    return n // 4


def _log(msg):
    print(f"[research] {msg}", file=sys.stderr)


_FIX = ("A reviewer flagged these issues. Fix them — call tools again if needed — "
        "then reprint the FULL corrected report:\n- {issues}")


def _verify(report, facts, verifier=VERIFIER_EQUITY):
    # facts is already deduped to the latest result per tool, so the whole set fits well
    # under this guard; the cap is a backstop, not the front-truncation it replaces.
    transcript = "\n".join(f["content"] for f in facts.values())[:24000]
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": verifier},
                  {"role": "user", "content": f"TRANSCRIPT:\n{transcript}\n\nDRAFT:\n{report}"}],
        response_format={"type": "json_object"})           # GLM-5.2 supports structured JSON
    try:
        return json.loads(r.choices[0].message.content)
    except Exception:
        return {"passed": True, "issues": []}              # fail-open, never loop forever


# ---------- helper model: independent structural check + resolver intent ----------
#
# The GLM verifier above also wrote the draft, so same-model blind spots survive. A small, cheap
# model can't be trusted on the numbers, but it CAN cheaply and INDEPENDENTLY confirm structure
# (every rating has its fields, OI/sector are present, disclaimer attached). Same small model also
# powers resolver intent. It is fail-open and gated by STRUCT_CHECK so an unavailable model never
# blocks a run. By default it reuses the Ollama Cloud connection with a small/fast model
# (deepseek-v4-flash) — no local daemon needed (works on a phone). To use a LOCAL model instead,
# set LOCAL_BASE_URL=http://localhost:11434/v1 and LOCAL_MODEL=llama3.2.

DEFAULT_HELPER_MODEL = "deepseek-v4-flash"   # small/fast cloud model for structural check + resolver
_local_client = None


def _helper_model():
    return os.environ.get("LOCAL_MODEL") or DEFAULT_HELPER_MODEL


def _get_local_client():
    global _local_client
    if _local_client is None:
        # default to the cloud Ollama connection; LOCAL_* overrides point it at a local daemon
        _local_client = OllamaClient(
            base_url=os.environ.get("LOCAL_BASE_URL") or os.environ.get("OLLAMA_BASE_URL", "https://ollama.com/v1"),
            api_key=os.environ.get("LOCAL_API_KEY") or os.environ.get("OLLAMA_API_KEY", "ollama"))
    return _local_client


STRUCT_VERIFIER = """You are a STRUCTURE checker, not a fact checker — ignore whether any number
is correct. Reply JSON only: {"passed": boolean, "issues": [string,...]}.
Fail (passed=false) and name the specific missing element if ANY is true:
1. There are not THREE ratings for SHORT, MID and LONG horizons.
2. Any of those ratings is missing any of: Rating, Conviction, Target, Entry, Stop,
   a 'WHAT KILLS THIS TRADE' line, or position size.
3. Options open interest (OI) / put-call is never mentioned.
4. The sector or peer context is never mentioned.
5. The 'not financial advice' disclaimer is absent."""


def _structural_check(report, kind="equity"):
    """Independent, local-model presence check. Fail-open: if the local model is unreachable or
    returns junk, return passed=True so it never blocks the run. Disable with STRUCT_CHECK=0.
    Only meaningful for equities (its checklist is rating/OI/sector-shaped); skip other kinds."""
    if os.environ.get("STRUCT_CHECK", "1") == "0" or kind != "equity":
        return {"passed": True, "issues": []}
    try:
        r = _get_local_client().chat.completions.create(
            model=_helper_model(),
            messages=[{"role": "system", "content": STRUCT_VERIFIER},
                      {"role": "user", "content": f"REPORT:\n{report[:12000]}"}],
            response_format={"type": "json_object"})
        v = json.loads(r.choices[0].message.content)
        if isinstance(v, dict) and "passed" in v:
            return {"passed": bool(v["passed"]), "issues": v.get("issues", [])}
    except Exception as e:
        _log(f"structural check skipped (local model unavailable): {e}")
    return {"passed": True, "issues": []}


def _strip_preamble(report):
    """Drop any model chatter before the first markdown heading (e.g. 'Now I have enough data…'),
    plus a stray leading horizontal rule — so the report opens on its title."""
    m = re.search(r"^#{1,6} ", report, re.M)
    return report[m.start():] if m else report.lstrip().lstrip("-").lstrip()


def _coverage_footer(facts, started):
    """Surface data gaps so silent failures become visible caveats, plus the data-as-of stamp."""
    gaps = []
    for f in facts.values():
        flat = f["content"].lower()
        if ('"error"' in flat or "unavailable" in flat or "no options listed" in flat) \
                and f["name"] not in gaps:
            gaps.append(f["name"])
    lines = ["\n\n---",
             f"Data coverage: fetched {started}; prices delayed ~15 min (Yahoo Finance)."]
    if gaps:
        lines.append("Incomplete/unavailable data from: " + ", ".join(gaps)
                     + " — treat related claims as lower-confidence.")
    return "\n".join(lines)


def _loads(s):
    try:
        return json.loads(s)
    except Exception:
        return s


def _ticker_label(query):
    m = re.findall(r"\b[A-Z]{1,5}\b", query)               # first all-caps token ~ the ticker
    return m[0] if m else "run"


def _persist(query, report, facts, started, label=None):
    """Best-effort save of {query, report, facts} for audit / longitudinal comparison.
    Never raises — a failed save must not fail the research run. Toggle off with SAVE_RUNS=0."""
    if os.environ.get("SAVE_RUNS", "1") == "0":
        return None
    runs_dir = os.environ.get("RUNS_DIR") or os.path.join(os.path.dirname(__file__), "runs")
    try:
        os.makedirs(runs_dir, exist_ok=True)
        stamp = started.replace("+00:00", "Z").replace(":", "")
        path = os.path.join(runs_dir, f"{stamp}_{re.sub(r'[^A-Za-z0-9]', '', label or '') or _ticker_label(query)}.json")
        payload = {"query": query, "started": started, "saved_at": _now_iso(), "report": report,
                   "facts": [{"name": f["name"], "args": f["args"], "result": _loads(f["content"])}
                             for f in facts.values()]}
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
        _log(f"saved run -> {path}")
        return path
    except Exception as e:
        _log(f"persist failed (non-fatal): {e}")
        return None


def _task_message(intent):
    """The opening user turn (from the kind registry): tells the model exactly what was resolved
    so it uses the right symbol/type and doesn't re-guess."""
    return _kind_spec(intent.get("kind", "equity"))["task"].format(
        name=intent.get("name") or intent.get("symbol") or "", symbol=intent.get("symbol"),
        sector_key=intent.get("sector_key"), q=intent.get("query", ""))


def research(user_msg: str, max_passes: int = 3, intent: dict = None) -> str:
    """Resolve the request to an instrument, then run the type-aware deep dive + verify-and-fix.
    The bot may pass a pre-resolved `intent` (after a clarification); otherwise we resolve here."""
    _cache().clear()                                       # fresh yfinance fetches per run
    started = _now_iso()

    if intent is None:
        import resolver                                     # lazy: avoids a circular import
        intent = resolver.resolve(user_msg)
    intent.setdefault("query", user_msg)
    kind = intent.get("kind", "equity")

    if kind == "unknown":
        return (f"I couldn't identify an instrument from “{user_msg}”. Try a ticker (e.g. NVDA, "
                "SOXX), an index (e.g. Nifty IT), or a sector (e.g. 'tech sector outlook').")
    if kind == "ambiguous":                                # CLI fallback (bot clarifies before calling)
        cands = intent.get("candidates") or []
        if not cands:
            return f"“{user_msg}” was ambiguous and I found no candidates. Try a specific ticker."
        top = cands[0]
        intent = {"kind": {"EQUITY": "equity", "ETF": "etf", "INDEX": "index",
                           "MUTUALFUND": "etf"}.get(top["quote_type"], "equity"),
                  "symbol": top["symbol"], "name": top["name"], "sector_key": None,
                  "query": user_msg}
        kind = intent["kind"]
        _log(f"ambiguous -> defaulting to top candidate {top['symbol']} ({kind})")

    system_p, verifier = _prompts_for(intent)
    label = (intent.get("symbol") or intent.get("sector_key")
             or re.sub(r"[^A-Za-z0-9]+", "-", intent.get("name") or user_msg).strip("-")[:24]
             or _ticker_label(user_msg))
    _log(f"resolved '{user_msg}' -> {kind} {label}")

    system = {"role": "system", "content": system_p}
    user = {"role": "user", "content": _task_message(intent)}
    msgs = [system, user]
    report, msgs = _agent_loop(msgs)

    facts = {}                                             # (name, args) -> latest tool result
    passed = False
    for n in range(max_passes):                            # verify -> fix -> re-verify
        _merge_tool_results(facts, msgs)
        _log(f"pass {n}: history ~{_approx_tokens(msgs)} tok, {len(facts)} distinct tool results")
        v = _verify(report, facts, verifier)               # GLM: numbers, sourcing, coherence (type-aware)
        if v.get("passed"):                                # GLM is the gate (reliable on the numbers)
            passed = True
            break
        # GLM already wants a fix — consult the local structural check ADVISORY-only here (and only
        # for kinds the registry marks structural), so its false flags can't *trigger* a pass.
        sc = _structural_check(report) if _kind_spec(kind)["structural"] else {"issues": []}
        issues = list(v.get("issues", [])) + [f"[structure] {i}" for i in sc.get("issues", [])]
        # rebuild a compact, valid history instead of growing the old one
        msgs = [system, user,
                {"role": "user", "content": _facts_block(facts)},
                {"role": "assistant", "content": report},  # latest draft only, for the model to edit
                {"role": "user", "content": _FIX.format(issues="\n- ".join(issues))}]
        report, msgs = _agent_loop(msgs)

    _merge_tool_results(facts, msgs)                       # include the final pass's tool results
    report = _strip_preamble(report)                       # drop any "let me compile…" preamble
    if not passed:
        report += "\n\n[Returned after max verification passes; minor flags may remain.]"
    final = report + _coverage_footer(facts, started)
    _persist(user_msg, final, facts, started, label=label)
    return final


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]).strip() or "Deep dive on NVDA"
    print(research(q))
