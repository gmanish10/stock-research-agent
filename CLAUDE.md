# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot that runs one comprehensive `/research <ticker>` equity deep dive.
**GLM-5.2** (via Ollama Cloud, OpenAI-compatible API) orchestrates and reasons; all
**deterministic math lives in `scripts/`**. News/sentiment comes from Brave Search. The
model decides which tools to call and interprets results — it never does arithmetic.

> Educational/engineering tool. Not financial advice. Market data is delayed ~15 min (Yahoo Finance).

## Commands

```bash
# Install deps (use requirements.txt, NOT pyproject.toml — see note below)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure: copy and fill in keys (OLLAMA_API_KEY, BRAVE_API_KEY, TELEGRAM_TOKEN, ALLOWED_IDS)
cp .env.example .env

# Run the agent directly, no Telegram (fastest dev loop — watch it call tools, draft, self-verify, revise)
python main.py "Deep dive on NVDA"

# Run the bot
python bot_telegram.py

# Run any scripts/ module standalone — each has a JSON-in / JSON-out CLI
python scripts/dcf.py --ticker AAPL          # self-fetches via yfinance
python scripts/dcf.py --input dcf_bundle.json
python scripts/financial_ratios.py --help
```

The entrypoints are `main.py` (the agent core + CLI) and `bot_telegram.py` (the bot, which
does `import main` and calls `main.research`).

## Architecture

```
/research TICKER (Telegram)
   bot_telegram.py   ── allow-list (ALLOWED_IDS) + asyncio.to_thread dispatch (concurrency-safe)
   main.py           ── GLM-5.2 tool loop, then self-verify → fix → re-verify
   scripts/          ── deterministic math (the model never recomputes these)
```

The two-layer split is the core design principle, enforced by the system prompt: **GLM
orchestrates and reasons; `scripts/` compute every number.** Do not let the model "improve"
math inline, and do not move calculations out of `scripts/` into the agent.

### `main.py` — the engine

- **Tools** (`tool_*` functions, registered in `TOOLS_IMPL` + `TOOLS_SPEC`) are thin wrappers:
  fetch data from yfinance/Brave, hand it to a `scripts/` function, return JSON with a
  `"source"` field. Every tool tags its data source so the verifier can audit attribution.
  There are 8: `tool_snapshot`, `tool_ratios`, `tool_technicals`, `tool_dcf`, `tool_options`,
  `tool_sector`, `tool_analyst` (earnings date + analyst targets/recommendations), `tool_web_search`.
- **Shared yfinance fetch: `_ticker()` / `_info()` / `_retry()`.** Tools never call `yf.Ticker`
  directly — `_ticker()` returns one cached `Ticker` per symbol per run (so `.info`/statements are
  scraped once, not 5+ times), and `_retry()` wraps each network access with exponential backoff.
  The cache is **thread-local** (`_tls`) because the bot runs concurrent `research()` calls via
  `asyncio.to_thread`; `research()` calls `_cache().clear()` on entry for per-run freshness.
- **`_latest(df)` / `_prev(df)`** adapt yfinance statement DataFrames to the `{row_label: value}`
  dicts the scripts expect. Scripts are defensive about key-name variants (e.g. `"Total Revenue"`
  vs `"totalRevenue"`).
- **`tool_dcf`** seeds base growth from a real signal (`_estimate_growth`: trailing revenue/earnings
  growth, faded/capped at 25%) when the model gives no override — `dcf.py`'s flat 8% default is
  wildly wrong for high-growth names. It then re-runs the (pure/instant) DCF across a growth×discount
  grid around the *resolved* base assumptions and returns a `sensitivity` block with bear/base/bull.
- **`tool_options`** analyses a **near expiry + a ~monthly** (`_pick_expiries`, ≥25 days out),
  returning a `term_structure` list; nearest-expiry OI is mirrored to the top level. When total OI
  is ~0 (stale/off-hours feed) the OI-derived fields (max-pain/magnets/unusual) are nulled with an
  "unavailable" note rather than emitting meaningless artifacts.
