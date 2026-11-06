
"""Solana Telegram Copy Trader — single-file application."""

import asyncio
import base64
import html
import json
import logging
import os
import secrets
import struct
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import aiohttp
import asyncpg
import base58
from cryptography.fernet import Fernet
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.hash import Hash
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction, Transaction
from solders.instruction import Instruction, AccountMeta
from solders.system_program import transfer, TransferParams, ID as SYSTEM_PROGRAM_ID
from telegram import (
    InlineKeyboardButton as B,
    InlineKeyboardMarkup as K,
    BotCommand,
    CopyTextButton,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
LOG = logging.getLogger("copybot")

TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
DSN = os.getenv("DATABASE_URL")
JUP_KEY = os.getenv("JUPITER_API_KEY", "")
RPC = os.getenv("HELIUS_RPC_URL", "").strip()

if not TOKEN or not DSN or not RPC:
    raise SystemExit(
        "Set TELEGRAM_TOKEN, DATABASE_URL and HELIUS_RPC_URL"
    )

parsed_rpc = urlsplit(RPC)

if (
    parsed_rpc.scheme != "https"
    or parsed_rpc.hostname != "mainnet.helius-rpc.com"
    or not parse_qs(parsed_rpc.query).get("api-key", [""])[0]
):
    raise SystemExit(
        "HELIUS_RPC_URL must be a valid Helius mainnet HTTPS RPC URL"
    )

JUP = "https://api.jup.ag/swap/v2"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"

QUOTE_MINTS = {USDC, WSOL}
MIN_SOL_RESERVE = 10_000_000
MAX_SLIPPAGE_BPS = 7000

TOKEN_PROGRAM = Pubkey.from_string(
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
)
ATA_PROGRAM = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
USDC_MINT = Pubkey.from_string(USDC)

DDL = """
CREATE TABLE IF NOT EXISTS copy_users (
    id BIGINT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    max_usd NUMERIC(12,2) NOT NULL DEFAULT 10,
    slip_bps INTEGER NOT NULL DEFAULT 2000,
    copy_buys BOOLEAN NOT NULL DEFAULT TRUE,
    copy_sells BOOLEAN NOT NULL DEFAULT FALSE,
    address TEXT,
    encrypted_secret TEXT,
    poll_interval_seconds NUMERIC(4,1) NOT NULL DEFAULT 1.5,
    max_signal_age_seconds INTEGER NOT NULL DEFAULT 90,
    allow_usdc_fallback BOOLEAN NOT NULL DEFAULT FALSE
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

CREATE TABLE IF NOT EXISTS copy_wallets (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES copy_users(id),
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    encrypted_secret TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id,address)
);

CREATE TABLE IF NOT EXISTS copy_secret_messages (
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(chat_id,message_id)
);

CREATE TABLE IF NOT EXISTS copy_withdrawals (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES copy_users(id),
    wallet_address TEXT NOT NULL,
    asset TEXT NOT NULL,
    destination TEXT NOT NULL,
    amount TEXT NOT NULL,
    signature TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def keyboard(rows):
    buttons = []

    for row in rows:
        line = []

        for item in row:
            if len(item) == 3:
                line.append(
                    B(
                        item[0],
                        copy_text=CopyTextButton(text=item[2]),
                    )
                )
            elif item[1].startswith("url:"):
                line.append(
                    B(item[0], url=item[1][4:])
                )
            else:
                line.append(
                    B(item[0], callback_data=item[1])
                )

        buttons.append(line)

    return K(buttons)


def valid_address(value):
    try:
        return (
            len(base58.b58decode(value)) == 32
            and str(Pubkey.from_string(value)) == value
        )
    except Exception:
        return False


def amount_number(value):
    try:
        n = Decimal(
            value.strip().replace(",", "").replace("$", "")
        )

        if not n.is_finite():
            raise ValueError()

        return n
    except (ValueError, InvalidOperation):
        raise ValueError("Enter a valid number.")


def shortened(value):
    return value[:5] + "…" + value[-5:]


def import_key(value):
    try:
        value = value.strip()

        if value.startswith("["):
            data = json.loads(value)

            if not isinstance(data, list) or len(data) != 64:
                raise ValueError()

            raw = bytes(data)
        else:
            raw = base58.b58decode(value)

        if len(raw) == 64:
            return Keypair.from_bytes(raw)

        if len(raw) == 32:
            return Keypair.from_seed(raw)

    except Exception:
        pass

    raise ValueError(
        "Invalid private key. Use a Solana base58 secret "
        "or 64-byte JSON array."
    )


def encryption_key():
    path = Path("/var/lib/tg-bot/wallet.key")

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=0o700,
    )

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
            "Wallet encryption key permissions are unsafe."
        )

    return Fernet(path.read_bytes().strip())


def associated_token(owner):
    return Pubkey.find_program_address(
        [
            bytes(owner),
            bytes(TOKEN_PROGRAM),
            bytes(USDC_MINT),
        ],
        ATA_PROGRAM,
    )[0]


def usdc_transfer_instructions(sender, recipient, raw_amount):
    source = associated_token(sender)
    destination = associated_token(recipient)

    create_ata = Instruction(
        ATA_PROGRAM,
        bytes([1]),
        [
            AccountMeta(sender, True, True),
            AccountMeta(destination, False, True),
            AccountMeta(recipient, False, False),
            AccountMeta(USDC_MINT, False, False),
            AccountMeta(SYSTEM_PROGRAM_ID, False, False),
            AccountMeta(TOKEN_PROGRAM, False, False),
        ],
    )

    transfer_checked = Instruction(
        TOKEN_PROGRAM,
        struct.pack("<BQB", 12, raw_amount, 6),
        [
            AccountMeta(source, False, True),
            AccountMeta(USDC_MINT, False, False),
            AccountMeta(destination, False, True),
            AccountMeta(sender, True, False),
        ],
    )

    return [create_ata, transfer_checked]


class BotService:

    def __init__(self):
        self.pool = None
        self.session = None
        self.fernet = None
        self.app = None

        self.monitor_task = None
        self.cleanup_task = None

        self.lock = asyncio.Lock()
        self.rpc_limit = asyncio.Semaphore(4)

        self.user_locks = {}
        self.price_cache = (0.0, None)
        self.inflight = set()

    async def startup(self, app):
        if not JUP_KEY:
            raise RuntimeError(
                "JUPITER_API_KEY is required for this live-only bot"
            )

        self.app = app
        self.fernet = encryption_key()

        self.pool = await asyncpg.create_pool(
            DSN,
            min_size=1,
            max_size=4,
        )

        await self.pool.execute(DDL)

        for sql in (
            "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS "
            "poll_interval_seconds NUMERIC(4,1) NOT NULL DEFAULT 1.5",

            "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS "
            "max_signal_age_seconds INTEGER NOT NULL DEFAULT 90",

            "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS "
            "allow_usdc_fallback BOOLEAN NOT NULL DEFAULT FALSE",
        ):
            await self.pool.execute(sql)

        await self.pool.execute("""
            INSERT INTO copy_wallets(
                user_id,
                name,
                address,
                encrypted_secret
            )
            SELECT
                id,
                'Wallet 1',
                address,
                encrypted_secret
            FROM copy_users
            WHERE address IS NOT NULL
              AND encrypted_secret IS NOT NULL
            ON CONFLICT(user_id,address) DO NOTHING
        """)

        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=18)
        )

        await self.reconcile_trades()

        await self.pool.execute("""
            UPDATE copy_users
            SET enabled=FALSE
            WHERE id IN (
                SELECT user_id
                FROM copy_trades
                WHERE status IN (
                    'UNKNOWN',
                    'SUBMITTING',
                    'SUBMITTED'
                )
            )
        """)

        await self.pool.execute("""
            UPDATE copy_trades
            SET
                status='SKIPPED',
                message='Restart interrupted signal before submission'
            WHERE status IN ('QUEUED','QUOTING')
        """)

        self.monitor_task = asyncio.create_task(
            self.monitor()
        )

        self.cleanup_task = asyncio.create_task(
            self.cleanup_keys()
        )

        await app.bot.set_my_commands([
            BotCommand("start", "Open dashboard"),
            BotCommand("wallet", "Manage wallets"),
            BotCommand("send", "Send funds"),
            BotCommand("masters", "Master wallets"),
            BotCommand("settings", "Trading settings"),
            BotCommand("history", "Trade history"),
            BotCommand("cancel", "Cancel action"),
        ])

        LOG.info("Solana copy bot started in LIVE-only mode")

    async def shutdown(self, app):
        for task in (
            self.monitor_task,
            self.cleanup_task,
        ):
            if task:
                task.cancel()
                await asyncio.gather(
                    task,
                    return_exceptions=True,
                )

        if self.inflight:
            _, pending = await asyncio.wait(
                self.inflight,
                timeout=20,
            )

            for task in pending:
                task.cancel()

            if pending:
                await asyncio.gather(
                    *pending,
                    return_exceptions=True,
                )

        if self.session:
            await self.session.close()

        if self.pool:
            await self.pool.close()

    async def rpc(self, method, params):
        async with self.rpc_limit:
            async with self.session.post(
                RPC,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
            ) as response:

                if response.status != 200:
                    raise RuntimeError(
                        f"Helius RPC {method} returned "
                        f"HTTP {response.status}"
                    )

                data = await response.json()

                if data.get("error"):
                    error = data["error"]
                    raise RuntimeError(
                        f"Helius RPC {method} failed "
                        f"(code {error.get('code', 'unknown')})"
                    )

                return data.get("result")

    async def user(self, uid):
        await self.pool.execute(
            "INSERT INTO copy_users(id) VALUES($1) "
            "ON CONFLICT DO NOTHING",
            uid,
        )

        return await self.pool.fetchrow(
            "SELECT * FROM copy_users WHERE id=$1",
            uid,
        )

    async def wallets(self, uid):
        return await self.pool.fetch(
            "SELECT id,name,address FROM copy_wallets "
            "WHERE user_id=$1 ORDER BY id",
            uid,
        )

    async def wallet(self, uid, wid):
        return await self.pool.fetchrow(
            "SELECT * FROM copy_wallets "
            "WHERE id=$1 AND user_id=$2",
            wid,
            uid,
        )

    async def masters(self, uid):
        return await self.pool.fetch(
            "SELECT * FROM copy_masters "
            "WHERE user_id=$1 ORDER BY id",
            uid,
        )

    async def save_wallet(self, uid, key):
        await self.user(uid)

        address = str(key.pubkey())
        encrypted = self.fernet.encrypt(
            bytes(key)
        ).decode()

        async with self.pool.acquire() as conn:
            async with conn.transaction():

                existing = await conn.fetchval(
                    "SELECT id FROM copy_wallets "
                    "WHERE user_id=$1 AND address=$2",
                    uid,
                    address,
                )

                if existing:
                    raise ValueError(
                        "This wallet is already imported."
                    )

                count = await conn.fetchval(
                    "SELECT count(*) FROM copy_wallets "
                    "WHERE user_id=$1",
                    uid,
                )

                if count >= 10:
                    raise ValueError(
                        "You can manage up to 10 wallets."
                    )

                wid = await conn.fetchval("""
                    INSERT INTO copy_wallets(
                        user_id,
                        name,
                        address,
                        encrypted_secret
                    )
                    VALUES($1,$2,$3,$4)
                    RETURNING id
                """,
                    uid,
                    f"Wallet {count + 1}",
                    address,
                    encrypted,
                )

                previous = await conn.fetchval(
                    "SELECT address FROM copy_users "
                    "WHERE id=$1 FOR UPDATE",
                    uid,
                )

                if previous is None:
                    await conn.execute("""
                        UPDATE copy_users
                        SET
                            address=$2,
                            encrypted_secret=$3,
                            enabled=FALSE
                        WHERE id=$1
                    """,
                        uid,
                        address,
                        encrypted,
                    )

        return wid

    async def select_wallet(self, uid, wid):
        w = await self.wallet(uid, wid)

        if not w:
            raise ValueError("Wallet not found.")

        await self.pool.execute("""
            UPDATE copy_users
            SET
                address=$2,
                encrypted_secret=$3,
                enabled=FALSE
            WHERE id=$1
        """,
            uid,
            w["address"],
            w["encrypted_secret"],
        )

    async def token_balance(self, address, mint=USDC):
        data = await self.rpc(
            "getTokenAccountsByOwner",
            [
                address,
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        )

        accounts = data.get("value", [])

        if not accounts:
            return 0, 6 if mint == USDC else None

        values = [
            a["account"]["data"]["parsed"]["info"]["tokenAmount"]
            for a in accounts
        ]

        return (
            sum(int(v["amount"]) for v in values),
            int(values[0]["decimals"]),
        )

    async def wallet_balances(self, address):
        sol = await self.rpc(
            "getBalance",
            [address],
        )

        usdc_raw, decimals = await self.token_balance(
            address
        )

        return (
            Decimal(sol["value"]) / Decimal(10**9),
            Decimal(usdc_raw) / Decimal(10**decimals),
        )

    async def cleanup_keys(self):
        while True:
            try:
                rows = await self.pool.fetch("""
                    SELECT chat_id,message_id
                    FROM copy_secret_messages
                    WHERE expires_at <= now()
                    LIMIT 100
                """)

                for row in rows:
                    try:
                        await self.app.bot.delete_message(
                            row["chat_id"],
                            row["message_id"],
                        )
                    except (BadRequest, Forbidden):
                        pass
                    except Exception:
                        continue

                    await self.pool.execute("""
                        DELETE FROM copy_secret_messages
                        WHERE chat_id=$1
                          AND message_id=$2
                    """,
                        row["chat_id"],
                        row["message_id"],
                    )

            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception(
                    "Temporary-message cleanup failed"
                )

            await asyncio.sleep(15)

    async def reveal(self, uid, wid):
        w = await self.wallet(uid, wid)

        if not w:
            raise ValueError("Wallet not found.")

        secret = str(
            Keypair.from_bytes(
                self.fernet.decrypt(
                    w["encrypted_secret"].encode()
                )
            )
        )

        msg = await self.app.bot.send_message(
            uid,
            f"🔑 <b>Private Key</b>\n\n"
            f"<code>{html.escape(secret)}</code>\n\n"
            "This message will be deleted in one hour. "
            "Anyone with this key can control your funds.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard([
                [
                    (
                        "📋 Copy Private Key",
                        "copy",
                        secret,
                    )
                ]
            ]),
            protect_content=True,
        )

        await self.pool.execute("""
            INSERT INTO copy_secret_messages(
                chat_id,
                message_id,
                expires_at
            )
            VALUES(
                $1,
                $2,
                now() + interval '1 hour'
            )
            ON CONFLICT DO NOTHING
        """,
            uid,
            msg.message_id,
        )

    async def send_funds(
        self,
        uid,
        wid,
        asset,
        recipient,
        amount,
    ):
        w = await self.wallet(uid, wid)

        if not w:
            raise ValueError("Wallet not found.")

        if not valid_address(recipient):
            raise ValueError(
                "Invalid recipient address."
            )

        if recipient == w["address"]:
            raise ValueError(
                "Recipient cannot equal sender."
            )

        if asset not in ("SOL", "USDC"):
            raise ValueError(
                "Unsupported asset."
            )

        decimals = 9 if asset == "SOL" else 6
        raw = amount * (10**decimals)

        if (
            amount <= 0
            or not amount.is_finite()
            or raw != raw.to_integral_value()
        ):
            raise ValueError(
                "Invalid transfer amount."
            )

        raw = int(raw)

        if raw >= 2**64:
            raise ValueError(
                "Transfer amount too large."
            )

        async with self.lock:
            u = await self.user(uid)

            if (
                u["enabled"]
                and u["address"] == w["address"]
            ):
                raise ValueError(
                    "Pause auto trading before sending funds."
                )

            sender = Pubkey.from_string(
                w["address"]
            )
            dest = Pubkey.from_string(
                recipient
            )

            key = Keypair.from_bytes(
                self.fernet.decrypt(
                    w["encrypted_secret"].encode()
                )
            )

            balance = await self.rpc(
                "getBalance",
                [w["address"]],
            )

            lamports = int(balance["value"])

            if asset == "SOL":
                if raw + 2_000_000 > lamports:
                    raise ValueError(
                        "Insufficient SOL. Leave "
                        "0.002 SOL for fees."
                    )

                instructions = [
                    transfer(
                        TransferParams(
                            from_pubkey=sender,
                            to_pubkey=dest,
                            lamports=raw,
                        )
                    )
                ]

            else:
                if lamports < 3_000_000:
                    raise ValueError(
                        "At least 0.003 SOL is needed for fees."
                    )

                available, _ = await self.token_balance(
                    w["address"]
                )

                if raw > available:
                    raise ValueError(
                        "Insufficient USDC."
                    )

                instructions = usdc_transfer_instructions(
                    sender,
                    dest,
                    raw,
                )

            latest = await self.rpc(
                "getLatestBlockhash",
                [{"commitment": "confirmed"}],
            )

            tx = Transaction.new_signed_with_payer(
                instructions,
                sender,
                [key],
                Hash.from_string(
                    latest["value"]["blockhash"]
                ),
            )

            signature = str(tx.signatures[0])
            encoded = base64.b64encode(
                bytes(tx)
            ).decode()

            transfer_id = await self.pool.fetchval("""
                INSERT INTO copy_withdrawals(
                    user_id,
                    wallet_address,
                    asset,
                    destination,
                    amount,
                    signature,
                    status
                )
                VALUES(
                    $1,$2,$3,$4,$5,$6,'SUBMITTING'
                )
                RETURNING id
            """,
                uid,
                w["address"],
                asset,
                recipient,
                str(amount),
                signature,
            )

            try:
                await self.rpc(
                    "sendTransaction",
                    [
                        encoded,
                        {
                            "encoding": "base64",
                            "skipPreflight": False,
                            "preflightCommitment": "confirmed",
                            "maxRetries": 0,
                        },
                    ],
                )

            except Exception as exc:
                await self.pool.execute(
                    "UPDATE copy_withdrawals "
                    "SET status='UNKNOWN' "
                    "WHERE id=$1",
                    transfer_id,
                )

                raise RuntimeError(
                    "Submission uncertain. "
                    "Check transaction " + signature
                ) from exc

            await self.pool.execute(
                "UPDATE copy_withdrawals "
                "SET status='SUBMITTED' "
                "WHERE id=$1",
                transfer_id,
            )

            return signature

    async def screen(self, uid, page="home"):
        u = await self.user(uid)

        if page == "home":
            ms = await self.masters(uid)

            active = (
                "🟢 ON"
                if u["enabled"]
                else "⏸ OFF"
            )

            address = u["address"] or "Not created"

            text = (
                "🤖 <b>Solana Copy Trader</b>\n\n"
                f"Auto trade: <b>{active}</b> (LIVE)\n"
                f"Masters: <b>{len(ms)}/3</b>\n"
                f"Max trade: <b>${u['max_usd']:.2f}</b>\n"
                f"Slippage: "
                f"<b>{Decimal(u['slip_bps']) / 100}%</b>\n"
                f"Wallet: <code>{address}</code>\n\n"
                "Buys use SOL. USDC fallback is optional. "
                "Diagnostics stay on the server."
            )

            rows = [
                [
                    (
                        "⏸ Pause"
                        if u["enabled"]
                        else "▶️ Enable Auto Trade",
                        "toggle",
                    )
                ],
                [
                    ("👀 Masters", "masters"),
                    ("⚙️ Settings", "settings"),
                ],
                [
                    ("👛 My Wallets", "wallet"),
                    ("📜 History", "history"),
                ],
            ]

            if u["address"]:
                rows.append([
                    (
                        "📋 Copy Address",
                        "copy",
                        u["address"],
                    )
                ])

            rows.append([
                ("🔄 Refresh", "home")
            ])

            return text, keyboard(rows)

        if page == "wallet":
            ws = await self.wallets(uid)

            lines = [
                "👛 <b>My Wallets</b>",
                "Choose a wallet to manage.",
            ]

            rows = []

            for w in ws:
                active = w["address"] == u["address"]

                lines.append(
                    f"\n{'🟢' if active else '⚪'} "
                    f"<b>{html.escape(w['name'])}</b>\n"
                    f"<code>{w['address']}</code>"
                )

                rows.append([
                    (
                        f"👛 {w['name']}",
                        f"walletview:{w['id']}",
                    )
                ])

                rows.append([
                    (
                        "📋 Copy Address",
                        "copy",
                        w["address"],
                    )
                ])

            if not ws:
                lines.append("\nNo saved wallets.")

            rows += [
                [
                    ("➕ Generate", "generate"),
                    ("📥 Import", "import"),
                ],
                [
                    ("🏠 Home", "home")
                ],
            ]

            return "\n".join(lines), keyboard(rows)

        if page.startswith("walletview:"):
            wid = int(page.split(":")[1])

            w = await self.wallet(uid, wid)

            if not w:
                return await self.screen(
                    uid,
                    "wallet",
                )

            try:
                sol, usdc = await self.wallet_balances(
                    w["address"]
                )

                balances = (
                    f"SOL: <b>{sol:.6f}</b>\n"
                    f"USDC: <b>{usdc:.2f}</b>"
                )

            except Exception:
                balances = (
                    "Balances temporarily unavailable."
                )

            is_active = (
                w["address"] == u["address"]
            )

            text = (
                f"👛 <b>{html.escape(w['name'])}</b>"
                f"{' — Active' if is_active else ''}\n\n"
                f"<code>{w['address']}</code>\n\n"
                f"{balances}\n\n"
                "Private-key messages expire "
                "after one hour."
            )

            rows = [
                [
                    (
                        "📋 Copy Address",
                        "copy",
                        w["address"],
                    )
                ],
                [
                    (
                        "📤 Send SOL",
                        f"send:{wid}:SOL",
                    ),
                    (
                        "📤 Send USDC",
                        f"send:{wid}:USDC",
                    ),
                ],
                [
                    (
                        "🔑 Private Key",
                        f"key:{wid}",
                    )
                ],
            ]

            if not is_active:
                rows.append([
                    (
                        "✅ Set Active",
                        f"active:{wid}",
                    )
                ])

            rows += [
                [
                    (
                        "🗑 Delete Wallet",
                        f"del:{wid}",
                    )
                ],
                [
                    ("👛 All Wallets", "wallet")
                ],
            ]

            return text, keyboard(rows)

        if page == "masters":
            ms = await self.masters(uid)

            lines = [
                "👀 <b>Master Wallets</b>"
            ]

            rows = []

            for m in ms:
                lines.append(
                    f"<b>{html.escape(m['name'])}</b> "
                    f"{'🟢' if m['active'] else '⏸'}\n"
                    f"<code>{m['address']}</code>"
                )

                rows.append([
                    (
                        "📋 Copy Address",
                        "copy",
                        m["address"],
                    )
                ])

                rows.append([
                    (
                        "⏯ Pause/Resume",
                        f"mastertoggle:{m['id']}",
                    ),
                    (
                        "🗑 Remove",
                        f"masterdelete:{m['id']}",
                    ),
                ])

            if len(ms) < 3:
                rows.append([
                    ("➕ Add Master", "addmaster")
                ])

            rows.append([
                ("🏠 Home", "home")
            ])

            return "\n".join(lines), keyboard(rows)

        if page == "settings":
            text = (
                "⚙️ <b>Trade Settings</b>\n\n"
                f"Max trade: ${u['max_usd']:.2f}\n"
                f"Slippage: "
                f"{Decimal(u['slip_bps']) / 100}%\n"
                f"Copy buys: "
                f"{'ON' if u['copy_buys'] else 'OFF'}\n"
                f"Copy sells: "
                f"{'ON' if u['copy_sells'] else 'OFF'}\n"
                f"Poll interval: "
                f"{u['poll_interval_seconds']} sec\n"
                f"Max signal age: "
                f"{u['max_signal_age_seconds']} sec\n"
                f"USDC fallback: "
                f"{'ON' if u['allow_usdc_fallback'] else 'OFF'}\n\n"
                "Live mode spends real SOL. "
                "Higher slippage can cause severe losses."
            )

            return text, keyboard([
                [
                    ("💵 Max Trade", "edit:max"),
                    ("📉 Slippage", "edit:slip"),
                ],
                [
                    ("🔁 Buys", "setting:buy"),
                    ("🔁 Sells", "setting:sell"),
                ],
                [
                    ("⏱ Poll speed", "edit:poll"),
                    ("⌛ Signal age", "edit:age"),
                ],
                [
                    (
                        "💱 USDC fallback",
                        "setting:usdc",
                    )
                ],
                [
                    ("🏠 Home", "home")
                ],
            ])

        if page == "history":
            trades = await self.pool.fetch("""
                SELECT *
                FROM copy_trades
                WHERE user_id=$1
                ORDER BY id DESC
                LIMIT 8
            """, uid)

            lines = [
                "📜 <b>Recent Trades</b>"
            ]

            rows = []

            for t in trades:
                description = (
                    f"{t['side']} "
                    f"<code>{t['mint']}</code> "
                    f"${t['usd']:.2f} — "
                    f"{html.escape(t['status'])}"
                )

                if t["message"]:
                    description += (
                        f" ({html.escape(str(t['message'])[:85])})"
                    )

                lines.append(description)

                if t["signature"]:
                    rows.append([
                        (
                            "📋 Copy Signature",
                            "copy",
                            t["signature"],
                        )
                    ])

                    rows.append([
                        (
                            "🔎 View TX",
                            f"url:https://solscan.io/tx/"
                            f"{t['signature']}",
                        )
                    ])

            if not trades:
                lines.append("No trades yet.")

            rows.append([
                ("🏠 Home", "home")
            ])

            return "\n".join(lines), keyboard(rows)

        return await self.screen(uid, "home")

    async def show(
        self,
        update,
        page="home",
        notice="",
    ):
        text, markup = await self.screen(
            update.effective_user.id,
            page,
        )

        if notice:
            text = (
                html.escape(notice)
                + "\n\n"
                + text
            )

        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text,
                    reply_markup=markup,
                    parse_mode=ParseMode.HTML,
                )

            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    raise

        else:
            await update.message.reply_text(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )

    async def start(self, update, ctx):
        if update.effective_chat.type != "private":
            return

        ctx.user_data.clear()

        await self.show(update)

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
            "wallet",
            "masters",
            "settings",
            "history",
        ):
            ctx.user_data.pop("pending", None)
            return await self.show(update, data)

        if data == "generate":
            try:
                wid = await self.save_wallet(
                    uid,
                    Keypair(),
                )

                return await self.show(
                    update,
                    f"walletview:{wid}",
                    "Wallet generated.",
                )

            except ValueError as exc:
                return await self.show(
                    update,
                    "wallet",
                    str(exc),
                )

        if data == "import":
            ctx.user_data["pending"] = ("import",)

            return await q.edit_message_text(
                "📥 Send your Solana private key "
                "in this private chat.\n"
                "The original message will be "
                "deleted where possible.\n"
                "Use /cancel to abort."
            )

        if data.startswith("walletview:"):
            ctx.user_data.pop("pending", None)

            return await self.show(
                update,
                data,
            )

        if data.startswith("key:"):
            try:
                await self.reveal(
                    uid,
                    int(data.split(":")[1]),
                )

            except ValueError as exc:
                await q.message.reply_text(
                    str(exc)
                )

            return

        if data.startswith("active:"):
            wid = int(data.split(":")[1])

            await self.select_wallet(uid, wid)

            return await self.show(
                update,
                f"walletview:{wid}",
                "Active wallet changed. "
                "Auto trading paused.",
            )

        if data.startswith("del:"):
            wid = int(data.split(":")[1])
            w = await self.wallet(uid, wid)

            if not w:
                return await self.show(
                    update,
                    "wallet",
                )

            nonce = secrets.token_hex(6)
            ctx.user_data["delete"] = (
                wid,
                nonce,
            )

            return await q.edit_message_text(
                "⚠️ <b>Delete Wallet?</b>\n\n"
                f"<code>{w['address']}</code>\n\n"
                "Back up your private key first. "
                "Deleting this wallet does not move its funds.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard([
                    [
                        (
                            "✅ Delete",
                            f"delconfirm:{nonce}",
                        )
                    ],
                    [
                        (
                            "❌ Keep",
                            f"walletview:{wid}",
                        )
                    ],
                ]),
            )

        if data.startswith("delconfirm:"):
            pending = ctx.user_data.pop(
                "delete",
                None,
            )

            if (
                not pending
                or pending[1] != data.split(":")[1]
            ):
                return await self.show(
                    update,
                    "wallet",
                    "Confirmation expired.",
                )

            wid = pending[0]
            w = await self.wallet(uid, wid)

            if not w:
                return await self.show(
                    update,
                    "wallet",
                )

            async with self.lock:
                async with self.pool.acquire() as conn:
                    async with conn.transaction():

                        await conn.execute(
                            "UPDATE copy_users "
                            "SET enabled=FALSE "
                            "WHERE id=$1",
                            uid,
                        )

                        await conn.execute(
                            "DELETE FROM copy_wallets "
                            "WHERE id=$1 AND user_id=$2",
                            wid,
                            uid,
                        )

                        if u["address"] == w["address"]:
                            another = await conn.fetchrow("""
                                SELECT *
                                FROM copy_wallets
                                WHERE user_id=$1
                                ORDER BY id
                                LIMIT 1
                            """, uid)

                            await conn.execute("""
                                UPDATE copy_users
                                SET
                                    address=$2,
                                    encrypted_secret=$3
                                WHERE id=$1
                            """,
                                uid,
                                (
                                    another["address"]
                                    if another else None
                                ),
                                (
                                    another["encrypted_secret"]
                                    if another else None
                                ),
                            )

            return await self.show(
                update,
                "wallet",
                "Wallet removed.",
            )

        if data.startswith("send:"):
            _, wid, asset = data.split(":")

            ctx.user_data["pending"] = (
                "recipient",
                int(wid),
                asset,
            )

            return await q.edit_message_text(
                f"📤 <b>Send {asset}</b>\n\n"
                "Paste the recipient's Solana wallet address.",
                parse_mode=ParseMode.HTML,
            )

        if data.startswith("confirm_send:"):
            confirmation = ctx.user_data.pop(
                "transfer",
                None,
            )

            if (
                not confirmation
                or confirmation["nonce"] != data.split(":")[1]
            ):
                return await self.show(
                    update,
                    "wallet",
                    "Confirmation expired.",
                )

            if time.monotonic() > confirmation["expires"]:
                return await self.show(
                    update,
                    "wallet",
                    "Confirmation expired.",
                )

            await q.edit_message_text(
                "📤 Submitting transfer..."
            )

            try:
                signature = await self.send_funds(
                    uid,
                    confirmation["wid"],
                    confirmation["asset"],
                    confirmation["destination"],
                    confirmation["amount"],
                )

                await q.edit_message_text(
                    "✅ <b>Transfer submitted</b>\n\n"
                    f"<code>{signature}</code>\n\n"
                    "Confirm the transaction on-chain "
                    "before retrying.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard([
                        [
                            (
                                "📋 Copy Signature",
                                "copy",
                                signature,
                            )
                        ],
                        [
                            (
                                "🔎 View TX",
                                f"url:https://solscan.io/tx/{signature}",
                            )
                        ],
                    ]),
                )

            except Exception as exc:
                LOG.warning(
                    "Transfer failed: %s",
                    type(exc).__name__,
                )

                await q.edit_message_text(
                    "⚠️ Transfer was not confirmed.\n\n"
                    f"{html.escape(str(exc))[:300]}\n"
                    "Check your wallet before trying again."
                )

            return

        if data == "toggle":
            if not u["enabled"] and not JUP_KEY:
                return await self.show(
                    update,
                    "settings",
                    "Live trading needs "
                    "JUPITER_API_KEY on the server.",
                )

            unresolved = await self.pool.fetchval("""
                SELECT EXISTS(
                    SELECT 1
                    FROM copy_trades
                    WHERE user_id=$1
                      AND status IN (
                        'UNKNOWN',
                        'SUBMITTING',
                        'SUBMITTED'
                      )
                )
            """, uid)

            if not u["enabled"] and unresolved:
                return await self.show(
                    update,
                    "history",
                    "Unresolved transactions exist. "
                    "Check them on-chain first.",
                )

            if not u["enabled"] and (
                not u["encrypted_secret"]
                or not await self.masters(uid)
            ):
                return await self.show(
                    update,
                    "home",
                    "Create a wallet and add a master first.",
                )

            await self.pool.execute(
                "UPDATE copy_users "
                "SET enabled=NOT enabled "
                "WHERE id=$1",
                uid,
            )

            return await self.show(
                update,
                "home",
            )

        if data.startswith("setting:"):
            column = {
                "buy": "copy_buys",
                "sell": "copy_sells",
                "usdc": "allow_usdc_fallback",
            }.get(data.split(":")[1])

            if column:
                await self.pool.execute(
                    f"UPDATE copy_users "
                    f"SET {column}=NOT {column} "
                    f"WHERE id=$1",
                    uid,
                )

            return await self.show(
                update,
                "settings",
            )

        if data.startswith("edit:"):
            mode = data.split(":")[1]
            ctx.user_data["pending"] = mode

            prompts = {
                "max": (
                    "Enter max USD per trade (0.50–1000)."
                ),
                "slip": (
                    "Enter slippage percentage (0.1–70). "
                    "20% suggested; 70% extremely risky."
                ),
                "poll": (
                    "Enter polling interval in seconds "
                    "(0.5–10). Lower values use more RPC credits."
                ),
                "age": (
                    "Enter maximum signal age in seconds "
                    "(10–180). Older trades are not copied."
                ),
            }

            return await q.edit_message_text(
                prompts.get(mode, "Unknown setting")
            )

        if data == "addmaster":
            if len(await self.masters(uid)) >= 3:
                return await self.show(
                    update,
                    "masters",
                    "Maximum 3 masters.",
                )

            ctx.user_data["pending"] = "master"

            return await q.edit_message_text(
                "Paste the master wallet's "
                "public Solana address."
            )

        if data.startswith("mastertoggle:"):
            mid = int(data.split(":")[1])

            await self.pool.execute("""
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

        if data.startswith("masterdelete:"):
            mid = int(data.split(":")[1])

            ctx.user_data["remove_master"] = mid

            return await q.edit_message_text(
                "Remove this master wallet?",
                reply_markup=keyboard([
                    [
                        (
                            "✅ Remove",
                            f"removeconfirm:{mid}",
                        )
                    ],
                    [
                        (
                            "❌ Cancel",
                            "masters",
                        )
                    ],
                ]),
            )

        if data.startswith("removeconfirm:"):
            mid = int(data.split(":")[1])

            if (
                ctx.user_data.pop("remove_master", None)
                != mid
            ):
                return await self.show(
                    update,
                    "masters",
                )

            await self.pool.execute("""
                DELETE FROM copy_masters
                WHERE id=$1 AND user_id=$2
            """,
                mid,
                uid,
            )

            return await self.show(
                update,
                "masters",
            )

        return await self.show(
            update,
            "home",
        )

    async def text(self, update, ctx):
        if update.effective_chat.type != "private":
            return

        uid = update.effective_user.id
        pending = ctx.user_data.get("pending")
        value = update.message.text.strip()

        if not pending:
            return await self.show(update)

        try:
            if (
                isinstance(pending, tuple)
                and pending[0] == "import"
            ):
                try:
                    await update.message.delete()
                except (BadRequest, Forbidden):
                    pass

                key = import_key(value)

                wid = await self.save_wallet(
                    uid,
                    key,
                )

                ctx.user_data.pop("pending", None)

                return await self.show(
                    update,
                    f"walletview:{wid}",
                    "Wallet imported.",
                )

            if (
                isinstance(pending, tuple)
                and pending[0] == "recipient"
            ):
                _, wid, asset = pending

                if not valid_address(value):
                    raise ValueError(
                        "Invalid recipient address."
                    )

                w = await self.wallet(uid, wid)

                if not w or w["address"] == value:
                    raise ValueError(
                        "Recipient must be a different wallet."
                    )

                ctx.user_data["pending"] = (
                    "amount",
                    wid,
                    asset,
                    value,
                )

                return await update.message.reply_text(
                    f"How much {asset} would you like to send?"
                )

            if (
                isinstance(pending, tuple)
                and pending[0] == "amount"
            ):
                _, wid, asset, destination = pending

                amount = amount_number(value)
                decimals = 9 if asset == "SOL" else 6
                scaled = amount * (10**decimals)

                if (
                    amount <= 0
                    or scaled != scaled.to_integral_value()
                ):
                    raise ValueError(
                        "Invalid amount or too many decimal places."
                    )

                nonce = secrets.token_hex(8)
                ctx.user_data.pop("pending", None)

                ctx.user_data["transfer"] = {
                    "nonce": nonce,
                    "wid": wid,
                    "asset": asset,
                    "destination": destination,
                    "amount": amount,
                    "expires": time.monotonic() + 180,
                }

                w = await self.wallet(uid, wid)

                return await update.message.reply_text(
                    "⚠️ <b>Confirm Transfer</b>\n\n"
                    f"From: <code>{w['address']}</code>\n"
                    f"To: <code>{destination}</code>\n"
                    f"Amount: <b>{amount} {asset}</b>\n\n"
                    "Transfers are irreversible.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard([
                        [
                            (
                                "✅ Confirm & Send",
                                f"confirm_send:{nonce}",
                            )
                        ],
                        [
                            (
                                "❌ Cancel",
                                f"walletview:{wid}",
                            )
                        ],
                        [
                            (
                                "📋 Copy Recipient",
                                "copy",
                                destination,
                            )
                        ],
                    ]),
                )

            if pending == "master":
                if not valid_address(value):
                    raise ValueError(
                        "Invalid Solana wallet address."
                    )

                if len(await self.masters(uid)) >= 3:
                    raise ValueError(
                        "Maximum 3 masters."
                    )

                ctx.user_data["pending"] = (
                    "nickname",
                    value,
                )

                return await update.message.reply_text(
                    "Enter a nickname for this master."
                )

            if (
                isinstance(pending, tuple)
                and pending[0] == "nickname"
            ):
                await self.pool.execute("""
                    INSERT INTO copy_masters(
                        user_id,
                        address,
                        name
                    )
                    VALUES($1,$2,$3)
                """,
                    uid,
                    pending[1],
                    value[:24] or "Trader",
                )

                ctx.user_data.pop("pending", None)

                return await self.show(
                    update,
                    "masters",
                    "Master added.",
                )

            if pending in ("max", "slip", "poll", "age"):
                n = amount_number(value)

                if pending == "poll":
                    if not Decimal("0.5") <= n <= Decimal("10"):
                        raise ValueError(
                            "Poll interval must be between "
                            "0.5 and 10 seconds."
                        )

                    if n != n.quantize(Decimal("0.1")):
                        raise ValueError(
                            "Use increments of 0.1 seconds."
                        )

                    await self.pool.execute("""
                        UPDATE copy_users
                        SET poll_interval_seconds=$2
                        WHERE id=$1
                    """, uid, n)

                elif pending == "age":
                    if (
                        n != n.to_integral_value()
                        or not 10 <= n <= 180
                    ):
                        raise ValueError(
                            "Signal age must be a whole "
                            "number from 10 to 180."
                        )

                    await self.pool.execute("""
                        UPDATE copy_users
                        SET max_signal_age_seconds=$2
                        WHERE id=$1
                    """, uid, int(n))

                elif pending == "max":
                    if not Decimal("0.5") <= n <= Decimal("1000"):
                        raise ValueError(
                            "Amount must be between "
                            "$0.50 and $1,000."
                        )

                    await self.pool.execute("""
                        UPDATE copy_users
                        SET max_usd=$2
                        WHERE id=$1
                    """,
                        uid,
                        n.quantize(Decimal(".01")),
                    )

                else:
                    if not Decimal(".1") <= n <= Decimal("70"):
                        raise ValueError(
                            "Slippage must be between "
                            "0.1 and 70%."
                        )

                    await self.pool.execute("""
                        UPDATE copy_users
                        SET slip_bps=$2
                        WHERE id=$1
                    """, uid, int(n * 100))

                ctx.user_data.pop("pending", None)

                return await self.show(
                    update,
                    "settings",
                    "Saved.",
                )

        except (
            ValueError,
            asyncpg.UniqueViolationError,
        ) as exc:
            await update.message.reply_text(
                f"❌ {html.escape(str(exc))}"
            )

    async def cancel(self, update, ctx):
        ctx.user_data.clear()

        await self.show(
            update,
            "home",
            "Cancelled.",
        )

    async def jupiter_order(
        self,
        source,
        destination,
        amount,
        wallet=None,
        slippage=None,
    ):
        params = {
            "inputMint": source,
            "outputMint": destination,
            "amount": str(int(amount)),
            "maxSupportedTransactionVersion": "0",
        }

        if wallet:
            params["taker"] = wallet

        if slippage is not None:
            params["slippageBps"] = str(
                int(slippage)
            )

        headers = (
            {"x-api-key": JUP_KEY}
            if JUP_KEY else {}
        )

        async with self.session.get(
            JUP + "/order",
            params=params,
            headers=headers,
        ) as response:

            data = await response.json(
                content_type=None
            )

            if response.status != 200:
                raise RuntimeError(
                    f"Jupiter order HTTP {response.status}"
                )

            if (
                not isinstance(data, dict)
                or not data.get("outAmount")
            ):
                raise RuntimeError(
                    "Jupiter did not return an executable quote"
                )

            if wallet and (
                not data.get("transaction")
                or not data.get("requestId")
            ):
                raise ValueError(
                    "Jupiter cannot build this swap route"
                )

            return data

    async def sol_usd(self):
        now = time.monotonic()
        ts, cached = self.price_cache

        if (
            cached is not None
            and now - ts < 12
        ):
            return cached

        quote = await self.jupiter_order(
            WSOL,
            USDC,
            100_000_000,
        )

        value = (
            Decimal(str(quote["outAmount"]))
            / Decimal(100_000)
        )

        if value <= 0:
            raise RuntimeError(
                "Invalid SOL/USD quote"
            )

        self.price_cache = (now, value)

        return value

    async def jupiter_execute(self, order, key):
        if str(
            order.get("transactionVersion", "0")
        ) not in ("0", "legacy"):
            raise ValueError(
                "Unsupported Jupiter transaction version"
            )

        tx = VersionedTransaction.from_bytes(
            base64.b64decode(
                order["transaction"]
            )
        )

        required = (
            tx.message.header.num_required_signatures
        )

        signers = list(
            tx.message.account_keys[:required]
        )

        if key.pubkey() not in signers:
            raise ValueError(
                "Unexpected Jupiter transaction signer"
            )

        signatures = list(tx.signatures)

        message_data = to_bytes_versioned(
            tx.message
        )

        local_signature = key.sign_message(
            message_data
        )

        if not local_signature.verify(
            key.pubkey(),
            message_data,
        ):
            raise RuntimeError(
                "Local signature verification failed"
            )

        signatures[
            signers.index(key.pubkey())
        ] = local_signature

        signed = VersionedTransaction.populate(
            tx.message,
            signatures,
        )

        signed_signature = str(
            signed.signatures[0]
        )

        if signed_signature == str(
            type(signed.signatures[0]).default()
        ):
            raise ValueError(
                "Jupiter fee-payer signature missing"
            )

        headers = {
            "x-api-key": JUP_KEY
        }

        return (
            signed_signature,
            base64.b64encode(
                bytes(signed)
            ).decode(),
            headers,
        )

    async def submit_jupiter(
        self,
        order,
        signed_b64,
        headers,
    ):
        async with self.session.post(
            JUP + "/execute",
            json={
                "signedTransaction": signed_b64,
                "requestId": order["requestId"],
            },
            headers=headers,
            timeout=aiohttp.ClientTimeout(
                total=40
            ),
        ) as response:

            data = await response.json(
                content_type=None
            )

            if response.status != 200:
                raise RuntimeError(
                    f"Jupiter execute HTTP "
                    f"{response.status}"
                )

            if not isinstance(data, dict):
                raise RuntimeError(
                    "Jupiter returned invalid execution result"
                )

            return data

    def classify_swap(self, tx, master):
        if (
            not tx
            or not tx.get("meta")
            or tx["meta"].get("err")
        ):
            return None, (
                "Failed or unavailable transaction"
            )

        meta = tx["meta"]

        logs = " ".join(
            meta.get("logMessages") or []
        ).lower()

        swap_words = (
            "instruction: swap",
            "instruction: route",
            "instruction: buy",
            "instruction: sell",
            "instruction: sharedaccountsroute",
            "instruction: exactoutroute",
            "swapbasein",
            "swapbaseout",
            "instruction: buyexactin",
            "instruction: sellall",
            "instruction: buyexactout",
            "instruction: buyexactsolin",
            "instruction: buy_exact_sol_in",
            "instruction: sell_exact_sol_out",
        )

        if not any(
            word in logs
            for word in swap_words
        ):
            return None, (
                "No recognized swap or buy/sell instruction"
            )

        def owner_balances(entries):
            result = {}

            for item in entries or ():
                if (
                    item.get("owner") != master
                    or not item.get("mint")
                ):
                    continue

                mint = item["mint"]

                amount = int(
                    (
                        item.get("uiTokenAmount")
                        or {}
                    ).get("amount", "0")
                )

                result[mint] = (
                    result.get(mint, 0)
                    + amount
                )

            return result

        before = owner_balances(
            meta.get("preTokenBalances")
        )

        after = owner_balances(
            meta.get("postTokenBalances")
        )

        delta = {
            mint: (
                after.get(mint, 0)
                - before.get(mint, 0)
            )
            for mint in (
                before.keys() | after.keys()
            )
        }

        gained = [
            (mint, n)
            for mint, n in delta.items()
            if n > 0 and mint not in QUOTE_MINTS
        ]

        lost = [
            (mint, n)
            for mint, n in delta.items()
            if n < 0 and mint not in QUOTE_MINTS
        ]

        quote_spent = any(
            delta.get(q, 0) < 0
            for q in QUOTE_MINTS
        )

        quote_gained = any(
            delta.get(q, 0) > 0
            for q in QUOTE_MINTS
        )

        msg = (
            (tx.get("transaction") or {})
            .get("message")
            or {}
        )

        keys = [
            (
                k.get("pubkey")
                if isinstance(k, dict)
                else k
            )
            for k in msg.get("accountKeys", [])
        ]

        if master in keys:
            idx = keys.index(master)

            pre_s = (
                meta.get("preBalances")
                or []
            )

            post_s = (
                meta.get("postBalances")
                or []
            )

            if (
                idx < len(pre_s)
                and idx < len(post_s)
            ):
                fee = (
                    int(meta.get("fee") or 0)
                    if keys and keys[0] == master
                    else 0
                )

                sol_delta = (
                    int(post_s[idx])
                    - int(pre_s[idx])
                    + fee
                )

                if sol_delta < -1_000_000:
                    quote_spent = True

                if sol_delta > 1_000_000:
                    quote_gained = True

        if (
            len(gained) == 1
            and len(lost) == 0
            and quote_spent
        ):
            return (
                "BUY",
                gained[0][0],
                None,
            ), ""

        if (
            len(lost) == 1
            and len(gained) == 0
            and quote_gained
        ):
            available_before = before.get(
                lost[0][0],
                0,
            )

            fraction = (
                Decimal(-lost[0][1])
                / Decimal(available_before)
                if available_before > 0
                else Decimal(1)
            )

            return (
                "SELL",
                lost[0][0],
                min(Decimal(1), fraction),
            ), ""

        if (
            len(gained) == 1
            and len(lost) == 1
        ):
            return None, (
                "Token-to-token swap "
                "(not safely copyable yet)"
            )

        return None, (
            f"Ambiguous swap: "
            f"gained={len(gained)} "
            f"lost={len(lost)} "
            f"quote_spent={quote_spent} "
            f"quote_gained={quote_gained}"
        )

    async def notify(self, uid, message):
        try:
            await self.app.bot.send_message(
                uid,
                message,
                parse_mode=ParseMode.HTML,
            )

        except (BadRequest, Forbidden):
            pass

    async def reconcile_trades(self):
        rows = await self.pool.fetch("""
            SELECT
                id,
                signature,
                status
            FROM copy_trades
            WHERE status IN (
                'UNKNOWN',
                'SUBMITTING',
                'SUBMITTED'
            )
              AND signature IS NOT NULL
            ORDER BY id DESC
            LIMIT 100
        """)

        for row in rows:
            try:
                await self.reconcile_one(
                    row["id"],
                    row["signature"],
                )

            except Exception as exc:
                LOG.warning(
                    "Reconciliation failed for trade "
                    "%s: %s",
                    row["id"],
                    type(exc).__name__,
                )

    async def reconcile_one(
        self,
        tid,
        signature,
    ):
        result = await self.rpc(
            "getSignatureStatuses",
            [
                [signature],
                {
                    "searchTransactionHistory": True
                },
            ],
        )

        statuses = (
            (result or {}).get("value")
            or []
        )

        state = (
            statuses[0]
            if statuses
            else None
        )

        if (
            state
            and state.get("err") is not None
        ):
            await self.pool.execute("""
                UPDATE copy_trades
                SET
                    status='FAILED',
                    message='On-chain transaction failed'
                WHERE id=$1
            """, tid)

            return "FAILED"

        if state and state.get(
            "confirmationStatus"
        ) in ("confirmed", "finalized"):
            await self.pool.execute("""
                UPDATE copy_trades
                SET
                    status='CONFIRMED',
                    message=NULL
                WHERE id=$1
            """, tid)

            return "CONFIRMED"

        return "UNKNOWN"

    async def execute_copy(
        self,
        uid,
        master,
        signature,
        side,
        mint,
        observed,
        fraction=None,
        preexisting_id=None,
    ):
        if preexisting_id is not None:
            tid = preexisting_id

        else:
            row = await self.pool.fetchrow("""
                INSERT INTO copy_trades(
                    user_id,
                    master_address,
                    master_signature,
                    side,
                    mint,
                    status
                )
                VALUES(
                    $1,$2,$3,$4,$5,'QUEUED'
                )
                ON CONFLICT DO NOTHING
                RETURNING id
            """,
                uid,
                master,
                signature,
                side,
                mint,
            )

            if not row:
                return

            tid = row["id"]

        phase = "PREPARING"

        try:
            u = await self.user(uid)

            if (
                not u["enabled"]
                or time.time() - observed
                > int(u["max_signal_age_seconds"])
            ):
                raise ValueError(
                    "Trading disabled or signal expired"
                )

            if side == "BUY" and not u["copy_buys"]:
                raise ValueError(
                    "Copy buys disabled"
                )

            if side == "SELL" and not u["copy_sells"]:
                raise ValueError(
                    "Copy sells disabled"
                )

            if not u["encrypted_secret"]:
                raise ValueError(
                    "No active trading wallet"
                )

            if not valid_address(mint):
                raise ValueError(
                    "Invalid destination mint"
                )

            wallet = u["address"]
            slip = int(u["slip_bps"])

            if not 10 <= slip <= MAX_SLIPPAGE_BPS:
                raise ValueError(
                    "Slippage outside 0.1%-70% configuration"
                )

            if side == "BUY":
                lamports = int(
                    (
                        await self.rpc(
                            "getBalance",
                            [wallet],
                        )
                    )["value"]
                )

                available_sol = max(
                    0,
                    lamports - MIN_SOL_RESERVE,
                )

                price = await self.sol_usd()
                usd_limit = Decimal(u["max_usd"])

                max_lamports = int(
                    (
                        usd_limit
                        / price
                        * Decimal(10**9)
                    ).to_integral_value(
                        rounding=ROUND_DOWN
                    )
                )

                if (
                    available_sol >= 1_000_000
                    and max_lamports >= 1_000_000
                ):
                    source = WSOL
                    dest = mint
                    raw = min(
                        available_sol,
                        max_lamports,
                    )

                    usd = min(
                        usd_limit,
                        (
                            Decimal(raw)
                            / Decimal(10**9)
                            * price
                        ),
                    )

                    if usd < Decimal("0.50"):
                        raise ValueError(
                            "Trade below $0.50 minimum"
                        )

                elif u["allow_usdc_fallback"]:
                    available_usdc, _ = (
                        await self.token_balance(
                            wallet,
                            USDC,
                        )
                    )

                    if available_usdc < 500_000:
                        raise ValueError(
                            "Insufficient SOL and USDC "
                            "for copy buy"
                        )

                    source = USDC
                    dest = mint

                    raw = min(
                        available_usdc,
                        int(
                            usd_limit
                            * 1_000_000
                        ),
                    )

                    usd = (
                        Decimal(raw)
                        / Decimal(1_000_000)
                    )

                    if usd < Decimal("0.50"):
                        raise ValueError(
                            "Trade below $0.50 minimum"
                        )

                else:
                    raise ValueError(
                        "Insufficient spendable SOL; "
                        "fund wallet with SOL "
                        "plus fee reserve"
                    )

            else:
                if (
                    fraction is None
                    or not (
                        Decimal(0)
                        < fraction
                        <= Decimal(1)
                    )
                ):
                    raise ValueError(
                        "Cannot determine master's sell fraction"
                    )

                owned, _ = await self.token_balance(
                    wallet,
                    mint,
                )

                raw = int(
                    Decimal(owned)
                    * fraction
                )

                if raw <= 0:
                    raise ValueError(
                        "No copied tokens available to sell"
                    )

                source = mint
                dest = WSOL
                usd = Decimal(0)

            await self.pool.execute("""
                UPDATE copy_trades
                SET
                    status='QUOTING',
                    usd=$2
                WHERE id=$1
            """,
                tid,
                usd,
            )

            LOG.info(
                "Copy signal uid=%s side=%s mint=%s "
                "amount=%s source=%s mode=live",
                uid,
                side,
                shortened(mint),
                raw,
                (
                    "SOL"
                    if source == WSOL
                    else source
                ),
            )

            lock = self.user_locks.setdefault(
                uid,
                asyncio.Lock(),
            )

            async with lock:
                current = await self.user(uid)

                if (
                    time.time() - observed
                    > int(
                        current["max_signal_age_seconds"]
                    )
                ):
                    raise ValueError(
                        "Signal expired while waiting "
                        "for execution"
                    )

                if (
                    not current["enabled"]
                    or current["address"] != wallet
                ):
                    raise ValueError(
                        "Trading disabled or wallet changed"
                    )

                if (
                    side == "BUY"
                    and source == WSOL
                ):
                    now_balance = int(
                        (
                            await self.rpc(
                                "getBalance",
                                [wallet],
                            )
                        )["value"]
                    )

                    if (
                        now_balance - MIN_SOL_RESERVE
                        < raw
                    ):
                        raise ValueError(
                            "Spendable SOL changed before order"
                        )

                phase = "QUOTING"

                order = await self.jupiter_order(
                    source,
                    dest,
                    raw,
                    wallet,
                    slip,
                )

                key = Keypair.from_bytes(
                    self.fernet.decrypt(
                        current[
                            "encrypted_secret"
                        ].encode()
                    )
                )

                if str(key.pubkey()) != wallet:
                    raise ValueError(
                        "Wallet secret does not match "
                        "active address"
                    )

                (
                    signed_sig,
                    signed_b64,
                    headers,
                ) = await self.jupiter_execute(
                    order,
                    key,
                )

                await self.pool.execute("""
                    UPDATE copy_trades
                    SET
                        signature=$2,
                        status='SUBMITTING'
                    WHERE id=$1
                """,
                    tid,
                    signed_sig,
                )

                phase = "SUBMITTING"

                result = await self.submit_jupiter(
                    order,
                    signed_b64,
                    headers,
                )

                phase = "RECONCILING"

                result_sig = result.get(
                    "signature"
                )

                if (
                    result_sig
                    and result_sig != signed_sig
                ):
                    LOG.error(
                        "Provider signature mismatch "
                        "trade_id=%s",
                        tid,
                    )

                    raise RuntimeError(
                        "Provider signature differs "
                        "from locally signed transaction"
                    )

                status = result.get("status")

                if status == "Failed":
                    await self.pool.execute("""
                        UPDATE copy_trades
                        SET
                            status='FAILED',
                            message=$2
                        WHERE id=$1
                    """,
                        tid,
                        (
                            "Jupiter rejected execution "
                            f"(code {result.get('code')})"
                        ),
                    )

                    LOG.warning(
                        "Jupiter failed trade %s: "
                        "code=%s",
                        tid,
                        result.get("code"),
                    )

                    return

                if status != "Success":
                    raise RuntimeError(
                        "Jupiter execution outcome undetermined"
                    )

                chain = "UNKNOWN"

                for _ in range(5):
                    chain = await self.reconcile_one(
                        tid,
                        signed_sig,
                    )

                    if chain != "UNKNOWN":
                        break

                    await asyncio.sleep(0.6)

                if chain == "FAILED":
                    LOG.warning(
                        "Jupiter reported success but "
                        "Solana failed trade_id=%s",
                        tid,
                    )
                    return

                if chain != "CONFIRMED":
                    raise RuntimeError(
                        "Jupiter success returned; "
                        "chain confirmation pending"
                    )

                LOG.info(
                    "Copy confirmed trade_id=%s sig=%s",
                    tid,
                    shortened(signed_sig),
                )

        except ValueError as exc:
            await self.pool.execute("""
                UPDATE copy_trades
                SET
                    status='SKIPPED',
                    message=$2
                WHERE id=$1
            """,
                tid,
                str(exc)[:250],
            )

            LOG.info(
                "Copy skipped trade_id=%s "
                "phase=%s reason=%s",
                tid,
                phase,
                str(exc),
            )

        except Exception as exc:
            state = (
                "UNKNOWN"
                if phase in (
                    "SUBMITTING",
                    "RECONCILING",
                )
                else "FAILED"
            )

            await self.pool.execute("""
                UPDATE copy_trades
                SET
                    status=$2,
                    message=$3
                WHERE id=$1
            """,
                tid,
                state,
                (
                    f"{phase}: "
                    f"{type(exc).__name__}"
                ),
            )

            LOG.error(
                "Copy trade id=%s state=%s "
                "phase=%s exception=%s",
                tid,
                state,
                phase,
                type(exc).__name__,
            )

            if state == "UNKNOWN":
                await self.pool.execute(
                    "UPDATE copy_users "
                    "SET enabled=FALSE "
                    "WHERE id=$1",
                    uid,
                )

                LOG.warning(
                    "Auto trading paused for uid=%s "
                    "pending on-chain reconciliation",
                    uid,
                )

    def _trade_done(self, task):
        self.inflight.discard(task)

        try:
            task.result()

        except asyncio.CancelledError:
            LOG.warning(
                "Copy task cancelled during shutdown"
            )

        except Exception:
            LOG.exception(
                "Unexpected unhandled trade task error"
            )

    async def poll_master(self, address, max_age):
        previous = await self.pool.fetchval(
            "SELECT signature FROM copy_cursors "
            "WHERE address=$1",
            address,
        )

        entries = await self.rpc(
            "getSignaturesForAddress",
            [
                address,
                {
                    "limit": 100,
                    "commitment": "confirmed",
                },
            ],
        )

        if not entries:
            return

        newest = entries[0]["signature"]

        if previous is None:
            await self.pool.execute("""
                INSERT INTO copy_cursors(
                    address,
                    signature
                )
                VALUES($1,$2)
                ON CONFLICT DO NOTHING
            """,
                address,
                newest,
            )

            return

        seen = any(
            x["signature"] == previous
            for x in entries
        )

        if not seen:
            LOG.warning(
                "History gap for monitored wallet %s; "
                "automatic replay blocked",
                shortened(address),
            )

            await self.pool.execute("""
                UPDATE copy_users
                SET enabled=FALSE
                WHERE id IN (
                    SELECT m.user_id
                    FROM copy_masters m
                    WHERE m.address=$1
                      AND m.active
                )
            """, address)

            LOG.error(
                "History gap detected; "
                "copy trading paused address=%s",
                shortened(address),
            )

            await self.pool.execute("""
                UPDATE copy_cursors
                SET signature=$2
                WHERE address=$1
            """,
                address,
                newest,
            )

            return

        fresh = []

        for item in entries:
            if item["signature"] == previous:
                break

            if not item.get("err"):
                fresh.append(item)

        for item in reversed(fresh):
            signature = item["signature"]

            age = (
                time.time()
                - (item.get("blockTime") or 0)
            )

            if 0 <= age <= max_age:
                tx = await self.rpc(
                    "getTransaction",
                    [
                        signature,
                        {
                            "encoding": "jsonParsed",
                            "commitment": "confirmed",
                            "maxSupportedTransactionVersion": 1,
                        },
                    ],
                )

                if tx is None:
                    raise RuntimeError(
                        "RPC transaction details not ready"
                    )

                swap, reason = self.classify_swap(
                    tx,
                    address,
                )

                if swap:
                    side, mint, fraction = swap

                    followers = await self.pool.fetch("""
                        SELECT m.user_id
                        FROM copy_masters m
                        JOIN copy_users u
                          ON u.id=m.user_id
                        WHERE m.address=$1
                          AND m.active
                          AND u.enabled
                    """, address)

                    for follower in followers:
                        uid = follower["user_id"]

                        row = await self.pool.fetchrow("""
                            INSERT INTO copy_trades(
                                user_id,
                                master_address,
                                master_signature,
                                side,
                                mint,
                                status
                            )
                            VALUES(
                                $1,$2,$3,$4,$5,'QUEUED'
                            )
                            ON CONFLICT DO NOTHING
                            RETURNING id
                        """,
                            uid,
                            address,
                            signature,
                            side,
                            mint,
                        )

                        if not row:
                            continue

                        task = asyncio.create_task(
                            self.execute_copy(
                                uid,
                                address,
                                signature,
                                side,
                                mint,
                                (
                                    item.get("blockTime")
                                    or time.time()
                                ),
                                fraction,
                                preexisting_id=row["id"],
                            )
                        )

                        self.inflight.add(task)
                        task.add_done_callback(
                            self._trade_done
                        )

                else:
                    LOG.info(
                        "Ignored tx %s: %s",
                        shortened(signature),
                        reason,
                    )

            else:
                LOG.debug(
                    "Stale transaction %s: not copied",
                    shortened(signature),
                )

            await self.pool.execute("""
                INSERT INTO copy_cursors(
                    address,
                    signature
                )
                VALUES($1,$2)
                ON CONFLICT(address)
                DO UPDATE
                SET signature=EXCLUDED.signature
            """,
                address,
                signature,
            )

    async def monitor(self):
        next_poll = {}

        while True:
            try:
                masters = await self.pool.fetch("""
                    SELECT
                        m.address,
                        MIN(u.poll_interval_seconds)
                            AS interval,
                        MAX(u.max_signal_age_seconds)
                            AS max_age
                    FROM copy_masters m
                    JOIN copy_users u
                      ON u.id=m.user_id
                    WHERE m.active
                      AND u.enabled
                    GROUP BY m.address
                """)

                now = time.monotonic()

                active = {
                    m["address"]
                    for m in masters
                }

                for address in list(next_poll):
                    if address not in active:
                        next_poll.pop(
                            address,
                            None,
                        )

                for master in masters:
                    address = master["address"]

                    if now < next_poll.get(
                        address,
                        0,
                    ):
                        continue

                    next_poll[address] = (
                        now
                        + float(
                            master["interval"]
                        )
                    )

                    try:
                        await self.poll_master(
                            address,
                            int(master["max_age"]),
                        )

                    except Exception as exc:
                        LOG.error(
                            "Wallet monitoring error "
                            "wallet=%s type=%s detail=%s",
                            shortened(address),
                            type(exc).__name__,
                            str(exc).split(
                                "?api-key="
                            )[0][:150],
                        )

            except asyncio.CancelledError:
                raise

            except Exception:
                LOG.exception(
                    "Monitoring error"
                )

            await asyncio.sleep(0.25)


def main():
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
        ("send", "wallet"),
        ("masters", "masters"),
        ("settings", "settings"),
        ("history", "history"),
    ):

        async def open_page(
            update,
            ctx,
            target=page,
        ):
            if update.effective_chat.type == "private":
                await service.show(
                    update,
                    target,
                )

        app.add_handler(
            CommandHandler(
                command,
                open_page,
            )
        )

    app.add_handler(
        CommandHandler(
            "cancel",
            service.cancel,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            service.click
        )
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
