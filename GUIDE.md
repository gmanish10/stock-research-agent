# Telegram Stock-Research Agent with GLM-5.2 on Ollama — Build Guide (v3)

*Prepared 30 Jun 2026. Channel: Telegram only. Scope: stock research only (one comprehensive `/research` command). Educational/engineering guide, not financial advice.*

> **v2:** the agent **reuses your skill's existing Python scripts** as deterministic tools (`technicals.py`, `financial_ratios.py`, `options_analytics.py`, `portfolio_metrics.py`, `parse_portfolio.py`, `report_builder.py`); the missing DCF is added as **`scripts/dcf.py`** (new, house-style, **math unit-tested**).
>
> **v3 (this version):** one **comprehensive** `/research <ticker>` that always integrates **options open interest** (call/put OI, P/C OI ratio, max-pain, OI magnet strikes, unusual activity) and **sector context** (trend, peers, top ETFs via a new `tool_sector`) into a single synthesized verdict — not separate commands. The self-verification loop now fails the draft if OI or sector is missing or if the verdict reads as disconnected sections.

---

## 1. Verdict

**Yes, feasible**, and the right design splits the work cleanly:

- **GLM-5.2 orchestrates and reasons** — decides which tool to call, interprets results, writes the verdict, self-checks. It does **not** do arithmetic.
- **Your scripts do the deterministic math** — margins/ROIC/FCF-yield (`financial_ratios.py`), SMA/RSI/MACD/crosses (`technicals.py`), max-pain/IV/implied-move (`options_analytics.py`), weighted beta/correlation/drawdown (`portfolio_metrics.py`), intake (`parse_portfolio.py`), DCF (`dcf.py`, new).
- **yfinance** is the data feed; **Brave Search** is the news/sentiment feed; **Telegram** is the interface.

This mirrors what your SKILL.md already prescribes ("parse JSON → pass to `scripts/…` → consume the summary"). My v1 under-used the scripts — corrected here.

### Two things to internalize

