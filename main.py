# solana copy trading bot
# single file, runs on a droplet, no external web app needed
# state lives in memory, one asyncio.Lock guards everything

import asyncio
import logging
import random
import re
import secrets
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import base58
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN_HERE"
LOG_LEVEL = logging.INFO

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=LOG_LEVEL,
)
logger = logging.getLogger("sol-copy-bot")


# in-memory db
# one dataclass per user, dict keyed by telegram id
# DB_LOCK must be held on every read/write, simulator writes too

@dataclass
class UserState:
    auto_trade: bool = True
    budget_cap: float = 50.0
    allocation_mode: str = "FIRST COME"  # or "EQUAL SPLIT"
    masters: List[Optional[str]] = field(default_factory=lambda: [None, None, None])
    last_executions: List[str] = field(default_factory=list)


DB: Dict[int, UserState] = {}
DB_LOCK = asyncio.Lock()


async def get_state(user_id: int) -> UserState:
    async with DB_LOCK:
        if user_id not in DB:
            DB[user_id] = UserState()
        return DB[user_id]


async def update_state(user_id: int, mutator) -> UserState:
    # generic mutation helper, mutator runs while lock is held
    async with DB_LOCK:
        if user_id not in DB:
            DB[user_id] = UserState()
        mutator(DB[user_id])
        return DB[user_id]


# address validation
# a valid solana pubkey is base58 and decodes to exactly 32 bytes
# string length is usually 32-44 chars, we check both

_BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]+$")


def is_valid_solana_address(addr: str) -> bool:
    if not addr or not isinstance(addr, str):
        return False
    if not (32 <= len(addr) <= 44):
        return False
    if not _BASE58_RE.match(addr):
        return False
    try:
        decoded = base58.b58decode(addr)
    except Exception:
        return False
    return len(decoded) == 32


def shorten(addr: str, head: int = 4, tail: int = 4) -> str:
    if not addr:
        return ""
    if len(addr) <= head + tail + 3:
        return addr
    return f"{addr[:head]}...{addr[-tail:]}"


# ui builders
# all text is html, all keyboards are inline, nothing fancy

def dashboard_text(state: UserState) -> str:
    status = "⚡ <b>AUTO-TRADE ACTIVE</b>" if state.auto_trade else "🛑 <b>PAUSED</b>"
    filled = sum(1 for m in state.masters if m)
    lines = [
        "<b>🤖 Solana Copy Trading Bot</b>",
        "───────────────────────────",
        f"Status: {status}",
        f"Master Slots Used: <b>{filled}/3</b>",
        f"Global Budget Cap: <b>${state.budget_cap:.2f}</b>",
        f"Allocation Mode: <b>{state.allocation_mode}</b>",
        "───────────────────────────",
    ]
    if state.last_executions:
        lines.append("<b>Recent Executions:</b>")
        lines.extend(state.last_executions[-5:])
    return "\n".join(lines)


def dashboard_keyboard(state: UserState) -> InlineKeyboardMarkup:
    toggle_label = "🛑 Pause Auto-Trade" if state.auto_trade else "▶️ Start Auto-Trade"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data="toggle_auto")],
        [InlineKeyboardButton("👥 Manage Master Traders", callback_data="manage_masters")],
        [InlineKeyboardButton("⚙️ Edit Risk Settings", callback_data="risk_settings")],
        [InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh")],
    ])


def masters_text(state: UserState) -> str:
    lines = ["<b>👥 Master Traders (3 Slots)</b>", "───────────────────────────"]
    for i, addr in enumerate(state.masters):
        slot = i + 1
        if addr:
            lines.append(f"Slot {slot}: <code>{shorten(addr, 6, 6)}</code>")
        else:
            lines.append(f"Slot {slot}: <i>Empty</i>")
    return "\n".join(lines)


def masters_keyboard(state: UserState) -> InlineKeyboardMarkup:
    kb = []
    for i, addr in enumerate(state.masters):
        slot = i + 1
        if addr:
            kb.append([
                InlineKeyboardButton(f"✏️ Edit Slot {slot}", callback_data=f"edit_slot:{slot}"),
                InlineKeyboardButton(f"❌ Delete Slot {slot}", callback_data=f"del_slot:{slot}"),
            ])
        else:
            kb.append([
                InlineKeyboardButton(f"➕ Add Master Trader to Slot {slot}", callback_data=f"add_slot:{slot}")
            ])
    kb.append([InlineKeyboardButton("↩️ Back to Dashboard", callback_data="back_dash")])
    return InlineKeyboardMarkup(kb)


def risk_text(state: UserState) -> str:
    return (
        "<b>⚙️ Risk Management Configuration</b>\n"
        "───────────────────────────\n"
        f"Global Budget Cap: <b>${state.budget_cap:.2f}</b>\n"
        f"Allocation Mode: <b>{state.allocation_mode}</b>\n\n"
        "Mode explanation:\n"
        "• <b>FIRST COME</b> — first trade uses the full cap, concurrent ones are skipped.\n"
        "• <b>EQUAL SPLIT</b> — simultaneous trades split the cap evenly."
    )


