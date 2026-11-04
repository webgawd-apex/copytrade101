
#!/usr/bin/env python3

"""Personal-scale Solana Telegram copy bot.
Helius HTTP + Jupiter Swap V2.
Supports USDC-paired swaps only.
"""

import asyncio
import base64
import html
import logging
import os
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

import aiohttp
import asyncpg
import base58

from cryptography.fernet import Fernet
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from telegram import (
    Update,
    InlineKeyboardButton as B,
    InlineKeyboardMarkup as K,
    BotCommand,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

# ====================================================
# CONFIGURATION
# ====================================================

TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
HELIUS = os.getenv("HELIUS_KEY")
DSN = os.getenv("DATABASE_URL")
JUP_KEY = os.getenv("JUPITER_API_KEY", "")

if not all((TOKEN, HELIUS, DSN)):
    raise SystemExit(
        "Set TELEGRAM_TOKEN, HELIUS_KEY, DATABASE_URL"
    )

RPC = f"https://mainnet.helius-rpc.com/?api-key={HELIUS}"

PARSE_URL = (
    "https://api-mainnet.helius-rpc.com/v0/transactions/"
    f"?api-key={HELIUS}"
)

JUP = "https://api.jup.ag/swap/v2"

USDC = (
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
)

POLL_SECONDS = 4
MAX_AGE_SECONDS = 20

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(message)s",
)

LOG = logging.getLogger("copybot")


# ====================================================
# HELPERS
# ====================================================

def wallet_valid(value):
    try:
        decoded = base58.b58decode(value)
        return (
            len(decoded) == 32
            and str(Pubkey.from_string(value)) == value
        )
    except Exception:
        return False


def number(value):
    try:
        result = Decimal(
            value.strip().replace("$", "").replace(",", "")
        )

        if not result.is_finite():
            raise ValueError()

        return result

    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid number.")


def short(value):
    return value[:5] + "…" + value[-5:]


def kb(rows):
    return K([
        [
            B(label, callback_data=callback)
            for label, callback in row
        ]
        for row in rows
    ])


# ====================================================
# LOCAL WALLET ENCRYPTION
# ====================================================

def safe_keyfile():
    path = Path("/var/lib/tg-bot/wallet.key")

    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)

    if not path.exists():
        fd = os.open(
            str(path),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )

        with os.fdopen(fd, "wb") as file:
            file.write(Fernet.generate_key())

    if path.stat().st_mode & 0o077:
        raise RuntimeError(
            "wallet.key must have chmod 600"
        )

    return Fernet(path.read_bytes().strip())


# ====================================================
# POSTGRESQL SCHEMA
# ====================================================

DDL = """
CREATE TABLE IF NOT EXISTS copy_users (
    id BIGINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    max_usd NUMERIC(12,2) NOT NULL DEFAULT 10
        CHECK (max_usd > 0 AND max_usd <= 1000),
    slip_bps INT NOT NULL DEFAULT 100
        CHECK (slip_bps BETWEEN 10 AND 500),
    copy_buys BOOLEAN NOT NULL DEFAULT TRUE,
    copy_sells BOOLEAN NOT NULL DEFAULT FALSE,
    address TEXT,
    encrypted_secret TEXT
);

CREATE TABLE IF NOT EXISTS copy_masters (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES copy_users(id),
    address TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT 'Trader',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE(user_id,address)
);

CREATE TABLE IF NOT EXISTS copy_cursors (
    address TEXT PRIMARY KEY,
    signature TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS copy_trades (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES copy_users(id),
    master_address TEXT NOT NULL,
    master_signature TEXT NOT NULL,
    side TEXT NOT NULL,
    mint TEXT NOT NULL,
    usd NUMERIC(12,2) NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    signature TEXT,
    message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id,master_address,master_signature)
);

CREATE INDEX IF NOT EXISTS copy_trades_user_time
ON copy_trades(user_id,created_at DESC);
"""


# ====================================================
# MAIN BOT SERVICE
# ====================================================

