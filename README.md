# Stock-Research Agent (Telegram · GLM-5.2 · Ollama)

A Telegram bot that runs one comprehensive `/research <ticker>` deep dive. **GLM-5.2**
(via Ollama Cloud) orchestrates and reasons; the **deterministic math lives in `scripts/`**
(your equity-research scripts + a unit-tested `dcf.py`). News via **Brave Search**. Every
report integrates fundamentals, a DCF, technicals, **options open interest**, sector context,
and recent news — then a second GLM pass self-verifies and the agent iterates until it passes.

> Educational/engineering tool. **Not financial advice.** Market data is delayed ~15 min (Yahoo Finance).

## Architecture (division of labor)

```
/research TICKER (Telegram)
        │
   bot_telegram.py  ── allow-list + concurrency-safe dispatch
        │
     main.py        ── GLM-5.2 picks tools, synthesizes, self-verifies (verify → fix → re-verify)
        │
   ┌────┴───────────────────────────────────────────────┐
   │ tools (DATA = yfinance / Brave, MATH = scripts/)    │
   │  tool_snapshot  tool_ratios   → financial_ratios.py │
   │  tool_technicals→ technicals.py                     │
   │  tool_dcf       → dcf.py (unit-tested)              │
   │  tool_options   → options_analytics.py (OI, max-pain)│
   │  tool_sector    → yfinance Sector                   │
   │  tool_web_search→ Brave Search                      │
   └─────────────────────────────────────────────────────┘
```

The model never does arithmetic — it decides which tool to call and interprets the results.

## Folder structure

```
stock-research-agent/
├── main.py             # tools + GLM-5.2 loop + self-verification
├── bot_telegram.py     # /research command (1:1 or group)
├── requirements.txt
├── .env.example        # copy to .env and fill in
├── .gitignore
└── scripts/
    ├── dcf.py              # NEW, two-stage DCF, math unit-tested
    ├── technicals.py       # reused from your equity-research skill
    ├── financial_ratios.py # reused
    └── options_analytics.py# reused (OI, P/C, max-pain, magnets, unusual)
```

> The three reused scripts are copies of your equity-research skill's scripts. If you keep a
> canonical version, replace them here so updates stay in sync.

## Setup

1. **Python env + deps**
   ```bash
   cd stock-research-agent
   python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Get keys**
   - **Ollama**: install from ollama.com, `ollama signin`, then copy your API key. Test the model:
     `ollama run glm-5.2:cloud "hi"`.
   - **Brave Search**: create an API key at the Brave Search API dashboard. *A payment method is
     required* (Brave retired its free tier in Feb 2026; you get ~$5 free credits/month).
   - **Telegram**: message `@BotFather` → `/newbot` → copy the token. Keep privacy mode ON.
     Get your numeric user id from `@userinfobot`.

3. **Configure**
   ```bash
   cp .env.example .env
   # edit .env: OLLAMA_API_KEY, BRAVE_API_KEY, TELEGRAM_TOKEN, ALLOWED_IDS
   ```

## Run

Test the agent directly (no Telegram):
```bash
python main.py "Deep dive on NVDA"
```

Start the bot:
```bash
python bot_telegram.py
```
Then message your bot: `/research NVDA`.

**Group use:** add the bot to a group, add the group's (negative) chat id to `ALLOWED_IDS`,
keep BotFather privacy mode ON, and register the command via BotFather `/setcommands`
(`research - Deep-dive a ticker`). The bot runs research off the event loop, so concurrent
users don't block each other — but every request spends *your* Ollama/Brave quota.

## Costs (personal use)

| Item | Cost |
|---|---|
| Ollama Cloud | Free tier (daily quota) to start; ~$20/mo Pro for sustained use |
| Brave Search | Metered ~$5 / 1,000 queries; ~$5 free credits/mo; **card required** |
| Hosting | $0 — runs 24/7 on a spare Android phone via Termux (see DEPLOY.md) |
| yfinance | Free |

## Must-knows

- **`glm-5.2:cloud` is cloud inference** — prompts go to Z.ai via Ollama. Public tickers only.
- **`yfinance` is an unofficial scraper** — can rate-limit or break; `dcf.py` guards empty cash flows. Add retries for heavy use.
- **DCF assumptions are crude by default** (risk-free 4.3% / ERP 5% are static in `dcf.py`, echoed in every output). Override `growth_rate`/`discount_rate` per name before acting.
- **Self-verification ≠ correctness.** It enforces sourcing, OI/sector coverage, and coherence — not whether the thesis is right. Cross-check anything you trade on.
- **Keep `.env` private** — it holds your keys and the bot will research for anyone allow-listed.

See `DEPLOY.md` for running the bot 24/7 on an Android phone (Termux).