def risk_keyboard(state: UserState) -> InlineKeyboardMarkup:
    other = "EQUAL SPLIT" if state.allocation_mode == "FIRST COME" else "FIRST COME"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Edit Cap", callback_data="edit_cap")],
        [InlineKeyboardButton(f"🔀 Switch Mode → {other}", callback_data="toggle_mode")],
        [InlineKeyboardButton("↩️ Back to Dashboard", callback_data="back_dash")],
    ])


# dashboard handlers
# /start draws the dashboard and lazily boots the simulator once

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    state = await get_state(user.id)
    await update.effective_message.reply_text(
        dashboard_text(state),
        reply_markup=dashboard_keyboard(state),
        parse_mode=ParseMode.HTML,
    )
    if not ctx.bot_data.get("simulator_started"):
        ctx.bot_data["simulator_started"] = True
        ctx.application.create_task(simulated_trade_loop(update.get_bot()))


# every inline button lands here, we string-match on callback_data
async def cb_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    user_id = update.effective_user.id
    data = q.data or ""

    # plain dashboard buttons
    if data == "refresh":
        state = await get_state(user_id)
        await q.edit_message_text(
            dashboard_text(state), reply_markup=dashboard_keyboard(state), parse_mode=ParseMode.HTML
        )
        return

    if data == "toggle_auto":
        def mut(s: UserState):
            s.auto_trade = not s.auto_trade
        state = await update_state(user_id, mut)
        await q.edit_message_text(
            dashboard_text(state), reply_markup=dashboard_keyboard(state), parse_mode=ParseMode.HTML
        )
        return

    if data == "manage_masters":
        state = await get_state(user_id)
        await q.edit_message_text(
            masters_text(state), reply_markup=masters_keyboard(state), parse_mode=ParseMode.HTML
        )
        return

    if data == "risk_settings":
        state = await get_state(user_id)
        await q.edit_message_text(
            risk_text(state), reply_markup=risk_keyboard(state), parse_mode=ParseMode.HTML
        )
        return

    if data == "back_dash":
        state = await get_state(user_id)
        await q.edit_message_text(
            dashboard_text(state), reply_markup=dashboard_keyboard(state), parse_mode=ParseMode.HTML
        )
        return

    # master slot buttons
    # delete is instant, no confirm step
    if data.startswith("del_slot:"):
        slot = int(data.split(":")[1]) - 1
        def mut(s: UserState):
            s.masters[slot] = None
        state = await update_state(user_id, mut)
        await q.edit_message_text(
            masters_text(state), reply_markup=masters_keyboard(state), parse_mode=ParseMode.HTML
        )
        return

    # risk buttons
    if data == "toggle_mode":
        def mut(s: UserState):
            s.allocation_mode = "EQUAL SPLIT" if s.allocation_mode == "FIRST COME" else "FIRST COME"
        state = await update_state(user_id, mut)
        await q.edit_message_text(
            risk_text(state), reply_markup=risk_keyboard(state), parse_mode=ParseMode.HTML
        )
        return

    # buttons that kick off a text-input flow
    # we drop a flag in user_data, handle_text_input picks it up
    if data.startswith("add_slot:"):
        slot = int(data.split(":")[1]) - 1
        ctx.user_data["pending_slot"] = slot
        await q.edit_message_text(
            f"Send me the <b>Solana public key</b> of the Master Trader for Slot {slot + 1}.\n"
            "It must be a base58 string (32–44 chars). Send /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("edit_slot:"):
        slot = int(data.split(":")[1]) - 1
        ctx.user_data["pending_slot"] = slot
        await q.edit_message_text(
            f"Send the <b>new Solana public key</b> for Slot {slot + 1}.\n"
            "Send /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "edit_cap":
        ctx.user_data["awaiting_cap"] = True
        await q.edit_message_text(
            "Send me the new <b>Global Budget Cap</b> in USD (e.g. <code>10</code> or <code>250.50</code>).\n"
            "Send /cancel to abort.",
            parse_mode=ParseMode.HTML,
        )
        return


# text input handler
# one handler serves both flows (cap input and address input),
# we check user_data flags to know what the user is answering

async def handle_text_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text = (update.effective_message.text or "").strip()

    # cap prompt
    if ctx.user_data.get("awaiting_cap"):
        try:
            value = float(text.replace("$", "").replace(",", ""))
            if value <= 0 or value > 1_000_000:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text(
                "❌ Invalid amount. Send a positive number like <code>10</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        ctx.user_data.pop("awaiting_cap", None)
        def mut(s: UserState):
            s.budget_cap = round(value, 2)
        state = await update_state(user_id, mut)
        await update.effective_message.reply_text(
            f"✅ Budget cap updated to <b>${state.budget_cap:.2f}</b>.",
            reply_markup=risk_keyboard(state),
            parse_mode=ParseMode.HTML,
        )
        return

    # address prompt
    if "pending_slot" in ctx.user_data:
        slot = ctx.user_data["pending_slot"]
        if not is_valid_solana_address(text):
            await update.effective_message.reply_text(
                "❌ <b>Invalid Solana address.</b>\n"
                "It must be a base58-encoded public key (32–44 chars). Try again or /cancel.",
                parse_mode=ParseMode.HTML,
            )
            return
        ctx.user_data.pop("pending_slot", None)
        def mut(s: UserState):
            s.masters[slot] = text
        state = await update_state(user_id, mut)
        await update.effective_message.reply_text(
            f"✅ Slot {slot + 1} saved: <code>{shorten(text, 6, 6)}</code>",
            reply_markup=masters_keyboard(state),
            parse_mode=ParseMode.HTML,
        )
        return

    # random text, nothing pending
    await update.effective_message.reply_text(
        "Use /start to open the dashboard."
    )


# /cancel wipes the pending flags and drops you back on the dashboard

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.clear()
    state = await get_state(update.effective_user.id)
    await update.effective_message.reply_text(
        "Cancelled.",
        reply_markup=dashboard_keyboard(state),
        parse_mode=ParseMode.HTML,
    )


# simulated trade engine
# every ~20s we fire a batch of fake solana swaps per active user
# FIRST COME: only the first trade in the batch wins the cap
# EQUAL SPLIT: cap is divided across everyone in the batch

_MINT_POOL = [
    ("So11111111111111111111111111111111111111112", "WSOL"),
    ("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDC"),
    ("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263", "BONK"),
    ("JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN", "JUP"),
    ("7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr", "POPCAT"),
    ("EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm", "WIF"),
]


def _fake_signature() -> str:
    # 64 random bytes base58'd, looks like a real tx sig
    return base58.b58encode(secrets.token_bytes(64)).decode()


def _mint() -> Tuple[str, str]:
    return random.choice(_MINT_POOL)


def _format_exec(mode: str, master: str, mint_sym: str, mint_addr: str, usd: float, sig: str) -> str:
    return (
        f"✅ <b>Trade Executed</b> [{mode}]\n"
        f"Master: <code>{shorten(master, 6, 6)}</code>\n"
        f"Bought: <b>{mint_sym}</b> (<code>{shorten(mint_addr, 6, 6)}</code>)\n"
        f"Size: <b>${usd:.2f}</b>\n"
        f"Sig: <code>{shorten(sig, 8, 8)}</code>"
    )


def _format_skip(master: str, reason: str) -> str:
    return (
        f"⚠️ <b>Trade skipped</b>: {reason}\n"
        f"Master: <code>{shorten(master, 6, 6)}</code>"
    )


# guards the "claim the cap" moment so FIRST COME stays strict
_EXEC_LOCK = asyncio.Lock()


async def _process_trade_batch(bot, user_id: int, masters: List[str], state_snapshot: UserState) -> None:
    # masters = the trades that arrived in the same tick
    if not state_snapshot.auto_trade or not masters:
        return

    cap = state_snapshot.budget_cap
    mode = state_snapshot.allocation_mode
    results: List[str] = []

    if mode == "FIRST COME":
        async with _EXEC_LOCK:
            winner = masters[0]
            mint_addr, mint_sym = _mint()
            sig = _fake_signature()
            results.append(_format_exec("FIRST COME", winner, mint_sym, mint_addr, cap, sig))
            for loser in masters[1:]:
                results.append(_format_skip(loser, "Global Cap Reached"))
    else:
        n = len(masters)
        per = round(cap / n, 2)
        if per <= 0:
            for m in masters:
                results.append(_format_skip(m, "Budget too small"))
        else:
            for m in masters:
                mint_addr, mint_sym = _mint()
                sig = _fake_signature()
                results.append(_format_exec("EQUAL SPLIT", m, mint_sym, mint_addr, per, sig))

    def mut(s: UserState):
        s.last_executions = (s.last_executions + results)[-8:]
    await update_state(user_id, mut)

    text = "\n\n".join(results)
    try:
        await bot.send_message(user_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning("failed to push trade batch to %s: %s", user_id, e)


async def simulated_trade_loop(bot) -> None:
    await asyncio.sleep(5)  # let the app finish booting
    while True:
        try:
            async with DB_LOCK:
                snapshot = {uid: (s, list(s.masters)) for uid, s in DB.items()}

            for user_id, (state, masters_list) in snapshot.items():
                active = [m for m in masters_list if m]
                if not active or not state.auto_trade:
                    continue
                # pick 1..min(3, len) masters to fire this tick
                batch = random.sample(active, random.randint(1, min(3, len(active))))
                # fire and forget so one slow chat doesn't block the rest
                asyncio.create_task(_process_trade_batch(bot, user_id, batch, state))

            await asyncio.sleep(20)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("simulator loop error")
            await asyncio.sleep(5)


# entrypoint

def main() -> None:
    if BOT_TOKEN.startswith("YOUR_"):
        raise SystemExit("set BOT_TOKEN at the top of copy_trade_bot.py before running")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(cb_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    logger.info("bot starting (polling)")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
