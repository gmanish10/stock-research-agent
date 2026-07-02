"""
Resolve a free-text query to a concrete instrument + type, so users never need /etf or /sector
— they just type ("SOXX", "Nifty IT", "tech sector outlook", "is Apple a buy?").

Design: a small helper model *infers* intent (a linguistic task), but every decision is *verified
against real data* (yfinance quoteType / Search) so the weak model can never route on trust alone —
same principle as the structural checker. The helper model defaults to a small/fast Ollama Cloud
model (see main._helper_model); set LOCAL_* to use a local one. Ambiguous cases return `candidates`
so the bot can ask the user.

resolve(text) -> {
    "kind": "equity"|"etf"|"index"|"currency"|"sector"|"ambiguous"|"unknown",
    "symbol": str|None, "name": str|None, "sector_key": str|None,
    "candidates": [{"symbol","quote_type","name"}...],   # only when kind == "ambiguous"
    "query": original text,
}
"""

from __future__ import annotations

import os
import re
import json

import yfinance as yf

import main   # reuse the local-LLM client, yfinance retry, and Brave creds

# yfinance's 11 canonical sector keys
SECTORS = {"technology", "healthcare", "financial-services", "consumer-cyclical",
           "consumer-defensive", "energy", "industrials", "basic-materials",
           "real-estate", "utilities", "communication-services"}

# common named indices that yf.Search mis-ranks (it returns tracking ETFs first)
ALIASES = {
    "nifty": "^NSEI", "nifty 50": "^NSEI", "nifty50": "^NSEI",
    "nifty it": "^CNXIT", "nifty bank": "^NSEBANK", "bank nifty": "^NSEBANK",
    "sensex": "^BSESN", "s&p 500": "^GSPC", "s&p500": "^GSPC", "sp500": "^GSPC",
    "nasdaq": "^IXIC", "nasdaq 100": "^NDX", "dow": "^DJI", "dow jones": "^DJI",
    "russell 2000": "^RUT", "vix": "^VIX", "ftse": "^FTSE", "nikkei": "^N225",
}

_QT_TO_KIND = {"EQUITY": "equity", "ETF": "etf", "INDEX": "index", "MUTUALFUND": "etf",
               "CURRENCY": "currency"}
_KIND_TO_QT = {"equity": "EQUITY", "etf": "ETF", "index": "INDEX", "currency": "CURRENCY"}

# yfinance's Sector tool is US-only, so a geographic qualifier means a US sector outlook is wrong —
# route those to the theme pipeline (which discovers region-specific instruments via web search).
GEO_WORDS = {"india", "indian", "china", "chinese", "europe", "european", "japan", "japanese",
             "uk", "british", "asia", "asian", "emerging", "brazil", "korea", "korean", "taiwan",
             "eurozone", "german", "germany", "france", "french", "canada", "canadian", "australian"}

_CLASSIFY = """Classify a finance research request. Reply JSON only:
{"kind": "<equity|etf|index|currency|sector|theme|unknown>", "entity": "<company/fund/index name or ticker>",
 "sector_key": "<one of: technology, healthcare, financial-services, consumer-cyclical,
 consumer-defensive, energy, industrials, basic-materials, real-estate, utilities,
 communication-services — or empty>"}
- "equity": a single company (e.g. "Apple", "should I buy TSLA").
- "etf": a fund/ETF (e.g. "SOXX", "semiconductor etf").
- "index": a market index (e.g. "Nifty IT", "S&P 500", "Nasdaq").
- "currency": an FX pair or exchange rate (e.g. "USDINR", "dollar vs rupee", "euro rate").
  Set entity to the 6-letter pair code (USDINR, EURUSD) with the base currency first.
- "sector": ONE of the 11 standard sectors above (e.g. "tech sector outlook"). Set sector_key.
- "theme": a thematic, geographic, or screen-style basket that is NOT one of the 11 sectors and
  NOT a single instrument (e.g. "Indian manufacturing", "EV supply chain", "AI infrastructure plays",
  "high-dividend utilities in Europe"). Use this for anything open-ended."""


def _symbol_like(t):
    """True for things the user clearly typed AS a symbol: ^IXIC, AAPL, RELIANCE.NS, USDINR=X."""
    return bool(re.fullmatch(r"\^?[A-Z0-9]{1,6}(\.[A-Z]{1,4}|=X)?", t))


def _fx_pair(t):
    """'USDINR' / 'usd inr' / 'USD/INR' -> the Yahoo FX symbol 'USDINR=X', else None.
    Verification gates it: a 6-letter non-pair like 'ORACLE' fails _verify and falls through."""
    m = re.fullmatch(r"([A-Za-z]{3})[ /\-]?([A-Za-z]{3})", t.strip())
    return f"{m.group(1)}{m.group(2)}=X".upper() if m else None


def _verify(symbol):
    """Confirm a symbol exists and return its (quote_type, name), else (None, None)."""
    try:
        i = main._info(main._ticker(symbol))
        qt = i.get("quoteType")
        if qt:
            return qt, (i.get("longName") or i.get("shortName") or symbol)
    except Exception:
        pass
    return None, None


def _resolved(symbol, quote_type, name, query, confidence, candidates=None):
    kind = _QT_TO_KIND.get(quote_type, "unknown")
    return {"kind": kind, "symbol": symbol, "name": name, "sector_key": None,
            "confidence": confidence, "interpretation": f"{name} [{symbol}] — {kind}",
            "candidates": candidates or [], "query": query}


def _unknown(query):
    return {"kind": "unknown", "symbol": None, "name": None, "sector_key": None,
            "confidence": "low", "interpretation": None, "candidates": [], "query": query}


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _name_overlap(query, name):
    """Fraction of query words that appear in the candidate's name — a guard against the
    'indian manufacturing' -> Mizuho [MFG] class of coincidental match."""
    q = _tokens(query)
    return len(q & _tokens(name)) / len(q) if q else 0.0