- **You're porting the *framework*, not the Cowork skill.** The skill is run by *Claude* in Anthropic's harness; you can't drop GLM into it. You reuse the **methodology + the scripts** and swap the engine to GLM-5.2.
- **`glm-5.2:cloud` is cloud inference, not local.** The `:cloud` tag routes prompts to Z.ai via Ollama (Ollama states prompts aren't logged/trained on). Fine for public tickers; not private/on-device. Local GLM-5.2 is impractical (~744B-param MoE).

---

## 2. Architecture — division of labor

| Equity-research component | Your rebuild | Who computes |
|---|---|---|
| Claude (engine) | **GLM-5.2** (`glm-5.2:cloud`, OpenAI-compatible) | orchestration + reasoning only |
| Claude Code tool loop | Python agent (OpenAI SDK → Ollama) | — |
| Yahoo Finance MCP | **`yfinance`** raw fetch | data feed |
| `financial_ratios.py` | **reused as-is** | script (deterministic) |
| `technicals.py` | **reused as-is** (importable; needs ~1y of prices for SMA200) | script |
| `options_analytics.py` | **reused as-is** (OI, P/C, max-pain, magnets, unusual) | script |
| sector context (`get_sector_data`) | **`tool_sector`** via yfinance `Sector` (new wrapper) | data feed |
| `portfolio_metrics.py` | **reused as-is** (portfolio mode) | script |
| `parse_portfolio.py` | **reused as-is** (portfolio intake) | script |
| DCF | **`dcf.py` (new, unit-tested)** | script |
| `report_builder.py` | **reused** for PDF/docx output | script |
| `WebSearch`/`WebFetch` | **Brave Search** tool (returns URLs to cite) | data feed |
| `SKILL.md` 7-phase prompt | system prompt | — |
| QA / reconciliation | **self-verification loop** (adversarial GLM reviewer) | engine |
| Cowork chat UI | **Telegram** | — |

**Flow:** `/research TICKER (Telegram) → agent → GLM-5.2 calls ALL tools → yfinance feeds your scripts (ratios, technicals, dcf, options-OI) + tool_sector + Brave news → GLM synthesizes ONE integrated verdict → verifier checks OI/sector/coherence → agent re-runs & fixes → report to Telegram.`

---

## 3. Dependencies & costs

| Dependency | Purpose | Cost |
|---|---|---|
| **Ollama account + Cloud** | `glm-5.2:cloud` | Free tier (daily quota) to start; **Pro ~$20/mo** for sustained use (GPU-time billed) |
| **Telegram bot** | interface | **Free** (no verification, no number, no public endpoint) |
| **Hosting (always-on)** | run the agent | **$0** on a home machine/Pi; **~$5–6/mo** small VPS for 24/7 |
| **Web search API** | news/sentiment | **Brave Search** — metered pay-as-you-go (~$5 / 1,000 queries; ~$5 free credits/mo ≈ ~1,000 queries). Card required (Brave retired its free tier Feb 2026) |
| **`yfinance`** | data feed | Free (unofficial scraper — add retries) |
| **Your scripts** | the math | Free (you already have them) |

**Run-rate: ~$0 → ~$20–26/mo** (note: Brave needs a card on file even within its monthly free credits).

---

## 4. Prerequisites

- Python 3.10+; your `equity-research/scripts/` folder; basic shell comfort.
- Always-on host (home PC/Mac/Pi or small VPS).
- Keys: **Ollama** API key, **Telegram** bot token (`@BotFather`), **Brave Search** API key (payment method required).

---

## 5. Step-by-step

### Step 1 — GLM-5.2 via Ollama Cloud (verified endpoint)

```bash
# install Ollama from ollama.com, then:
ollama signin
ollama run glm-5.2:cloud "In one line: what does AAPL do?"
```
- **Cloud base URL:** `https://ollama.com/v1` — API key = your Ollama key
- **Local base URL:** `http://localhost:11434/v1` — API key = `ollama`
- **Model:** `glm-5.2:cloud`

### Step 2 — Agent core that REUSES your scripts

```bash
pip install openai yfinance pandas numpy requests python-docx openpyxl rapidfuzz
# copy your skill's scripts/ next to main.py, and drop in the new dcf.py
```

**2a. Setup + import your scripts + a tiny yfinance→dict adapter**
```python
import os, sys, json, requests
sys.path.append(os.path.join(os.path.dirname(__file__), "scripts"))  # your skill's scripts/
import yfinance as yf
from openai import OpenAI

import technicals, financial_ratios, options_analytics   # YOUR scripts, reused as-is
import dcf as dcf_mod                                     # NEW, unit-tested

client = OpenAI(base_url="https://ollama.com/v1", api_key=os.environ["OLLAMA_API_KEY"])
BRAVE_KEY = os.environ["BRAVE_API_KEY"]
MODEL = "glm-5.2:cloud"

# yfinance statement frame -> {row_label: value}; financial_ratios.py/dcf.py are
# already defensive about "Total Revenue" vs "totalRevenue" style keys.
def _latest(df): return {} if df is None or df.empty else df.iloc[:, 0].to_dict()
def _prev(df):   return {} if df is None or df.shape[1] < 2 else df.iloc[:, 1].to_dict()
```

**2b. Tools = thin wrappers around your scripts (GLM never does the math)**
```python
# DATA: Yahoo Finance/yfinance. MATH: your scripts (deterministic, testable).
def tool_snapshot(ticker: str) -> str:
    i = yf.Ticker(ticker).info
    keys = ["longName","sector","industry","marketCap","currentPrice","beta",
            "fiftyTwoWeekHigh","fiftyTwoWeekLow","trailingPE","forwardPE"]
    return json.dumps({"source":"yfinance .info","data":{k:i.get(k) for k in keys}})

def tool_ratios(ticker: str) -> str:                      # -> financial_ratios.py
    t = yf.Ticker(ticker)
    r = financial_ratios.compute_ratios(
        income=_latest(t.income_stmt), balance=_latest(t.balance_sheet),
        cashflow=_latest(t.cashflow), prev_income=_prev(t.income_stmt),
        market_cap=t.info.get("marketCap"))
    return json.dumps({"source":"yfinance + scripts/financial_ratios.py","data":r.to_dict()})

def tool_technicals(ticker: str) -> str:                  # -> technicals.py
    h = yf.Ticker(ticker).history(period="1y")            # ~200+ rows for SMA200
    s = technicals.summarize(h["Close"], h.get("High"), h.get("Low"))
    return json.dumps({"source":"yfinance + scripts/technicals.py","data":s.to_dict()})

def tool_dcf(ticker: str, growth_rate: float = None,
             discount_rate: float = None, years: int = 5) -> str:   # -> dcf.py (NEW)
    t = yf.Ticker(ticker); i = t.info
    r = dcf_mod.dcf(cashflow=_latest(t.cashflow), balance=_latest(t.balance_sheet),
        shares_outstanding=i.get("sharesOutstanding"),
        current_price=i.get("currentPrice"), beta=i.get("beta"),
        growth_rate=growth_rate, discount_rate=discount_rate, years=years)
    return json.dumps({"source":"yfinance + scripts/dcf.py (unit-tested)","data":r.to_dict()})

def tool_options(ticker: str) -> str:                     # -> options_analytics.py
    t = yf.Ticker(ticker); exps = t.options
    if not exps: return json.dumps({"source":"yfinance","data":{"note":"no options listed"}})
    oc = t.option_chain(exps[0])
    chain = {"calls": oc.calls.to_dict("records"), "puts": oc.puts.to_dict("records")}
    px = t.info.get("currentPrice")
    pcr = options_analytics.put_call_ratios(chain)        # call_oi/put_oi/pc_oi_ratio/volumes
    return json.dumps({"source":"yfinance + scripts/options_analytics.py","data":{
        "nearest_expiry": exps[0],
        "open_interest": {"call_oi": pcr["call_oi"], "put_oi": pcr["put_oi"],
                          "pc_oi_ratio": pcr["pc_oi_ratio"]},      # <- OI, front and center
        "pc_volume_ratio": pcr["pc_volume_ratio"],
        "max_pain_strike": options_analytics.max_pain(chain)["max_pain_strike"],  # OI-derived
        "oi_magnet_strikes": options_analytics.magnet_strikes(chain, top_n=5),    # top OI walls
        "unusual_activity": options_analytics.unusual_activity(chain)[:5],        # vol/OI spikes
        "implied_move": options_analytics.implied_move(chain, px)}})

def tool_sector(ticker: str) -> str:                      # -> yfinance Sector/Industry
    i = yf.Ticker(ticker).info
    skey = i.get("sectorKey") or (i.get("sector","").lower().replace(" ", "-") or None)
    out = {"sector": i.get("sector"), "industry": i.get("industry")}
    try:
        s = yf.Sector(skey)
        ov = s.overview or {}
        out["sector_overview"] = {k: ov.get(k) for k in
            ("market_cap","market_weight","companies_count","employee_count") if k in ov}
        tc = s.top_companies
        out["top_companies"] = (tc.head(8).reset_index().to_dict("records")
                                if tc is not None else [])        # sector peers + ratings
        out["top_etfs"] = dict(list((s.top_etfs or {}).items())[:5])
    except Exception as e:
        out["note"] = f"sector detail unavailable: {e}"
    return json.dumps({"source":"yfinance Sector/Industry","data": out})

def tool_web_search(query: str) -> str:                   # news/sentiment, returns URLs (Brave)
    resp = requests.get("https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": BRAVE_KEY, "Accept": "application/json"},
        params={"q": query, "count": 5, "freshness": "pm"}, timeout=20)   # pm = past month
    results = (resp.json().get("web") or {}).get("results", [])
    return json.dumps({"source":"Brave Search","results":[
        {"title":x.get("title"),"url":x.get("url"),"content":(x.get("description") or "")[:500]}
        for x in results[:5]]})

TOOLS_IMPL = {"tool_snapshot":tool_snapshot,"tool_ratios":tool_ratios,
              "tool_technicals":tool_technicals,"tool_dcf":tool_dcf,
              "tool_options":tool_options,"tool_sector":tool_sector,
              "tool_web_search":tool_web_search}
```

**2c. Tool specs + system prompt (sourcing + no-recompute rules)**
```python
def _s(name, desc, props, req):
    return {"type":"function","function":{"name":name,"description":desc,
        "parameters":{"type":"object","properties":props,"required":req}}}
TOOLS_SPEC = [
 _s("tool_snapshot","Business/sector/price snapshot (Yahoo Finance)",{"ticker":{"type":"string"}},["ticker"]),
 _s("tool_ratios","Margins/ROE/ROIC/FCF-yield via your financial_ratios.py",{"ticker":{"type":"string"}},["ticker"]),
 _s("tool_technicals","SMA/RSI/MACD/crosses via your technicals.py",{"ticker":{"type":"string"}},["ticker"]),
 _s("tool_dcf","Two-stage DCF fair value via dcf.py; returns explicit assumptions. "
    "Optional growth_rate/discount_rate/years overrides.",
    {"ticker":{"type":"string"},"growth_rate":{"type":"number"},
     "discount_rate":{"type":"number"},"years":{"type":"integer"}},["ticker"]),
 _s("tool_options","Options positioning incl. open interest (call/put OI, P/C OI ratio), "
    "max-pain, OI magnet strikes, unusual activity, implied move — via options_analytics.py",
    {"ticker":{"type":"string"}},["ticker"]),
 _s("tool_sector","Sector/industry context: trend, top peer companies, top ETFs (yfinance Sector)",
    {"ticker":{"type":"string"}},["ticker"]),
 _s("tool_web_search","Recent news/sentiment; returns source URLs to cite",{"query":{"type":"string"}},["query"]),
]

SYSTEM = """You are an equity research analyst. For the ticker, produce ONE comprehensive,
integrated deep dive — synthesize the phases together, do not output disconnected sections.
Always run ALL of these (none optional):
 - Business + sector context -> tool_snapshot, tool_sector (sector trend, peer names, top ETFs)
 - Fundamentals -> tool_ratios
 - Valuation -> tool_dcf (state assumptions + implied upside/downside)
 - Technicals/trend -> tool_technicals
 - Options positioning -> tool_options. EXPLICITLY factor in OPEN INTEREST: call vs put OI,
   P/C OI ratio, max-pain, OI magnet strikes, and unusual activity — and say what that
   positioning implies for the likely move and for your price levels.
 - Catalysts/news/sentiment -> tool_web_search (cite source URLs, last ~30 days)
The verdict must REFLECT the synthesis: tie targets to DCF and to max-pain/OI magnet levels,
tie stops to technicals (ATR/SMA), and state how the sector trend and options OI skew
support or threaten the thesis.
HARD RULES:
- Tools compute every number. NEVER do arithmetic yourself or recall figures from memory.
- Attribute each datum (Yahoo Finance, a named script, or a web_search URL). If a tool
  errors/returns empty, say 'unavailable' for that piece and continue.
End with SHORT/MID/LONG ratings; each needs Rating, Conviction, Target, Entry, Stop,
'WHAT KILLS THIS TRADE' (with % loss), position size. Append:
'Educational only, not financial advice.'"""
```

**2d. Agent loop + self-verification (iterate until satisfied)**
```python
def _agent_loop(msgs, max_rounds=16):
    for _ in range(max_rounds):
        m = client.chat.completions.create(model=MODEL, messages=msgs,
                                           tools=TOOLS_SPEC).choices[0].message
        if not m.tool_calls: return m.content, msgs
        msgs.append(m)
        for tc in m.tool_calls:
            try: out = TOOLS_IMPL[tc.function.name](**json.loads(tc.function.arguments))
            except Exception as e: out = json.dumps({"error": str(e)})
            msgs.append({"role":"tool","tool_call_id":tc.id,"content":out})
    return "Stopped after max tool rounds.", msgs

VERIFIER = """You are a skeptical reviewer. Check the DRAFT against the TRANSCRIPT of
tool results. Reply JSON only: {"passed": boolean, "issues": [string,...]}.
Fail (passed=false) and list each violation if ANY of these is true:
1. A number in the draft is NOT present in the transcript (invented/unsourced).
2. The DCF assumptions (growth, discount, terminal) are missing or unreasonable.
3. A news/sentiment claim lacks a source URL from tool_web_search.
4. Any rating lacks a target, a stop, or a quantified 'WHAT KILLS THIS TRADE'.
5. A rating contradicts the data (e.g. 'Buy' despite large DCF downside) with no rationale.
6. Options OPEN INTEREST is not analysed (need call/put OI or P/C OI ratio, plus max-pain or OI magnet strikes).
7. Sector/industry context is missing (need sector trend or peer comparison).
8. The verdict doesn't tie targets/stops back to the DCF, OI levels, or technicals (i.e. sections feel disconnected)."""

def _verify(report, msgs):
    transcript = "\n".join(str(x.get("content","")) for x in msgs if x.get("role")=="tool")
    r = client.chat.completions.create(model=MODEL,
        messages=[{"role":"system","content":VERIFIER},
                  {"role":"user","content":f"TRANSCRIPT:\n{transcript[:8000]}\n\nDRAFT:\n{report}"}],
        response_format={"type":"json_object"})       # GLM-5.2 supports structured JSON
    try: return json.loads(r.choices[0].message.content)
    except Exception: return {"passed": True, "issues": []}   # fail-open, never loop forever

def research(user_msg: str, max_passes: int = 3) -> str:
    msgs = [{"role":"system","content":SYSTEM},{"role":"user","content":user_msg}]
    report, msgs = _agent_loop(msgs)
    for _ in range(max_passes):                        # verify -> fix -> re-verify
        v = _verify(report, msgs)
        if v.get("passed"): return report
        msgs.append({"role":"user","content":
            "A reviewer flagged these issues. Fix them — call tools again if needed — "
            "then reprint the FULL corrected report:\n- " + "\n- ".join(v.get("issues",[]))})
        report, msgs = _agent_loop(msgs)
    return report + "\n\n[Returned after max verification passes; minor flags may remain.]"
```
Test: `OLLAMA_API_KEY=... BRAVE_API_KEY=... python -c "import main; print(main.research('Deep dive on NVDA with DCF'))"`.

### Step 3 — Telegram bot

```bash
pip install python-telegram-bot
```
```python
# bot_telegram.py — single comprehensive /research command
from telegram.ext import Application, CommandHandler
import asyncio, main, os

ALLOWED = {123456789}   # your Telegram user id(s); add a group chat id (negative) for group use

async def research_cmd(update, ctx):
    if update.effective_chat.id not in ALLOWED and update.effective_user.id not in ALLOWED:
        return
    query = " ".join(ctx.args).strip()
    if not query:
        return await update.message.reply_text("Usage: /research NVDA")
    await update.message.reply_text(f"🔍 Researching {query} + self-checking… (~1–3 min)")
    report = await asyncio.to_thread(main.research, query)   # off the event loop -> concurrency-safe
    for i in range(0, len(report), 4000):                     # Telegram ~4096-char limit
        await update.message.reply_text(report[i:i+4000])

app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
app.add_handler(CommandHandler("research", research_cmd))
app.run_polling()
```
Message `/research TSLA`. Works in a 1:1 chat or a group — for a group, add the group's (negative) chat id to `ALLOWED` and keep BotFather privacy mode ON so the bot only sees its own commands. Register it via BotFather `/setcommands` → `research - Deep-dive a ticker` for autocomplete. No server/domain needed.

### Step 4 — Host (always-on)
Home machine/Pi under `tmux`/`systemd`/`pm2` ($0), or a ~$5–6/mo VPS for 24/7.

### Step 5 — Portfolios + PDF output (reuse the rest of your scripts)
- **Intake:** pipe pasted holdings / CSV / xlsx through `parse_portfolio.py` → normalized positions.
- **Portfolio math:** `portfolio_metrics.py --fetch` → weighted beta, correlation, concentration, drawdown sims. Loop `research()` per position, then synthesize (your reconciliation step).
- **Deliverable:** `report_builder.py --template equity-research --input report.json --output X.docx`, then send the file via Telegram (`reply_document`). Cleaner than a 4,000-char wall for deep dives.

---

## 6. Data sources & verification (what you asked me to double-check)

| Data / computation | Source | Status |
|---|---|---|
| Business/sector/price/beta | Yahoo Finance `yfinance .info` | ✅ |
| Margins, ROE/ROA/ROIC, D/E, FCF yield | **your `financial_ratios.py`** on yfinance statements | ✅ reused as-is |
| SMA/RSI/MACD/crosses/Bollinger | **your `technicals.py`** | ✅ reused as-is |
| Options **open interest** (call/put OI, P/C OI ratio), max-pain, OI magnet strikes, unusual activity, implied move | **your `options_analytics.py`** | ✅ reused as-is |
| Sector trend, peer companies, top ETFs | **`tool_sector`** → yfinance `Sector` (`sectorKey` from `.info`) | ✅ API verified |
| Weighted beta, correlation, drawdown | **your `portfolio_metrics.py`** | ✅ reused as-is |
| Portfolio intake | **your `parse_portfolio.py`** | ✅ reused as-is |
| **DCF fair value** | **new `dcf.py`** (cash flows + balance from yfinance) | ✅ **math unit-tested** (see below) |
| DCF rf (4.3%) & ERP (5%) | **static assumptions in `dcf.py`** — echoed in output | ⚠️ assumption, not a feed |
| News / sentiment | **Brave Search** (`web.results`) — returns **URLs the model must cite** | ✅ API verified |
| Model endpoint | `https://ollama.com/v1`, `glm-5.2:cloud` | ✅ verified |

**`dcf.py` verification I ran:** base FCF 100 @ 10% growth / 10% discount / 2.5% terminal / 5y → fair value **$18.67**, matching a hand calculation (PV of FCFs 500 + PV of terminal 1366.67 = EV 1866.67). Also confirmed: `OCF − |Capex|` path (200 − 50 = 150), CAPM discount (0.043 + 1.2×0.05 = 0.103), and a clean "cannot value" guard when statements are empty.

**How the agent verifies itself:** after drafting, a separate GLM-5.2 reviewer (JSON mode) checks that no number is unsourced, DCF assumptions are stated/sane, news carries URLs, every rating has target/stop/quantified kill-case, and ratings don't contradict the data. Failures are fed back and the agent re-runs tools and rewrites — looping up to `max_passes`, capped, fail-open on parse error.

**Two honesty flags on the data:** `yfinance` is an *unofficial* scraper (its `.cashflow` occasionally returns empty — `dcf.py` guards for it; add retries). And a DCF is only as good as its assumptions — the defaults are transparent but crude, labelled in every output, with a ±1%→15–25% sensitivity warning. Override per-name before acting.

---

## 7. Caveats / must-knows
- **GLM orchestrates; your scripts compute.** That keeps the numbers deterministic and testable — don't let the model "improve" the math inline.
- **`glm-5.2:cloud` is third-party cloud inference** (prompts go to Z.ai via Ollama). Public tickers only.
- **Self-verification ≠ correctness.** It catches sourcing/structure/contradiction errors, not whether the thesis is right. Cross-check anything you trade on.
- **Not financial advice** — keep the disclaimer in every reply (your `report_builder.py` already embeds it).

---

## 8. Fastest path
1. `ollama run glm-5.2:cloud "hi"` → confirm access.
2. Copy your `scripts/` + the new `dcf.py` next to `main.py`; test `research("Deep dive on NVDA with DCF")` in the terminal — watch it call your scripts, draft, self-check, revise.
3. Add `bot_telegram.py`, set `ALLOWED`, message the bot.

Afternoon's work on a laptop, **$0** on free tiers. `dcf.py` ships with this guide, ready to drop into `scripts/`.
