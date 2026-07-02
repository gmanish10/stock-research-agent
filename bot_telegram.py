"""
Telegram front-end: one natural-language /research command.

Type anything — a ticker (NVDA), an ETF (SOXX), an index (Nifty IT), or a sector
("tech sector outlook"). The resolver infers what you meant (llama 3.2 + yfinance/Brave);
if it's genuinely ambiguous the bot lists candidates and you reply with a number.

Works in a 1:1 chat or a group. For a group, add the group's (negative) chat id to
ALLOWED_IDS and keep BotFather privacy mode ON so the bot only sees its own commands.

    python bot_telegram.py
"""

import os
import re
import asyncio

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, filters

import main as engine   # the research engine — aliased so it isn't shadowed by this module's main()
import resolver         # free-text -> instrument + type

try:
    import pdf_report    # optional: deliver the report as a dated PDF
except Exception:         # missing deps -> silently fall back to text
    pdf_report = None

load_dotenv()

# comma-separated Telegram user ids and/or group chat ids (negative) allowed to use the bot
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}

_QT_KIND = {"EQUITY": "equity", "ETF": "etf", "INDEX": "index", "MUTUALFUND": "etf"}


def _allowed(update):
    """None = bot not configured; True/False = this user/chat is allow-listed or not."""
    if not ALLOWED:
        return None
    uid, cid = update.effective_user.id, update.effective_chat.id
    return uid in ALLOWED or cid in ALLOWED


def _confirm_text(query, intent):
    """Ask before acting on a not-fully-certain reading — never silently run a coincidental match."""
    interp = intent.get("interpretation") or "something I'm not sure about"
    cands = intent.get("candidates") or []
    lines = [f"I'm not fully sure what you mean by “{query}”."]
    if cands:
        lines.append("Did you mean one of these? Reply with a number:")
        for i, c in enumerate(cands, 1):
            lines.append(f"{i}. {c['symbol']} ({c['quote_type']}) — {c['name']}")
        lines.append(f"…or reply 'yes' to use my best guess ({interp}), or just rephrase.")
    else:
        lines.append(f"Best guess: {interp}. Reply 'yes' to proceed, or send a ticker / rephrase.")
    return "\n".join(lines)


def _intent_from_candidate(c, query):
    return {"kind": _QT_KIND.get(c["quote_type"], "equity"), "symbol": c["symbol"],
            "name": c["name"], "sector_key": None, "query": query,
            "interpretation": f"{c['name']} [{c['symbol']}]", "confidence": "high"}


async def _handle_query(message, chat_data, query):
    """Resolve a free-text query and either research it (high confidence) or confirm first."""
    intent = await asyncio.to_thread(resolver.resolve, query)
    if intent.get("kind") == "unknown":
        return await message.reply_text(
            f"I couldn't identify an instrument from “{query}”. Try a ticker (NVDA, SOXX), "
            "an index (Nifty IT), or a sector (e.g. 'tech sector').")
    if intent.get("confidence") == "high":
        return await _run_and_send(message, query, intent)
    # not certain -> confirm before spending a research run
    chat_data["pending"] = {"primary": intent, "candidates": intent.get("candidates") or [],
                            "query": query}
    await message.reply_text(_confirm_text(query, intent))


def _slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")[:24] or "report"


# Chats with a research run in flight. All handlers run on the one event loop and the
# check-and-add below has no await between them, so a plain set is race-free (no lock).
_running: set[int] = set()


async def _run_and_send(message, query, intent):
    if message.chat_id in _running:
        return await message.reply_text(
            "⏳ Still working on your previous request — I'll send it here when it's ready. "
            "(One research at a time per chat.)")
    _running.add(message.chat_id)
    try:
        kind, sym = intent.get("kind"), intent.get("symbol")
        if kind in ("sector", "theme"):
            what = intent.get("name") or intent.get("sector_key")
        else:
            what = f"{intent.get('name') or sym} [{sym}]"
        await message.reply_text(f"🔍 Researching {what} ({kind}). Please wait.")
        report = await asyncio.to_thread(engine.research, query, 3, intent)
        label = sym or intent.get("sector_key") or _slug(intent.get("name") or query)
        await _deliver(message, report, label)
    finally:
        _running.discard(message.chat_id)