def _classify_llm(text):
    """llama intent (fail-soft): returns dict or None if the local model is unavailable."""
    if os.environ.get("RESOLVER_LLM", "1") == "0":
        return None
    try:
        r = main._get_local_client().chat.completions.create(
            model=main._helper_model(),
            messages=[{"role": "system", "content": _CLASSIFY},
                      {"role": "user", "content": text}],
            response_format={"type": "json_object"})
        v = json.loads(r.choices[0].message.content)
        return v if isinstance(v, dict) else None
    except Exception as e:
        main._log(f"resolver llm skipped (local model unavailable): {e}")
        return None


def _search(query):
    try:
        quotes = yf.Search(query).quotes or []
    except Exception:
        return []
    out = []
    for q in quotes:
        sym, qt = q.get("symbol"), q.get("quoteType")
        if sym and qt:
            out.append({"symbol": sym, "quote_type": qt,
                        "name": q.get("shortname") or q.get("longname") or sym})
    return out


def _rank(cands, prefer_qt):
    def score(c):
        s = 0
        if prefer_qt and c["quote_type"] == prefer_qt:
            s += 10
        if "." not in c["symbol"] and not c["symbol"].startswith("^"):
            s += 3                                         # US primary listing
        if c["quote_type"] in ("EQUITY", "ETF", "INDEX"):
            s += 1
        return -s
    return sorted(cands, key=score)


def resolve(text):
    """Return a best-guess intent with a `confidence` so the caller can confirm before acting.
    HIGH = exact symbol / alias (auto-proceed). MEDIUM/LOW = confirm with the user. We never
    silently pick a coincidental Search/Brave match — that produced 'indian manufacturing' -> MFG."""
    t = (text or "").strip()
    if not t:
        return _unknown(t)
    low = t.lower()

    # 1) typed as a symbol -> quoteType is authoritative (HIGH)
    if _symbol_like(t):
        qt, name = _verify(t)
        if qt:
            return _resolved(t, qt, name, t, "high")

    # 1b) currency-pair spelling (USDINR / usd inr / USD-INR) -> Yahoo '=X' symbol (HIGH).
    #     _verify's quoteType==CURRENCY is the gate, so random 6-letter words fall through.
    fx = _fx_pair(t)
    if fx:
        qt, name = _verify(fx)
        if qt == "CURRENCY":
            return _resolved(fx, qt, name, t, "high")

    # 2) alias map for named indices Search mis-ranks (HIGH)
    if low in ALIASES:
        qt, name = _verify(ALIASES[low])
        if qt:
            return _resolved(ALIASES[low], qt, name, t, "high")

    # 3) llama intent (verified below, never trusted blindly)
    intent = _classify_llm(t) or {}
    kind = intent.get("kind")
    geo = bool(_tokens(low) & GEO_WORDS)
    if kind == "sector":
        sk = (intent.get("sector_key") or "").strip().lower().replace(" ", "-")
        # a geographic qualifier (US-only Sector tool can't serve it) -> theme, not a US sector
        if sk in SECTORS and not geo:
            # HIGH only when the query literally names the sector ("tech sector"); else confirm
            conf = "high" if _tokens(sk) & _tokens(low) else "medium"
            return {"kind": "sector", "symbol": None, "name": t, "sector_key": sk,
                    "confidence": conf, "interpretation": f"a {sk.replace('-', ' ')} sector outlook",
                    "candidates": [], "query": t}
        kind = "theme"          # no canonical key, or a geographic qualifier -> open-ended theme
    if kind == "theme":
        return _theme(t)
    entity = (intent.get("entity") or "").strip() or t
    if kind == "currency":
        fx = _fx_pair(entity)
        if fx:
            qt, name = _verify(fx)
            if qt == "CURRENCY":
                # pair spelling came from LLM inference (verified real) -> confirm, don't auto-run
                return _resolved(fx, qt, name, t, "medium")

    # 4) yf.Search, biased by the inferred kind. A match must actually resemble the query
    #    (name overlap) to be auto-run; otherwise it's offered for confirmation, never assumed.
    prefer = _KIND_TO_QT.get(kind)
    ranked = _rank(_search(entity), prefer)
    if ranked:
        top = ranked[0]
        overlap = _name_overlap(t, top["name"])
        us_primary = "." not in top["symbol"] and not top["symbol"].startswith("^")
        dominant = len(ranked) == 1 or (us_primary and "." in ranked[1]["symbol"])
        if overlap >= 0.5 and (prefer is None or top["quote_type"] == prefer) and dominant:
            return _resolved(top["symbol"], top["quote_type"], top["name"], t, "high")
        # plausible but not certain -> hand back candidates for the user to confirm/choose
        conf = "medium" if overlap >= 0.34 else "low"
        return {**_resolved(top["symbol"], top["quote_type"], top["name"], t, conf, ranked[:4]),
                "interpretation": f"maybe {top['name']} [{top['symbol']}]"}

    # 5) nothing mapped to an instrument — a multi-word phrase is almost certainly a theme;
    #    a single failed token is more likely a typo/junk -> unknown.
    if len(_tokens(t)) >= 2:
        return _theme(t)
    return _unknown(t)


def _theme(query):
    """Open-ended/thematic/geographic query with no single instrument -> a research-driven brief.
    Medium confidence so the bot confirms (themes are interpretive)."""
    return {"kind": "theme", "symbol": None, "name": query, "sector_key": None,
            "confidence": "medium", "interpretation": f"a thematic brief on “{query}”",
            "candidates": [], "query": query}