class BotService:

    def __init__(self):
        self.pool = None
        self.session = None
        self.fernet = None
        self.app = None
        self.watcher = None

        self.orders_lock = asyncio.Lock()
        self.rpc_sem = asyncio.Semaphore(4)
        self.busy = set()

    # --------------------------------------------
    # STARTUP AND SHUTDOWN
    # --------------------------------------------

    async def startup(self, app):
        self.app = app
        self.fernet = safe_keyfile()

        self.pool = await asyncpg.create_pool(
            DSN,
            min_size=1,
            max_size=4,
            command_timeout=15,
        )

        await self.pool.execute(DDL)

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=18)
        )

        self.watcher = asyncio.create_task(
            self.watch_loop(),
            name="helius-monitor",
        )

        await app.bot.set_my_commands([
            BotCommand("start", "Home"),
            BotCommand("wallet", "Your trading wallet"),
            BotCommand("settings", "Trade settings"),
            BotCommand("masters", "Master wallets"),
            BotCommand("history", "Trade history"),
        ])

        LOG.info("Solana copy bot started")

    async def shutdown(self, app):
        if self.watcher:
            self.watcher.cancel()

            await asyncio.gather(
                self.watcher,
                return_exceptions=True,
            )

        if self.session:
            await self.session.close()

        if self.pool:
            await self.pool.close()

    # --------------------------------------------
    # SOLANA RPC
    # --------------------------------------------

    async def rpc(self, method, params):
        async with self.rpc_sem:
            async with self.session.post(
                RPC,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
            ) as response:

                response.raise_for_status()
                data = await response.json()

                if data.get("error"):
                    raise RuntimeError(
                        f"RPC {method}: {data['error']}"
                    )

                return data.get("result")

    # --------------------------------------------
    # DATABASE
    # --------------------------------------------

    async def user(self, uid):
        await self.pool.execute(
            """
            INSERT INTO copy_users(id)
            VALUES($1)
            ON CONFLICT DO NOTHING
            """,
            uid,
        )

        return await self.pool.fetchrow(
            "SELECT * FROM copy_users WHERE id=$1",
            uid,
        )

    async def masters(self, uid):
        return await self.pool.fetch(
            """
            SELECT * FROM copy_masters
            WHERE user_id=$1
            ORDER BY id
            """,
            uid,
        )

    # --------------------------------------------
    # TELEGRAM SCREENS
    # --------------------------------------------

    async def render(self, uid, page="home"):
        u = await self.user(uid)

        if page == "home":
            masters = await self.masters(uid)

            status = "🟢 ON" if u["enabled"] else "⏸ OFF"

            address = (
                short(u["address"])
                if u["address"]
                else "Not created"
            )

            slippage = Decimal(u["slip_bps"]) / 100

            text = (
                "🤖 <b>Solana Copy Trader</b>\n\n"
                f"Auto trade: <b>{status}</b>\n"
                f"Masters: <b>{len(masters)}/3</b>\n"
                f"Max trade: <b>${u['max_usd']:.2f}</b>\n"
                f"Slippage: <b>{slippage}%</b>\n"
                f"Trading wallet: <b>{address}</b>\n\n"
                "<i>Copies supported USDC-paired swaps. "
                "Fund your wallet with USDC and SOL.</i>"
            )

            buttons = [
                [(
                    "⏸ Pause" if u["enabled"]
                    else "▶️ Enable Auto Trade",
                    "toggle",
                )],
                [
                    ("👀 Master Wallets", "masters"),
                    ("⚙️ Settings", "settings"),
                ],
                [
                    ("👛 My Wallet", "wallet"),
                    ("📜 History", "history"),
                ],
                [("🔄 Refresh", "home")],
            ]

            return text, kb(buttons)

        if page == "settings":
            slippage = Decimal(u["slip_bps"]) / 100

            text = (
                "⚙️ <b>Trading Settings</b>\n\n"
                f"Max trade: <b>${u['max_usd']:.2f}</b>\n"
                f"Slippage: <b>{slippage}%</b>\n"
                f"Copy buys: <b>{'ON' if u['copy_buys'] else 'OFF'}</b>\n"
                f"Copy sells: <b>{'ON' if u['copy_sells'] else 'OFF'}</b>\n\n"
                "Sell signals sell the entire matching token "
                "balance in your bot wallet."
            )

            return text, kb([
                [
                    ("💵 Max Trade", "edit:max"),
                    ("📉 Slippage", "edit:slip"),
                ],
                [
                    ("Toggle Buys", "toggle:buy"),
                    ("Toggle Sells", "toggle:sell"),
                ],
                [("🏠 Home", "home")],
            ])

        if page == "masters":
            masters = await self.masters(uid)

            lines = ["👀 <b>Master Wallets</b>"]
            rows = []

            for master in masters:
                status = (
                    "🟢" if master["active"] else "⏸"
                )

                lines.append(
                    f"{status} "
                    f"{html.escape(master['name'])} — "
                    f"<code>{short(master['address'])}</code>"
                )

                action = (
                    "Pause" if master["active"]
                    else "Resume"
                )

                rows.append([
                    (
                        f"{action} {master['name'][:14]}",
                        f"mpause:{master['id']}",
                    ),
                    (
                        "🗑 Remove",
                        f"remove:{master['id']}",
                    ),
                ])

            if len(masters) < 3:
                rows.append([
                    ("➕ Add Master", "add")
                ])

            rows.append([
                ("🏠 Home", "home")
            ])

            return "\n".join(lines), kb(rows)

        if page == "wallet":
            if not u["address"]:
                return (
                    "👛 <b>My Trading Wallet</b>\n\n"
                    "Create a dedicated hot wallet.\n"
                    "Keep only small amounts in it.\n\n"
                    "Private keys are encrypted on the VPS.",
                    kb([
                        [("➕ Create Wallet", "create")],
                        [("🏠 Home", "home")],
                    ]),
                )

            try:
                sol = await self.rpc(
                    "getBalance",
                    [u["address"]],
                )

                sol_balance = (
                    Decimal(sol["value"])
                    / Decimal(10**9)
                )

                usdc_raw, usdc_decimals = (
                    await self.token_balance(
                        u["address"],
                        USDC,
                    )
                )

                usdc_balance = (
                    Decimal(usdc_raw)
                    / Decimal(10**usdc_decimals)
                )

                sol_display = f"{sol_balance:.5f}"
                usdc_display = f"{usdc_balance:.2f}"

            except Exception:
                sol_display = "unavailable"
                usdc_display = "unavailable"

            text = (
                "👛 <b>My Trading Wallet</b>\n\n"
                f"<code>{u['address']}</code>\n\n"
                f"SOL: <b>{sol_display}</b>\n"
                f"USDC: <b>${usdc_display}</b>\n\n"
                "Deposit USDC for buys and SOL for fees.\n"
                "<i>Use only a dedicated small-balance wallet. "
                "Back up its secret using the VPS console.</i>"
            )

            return text, kb([
                [("🏠 Home", "home")]
            ])

        if page == "history":
            trades = await self.pool.fetch(
                """
                SELECT * FROM copy_trades
                WHERE user_id=$1
                ORDER BY id DESC
                LIMIT 8
                """,
                uid,
            )

            lines = [
                "📜 <b>Recent Trades</b>"
            ]

            for trade in trades:
                link = ""

                if trade["signature"]:
                    link = (
                        '<a href="https://solscan.io/tx/'
                        f'{trade["signature"]}">View TX</a>'
                    )

                lines.append(
                    f'{trade["side"]} '
                    f'{short(trade["mint"])} '
                    f'${trade["usd"]:.2f} — '
                    f'{html.escape(trade["status"])} '
                    f'{link}'
                )

            if not trades:
                lines.append("No trades yet.")

            return "\n".join(lines), kb([
                [("🏠 Home", "home")]
            ])

        return await self.render(uid, "home")

    # --------------------------------------------
    # MESSAGE DISPLAY
    # --------------------------------------------

    async def show(self, update, page="home", notice=""):
        uid = update.effective_user.id

        text, buttons = await self.render(
            uid,
            page,
        )

        if notice:
            text = html.escape(notice) + "\n\n" + text

        q = update.callback_query

        if q:
            try:
                await q.edit_message_text(
                    text,
                    reply_markup=buttons,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )

            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    raise

        else:
            await update.message.reply_text(
                text,
                reply_markup=buttons,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    async def start(self, update, ctx):
        if update.effective_chat.type != "private":
            return

        ctx.user_data.clear()

        await self.show(update)

    # --------------------------------------------
    # INLINE BUTTON HANDLERS
    # --------------------------------------------

    async def click(self, update, ctx):
        q = update.callback_query
        await q.answer()

        if update.effective_chat.type != "private":
            return

        uid = update.effective_user.id
        data = q.data or ""
        u = await self.user(uid)

        if data in (
            "home",
            "settings",
            "masters",
            "wallet",
            "history",
        ):
            return await self.show(update, data)

        if data == "create":
            if u["address"]:
                return await self.show(
                    update,
                    "wallet",
                    "Wallet already exists.",
                )

            key = Keypair()
            encrypted = self.fernet.encrypt(
                bytes(key)
            ).decode()

            await self.pool.execute(
                """
                UPDATE copy_users
                SET address=$2, encrypted_secret=$3
                WHERE id=$1 AND address IS NULL
                """,
                uid,
                str(key.pubkey()),
                encrypted,
            )

            return await self.show(
                update,
                "wallet",
                "Wallet created. Back up its secret "
                "using the VPS console.",
            )

        if data == "toggle":
            if not u["enabled"]:
                if (
                    not u["encrypted_secret"]
                    or not await self.masters(uid)
                ):
                    return await self.show(
                        update,
                        "home",
                        "Create a trading wallet and "
                        "add a master first.",
                    )

            await self.pool.execute(
                """
                UPDATE copy_users
                SET enabled=NOT enabled
                WHERE id=$1
                """,
                uid,
            )

            return await self.show(
                update,
                "home",
                "Auto trade setting updated.",
            )

        if data.startswith("toggle:"):
            option = data.split(":")[1]

            column = {
                "buy": "copy_buys",
                "sell": "copy_sells",
            }.get(option)

            if column:
                await self.pool.execute(
                    f"""
                    UPDATE copy_users
                    SET {column}=NOT {column}
                    WHERE id=$1
                    """,
                    uid,
                )

            return await self.show(
                update,
                "settings",
            )

        if data.startswith("edit:"):
            option = data.split(":")[1]
            ctx.user_data["pending"] = option

            if option == "max":
                prompt = (
                    "Enter maximum USD per trade "
                    "(0.50–1000)."
                )
            else:
                prompt = (
                    "Enter slippage percentage "
                    "(0.1–5)."
                )

            return await q.edit_message_text(
                prompt,
                reply_markup=kb([
                    [("Cancel", "home")]
                ]),
            )

        if data == "add":
            if len(await self.masters(uid)) >= 3:
                return await self.show(
                    update,
                    "masters",
                    "Three-wallet limit reached.",
                )

            ctx.user_data["pending"] = "master"

            return await q.edit_message_text(
                "Paste the master's PUBLIC "
                "Solana wallet address:",
                reply_markup=kb([
                    [("Cancel", "masters")]
                ]),
            )

        if data.startswith("mpause:"):
            mid = int(data.split(":")[1])

            await self.pool.execute(
                """
                UPDATE copy_masters
                SET active=NOT active
                WHERE id=$1 AND user_id=$2
                """,
                mid,
                uid,
            )

            return await self.show(
                update,
                "masters",
            )

        if data.startswith("remove:"):
            mid = int(data.split(":")[1])

            master = await self.pool.fetchrow(
                """
                SELECT * FROM copy_masters
                WHERE id=$1 AND user_id=$2
                """,
                mid,
                uid,
            )

            if not master:
                return await self.show(
                    update,
                    "masters",
                    "Already removed.",
                )

            text = (
                "Are you sure you want to remove "
                f"<b>{html.escape(master['name'])}</b> "
                f"({short(master['address'])})?"
            )

            return await q.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb([
                    [
                        (
                            "✅ Yes, Remove",
                            f"confirm:{mid}",
                        )
                    ],
                    [
                        (
                            "❌ No, Keep",
                            "masters",
                        )
                    ],
                ]),
            )

        if data.startswith("confirm:"):
            mid = int(data.split(":")[1])

            await self.pool.execute(
                """
                DELETE FROM copy_masters
                WHERE id=$1 AND user_id=$2
                """,
                mid,
                uid,
            )

            return await self.show(
                update,
                "masters",
                "Master removed.",
            )

        await self.show(update, "home")

    # --------------------------------------------
    # TEXT INPUT HANDLERS
    # --------------------------------------------

    async def text(self, update, ctx):
        if update.effective_chat.type != "private":
            return

        uid = update.effective_user.id
        pending = ctx.user_data.get("pending")
        value = update.message.text.strip()

        if not pending:
            return await self.show(update)

        try:
            if pending == "master":
                if not wallet_valid(value):
                    raise ValueError(
                        "Invalid Solana wallet address."
                    )

                if len(await self.masters(uid)) >= 3:
                    raise ValueError(
                        "Already at three wallets."
                    )

                ctx.user_data["pending"] = (
                    "nickname",
                    value,
                )

                return await update.message.reply_text(
                    "Give this master a nickname "
                    "(maximum 24 characters):"
                )

            if (
                isinstance(pending, tuple)
                and pending[0] == "nickname"
            ):
                nickname = value[:24]

                if not nickname:
                    raise ValueError(
                        "Nickname cannot be empty."
                    )

                await self.pool.execute(
                    """
                    INSERT INTO copy_masters
                    (user_id,address,name)
                    VALUES($1,$2,$3)
                    """,
                    uid,
                    pending[1],
                    nickname,
                )

                ctx.user_data.pop("pending", None)

                return await self.show(
                    update,
                    "masters",
                    "Master added.",
                )

            amount = number(value)

            if pending == "max":
                if not (
                    Decimal(".50")
                    <= amount
                    <= Decimal("1000")
                ):
                    raise ValueError(
                        "Max trade must be $0.50–$1,000."
                    )

                await self.pool.execute(
                    """
                    UPDATE copy_users
                    SET max_usd=$2
                    WHERE id=$1
                    """,
                    uid,
                    amount.quantize(Decimal(".01")),
                )

            elif pending == "slip":
                if not (
                    Decimal(".1")
                    <= amount
                    <= Decimal("5")
                ):
                    raise ValueError(
                        "Slippage must be 0.1–5%."
                    )

                await self.pool.execute(
                    """
                    UPDATE copy_users
                    SET slip_bps=$2
                    WHERE id=$1
                    """,
                    uid,
                    int(amount * 100),
                )

            ctx.user_data.pop("pending", None)

            await self.show(
                update,
                "settings",
                "Saved.",
            )

        except (
            ValueError,
            asyncpg.UniqueViolationError,
        ) as exc:
            await update.message.reply_text(
                "❌ "
                + html.escape(str(exc))
                + " Try again or /cancel."
            )

    async def cancel(self, update, ctx):
        ctx.user_data.clear()

        await self.show(
            update,
            "home",
            "Cancelled.",
        )

    # ====================================================
    # TOKEN BALANCES
    # ====================================================

    async def token_balance(self, address, mint):
        response = await self.rpc(
            "getTokenAccountsByOwner",
            [
                address,
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        )

        accounts = (
            response.get("value", [])
            if response
            else []
        )

        if not accounts:
            decimals = 6 if mint == USDC else None
            return 0, decimals

        amounts = [
            account["account"]["data"]["parsed"]["info"]
            ["tokenAmount"]
            for account in accounts
        ]

        total = sum(
            int(token["amount"])
            for token in amounts
        )

        return total, int(amounts[0]["decimals"])

    # ====================================================
    # JUPITER SWAP V2
    # ====================================================

    async def jup_order(
        self,
        input_mint,
        output_mint,
        raw_amount,
        wallet,
        slippage_bps,
    ):
        headers = (
            {"x-api-key": JUP_KEY}
            if JUP_KEY
            else {}
        )

        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(raw_amount),
            "taker": wallet,
            "slippageBps": str(slippage_bps),
        }

        async with self.session.get(
            JUP + "/order",
            params=params,
            headers=headers,
        ) as response:

            body = await response.json(
                content_type=None
            )

            if (
                response.status != 200
                or not body.get("transaction")
            ):
                raise RuntimeError(
                    "Jupiter order unavailable: "
                    + str(
                        body.get("errorMessage")
                        or response.status
                    )
                )

            return body

    async def jup_execute(self, order, keypair):
        raw = base64.b64decode(
            order["transaction"]
        )

        transaction = (
            VersionedTransaction.from_bytes(raw)
        )

        required = (
            transaction.message.header
            .num_required_signatures
        )

        signers = list(
            transaction.message.account_keys[:required]
        )

        address = keypair.pubkey()

        if address not in signers:
            raise RuntimeError(
                "Trading wallet is not an expected signer"
            )

        signatures = list(
            transaction.signatures
        )

        signatures[
            signers.index(address)
        ] = keypair.sign_message(
            bytes(transaction.message)
        )

        signed = VersionedTransaction.populate(
            transaction.message,
            signatures,
        )

        payload = {
            "signedTransaction": base64.b64encode(
                bytes(signed)
            ).decode(),
            "requestId": order["requestId"],
        }

        headers = (
            {"x-api-key": JUP_KEY}
            if JUP_KEY
            else {}
        )

        async with self.session.post(
            JUP + "/execute",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=35),
        ) as response:

            result = await response.json(
                content_type=None
            )

            if response.status != 200:
                raise RuntimeError(
                    "Jupiter execution HTTP "
                    + str(response.status)
                )

            return result

    # ====================================================
    # MASTER SWAP CLASSIFICATION
    # ====================================================

    def parse_swap(self, tx):
        if not tx or tx.get("type") != "SWAP":
            return None

        swap = (
            (tx.get("events") or {}).get("swap")
            or {}
        )

        inputs = swap.get("tokenInputs") or []
        outputs = swap.get("tokenOutputs") or []

        if len(inputs) != 1 or len(outputs) != 1:
            return None

        source = inputs[0].get("mint")
        destination = outputs[0].get("mint")

        if not source or not destination:
            return None

        if source == USDC and destination != USDC:
            side = "BUY"
            mint = destination

        elif destination == USDC and source != USDC:
            side = "SELL"
            mint = source

        else:
            return None

        if not wallet_valid(mint):
            return None

        return side, mint

    # ====================================================
    # NOTIFICATIONS
    # ====================================================

    async def notice(self, uid, text):
        try:
            await self.app.bot.send_message(
                uid,
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

        except (Forbidden, BadRequest):
            pass

        except RetryAfter:
            LOG.warning(
                "Telegram rate limited; notification omitted"
            )

        except Exception:
            LOG.exception(
                "Telegram notification failed"
            )

    # ====================================================
    # AUTOMATIC COPY EXECUTION
    # ====================================================

    async def handle_trade(
        self,
        uid,
        master,
        master_signature,
        side,
        mint,
        observed_time,
    ):
        # Deduplicate each copied signal in PostgreSQL.

        row = await self.pool.fetchrow(
            """
            INSERT INTO copy_trades
            (user_id,master_address,master_signature,
             side,mint,status)
            VALUES($1,$2,$3,$4,$5,'QUEUED')
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            uid,
            master,
            master_signature,
            side,
            mint,
        )

        if not row:
            return

        trade_id = row["id"]
        signature = None

        try:
            user = await self.user(uid)

            if time.time() - observed_time > MAX_AGE_SECONDS:
                raise ValueError("Stale signal")

            if not user["enabled"]:
                raise ValueError("Auto trading disabled")

            if side == "BUY" and not user["copy_buys"]:
                raise ValueError("Copy buys disabled")

            if side == "SELL" and not user["copy_sells"]:
                raise ValueError("Copy sells disabled")

            keypair = Keypair.from_bytes(
                self.fernet.decrypt(
                    user["encrypted_secret"].encode()
                )
            )

            if side == "BUY":
                balance, _ = await self.token_balance(
                    user["address"],
                    USDC,
                )

                max_raw = int(
                    Decimal(user["max_usd"])
                    * Decimal(10**6)
                )

                amount = min(max_raw, balance)

                if amount < 500000:
                    raise ValueError(
                        "Insufficient USDC (minimum $0.50)"
                    )

                input_mint = USDC
                output_mint = mint
                usd = Decimal(amount) / Decimal(10**6)

            else:
                amount, decimals = (
                    await self.token_balance(
                        user["address"],
                        mint,
                    )
                )

                if not amount or decimals is None:
                    raise ValueError(
                        "No tokens to sell"
                    )

                input_mint = mint
                output_mint = USDC
                usd = Decimal(0)

            # Persist intent before attempting a swap.
            # Never blindly retry an uncertain submission.

            await self.pool.execute(
                """
                UPDATE copy_trades
                SET status=$2,usd=$3
                WHERE id=$1
                """,
                trade_id,
                "SUBMITTING",
                usd,
            )

            # Serialized Jupiter requests are appropriate
            # for a small personal bot using keyless access.

            async with self.orders_lock:
                order = await self.jup_order(
                    input_mint,
                    output_mint,
                    amount,
                    user["address"],
                    user["slip_bps"],
                )

                result = await self.jup_execute(
                    order,
                    keypair,
                )

                if not JUP_KEY:
                    await asyncio.sleep(2.1)

            signature = result.get("signature")

            if (
                result.get("status") != "Success"
                or not signature
            ):
                raise RuntimeError(
                    "Swap not confirmed by Jupiter: "
                    + str(
                        result.get("error")
                        or result.get("status")
                    )
                )

            await self.pool.execute(
                """
                UPDATE copy_trades
                SET status=$2,signature=$3
                WHERE id=$1
                """,
                trade_id,
                "CONFIRMED",
                signature,
            )

            await self.notice(
                uid,
                f"✅ <b>Copied {side}</b> "
                f"{short(mint)}\n"
                '<a href="https://solscan.io/tx/'
                f'{signature}">View transaction</a>',
            )

        except ValueError as exc:
            await self.pool.execute(
                """
                UPDATE copy_trades
                SET status=$2,message=$3
                WHERE id=$1
                """,
                trade_id,
                "SKIPPED",
                str(exc)[:300],
            )

            await self.notice(
                uid,
                f"⚠️ Skipped {side} "
                f"{short(mint)}: "
                f"{html.escape(str(exc))}",
            )

        except Exception as exc:
            # An execution timeout may occur after
            # the transaction was submitted.
            # Its outcome is unknown, not safe to retry.

            await self.pool.execute(
                """
                UPDATE copy_trades
                SET status=$2,signature=$3,message=$4
                WHERE id=$1
                """,
                trade_id,
                "UNKNOWN",
                signature,
                str(exc)[:300],
            )

            LOG.exception(
                "Copy trade outcome uncertain: %s",
                trade_id,
            )

            await self.notice(
                uid,
                "⚠️ Trade outcome uncertain. "
                "Check your wallet and Solscan "
                "before attempting another trade.",
            )

    # ====================================================
    # HELIUS WALLET MONITORING — HTTP ONLY
    # ====================================================

    async def poll_wallet(self, address):
        previous = await self.pool.fetchval(
            """
            SELECT signature FROM copy_cursors
            WHERE address=$1
            """,
            address,
        )

        rows = await self.rpc(
            "getSignaturesForAddress",
            [
                address,
                {
                    "limit": 20,
                    "commitment": "confirmed",
                },
            ],
        )

        if not rows:
            return

        newest = rows[0]["signature"]

        # First observation establishes a baseline.
        # We do not copy historical transactions.

        if previous is None:
            await self.pool.execute(
                """
                INSERT INTO copy_cursors(address,signature)
                VALUES($1,$2)
                ON CONFLICT DO NOTHING
                """,
                address,
                newest,
            )
            return

        fresh = []

        for item in rows:
            if item["signature"] == previous:
                break

            age = (
                time.time() - item["blockTime"]
                if item.get("blockTime")
                else float("inf")
            )

            if not item.get("err") and age < MAX_AGE_SECONDS:
                fresh.append(item["signature"])

        found_cursor = any(
            item["signature"] == previous
            for item in rows
        )

        if not found_cursor:
            LOG.warning(
                "High activity or history gap for %s",
                short(address),
            )

            await self.pool.execute(
                """
                UPDATE copy_cursors
                SET signature=$2
                WHERE address=$1
                """,
                address,
                newest,
            )
            return

        for signature in reversed(fresh):
            async with self.session.post(
                PARSE_URL,
                json={"transactions": [signature]},
            ) as response:

                response.raise_for_status()
                parsed = await response.json()

            if not parsed:
                continue

            transaction = parsed[0]

            # Conservatively require the monitored
            # wallet to be the fee payer.

            if transaction.get("feePayer") != address:
                continue

            swap = self.parse_swap(transaction)

            if not swap:
                continue

            side, mint = swap

            followers = await self.pool.fetch(
                """
                SELECT m.user_id
                FROM copy_masters m
                JOIN copy_users u ON u.id=m.user_id
                WHERE m.address=$1
                  AND m.active
                  AND u.enabled
                """,
                address,
            )

            item = next(
                (
                    row
                    for row in rows
                    if row["signature"] == signature
                ),
                None,
            )

            observed = (
                item["blockTime"]
                if item and item.get("blockTime")
                else time.time()
            )

            for follower in followers:
                uid = follower["user_id"]

                if uid in self.busy:
                    continue

                self.busy.add(uid)

                try:
                    await self.handle_trade(
                        uid,
                        address,
                        signature,
                        side,
                        mint,
                        observed,
                    )

                finally:
                    self.busy.discard(uid)

        await self.pool.execute(
            """
            UPDATE copy_cursors
            SET signature=$2
            WHERE address=$1
            """,
            address,
            newest,
        )

    async def watch_loop(self):
        while True:
            try:
                masters = await self.pool.fetch(
                    """
                    SELECT DISTINCT m.address
                    FROM copy_masters m
                    JOIN copy_users u ON u.id=m.user_id
                    WHERE m.active AND u.enabled
                    """
                )

                for master in masters:
                    try:
                        await self.poll_wallet(
                            master["address"]
                        )

                    except Exception:
                        LOG.exception(
                            "Wallet monitoring failed for %s",
                            short(master["address"]),
                        )

            except asyncio.CancelledError:
                raise

            except Exception:
                LOG.exception(
                    "Monitoring loop error"
                )

            await asyncio.sleep(POLL_SECONDS)


# ====================================================
# WALLET RECOVERY THROUGH THE VPS CONSOLE
# ====================================================

async def export_wallet_cli(uid):
    pool = await asyncpg.create_pool(
        DSN,
        min_size=1,
        max_size=1,
    )

    try:
        row = await pool.fetchrow(
            """
            SELECT address,encrypted_secret
            FROM copy_users
            WHERE id=$1
            """,
            uid,
        )

        if not row or not row["encrypted_secret"]:
            raise SystemExit(
                "No wallet found for this user ID"
            )

        keypair = Keypair.from_bytes(
            safe_keyfile().decrypt(
                row["encrypted_secret"].encode()
            )
        )

        print("Wallet:", row["address"])
        print("PRIVATE KEY — KEEP SECRET:", str(keypair))

    finally:
        await pool.close()


# ====================================================
# MAIN ENTRY POINT
# ====================================================

def main():
    if (
        len(sys.argv) == 3
        and sys.argv[1] == "--export-wallet"
    ):
        asyncio.run(
            export_wallet_cli(int(sys.argv[2]))
        )
        return

    service = BotService()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(service.startup)
        .post_shutdown(service.shutdown)
        .build()
    )

    app.add_handler(
        CommandHandler(
            ["start", "menu"],
            service.start,
        )
    )

    for command, page in (
        ("wallet", "wallet"),
        ("settings", "settings"),
        ("masters", "masters"),
        ("history", "history"),
    ):
        async def open_page(update, ctx, target=page):
            if update.effective_chat.type != "private":
                return
            await service.show(update, target)

        app.add_handler(
            CommandHandler(command, open_page)
        )

    app.add_handler(
        CommandHandler("cancel", service.cancel)
    )

    app.add_handler(
        CallbackQueryHandler(service.click)
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            service.text,
        )
    )

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