async def _deliver(message, report, label):
    """Deliver as a dated PDF when possible; fall back to chunked text on any failure.
    The report's '## TL;DR' section is the first page of the PDF — deliberately NOT also sent
    as a text message: raw-markdown tables/bold render ugly in Telegram text."""
    if pdf_report is not None and os.environ.get("REPORT_PDF", "1") != "0":
        try:
            path = await asyncio.to_thread(pdf_report.build_pdf, report, label)
            with open(path, "rb") as fh:
                await message.reply_document(
                    document=fh, filename=os.path.basename(path),
                    caption=f"{label} — {os.path.basename(path)[:10]}. "
                            "Educational only, not financial advice.")
            return
        except Exception as e:
            print(f"[bot] PDF delivery failed, falling back to text: {e!r}", flush=True)
    await _send_chunked(message, report)


async def _send_chunked(message, text):
    """Send a long report in <=4000-char chunks, retrying each chunk — a single transient
    Telegram timeout shouldn't drop the whole report (and leave the user with nothing)."""
    for i in range(0, len(text), 4000):            # Telegram ~4096-char limit
        chunk = text[i:i + 4000]
        for attempt in range(3):
            try:
                await message.reply_text(chunk)
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(2 * (attempt + 1))


async def research_cmd(update, ctx):
    allowed = _allowed(update)
    if allowed is None:
        return await update.message.reply_text(
            "Bot not configured: set ALLOWED_IDS in .env (your Telegram user id).")
    if not allowed:
        return  # silently ignore anyone not allow-listed

    query = " ".join(ctx.args).strip()
    if not query:
        return await update.message.reply_text(
            "Usage: /research <anything> — a ticker (NVDA), ETF (SOXX), index (Nifty IT), "
            "or sector ('tech sector').")
    await _handle_query(update.message, ctx.chat_data, query)


async def choice_cmd(update, ctx):
    """Plain-text replies: only act when a clarification/confirmation is pending."""
    if not _allowed(update):
        return
    pending = ctx.chat_data.get("pending")
    if not pending:
        return
    text = (update.message.text or "").strip()
    low = text.lower()
    cands = pending.get("candidates") or []

    if low in ("yes", "y", "ok", "okay", "yep", "sure", "proceed", "go"):
        ctx.chat_data.pop("pending", None)
        primary = pending.get("primary")
        if primary:
            return await _run_and_send(update.message, pending["query"], primary)
        return await update.message.reply_text("No saved guess — send a ticker.")
    if low in ("no", "n", "cancel", "stop", "nvm"):
        ctx.chat_data.pop("pending", None)
        return await update.message.reply_text("Okay, cancelled.")
    if text.isdigit() and 1 <= int(text) <= len(cands):
        ctx.chat_data.pop("pending", None)
        return await _run_and_send(update.message, pending["query"],
                                   _intent_from_candidate(cands[int(text) - 1], pending["query"]))
    chosen = next((c for c in cands if c["symbol"].upper() == text.upper()), None)
    if chosen:
        ctx.chat_data.pop("pending", None)
        return await _run_and_send(update.message, pending["query"],
                                   _intent_from_candidate(chosen, pending["query"]))
    # not a selection — treat it as a brand-new query
    ctx.chat_data.pop("pending", None)
    await _handle_query(update.message, ctx.chat_data, text)


async def _on_error(_update, context):
    print(f"[bot] handler error: {context.error!r}", flush=True)


def main():
    token = os.environ["TELEGRAM_TOKEN"]
    # concurrent_updates: without it PTB processes updates strictly sequentially, so one
    # multi-minute research run head-of-line blocks EVERY chat (second user gets silence).
    # The engine is concurrency-safe (thread-local yfinance cache per run); cap at 8 so a
    # burst can't stampede the LLM/yfinance/Brave rate limits. Per-chat duplicates are
    # rejected by the _running guard in _run_and_send.
    app = (Application.builder().token(token).concurrent_updates(8)
           .read_timeout(30).write_timeout(60).connect_timeout(30).pool_timeout(30)
           .build())
    app.add_handler(CommandHandler("research", research_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, choice_cmd))
    app.add_error_handler(_on_error)
    print("Bot running (polling). Send /research <anything>. Ctrl+C to stop.", flush=True)
    app.run_polling()


if __name__ == "__main__":
    main()