- **`tool_web_search`** (Brave) hard-filters results older than `BRAVE_MAX_AGE_DAYS` by `page_age`
  (Brave's own `freshness` is loose) and surfaces each result's `age`.
- **`_agent_loop`** runs the OpenAI-compatible tool-calling loop (max 16 rounds) and `_stamp`s
  every tool result with an `as_of` UTC timestamp.
- **`research()`** is the public entry: run the loop, then up to `max_passes` (default 3) cycles
  of `_verify()` → feed issues back → re-run. `_verify()` is a second GLM pass in JSON mode
  (the `VERIFIER` prompt) checking sourcing, DCF sanity, OI/sector coverage, and that the verdict
  is synthesized rather than disconnected sections. **`_verify()` fails open** — a JSON parse
  error returns `passed=True` so it never loops forever.
- **Context compaction across passes.** History does not grow unboundedly. `_merge_tool_results`
  accumulates the latest result per distinct tool call into a `facts` dict (re-running a tool
  overwrites its entry rather than appending), and each fix pass rebuilds a fresh, compact,
  API-valid history: `system + user + _facts_block(facts) + latest draft + reviewer issues`.
  `_verify()` is fed that deduped `facts` set (not a front-truncated transcript slice), so it
  always sees every tool's freshest output. `_mget()` exists because history mixes SDK message
  objects (assistant turns) with plain dicts (tool/user turns); never delete a `tool` message
  without its paired assistant `tool_call` or the API rejects the request. `research()` also
  `_log`s an approximate token count per pass to stderr — watch it to confirm you're nowhere
  near the model's window.
- **Independent structural check (`_structural_check`), advisory.** The GLM `_verify` is the sole
  gate. Only when it fails is the **local** model (Llama 3.2 via Ollama) consulted for an independent
  structure-only review (every rating has its fields, OI/sector present, disclaimer attached); its
  issues (prefixed `[structure]`) enrich the fix that's already happening. It is deliberately *not*
  a gate — the small local model is noisy and a false flag must never *trigger* a wasted pass. It's
  **fail-open** and gated by `STRUCT_CHECK`. Configure via `LOCAL_BASE_URL` / `LOCAL_API_KEY` / `LOCAL_MODEL`.
- **Coverage footer + persistence.** `research()` appends a `_coverage_footer` (data as-of stamp,
  ~15-min delay note, and a list of tools that returned errors/unavailable) at a single exit, then
  `_persist()` best-effort saves `{query, report, facts}` to `RUNS_DIR` (default `./runs`, gitignored)
  for audit/longitudinal comparison. Persistence never raises; disable with `SAVE_RUNS=0`.

- **Instrument resolution + confidence (`resolver.py`).** `research()` first calls
  `resolver.resolve(query)` to turn free text into an instrument: exact symbol → `quoteType`; an
  alias map for named indices Search mis-ranks (nifty it→^CNXIT…); **llama 3.2 intent** (verified,
  never trusted) + `yf.Search`. It returns `kind` ∈ {equity, etf, index, sector, theme, unknown} plus
  a **`confidence`** (high/medium/low), an `interpretation` string, and `candidates`. Confidence is
  the safety gate: only **high** (exact symbol / alias / a Search match whose name overlaps the query)
  auto-runs; medium/low make the bot **confirm first**. There is deliberately no blind Brave/Search
  auto-pick — that produced "indian manufacturing" → Mizuho [MFG]. A geographic qualifier
  (`GEO_WORDS`) forces the `theme` path because yfinance `Sector` is US-only.
- **Type-aware routing via the `KINDS` registry.** `KINDS` in `main.py` is the single place each kind
  is configured: `{system, verifier, structural, task}`. `research()` is generic — it reads the
  prompt, verifier, structural-check flag, and opening task message from `KINDS[kind]`. Per kind:
  equity = full pipeline (DCF/ratios/options/ratings); ETF → `tool_etf`, no DCF; index → level +
  technicals + macro; sector → `tool_sector_overview`, outlook only; theme → model discovers exposure
  vehicles (ETFs/indices/stocks) via `tool_web_search`, then briefs. **Adding a query type = a new
  `SYSTEM_*`/`VERIFIER_*` pair + one `KINDS` row**, no engine changes.
- **Bot clarify/confirm loop.** The bot resolves *before* `research()`: high → run; otherwise it
  stores a `pending` entry and asks (candidates by number, or 'yes' for the best guess, or rephrase).
  `research()` also resolves internally for the CLI. `RESOLVER_LLM=0` disables the llama step.

When adding a tool: write the math in `scripts/`, add a `tool_*` wrapper (fetch via `_ticker()`/
`_retry()`, return JSON with a `"source"`), then register it in **both** `TOOLS_IMPL` and
`TOOLS_SPEC`. If it must always run for a given instrument kind, add it to that kind's `SYSTEM_*`
prompt and a matching check to the corresponding `VERIFIER_*`.

### `scripts/` — deterministic math (reused from an equity-research skill)

Public functions called by the tools:
- `financial_ratios.compute_ratios(income, balance, cashflow, prev_income, market_cap)` → margins/ROE/ROIC/FCF-yield
- `technicals.summarize(close, high, low)` → SMA/RSI/MACD/crosses/Bollinger/ATR (needs ~1y of prices for SMA200)
- `options_analytics`: `put_call_ratios`, `max_pain`, `magnet_strikes`, `unusual_activity`, `implied_move` — all operate on a `{"calls": [...], "puts": [...]}` chain dict
- `dcf.dcf(cashflow, balance, shares_outstanding, current_price, beta, growth_rate, discount_rate, years)` → two-stage DCF fair value, echoing every assumption

`dcf.py` is the only new script (house-style companion to `financial_ratios.py`); the other
three are copies of an external equity-research skill — if you maintain a canonical version
elsewhere, sync changes back here.

## Things to know

- **`requirements.txt` is the source of truth for deps**, not `pyproject.toml` (which has an
  empty `dependencies = []` and a `requires-python` of 3.12, while the actual code targets 3.10+).
- **`dcf.py` defaults are static and crude** — risk-free 4.3% / ERP 5% are hardcoded constants,
  echoed in every output. They are *assumptions, not a feed*. Override `growth_rate`/`discount_rate`
  per name before relying on a number.
- **The README/GUIDE call `dcf.py` "unit-tested," but there are no test files in the repo.** The
  math was verified by hand (documented in `GUIDE.md` §6), not by an automated suite. If you change
  `dcf.py`, verify against that worked example (base FCF 100 @ 10%/10%/2.5%/5y → $18.67).
- **`yfinance` is an unofficial scraper** — it can rate-limit or return empty statements; `dcf.py`
  guards empty cash flows. Add retries for heavy use.
- **`glm-5.2:cloud` is third-party cloud inference** (prompts route to Z.ai via Ollama). Public
  tickers only; not for private/on-device data.
- **Self-verification ≠ correctness.** It enforces sourcing/structure/coherence, not whether the
  thesis is right.
- `GUIDE.md` holds the full design rationale and the data-source verification table.
