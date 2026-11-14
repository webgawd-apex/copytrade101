"""Solana Telegram Copy Trader — single-file application."""
import asyncio
import base64
import html
import json
import logging
import random
import re
import os
import secrets
import struct
import time
from datetime import datetime, timezone
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
from solders.signature import Signature
from solders.transaction import VersionedTransaction, Transaction
from solders.instruction import Instruction, AccountMeta
from solders.system_program import transfer, TransferParams, ID as SYSTEM_PROGRAM_ID
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K, BotCommand, CopyTextButton
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
LOG = logging.getLogger("copybot")

TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
DSN = os.getenv("DATABASE_URL")
JUP_KEY = os.getenv("JUPITER_API_KEY", "")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0") or "0")
RPC = os.getenv("HELIUS_RPC_URL", "").strip()
if not TOKEN or not DSN or not RPC:
    raise SystemExit("Set TELEGRAM_TOKEN, DATABASE_URL and HELIUS_RPC_URL")
parsed_rpc = urlsplit(RPC)
if (parsed_rpc.scheme != "https" or
        parsed_rpc.hostname != "mainnet.helius-rpc.com" or
        not parse_qs(parsed_rpc.query).get("api-key", [""])[0]):
    raise SystemExit("HELIUS_RPC_URL must be a valid Helius mainnet HTTPS RPC URL")

JUP = "https://api.jup.ag/swap/v2"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
USDT = "Es9vMFrzaCER1LEJzKSSGfYxj7ZLyuuz3QfbD7cGoE"
QUOTE_MINTS = {USDC, USDT, WSOL}
# User-configurable trading settings are persisted in PostgreSQL, not .env.
# Only credentials/infrastructure values belong in /etc/tg-bot.env.
DEFAULT_SLIPPAGE_BPS = 2000  # 20%
MIN_SOL_RESERVE = 10_000_000  # 0.01 SOL fee/rent reserve
MAX_SLIPPAGE_BPS = 7000
PUMPPORTAL_URL = "https://pumpportal.fun/api/trade-local"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
JUP_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET_ID = "ComputeBudget111111111111111111111111111111"
SYSTEM_ID = str(SYSTEM_PROGRAM_ID)
PORTAL_ALLOWED_PROGRAMS = {
    PUMP_PROGRAM, PUMPSWAP_PROGRAM, PUMP_FEE_PROGRAM, ATA_PROGRAM_ID,
    TOKEN_PROGRAM_ID, TOKEN_2022_ID, SYSTEM_ID, COMPUTE_BUDGET_ID,
}
PRIORITY_FEE_SOL = Decimal("0.0001")
MAX_PRIORITY_LAMPORTS = 5_000_000
PORTAL_RENT_ALLOWANCE = 10_000_000
SIGNATURE_EXPIRY_SECONDS = 120
UNRESOLVED = ["SIGNED", "SUBMITTED", "UNKNOWN", "SUBMITTING", "REVIEW_REQUIRED"]
TRADE_CONCURRENCY = 4

STATUS_WORDS = {
    "QUEUED": "Waiting",
    "QUOTING": "Preparing",
    "SIGNED": "Sending",
    "SUBMITTING": "Sending",
    "SUBMITTED": "Sending",
    "CONFIRMED": "Done ✅",
    "SKIPPED": "Skipped",
    "FAILED": "Failed ❌",
    "UNKNOWN": "Checking",
    "REVIEW_REQUIRED": "Review pending",
}
VIA_WORDS = {"jupiter": "Jupiter", "pumpportal": "Pump.fun fast mode"}

TOKEN_PROGRAM = Pubkey.from_string(TOKEN_PROGRAM_ID)
ATA_PROGRAM = Pubkey.from_string(ATA_PROGRAM_ID)
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
 allow_usdc_fallback BOOLEAN NOT NULL DEFAULT FALSE,
 use_pumpportal BOOLEAN NOT NULL DEFAULT FALSE,
 enabled_since TIMESTAMPTZ,
 take_profit_enabled BOOLEAN NOT NULL DEFAULT FALSE,
 take_profit_pct NUMERIC(8,2) NOT NULL DEFAULT 40 CHECK(take_profit_pct BETWEEN 1 AND 500),
 take_profit_sell_pct NUMERIC(5,2) NOT NULL DEFAULT 100 CHECK(take_profit_sell_pct BETWEEN 1 AND 100),
 take_profit_since TIMESTAMPTZ,
 prevent_repeat_buys BOOLEAN NOT NULL DEFAULT FALSE,
 stop_loss_enabled BOOLEAN NOT NULL DEFAULT FALSE,
 stop_loss_pct NUMERIC(8,2) NOT NULL DEFAULT 30 CHECK(stop_loss_pct BETWEEN 1 AND 99),
 stop_loss_since TIMESTAMPTZ
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
 block_time BIGINT,
 fraction NUMERIC,
 venue TEXT,
 route TEXT,
 signed_at TIMESTAMPTZ,
 tp_position_id BIGINT,
 tp_raw_amount NUMERIC(38,0),
 auto_exit_kind TEXT,
 UNIQUE(user_id,master_address,master_signature)
);
CREATE TABLE IF NOT EXISTS copy_positions (
 id BIGSERIAL PRIMARY KEY,
 trade_id BIGINT NOT NULL UNIQUE REFERENCES copy_trades(id),
 user_id BIGINT NOT NULL REFERENCES copy_users(id),
 wallet TEXT NOT NULL,
 mint TEXT NOT NULL,
 token_amount NUMERIC(38,0) NOT NULL,
 decimals INTEGER NOT NULL,
 cost_usd NUMERIC(20,8) NOT NULL,
 status TEXT NOT NULL DEFAULT 'OPEN',
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CHECK(token_amount >= 0),
 CHECK(decimals BETWEEN 0 AND 18)
);
CREATE INDEX IF NOT EXISTS copy_positions_open_idx ON copy_positions(status,user_id);
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

MIGRATIONS = (
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS poll_interval_seconds NUMERIC(4,1) NOT NULL DEFAULT 1.5",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS max_signal_age_seconds INTEGER NOT NULL DEFAULT 90",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS allow_usdc_fallback BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS use_pumpportal BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS enabled_since TIMESTAMPTZ",
    "ALTER TABLE copy_users ALTER COLUMN slip_bps SET DEFAULT 2000",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS block_time BIGINT",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS fraction NUMERIC",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS venue TEXT",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS route TEXT",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS signed_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS copy_trades_status_idx ON copy_trades(status)",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS take_profit_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS take_profit_pct NUMERIC(8,2) NOT NULL DEFAULT 40",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS take_profit_sell_pct NUMERIC(5,2) NOT NULL DEFAULT 100",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS take_profit_since TIMESTAMPTZ",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS tp_position_id BIGINT",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS tp_raw_amount NUMERIC(38,0)",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS auto_exit_kind TEXT",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS prevent_repeat_buys BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS stop_loss_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS stop_loss_pct NUMERIC(8,2) NOT NULL DEFAULT 30",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS stop_loss_since TIMESTAMPTZ",
    # An existing v2 table may still have CHECK(token_amount > 0), which
    # rejects a successful 100% close. Relax it exactly once if necessary.
    """DO $$ BEGIN
       IF EXISTS (
           SELECT 1 FROM pg_constraint
           WHERE conrelid='copy_positions'::regclass
             AND conname='copy_positions_token_amount_check'
             AND pg_get_constraintdef(oid) LIKE '%> 0%'
       ) THEN
           ALTER TABLE copy_positions
             DROP CONSTRAINT copy_positions_token_amount_check;
           ALTER TABLE copy_positions
             ADD CONSTRAINT copy_positions_token_amount_check
             CHECK (token_amount >= 0);
       END IF;
    END $$""",
)


class SendUncertain(Exception):
    """A transfer was sent but the result could not be confirmed."""


def keyboard(rows):
    buttons = []
    for row in rows:
        line = []
        for item in row:
            if len(item) == 3:
                line.append(B(item[0], copy_text=CopyTextButton(text=item[2])))
            elif item[1].startswith("url:"):
                line.append(B(item[0], url=item[1][4:]))
            else:
                line.append(B(item[0], callback_data=item[1]))
        buttons.append(line)
    return K(buttons)


def valid_address(value):
    try:
        return len(base58.b58decode(value)) == 32 and str(Pubkey.from_string(value)) == value
    except Exception:
        return False


def amount_number(value):
    try:
        n = Decimal(value.strip().replace(",", "").replace("$", ""))
        if not n.is_finite():
            raise ValueError()
        return n
    except (ValueError, InvalidOperation):
        raise ValueError("Invalid number")


def shortened(value):
    return value[:5] + "…" + value[-5:]


def pct(bps):
    return f"{Decimal(bps) / 100:g}%"


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
    raise ValueError("Invalid private key")


def encryption_key():
    path = Path("/var/lib/tg-bot/wallet.key")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(Fernet.generate_key())
    if path.stat().st_mode & 0o077:
        raise RuntimeError("Wallet encryption key permissions are unsafe.")
    return Fernet(path.read_bytes().strip())


def associated_token(owner):
    return Pubkey.find_program_address(
        [bytes(owner), bytes(TOKEN_PROGRAM), bytes(USDC_MINT)],
        ATA_PROGRAM
    )[0]


def usdc_transfer_instructions(sender, recipient, raw_amount):
    source = associated_token(sender)
    destination = associated_token(recipient)
    create_ata = Instruction(ATA_PROGRAM, bytes([1]), [
        AccountMeta(sender, True, True),
        AccountMeta(destination, False, True),
        AccountMeta(recipient, False, False),
        AccountMeta(USDC_MINT, False, False),
        AccountMeta(SYSTEM_PROGRAM_ID, False, False),
        AccountMeta(TOKEN_PROGRAM, False, False),
    ])
    transfer_checked = Instruction(
        TOKEN_PROGRAM, struct.pack("<BQB", 12, raw_amount, 6), [
            AccountMeta(source, False, True),
            AccountMeta(USDC_MINT, False, False),
            AccountMeta(destination, False, True),
            AccountMeta(sender, True, False),
        ]
    )
    return [create_ata, transfer_checked]


def detect_venue(programs):
    if PUMP_PROGRAM in programs:
        return "pumpfun"
    if PUMPSWAP_PROGRAM in programs:
        return "pumpswap"
    if JUP_PROGRAM in programs:
        return "jupiter"
    return "other"


class BotService:
    def __init__(self):
        self.pool = None
        self.session = None
        self.fernet = None
        self.app = None
        self.monitor_task = None
        self.cleanup_task = None
        self.ws_task = None
        self.worker_task = None
        self.reconcile_task = None
        self.take_profit_task = None
        self.tp_cursor = 0
        self.tp_sem = asyncio.Semaphore(4)
        self.tp_inflight = set()
        self.tp_check_tasks = set()
        self.reconcile_inflight = set()
        self.ws_event_tasks = set()
        self.ws_signatures_inflight = set()
        self.ws_recent_signatures = {}
        self.ws_signal_times = {}
        self.rpc_limit = asyncio.Semaphore(4)
        # Shared pacing/cooldown across ALL HTTP RPC users (monitoring + execution).
        self.rpc_pace_lock = asyncio.Lock()
        self.monitor_sem = asyncio.Semaphore(2)  # poll two distinct masters concurrently
        self.rpc_next_at = 0.0
        self.rpc_cooldown_until = 0.0
        # Default to 10 HTTP RPC requests/second; a shared 429 cooldown backs off
        # automatically. Override in /etc/tg-bot.env if the Helius plan requires it.
        try:
            rpc_interval_ms = float(os.getenv("HELIUS_RPC_MIN_INTERVAL_MS", "100"))
        except (TypeError, ValueError):
            rpc_interval_ms = 100.0
        self.rpc_spacing_seconds = max(0.05, min(1.0, rpc_interval_ms / 1000.0))
        self.trade_sem = asyncio.Semaphore(TRADE_CONCURRENCY)
        self.ws_signal = asyncio.Event()
        self.trade_wake = asyncio.Event()
        self.hot_addresses = set()
        self.poll_locks = {}
        self.poll_cooldowns = {}
        self.wallet_locks = {}
        self.price_cache = (0.0, None)
        self.inflight = set()
        self.awaiting_resolution = set()
        self.notice_throttle = {}

    # ------------------------------------------------------------------ lifecycle
    async def startup(self, app):
        if not JUP_KEY:
            raise RuntimeError("JUPITER_API_KEY is required for this live-only bot")
        self.app = app
        self.fernet = encryption_key()
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=6)
        await self.pool.execute(DDL)
        for sql in MIGRATIONS:
            await self.pool.execute(sql)
        await self.pool.execute("""
            INSERT INTO copy_wallets(user_id,name,address,encrypted_secret)
            SELECT id,'Wallet 1',address,encrypted_secret
            FROM copy_users
            WHERE address IS NOT NULL AND encrypted_secret IS NOT NULL
            ON CONFLICT(user_id,address) DO NOTHING
        """)
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=18),
            connector=aiohttp.TCPConnector(limit=20),
        )
        # Rows in QUOTING never had a signature persisted, so nothing was broadcast.
        await self.pool.execute("""
            UPDATE copy_trades
            SET status='QUEUED', message='Recovered after restart'
            WHERE status='QUOTING' AND signature IS NULL
        """)
        await self.reconcile_all()
        paused = await self.pool.fetch("""
            UPDATE copy_users SET enabled=FALSE
            WHERE enabled AND id IN (
                SELECT user_id FROM copy_trades
                WHERE status IN ('SIGNED','SUBMITTED','UNKNOWN','SUBMITTING')
                  AND COALESCE(signed_at, created_at) > now() - interval '15 minutes'
            ) RETURNING id
        """)
        for row in paused:
            self.awaiting_resolution.add(row["id"])
            await self.notify(
                row["id"],
                "Auto Trade paused"
            )
        self.monitor_task = asyncio.create_task(self.monitor())
        self.ws_task = asyncio.create_task(self.websocket_monitor())
        self.cleanup_task = asyncio.create_task(self.cleanup_keys())
        self.worker_task = asyncio.create_task(self.trade_worker())
        self.reconcile_task = asyncio.create_task(self.reconcile_loop())
        self.take_profit_task = asyncio.create_task(self.take_profit_loop())
        await app.bot.set_my_commands([
            BotCommand("start", "Open dashboard"),
            BotCommand("wallet", "Manage wallets"),
            BotCommand("send", "Send funds"),
            BotCommand("masters", "Master wallets"),
            BotCommand("settings", "Trading settings"),
            BotCommand("history", "Trade history"),
            BotCommand("pause", "Stop new trades now"),
            BotCommand("cancel", "Cancel action"),
            BotCommand("help", "Show bot commands"),
            BotCommand("menu", "Open dashboard"),
        ])
        LOG.info("Solana live copy bot started (WS + poll monitoring, DB-backed queue)")

    async def shutdown(self, app):
        for task in (self.monitor_task, self.cleanup_task, self.ws_task,
                     self.worker_task, self.reconcile_task, self.take_profit_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.inflight:
            done, pending = await asyncio.wait(self.inflight, timeout=20)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        # Cancel child tasks only after trade workers have stopped scheduling them.
        background = list(self.ws_event_tasks | self.tp_check_tasks | self.reconcile_inflight)
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        if self.session:
            await self.session.close()
        if self.pool:
            await self.pool.close()

    # ------------------------------------------------------------------ rpc + data
    async def rpc(self, method, params):
        """Globally paced Helius RPC; bounded retries and shared 429 cooldown.

        No automatic retries for sendTransaction: an uncertain broadcast must be
        reconciled by its signature, not retransmitted as a new attempt.
        """
        attempts = 1 if method == "sendTransaction" else 4
        for attempt in range(attempts):
            # Reserve a pacing slot under the lock, then wait outside it.
            # This prevents one delayed RPC from holding every other caller.
            async with self.rpc_pace_lock:
                now = time.monotonic()
                ready = max(now, self.rpc_next_at, self.rpc_cooldown_until)
                self.rpc_next_at = ready + self.rpc_spacing_seconds
            delay = ready - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            # A 429 may have established a cooldown after this slot was reserved.
            while True:
                async with self.rpc_pace_lock:
                    remaining = self.rpc_cooldown_until - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            try:
                async with self.rpc_limit:
                    async with self.session.post(RPC, json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": method, "params": params,
                    }) as response:
                        if response.status == 429:
                            retry_after = response.headers.get("Retry-After")
                            try:
                                delay = max(1.0, min(120.0, float(retry_after)))
                            except (TypeError, ValueError):
                                delay = min(60.0, 3.0 * (2 ** attempt))
                            delay += random.uniform(0.0, 0.5)
                            async with self.rpc_pace_lock:
                                self.rpc_cooldown_until = max(
                                    self.rpc_cooldown_until, time.monotonic() + delay
                                )
                            LOG.warning("Helius throttled method=%s cooldown=%.1fs", method, delay)
                            if method == "sendTransaction":
                                raise RuntimeError("Helius sendTransaction HTTP 429 (broadcast uncertain)")
                            continue
                        if response.status != 200:
                            raise RuntimeError(f"Helius RPC {method} returned HTTP {response.status}")
                        data = await response.json()
                        if data.get("error"):
                            error = data["error"]
                            raise RuntimeError(
                                f"Helius RPC {method} failed (code {error.get('code', 'unknown')})"
                            )
                        return data.get("result")
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if method == "sendTransaction" or attempt + 1 == attempts:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * 2 ** attempt))
        raise RuntimeError(f"Helius RPC {method} remains rate limited after retries")

    def wallet_lock(self, address):
        return self.wallet_locks.setdefault(address, asyncio.Lock())

    async def user(self, uid):
        await self.pool.execute(
            "INSERT INTO copy_users(id) VALUES($1) ON CONFLICT DO NOTHING", uid
        )
        return await self.pool.fetchrow("SELECT * FROM copy_users WHERE id=$1", uid)

    async def wallets(self, uid):
        return await self.pool.fetch(
            "SELECT id,name,address FROM copy_wallets WHERE user_id=$1 ORDER BY id", uid
        )

    async def wallet(self, uid, wid):
        return await self.pool.fetchrow(
            "SELECT * FROM copy_wallets WHERE id=$1 AND user_id=$2", wid, uid
        )

    async def masters(self, uid):
        return await self.pool.fetch(
            "SELECT * FROM copy_masters WHERE user_id=$1 ORDER BY id", uid
        )

    async def has_unresolved(self, uid, statuses=None):
        # Old review records remain in history, but do not lock a new session.
        # Keep active, recently signed submissions protected from overlap.
        active = [s for s in (statuses or UNRESOLVED) if s != "REVIEW_REQUIRED"]
        if not active:
            return False
        return await self.pool.fetchval("""
            SELECT EXISTS (
                SELECT 1 FROM copy_trades
                WHERE user_id=$1 AND status=ANY($2::text[])
                  AND COALESCE(signed_at, created_at) > now() - interval '15 minutes'
            )
        """, uid, active)

    async def save_wallet(self, uid, key):
        await self.user(uid)
        address = str(key.pubkey())
        encrypted = self.fernet.encrypt(bytes(key)).decode()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetchval(
                    "SELECT id FROM copy_wallets WHERE user_id=$1 AND address=$2",
                    uid, address
                )
                if existing:
                    raise ValueError("Wallet already added")
                count = await conn.fetchval(
                    "SELECT count(*) FROM copy_wallets WHERE user_id=$1", uid
                )
                if count >= 10:
                    raise ValueError("Wallet limit reached")
                wid = await conn.fetchval("""
                    INSERT INTO copy_wallets(user_id,name,address,encrypted_secret)
                    VALUES($1,$2,$3,$4) RETURNING id
                """, uid, f"Wallet {count+1}", address, encrypted)
                previous = await conn.fetchval(
                    "SELECT address FROM copy_users WHERE id=$1 FOR UPDATE", uid
                )
                if previous is None:
                    await conn.execute("""
                        UPDATE copy_users
                        SET address=$2, encrypted_secret=$3, enabled=FALSE
                        WHERE id=$1
                    """, uid, address, encrypted)
        return wid

    async def select_wallet(self, uid, wid):
        w = await self.wallet(uid, wid)
        if not w:
            raise ValueError("Wallet not found.")
        await self.pool.execute("""
            UPDATE copy_users
            SET address=$2, encrypted_secret=$3, enabled=FALSE
            WHERE id=$1
        """, uid, w["address"], w["encrypted_secret"])

    async def token_balance(self, address, mint=USDC):
        data = await self.rpc("getTokenAccountsByOwner", [
            address, {"mint": mint}, {"encoding": "jsonParsed"}
        ])
        accounts = data.get("value", [])
        if not accounts:
            return 0, 6 if mint == USDC else None
        values = [
            a["account"]["data"]["parsed"]["info"]["tokenAmount"]
            for a in accounts
        ]
        return sum(int(v["amount"]) for v in values), int(values[0]["decimals"])

    async def wallet_balances(self, address):
        sol = await self.rpc("getBalance", [address])
        usdc_raw, decimals = await self.token_balance(address)
        return (
            Decimal(sol["value"]) / Decimal(10**9),
            Decimal(usdc_raw) / Decimal(10**decimals),
        )

    async def cleanup_keys(self):
        while True:
            try:
                rows = await self.pool.fetch("""
                    SELECT chat_id,message_id FROM copy_secret_messages
                    WHERE expires_at <= now() LIMIT 100
                """)
                for row in rows:
                    try:
                        await self.app.bot.delete_message(
                            row["chat_id"], row["message_id"]
                        )
                    except (BadRequest, Forbidden):
                        pass
                    except Exception:
                        continue
                    await self.pool.execute("""
                        DELETE FROM copy_secret_messages
                        WHERE chat_id=$1 AND message_id=$2
                    """, row["chat_id"], row["message_id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Temporary-message cleanup failed")
            await asyncio.sleep(15)

    async def reveal(self, uid, wid):
        w = await self.wallet(uid, wid)
        if not w:
            raise ValueError("Wallet not found.")
        secret = str(Keypair.from_bytes(
            self.fernet.decrypt(w["encrypted_secret"].encode())
        ))
        msg = await self.app.bot.send_message(
            uid,
            f"🔑 <b>Private Key</b>\n\n<code>{html.escape(secret)}</code>\n\n"
            "Don't share this with anyone. It disappears in 1 hour.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard([
                [("📋 Copy Private Key", "copy", secret)]
            ]),
            protect_content=True,
        )
        await self.pool.execute("""
            INSERT INTO copy_secret_messages(chat_id,message_id,expires_at)
            VALUES($1,$2,now()+interval '1 hour')
            ON CONFLICT DO NOTHING
        """, uid, msg.message_id)

    async def notify(self, uid, message):
        try:
            await self.app.bot.send_message(
                uid, message, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        except (BadRequest, Forbidden):
            pass
        except Exception:
            LOG.warning("Notification to uid=%s failed", uid)

    async def pause_user(self, uid, reason, unresolved=False):
        await self.pool.execute("UPDATE copy_users SET enabled=FALSE WHERE id=$1", uid)
        await self.pool.execute("""
            UPDATE copy_trades SET status='SKIPPED', message='Auto trading paused'
            WHERE user_id=$1 AND status='QUEUED'
        """, uid)
        if unresolved:
            self.awaiting_resolution.add(uid)
        await self.notify(uid, f"<b>Auto Trade paused</b>\n{html.escape(reason)}")

    # ------------------------------------------------------------------ transfers
    async def confirm_signature(self, signature, timeout):
        deadline = time.monotonic() + timeout
        while True:
            result = await self.rpc("getSignatureStatuses", [
                [signature], {"searchTransactionHistory": True}
            ])
            state = ((result or {}).get("value") or [None])[0]
            if state:
                if state.get("err") is not None:
                    return "FAILED"
                if state.get("confirmationStatus") in ("confirmed", "finalized"):
                    return "CONFIRMED"
            if time.monotonic() >= deadline:
                return "PENDING"
            await asyncio.sleep(1.0)

    async def send_funds(self, uid, wid, asset, recipient, amount):
        w = await self.wallet(uid, wid)
        if not w:
            raise ValueError("Wallet not found.")
        if not valid_address(recipient):
            raise ValueError("Invalid wallet address")
        if recipient == w["address"]:
            raise ValueError("Same wallet selected")
        if asset not in ("SOL", "USDC"):
            raise ValueError("Unsupported asset.")
        decimals = 9 if asset == "SOL" else 6
        raw = amount * (10**decimals)
        if amount <= 0 or not amount.is_finite() or raw != raw.to_integral_value():
            raise ValueError("Invalid amount")
        raw = int(raw)
        if raw >= 2**64:
            raise ValueError("Amount too large")

        async with self.wallet_lock(w["address"]):
            u = await self.user(uid)
            if u["enabled"] and u["address"] == w["address"]:
                raise ValueError("Pause Auto Trade first")
            if await self.has_unresolved(uid):
                raise ValueError("Transaction pending")
            sender = Pubkey.from_string(w["address"])
            dest = Pubkey.from_string(recipient)
            key = Keypair.from_bytes(
                self.fernet.decrypt(w["encrypted_secret"].encode())
            )
            balance = await self.rpc("getBalance", [w["address"]])
            lamports = int(balance["value"])
            if asset == "SOL":
                if raw + 2_000_000 > lamports:
                    raise ValueError("Not enough SOL (0.002 SOL fee reserve)")
                instructions = [transfer(TransferParams(
                    from_pubkey=sender, to_pubkey=dest, lamports=raw
                ))]
            else:
                if lamports < 3_000_000:
                    raise ValueError("Not enough SOL for fees")
                available, _ = await self.token_balance(w["address"])
                if raw > available:
                    raise ValueError("Not enough USDC.")
                instructions = usdc_transfer_instructions(sender, dest, raw)

            latest = await self.rpc("getLatestBlockhash", [
                {"commitment": "confirmed"}
            ])
            tx = Transaction.new_signed_with_payer(
                instructions, sender, [key],
                Hash.from_string(latest["value"]["blockhash"])
            )
            signature = str(tx.signatures[0])
            encoded = base64.b64encode(bytes(tx)).decode()
            transfer_id = await self.pool.fetchval("""
                INSERT INTO copy_withdrawals
                (user_id,wallet_address,asset,destination,amount,signature,status)
                VALUES($1,$2,$3,$4,$5,$6,'SUBMITTING') RETURNING id
            """, uid, w["address"], asset, recipient, str(amount), signature)
            try:
                await self.rpc("sendTransaction", [
                    encoded, {
                        "encoding": "base64",
                        "skipPreflight": False,
                        "preflightCommitment": "confirmed",
                        "maxRetries": 0,
                    }
                ])
            except Exception as exc:
                await self.pool.execute(
                    "UPDATE copy_withdrawals SET status='UNKNOWN' WHERE id=$1",
                    transfer_id
                )
                raise SendUncertain(signature) from exc
            await self.pool.execute(
                "UPDATE copy_withdrawals SET status='SUBMITTED' WHERE id=$1",
                transfer_id
            )
            try:
                outcome = await self.confirm_signature(signature, 25)
            except Exception:
                outcome = "PENDING"
            if outcome in ("CONFIRMED", "FAILED"):
                await self.pool.execute(
                    "UPDATE copy_withdrawals SET status=$2 WHERE id=$1",
                    transfer_id, outcome
                )
            return signature, outcome

    # ------------------------------------------------------------------ screens
    async def screen(self, uid, page="home"):
        u = await self.user(uid)
        if page == "home":
            ms = await self.masters(uid)
            state = "🟢 ON" if u["enabled"] else "⏸ OFF"
            address = u["address"] or "Not configured"
            balances = ""
            if u["address"]:
                try:
                    sol, usdc = await self.wallet_balances(u["address"])
                    balances = f"\nSOL: <b>{sol:.4f}</b> | USDC: <b>{usdc:.2f}</b>"
                except Exception:
                    balances = "\nBalances aren't available right now."
            text = (
                "<b>Copy Trader</b>\nLive trading\n\n"
                f"Auto Trade: <b>{state}</b>\n"
                f"Master Wallets: <b>{len(ms)} / 3</b>\n"
                f"Max Trade: <b>${u['max_usd']:.2f}</b>\n"
                f"Slippage: <b>{pct(u['slip_bps'])}</b>\n"
                f"Active Wallet: <code>{address}</code>{balances}"
            )
            rows = [
                [("Pause Auto Trade" if u["enabled"] else "Enable Auto Trade", "toggle")],
            ]
            if u["enabled"]:
                rows.append([("Stop Trading", "estop")])
            rows += [
                [("Master Wallets", "masters"), ("My Wallets", "wallet")],
                [("Settings", "settings"), ("Trade History", "history")],
            ]
            if u["address"]:
                rows.append([("📋 Copy Address", "copy", u["address"])])
            rows.append([("Refresh", "home")])
            return text, keyboard(rows)

        if page == "wallet":
            ws = await self.wallets(uid)
            lines = ["👛 <b>My Wallets</b>", "Pick a wallet to manage."]
            rows = []
            for w in ws:
                active = w["address"] == u["address"]
                lines.append(
                    f"\n{'🟢' if active else '⚪'} "
                    f"<b>{html.escape(w['name'])}</b>\n"
                    f"<code>{w['address']}</code>"
                )
                rows.append([(f"👛 {w['name']}", f"walletview:{w['id']}")])
                rows.append([("📋 Copy Address", "copy", w["address"])])
            if not ws:
                lines.append("\nNo wallets")
            rows += [
                [("➕ Generate", "generate"), ("📥 Import", "import")],
                [("🏠 Home", "home")],
            ]
            return "\n".join(lines), keyboard(rows)

        if page.startswith("walletview:"):
            wid = int(page.split(":")[1])
            w = await self.wallet(uid, wid)
            if not w:
                return await self.screen(uid, "wallet")
            try:
                sol, usdc = await self.wallet_balances(w["address"])
                balances = f"SOL: <b>{sol:.6f}</b>\nUSDC: <b>{usdc:.2f}</b>"
            except Exception:
                balances = "Balances aren't available right now."
            is_active = w["address"] == u["address"]
            text = (
                f"👛 <b>{html.escape(w['name'])}</b>"
                f"{' — Active' if is_active else ''}\n\n"
                f"<code>{w['address']}</code>\n\n{balances}"
            )
            rows = [
                [("📋 Copy Address", "copy", w["address"])],
                [("📤 Send SOL", f"send:{wid}:SOL"),
                 ("📤 Send USDC", f"send:{wid}:USDC")],
                [("🔑 Private Key", f"key:{wid}")],
            ]
            if not is_active:
                rows.append([("✅ Set Active", f"active:{wid}")])
            rows += [
                [("🗑 Delete Wallet", f"del:{wid}")],
                [("👛 All Wallets", "wallet")],
            ]
            return text, keyboard(rows)

        if page == "masters":
            ms = await self.masters(uid)
            lines = ["👀 <b>Master Wallets</b>"]
            rows = []
            for m in ms:
                lines.append(
                    f"\n<b>{html.escape(m['name'])}</b> "
                    f"{'🟢' if m['active'] else '⏸'}\n"
                    f"<code>{m['address']}</code>"
                )
                rows.append([("📋 Copy Address", "copy", m["address"])])
                rows.append([
                    ("⏯ Pause/Resume", f"mastertoggle:{m['id']}"),
                    ("🗑 Remove", f"masterdelete:{m['id']}")
                ])
                rows.append([
                    ("✏️ Rename", f"masterrename:{m['id']}"),
                    ("🔁 Change Address", f"masterreplace:{m['id']}")
                ])
            if not ms:
                lines.append("\nNo master wallets")
            if len(ms) < 3:
                rows.append([("➕ Add Master", "addmaster")])
            rows.append([("🏠 Home", "home")])
            return "\n".join(lines), keyboard(rows)

        if page == "settings":
            text = (
                "⚙️ <b>Settings</b>\n\n"
                f"Max trade: ${u['max_usd']:.2f}\n"
                f"Slippage: {pct(u['slip_bps'])}\n"
                f"Copy buys: {'ON' if u['copy_buys'] else 'OFF'}\n"
                f"Copy sells: {'ON' if u['copy_sells'] else 'OFF'}\n"
                f"Check speed: every {u['poll_interval_seconds']} sec\n"
                f"Ignore trades older than: {u['max_signal_age_seconds']} sec\n"
                f"Use USDC if no SOL: {'ON' if u['allow_usdc_fallback'] else 'OFF'}\n"
                f"Pump.fun fast mode: {'ON' if u['use_pumpportal'] else 'OFF'}\n"
                f"Take profit: {'ON' if u['take_profit_enabled'] else 'OFF'} "
                f"(+{u['take_profit_pct']}%, sell {u['take_profit_sell_pct']}%)\n"
                f"Stop loss: {'ON' if u['stop_loss_enabled'] else 'OFF'} (-{u['stop_loss_pct']}%)\n"
                f"Repeat buys blocked: {'ON' if u['prevent_repeat_buys'] else 'OFF'}"
            )
            return text, keyboard([
                [("💵 Max Trade", "edit:max"),
                 ("📉 Slippage", "edit:slip")],
                [("🔁 Buys", "setting:buy"),
                 ("🔁 Sells", "setting:sell")],
                [("⏱ Check speed", "edit:poll"),
                 ("⌛ Trade age", "edit:age")],
                [("💱 Use USDC if no SOL", "setting:usdc")],
                [("🚀 Pump.fun fast mode", "setting:pump")],
                [("Take Profit On/Off", "setting:tp")],
                [("Profit Target %", "edit:tp")],
                [("Amount to Sell %", "edit:tp_sell")],
                [("Stop Loss On/Off", "setting:sl"), ("Loss Limit %", "edit:sl")],
                [("Block Repeat Buys", "setting:repeat")],
                [("Stop Trading", "estop")],
                [("🏠 Home", "home")],
            ])

        if page == "history":
            trades = await self.pool.fetch("""
                SELECT * FROM copy_trades WHERE user_id=$1
                ORDER BY id DESC LIMIT 6
            """, uid)
            lines = ["📜 <b>Recent Trades</b>"]
            rows = []
            for t in trades:
                stamp = t["created_at"].strftime("%m-%d %H:%M UTC")
                status = STATUS_WORDS.get(t["status"], t["status"])
                line = (
                    f"\n<b>{t['side']}</b> — {status}\n"
                    f"Token: <code>{t['mint']}</code>\n"
                    f"${t['usd']:.2f} • {stamp}\n"
                    f"Copied from: {shortened(t['master_address'])}"
                )
                via = VIA_WORDS.get(t["route"] or "")
                if via:
                    line += f" • Via: {via}"
                if t["status"] in ("SKIPPED", "REVIEW_REQUIRED") and t["message"]:
                    line += f"\n<i>{html.escape(str(t['message'])[:110])}</i>"
                lines.append(line)
                if t["signature"]:
                    rows.append([
                        ("📋 Copy ID", "copy", t["signature"]),
                        ("🔎 View", f"url:https://solscan.io/tx/{t['signature']}"),
                    ])
            if not trades:
                lines.append("No trades")
            rows.append([("🏠 Home", "home")])
            return "\n".join(lines), keyboard(rows)

        return await self.screen(uid, "home")

    async def show(self, update, page="home", notice=""):
        text, markup = await self.screen(update.effective_user.id, page)
        if notice:
            text = html.escape(notice) + "\n\n" + text
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text, reply_markup=markup, parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    raise
        else:
            await update.message.reply_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )

    async def start(self, update, ctx):
        if update.effective_chat.type != "private":
            return
        ctx.user_data.clear()
        await self.show(update)

    async def resolve_trade_command(self, update, ctx):
        # Operator attests independent verification for an expired, absent signature.

        # Never guesses an unobserved transaction is failed. The signed transaction
        # itself is never resent. Requires BOT_OWNER_ID configured separately.
        uid = update.effective_user.id
        if not BOT_OWNER_ID or uid != BOT_OWNER_ID or update.effective_chat.type != "private":
            return
        if len(ctx.args) != 2 or ctx.args[1] != "EXPIRED_UNCONFIRMED":
            return await update.message.reply_text(
                "Usage: /resolve_trade ID EXPIRED_UNCONFIRMED"
            )
        try:
            trade_id = int(ctx.args[0])
        except ValueError:
            return await update.message.reply_text("Invalid trade ID")
        row = await self.pool.fetchrow(
            "SELECT * FROM copy_trades WHERE id=$1", trade_id
        )
        if not row or row['status'] not in UNRESOLVED or not row['signature']:
            return await update.message.reply_text("Trade unavailable")
        if not row['signed_at'] or (
            datetime.now(timezone.utc) - row['signed_at']
        ).total_seconds() < 900:
            return await update.message.reply_text("Trade still recent")
        # Last on-chain recheck. A network failure must not release the lock.
        outcome = await self.reconcile_one(row['id'], row['signature'], row['signed_at'])
        if outcome in ('CONFIRMED', 'FAILED'):
            return await update.message.reply_text(
                f"On-chain status: {outcome}"
            )
        tx = await self.rpc('getTransaction', [row['signature'], {
            'encoding': 'jsonParsed', 'commitment': 'finalized',
            'maxSupportedTransactionVersion': 1,
        }])
        if tx is not None:
            return await update.message.reply_text(
                "Transaction found"
            )
        # This command is deliberately a human attestation, not automated proof.
        changed = await self.pool.fetchrow('''
            UPDATE copy_trades SET status='EXPIRED_UNCONFIRMED',
                message='Operator attested independent expiry and wallet-history checks'
            WHERE id=$1 AND status = ANY($2) RETURNING user_id, signature
        ''', trade_id, UNRESOLVED)
        if not changed:
            return await update.message.reply_text("Trade status changed")
        LOG.warning('OPERATOR RESOLUTION uid=%s trade_id=%s status=EXPIRED_UNCONFIRMED signature=%s',
                    uid, trade_id, changed['signature'])
        self.awaiting_resolution.add(changed['user_id'])
        await update.message.reply_text(
            f"Trade {trade_id} updated"
        )
        await self.notify(changed['user_id'],
            'Transaction reviewed')

    async def emergency_pause(self, uid):
        await self.pool.execute("UPDATE copy_users SET enabled=FALSE WHERE id=$1", uid)
        await self.pool.execute("""
            UPDATE copy_trades SET status='SKIPPED', message='Stopped by you'
            WHERE user_id=$1 AND status='QUEUED'
        """, uid)

    async def pause_command(self, update, ctx):
        if update.effective_chat.type != "private":
            return
        await self.user(update.effective_user.id)
        await self.emergency_pause(update.effective_user.id)
        await self.show(update, "home", "Auto Trade off")

    async def click(self, update, ctx):
        q = update.callback_query
        await q.answer()
        if update.effective_chat.type != "private":
            return
        uid = update.effective_user.id
        data = q.data or ""
        u = await self.user(uid)

        if data in ("home", "wallet", "masters", "settings", "history"):
            ctx.user_data.pop("pending", None)
            return await self.show(update, data)

        if data == "estop":
            await self.emergency_pause(uid)
            return await self.show(update, "home", "Auto Trade off")

        if data == "generate":
            try:
                wid = await self.save_wallet(uid, Keypair())
                return await self.show(update, f"walletview:{wid}", "Wallet created.")
            except ValueError as exc:
                return await self.show(update, "wallet", str(exc))

        if data == "import":
            ctx.user_data["pending"] = ("import",)
            return await q.edit_message_text(
                "Private key (message deleted after import):\n"
                "/cancel to exit"
            )

        if data.startswith("walletview:"):
            ctx.user_data.pop("pending", None)
            return await self.show(update, data)

        if data.startswith("key:"):
            try:
                await self.reveal(uid, int(data.split(":")[1]))
            except ValueError as exc:
                await q.message.reply_text(str(exc))
            return

        if data.startswith("active:"):
            wid = int(data.split(":")[1])
            await self.select_wallet(uid, wid)
            return await self.show(
                update, f"walletview:{wid}",
                "Wallet changed. Auto Trade off"
            )

        if data.startswith("del:"):
            wid = int(data.split(":")[1])
            w = await self.wallet(uid, wid)
            if not w:
                return await self.show(update, "wallet")
            nonce = secrets.token_hex(6)
            ctx.user_data["delete"] = (wid, nonce)
            return await q.edit_message_text(
                "🗑 <b>Delete this wallet?</b>\n\n"
                f"<code>{w['address']}</code>\n\n"
                "Save its private key first. Any funds stay in the wallet.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard([
                    [("✅ Delete", f"delconfirm:{nonce}")],
                    [("❌ Keep", f"walletview:{wid}")],
                ])
            )

        if data.startswith("delconfirm:"):
            pending = ctx.user_data.pop("delete", None)
            if not pending or pending[1] != data.split(":")[1]:
                return await self.show(update, "wallet", "Confirmation expired")
            wid = pending[0]
            w = await self.wallet(uid, wid)
            if not w:
                return await self.show(update, "wallet")
            async with self.wallet_lock(w["address"]):
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        if u["address"] == w["address"]:
                            await conn.execute(
                                "UPDATE copy_users SET enabled=FALSE WHERE id=$1", uid
                            )
                        await conn.execute(
                            "DELETE FROM copy_wallets WHERE id=$1 AND user_id=$2",
                            wid, uid
                        )
                        if u["address"] == w["address"]:
                            another = await conn.fetchrow("""
                                SELECT * FROM copy_wallets
                                WHERE user_id=$1 ORDER BY id LIMIT 1
                            """, uid)
                            await conn.execute("""
                                UPDATE copy_users
                                SET address=$2, encrypted_secret=$3
                                WHERE id=$1
                            """, uid,
                                another["address"] if another else None,
                                another["encrypted_secret"] if another else None)
            return await self.show(update, "wallet", "Wallet deleted.")

        if data.startswith("send:"):
            _, wid, asset = data.split(":")
            ctx.user_data["pending"] = ("recipient", int(wid), asset)
            return await q.edit_message_text(
                f"📤 <b>Send {asset}</b>\n\n"
                "Paste the wallet address you want to send to.",
                parse_mode=ParseMode.HTML
            )

        if data.startswith("confirm_send:"):
            confirmation = ctx.user_data.pop("transfer", None)
            if not confirmation or confirmation["nonce"] != data.split(":")[1]:
                return await self.show(update, "wallet", "Confirmation expired")
            if time.monotonic() > confirmation["expires"]:
                return await self.show(update, "wallet", "Confirmation expired")
            await q.edit_message_text("📤 Sending...")
            try:
                signature, outcome = await self.send_funds(
                    uid, confirmation["wid"], confirmation["asset"],
                    confirmation["destination"], confirmation["amount"]
                )
                title = {
                    "CONFIRMED": "✅ <b>Sent</b>",
                    "FAILED": "❌ <b>Transfer failed</b>",
                    "PENDING": "⏳ <b>Pending confirmation</b>",
                }[outcome]
                await q.edit_message_text(
                    f"{title}\n\n<code>{signature}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard([
                        [("📋 Copy ID", "copy", signature)],
                        [("🔎 View", f"url:https://solscan.io/tx/{signature}")],
                        [("👛 Wallets", "wallet")],
                    ])
                )
            except SendUncertain as exc:
                sig = str(exc)
                LOG.warning("Transfer uncertain sig=%s", shortened(sig))
                await q.edit_message_text(
                    f"Transfer pending\n<code>{html.escape(sig)}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard([
                        [("🔎 View", f"url:https://solscan.io/tx/{sig}")],
                        [("👛 Wallets", "wallet")],
                    ])
                )
            except ValueError as exc:
                await q.edit_message_text(
                    f"❌ {html.escape(str(exc))}",
                    reply_markup=keyboard([[("👛 Wallets", "wallet")]])
                )
            except Exception as exc:
                LOG.warning("Transfer failed: %s", type(exc).__name__)
                await q.edit_message_text(
                    "Transfer unavailable",
                    reply_markup=keyboard([[("👛 Wallets", "wallet")]])
                )
            return

        if data == "toggle":
            if u["enabled"]:
                await self.emergency_pause(uid)
                return await self.show(update, "home")
            if await self.has_unresolved(uid):
                return await self.show(
                    update, "history",
                    "Transaction pending"
                )
            if not u["encrypted_secret"] or not await self.masters(uid):
                return await self.show(
                    update, "home", "Add a wallet and a master wallet first."
                )
            await self.pool.execute(
                "UPDATE copy_users SET enabled=TRUE, enabled_since=now() WHERE id=$1", uid
            )
            return await self.show(update, "home")

        if data.startswith("setting:"):
            column = {
                "buy": "copy_buys",
                "sell": "copy_sells",
                "usdc": "allow_usdc_fallback",
                "pump": "use_pumpportal",
                "tp": "take_profit_enabled",
                "sl": "stop_loss_enabled",
                "repeat": "prevent_repeat_buys",
            }.get(data.split(":")[1])
            if column:
                if column == 'take_profit_enabled':
                    await self.pool.execute("""
                        UPDATE copy_users SET take_profit_enabled=NOT take_profit_enabled,
                        take_profit_since=COALESCE(take_profit_since,now())
                        WHERE id=$1
                    """,uid)
                elif column == 'stop_loss_enabled':
                    await self.pool.execute("""
                        UPDATE copy_users SET stop_loss_enabled=NOT stop_loss_enabled,
                        stop_loss_since=COALESCE(stop_loss_since,now())
                        WHERE id=$1
                    """,uid)
                else:
                    await self.pool.execute(
                        f"UPDATE copy_users SET {column}=NOT {column} WHERE id=$1", uid
                    )
            return await self.show(update, "settings")

        if data.startswith("edit:"):
            mode = data.split(":")[1]
            ctx.user_data["pending"] = mode
            prompts = {
                "max": "Trade amount (USD):",
                "slip": "Slippage (%):",
                "poll": "Check interval (seconds):",
                "age": "Maximum trade age (seconds):",
                "tp": "Take-profit target (%):",
                "tp_sell": "Amount to sell (%):",
                "sl": "Stop loss (%):",
            }
            return await q.edit_message_text(prompts.get(mode, "Unknown setting"))

        if data == "addmaster":
            if len(await self.masters(uid)) >= 3:
                return await self.show(update, "masters", "Maximum 3 masters")
            ctx.user_data["pending"] = "master"
            return await q.edit_message_text(
                "Master wallet address:"
            )

        if data.startswith("mastertoggle:"):
            mid = int(data.split(":")[1])
            await self.pool.execute("""
                UPDATE copy_masters SET active=NOT active
                WHERE id=$1 AND user_id=$2
            """, mid, uid)
            return await self.show(update, "masters")

        if data.startswith("masterrename:"):
            ctx.user_data["pending"] = ("mrename", int(data.split(":")[1]))
            return await q.edit_message_text("Master name:")

        if data.startswith("masterreplace:"):
            ctx.user_data["pending"] = ("mreplace", int(data.split(":")[1]))
            return await q.edit_message_text(
                "New master address:"
            )

        if data.startswith("masterdelete:"):
            mid = int(data.split(":")[1])
            ctx.user_data["remove_master"] = mid
            return await q.edit_message_text(
                "Remove this master wallet?",
                reply_markup=keyboard([
                    [("✅ Remove", f"removeconfirm:{mid}")],
                    [("❌ Cancel", "masters")],
                ])
            )

        if data.startswith("removeconfirm:"):
            mid = int(data.split(":")[1])
            if ctx.user_data.pop("remove_master", None) != mid:
                return await self.show(update, "masters")
            await self.pool.execute("""
                DELETE FROM copy_masters WHERE id=$1 AND user_id=$2
            """, mid, uid)
            return await self.show(update, "masters")

        return await self.show(update, "home")

    async def text(self, update, ctx):
        if update.effective_chat.type != "private":
            return
        uid = update.effective_user.id
        pending = ctx.user_data.get("pending")
        value = update.message.text.strip()
        if not pending:
            return await self.show(update)
        try:
            if isinstance(pending, tuple) and pending[0] == "import":
                try:
                    await update.message.delete()
                except (BadRequest, Forbidden):
                    pass
                key = import_key(value)
                wid = await self.save_wallet(uid, key)
                ctx.user_data.pop("pending", None)
                return await self.show(update, f"walletview:{wid}", "Wallet added.")

            if isinstance(pending, tuple) and pending[0] == "recipient":
                _, wid, asset = pending
                if not valid_address(value):
                    raise ValueError("Invalid wallet address")
                w = await self.wallet(uid, wid)
                if not w or w["address"] == value:
                    raise ValueError("Use a different wallet")
                ctx.user_data["pending"] = ("amount", wid, asset, value)
                return await update.message.reply_text(
                    f"How much {asset} do you want to send?"
                )

            if isinstance(pending, tuple) and pending[0] == "amount":
                _, wid, asset, destination = pending
                amount = amount_number(value)
                decimals = 9 if asset == "SOL" else 6
                scaled = amount * (10**decimals)
                if amount <= 0 or scaled != scaled.to_integral_value():
                    raise ValueError("Invalid amount or decimals")
                nonce = secrets.token_hex(8)
                ctx.user_data.pop("pending", None)
                ctx.user_data["transfer"] = {
                    "nonce": nonce, "wid": wid, "asset": asset,
                    "destination": destination, "amount": amount,
                    "expires": time.monotonic() + 180,
                }
                w = await self.wallet(uid, wid)
                return await update.message.reply_text(
                    "📤 <b>Confirm</b>\n\n"
                    f"From: <code>{w['address']}</code>\n"
                    f"To: <code>{destination}</code>\n"
                    f"Amount: <b>{amount} {asset}</b>\n\n"
                    "Sent funds can't be taken back.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard([
                        [("✅ Confirm & Send", f"confirm_send:{nonce}")],
                        [("❌ Cancel", f"walletview:{wid}")],
                        [("📋 Copy Recipient", "copy", destination)],
                    ])
                )

            if pending == "master" or (isinstance(pending, tuple) and pending[0] == "mreplace"):
                if not valid_address(value):
                    raise ValueError("Invalid wallet address")
                if await self.pool.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM copy_wallets WHERE user_id=$1 AND address=$2)",
                    uid, value
                ):
                    raise ValueError("Cannot copy own wallet")
                if pending == "master":
                    if len(await self.masters(uid)) >= 3:
                        raise ValueError("Maximum 3 masters")
                    if await self.pool.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM copy_masters WHERE user_id=$1 AND address=$2)",
                        uid, value
                    ):
                        raise ValueError("Master already added")
                    ctx.user_data["pending"] = ("nickname", value)
                    return await update.message.reply_text("Master name:")
                await self.pool.execute(
                    "UPDATE copy_masters SET address=$3 WHERE id=$1 AND user_id=$2",
                    pending[1], uid, value
                )
                ctx.user_data.pop("pending", None)
                return await self.show(update, "masters", "Master address changed.")

            if isinstance(pending, tuple) and pending[0] == "nickname":
                await self.pool.execute("""
                    INSERT INTO copy_masters(user_id,address,name)
                    VALUES($1,$2,$3)
                """, uid, pending[1], value[:24] or "Trader")
                ctx.user_data.pop("pending", None)
                return await self.show(update, "masters", "Master added.")

            if isinstance(pending, tuple) and pending[0] == "mrename":
                await self.pool.execute(
                    "UPDATE copy_masters SET name=$3 WHERE id=$1 AND user_id=$2",
                    pending[1], uid, value[:24] or "Trader"
                )
                ctx.user_data.pop("pending", None)
                return await self.show(update, "masters", "Master renamed.")

            if pending in ("max", "slip", "poll", "age", "tp", "tp_sell", "sl"):
                n = amount_number(value)
                if pending == "tp_sell":
                    if not Decimal("1") <= n <= Decimal("100"):
                        raise ValueError("Sell: 1–100%")
                    await self.pool.execute(
                        "UPDATE copy_users SET take_profit_sell_pct=$2 WHERE id=$1",
                        uid, n.quantize(Decimal("0.01"))
                    )
                elif pending == "tp":
                    if not Decimal("1") <= n <= Decimal("500"):
                        raise ValueError("Target: 1–500%")
                    await self.pool.execute(
                        "UPDATE copy_users SET take_profit_pct=$2 WHERE id=$1",
                        uid, n.quantize(Decimal("0.01"))
                    )
                elif pending == "sl":
                    if not Decimal("1") <= n <= Decimal("99"):
                        raise ValueError("Loss: 1–99%")
                    await self.pool.execute(
                        "UPDATE copy_users SET stop_loss_pct=$2 WHERE id=$1",
                        uid, n.quantize(Decimal("0.01"))
                    )
                elif pending == "poll":
                    if not Decimal("0.5") <= n <= Decimal("10"):
                        raise ValueError("Range: 0.5–10")
                    if n != n.quantize(Decimal("0.1")):
                        raise ValueError("Use 0.1 increments")
                    await self.pool.execute(
                        "UPDATE copy_users SET poll_interval_seconds=$2 WHERE id=$1", uid, n
                    )
                elif pending == "age":
                    if n != n.to_integral_value() or not 10 <= n <= 180:
                        raise ValueError("Range: 10–180 (whole number)")
                    await self.pool.execute(
                        "UPDATE copy_users SET max_signal_age_seconds=$2 WHERE id=$1",
                        uid, int(n)
                    )
                elif pending == "max":
                    if not Decimal("0.5") <= n <= Decimal("1000"):
                        raise ValueError("Range: $0.50–$1,000")
                    await self.pool.execute(
                        "UPDATE copy_users SET max_usd=$2 WHERE id=$1",
                        uid, n.quantize(Decimal(".01"))
                    )
                else:
                    if not Decimal(".1") <= n <= Decimal("70"):
                        raise ValueError("Range: 0.1–70%")
                    await self.pool.execute(
                        "UPDATE copy_users SET slip_bps=$2 WHERE id=$1",
                        uid, int((n * 100).to_integral_value())
                    )
                ctx.user_data.pop("pending", None)
                return await self.show(update, "settings", "Saved.")
        except asyncpg.UniqueViolationError:
            await update.message.reply_text("❌ Master already added")
        except ValueError as exc:
            await update.message.reply_text(f"❌ {html.escape(str(exc))}")

    async def cancel(self, update, ctx):
        ctx.user_data.clear()
        await self.show(update, "home", "Cancelled.")

    # ------------------------------------------------------------------ pumpportal
    async def portal_transaction(self, wallet, mint, side, amount, slip):
        """Third-party local builder. Never send private keys."""
        args = {
            "publicKey": wallet,
            "action": side.lower(),
            "mint": mint,
            "amount": amount if isinstance(amount, str) else str(amount),
            "denominatedInSol": "true" if side == "BUY" else "false",
            "slippage": float(Decimal(slip) / 100),
            "priorityFee": float(PRIORITY_FEE_SOL),
            "pool": "auto",
        }
        async with self.session.post(
            PUMPPORTAL_URL, json=args,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as response:
            blob = await response.read()
            if response.status != 200:
                raise ValueError(
                    f"PumpPortal builder rejected request HTTP {response.status}"
                )
            if len(blob) > 100000 or len(blob) < 30:
                raise RuntimeError("Invalid PumpPortal serialized transaction")
            return blob

    def validate_portal_tx(self, blob, key, side, mint, spend_lamports, slip_bps):
        """Strict validation of the third-party transaction. Raises ValueError if unsafe."""
        tx = VersionedTransaction.from_bytes(blob)
        msg = tx.message
        keys = [str(k) for k in msg.account_keys]
        wallet = str(key.pubkey())
        if msg.header.num_required_signatures != 1 or not keys or keys[0] != wallet:
            raise ValueError("PumpPortal transaction has unexpected signer/fee payer")
        lookups = list(getattr(msg, "address_table_lookups", []) or [])
        if lookups:
            raise ValueError(
                "PumpPortal address-lookup transactions require account resolution "
                "before they can be validated; trade blocked"
            )
        if mint not in keys:
            raise ValueError("PumpPortal transaction does not reference the target mint")

        system_out = 0
        ata_count = 0
        price = 0
        limit = None
        saw_pump = False
        non_budget = 0
        for ix in msg.instructions:
            program = keys[ix.program_id_index]
            data = bytes(ix.data)
            accts = [keys[i] if i < len(keys) else None for i in bytes(ix.accounts)]
            if program not in PORTAL_ALLOWED_PROGRAMS:
                raise ValueError(f"PumpPortal transaction uses unexpected program {program}")
            if program != COMPUTE_BUDGET_ID:
                non_budget += 1
            if program in (PUMP_PROGRAM, PUMPSWAP_PROGRAM):
                saw_pump = True
            elif program == SYSTEM_ID:
                kind = int.from_bytes(data[:4], "little")
                if kind in (0, 2):
                    if not accts or accts[0] != wallet or len(data) < 12:
                        raise ValueError("System instruction does not originate from wallet")
                    system_out += int.from_bytes(data[4:12], "little")
                else:
                    raise ValueError(f"Disallowed System instruction type {kind}")
            elif program in (TOKEN_PROGRAM_ID, TOKEN_2022_ID):
                op = data[0] if data else -1
                if op in (1, 16, 17, 18, 22):
                    pass
                elif op == 9:
                    if len(accts) < 2 or accts[1] != wallet:
                        raise ValueError("Token close sends funds to a foreign account")
                else:
                    raise ValueError(f"Disallowed token instruction {op}")
            elif program == ATA_PROGRAM_ID:
                ata_count += 1
                if ata_count > 4:
                    raise ValueError("Too many associated-token-account creations")
            elif program == COMPUTE_BUDGET_ID:
                op = data[0] if data else -1
                if op == 3 and len(data) >= 9:
                    price = int.from_bytes(data[1:9], "little")
                elif op == 2 and len(data) >= 5:
                    limit = int.from_bytes(data[1:5], "little")
                elif op not in (1, 4):
                    raise ValueError("Unexpected compute-budget instruction")
        if not saw_pump:
            raise ValueError("Builder returned transaction without Pump/PumpSwap program")
        effective_limit = limit if limit is not None else 200_000 * max(1, non_budget)
        if price * effective_limit // 1_000_000 > MAX_PRIORITY_LAMPORTS:
            raise ValueError("Builder priority fee is too high")
        if side == "BUY":
            cap = int(spend_lamports * (10_000 + slip_bps) // 10_000) + PORTAL_RENT_ALLOWANCE
        else:
            cap = PORTAL_RENT_ALLOWANCE
        if system_out > cap:
            raise ValueError("Builder transaction moves more SOL than allowed")
        return msg

    def sign_portal_transaction(self, blob, key, side, mint, spend_lamports, slip_bps):
        msg = self.validate_portal_tx(blob, key, side, mint, spend_lamports, slip_bps)
        signed = VersionedTransaction(msg, [key])
        signature = signed.signatures[0]
        if not signature.verify(key.pubkey(), to_bytes_versioned(msg)):
            raise RuntimeError("Invalid local PumpPortal transaction signature")
        return str(signature), base64.b64encode(bytes(signed)).decode()

    async def submit_portal(self, signed_b64):
        return await self.rpc("sendTransaction", [
            signed_b64,
            {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 0,
            },
        ])

    # ------------------------------------------------------------------ websocket
    @staticmethod
    def normalize_helius_transaction(notification):
        """Normalize Helius transactionSubscribe payload to getTransaction shape."""
        if not isinstance(notification, dict):
            return None
        outer = notification.get("transaction")
        if isinstance(outer, dict) and "transaction" in outer and "meta" in outer:
            transaction = outer.get("transaction")
            meta = outer.get("meta")
        else:
            transaction = outer
            meta = notification.get("meta")
        # jsonParsed should return a transaction object. If the provider returns
        # a base64 tuple instead, the caller falls back to HTTP getTransaction.
        if not isinstance(transaction, dict) or not isinstance(meta, dict):
            return None
        if "message" not in transaction:
            return None
        return {
            "transaction": transaction,
            "meta": meta,
            "blockTime": notification.get("blockTime"),
        }

    def schedule_ws_transaction(self, address, notification):
        """Keep the socket reader free; classify and queue in independent tasks."""
        signature = notification.get("signature") if isinstance(notification, dict) else None
        if not signature:
            return
        key = (address, signature)
        if key in self.ws_signatures_inflight or key in self.ws_recent_signatures:
            return
        received = time.monotonic()
        self.ws_signatures_inflight.add(key)
        task = asyncio.create_task(
            self.process_ws_transaction(address, notification, received)
        )
        self.ws_event_tasks.add(task)
        task.add_done_callback(self.ws_event_tasks.discard)

    async def process_ws_transaction(self, address, notification, received_at):
        """Queue a master buy directly from the pushed full transaction payload."""
        signature = notification.get("signature")
        key = (address, signature)
        try:
            tx = self.normalize_helius_transaction(notification)
            if tx is None:
                # Defensive compatibility fallback; this should not happen with
                # transactionDetails=full + encoding=jsonParsed.
                tx = await self.get_transaction(signature)
            if not tx or not tx.get("meta") or tx["meta"].get("err") is not None:
                return
            block_time = notification.get("blockTime") or int(time.time())
            swap, reason = self.classify_swap(tx, address)
            classified_ms = (time.monotonic() - received_at) * 1000
            if not swap:
                LOG.debug("WS ignored wallet=%s sig=%s reason=%s classify_ms=%.1f",
                          shortened(address), shortened(signature), reason, classified_ms)
                return
            side, mint, fraction, venue = swap
            followers = await self.pool.fetch("""
                SELECT m.user_id, u.enabled_since
                FROM copy_masters m
                JOIN copy_users u ON u.id=m.user_id
                WHERE m.address=$1 AND m.active AND u.enabled
            """, address)
            queued = 0
            for follower in followers:
                since = follower["enabled_since"]
                if since is not None and block_time < since.timestamp():
                    continue
                inserted = await self.pool.fetchrow("""
                    INSERT INTO copy_trades
                    (user_id,master_address,master_signature,side,mint,status,
                     block_time,fraction,venue)
                    VALUES($1,$2,$3,$4,$5,'QUEUED',$6,$7,$8)
                    ON CONFLICT DO NOTHING RETURNING id
                """, follower["user_id"], address, signature, side, mint,
                    int(block_time), fraction, venue)
                if inserted:
                    queued += 1
                    self.ws_signal_times[(follower["user_id"], signature)] = received_at
                    if len(self.ws_signal_times) > 5000:
                        self.ws_signal_times.pop(next(iter(self.ws_signal_times)), None)
                    receive_to_queue_ms = (time.monotonic() - received_at) * 1000
                    LOG.info("WS queued side=%s uid=%s mint=%s sig=%s classify_ms=%.1f receive_to_queue_ms=%.1f",
                             side, follower["user_id"], shortened(mint),
                             shortened(signature), classified_ms, receive_to_queue_ms)
            if queued:
                self.trade_wake.set()
            # Keep the dedupe window bounded; DB uniqueness remains the final guard.
            self.ws_recent_signatures[key] = time.monotonic()
            if len(self.ws_recent_signatures) > 5000:
                oldest = next(iter(self.ws_recent_signatures))
                self.ws_recent_signatures.pop(oldest, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.error("WS transaction processing failed wallet=%s sig=%s type=%s",
                      shortened(address), shortened(signature), type(exc).__name__)
        finally:
            self.ws_signatures_inflight.discard(key)

    async def websocket_monitor(self):
        """Enhanced processed transaction stream; confirmed logs + polling fallback."""
        backoff = 1
        while True:
            try:
                ws_url = RPC.replace("https://", "wss://", 1)
                async with self.session.ws_connect(ws_url, heartbeat=25) as ws:
                    LOG.info("WebSocket connected")
                    backoff = 1
                    subs = {}      # subscription id -> (address, mode)
                    active = {}    # address -> (subscription id, mode)
                    pending = {}   # request id -> (address, mode, sent_at)
                    req = 0
                    last_sync = 0.0
                    while True:
                        now = time.monotonic()
                        if now - last_sync >= 3:
                            last_sync = now
                            rows = await self.pool.fetch("""
                                SELECT DISTINCT m.address
                                FROM copy_masters m
                                JOIN copy_users u ON u.id=m.user_id
                                WHERE m.active AND u.enabled
                            """)
                            desired = {r["address"] for r in rows}
                            in_flight = {a for a, _, _ in pending.values()}
                            for addr in desired - set(active) - in_flight:
                                req += 1
                                pending[req] = (addr, "enhanced", now)
                                await ws.send_json({
                                    "jsonrpc": "2.0", "id": req,
                                    "method": "transactionSubscribe",
                                    "params": [
                                        {"accountInclude": [addr], "vote": False, "failed": False},
                                        {"commitment": "processed", "encoding": "jsonParsed",
                                         "transactionDetails": "full", "showRewards": False,
                                         "maxSupportedTransactionVersion": 1},
                                    ],
                                })
                            for addr, (sid, mode) in list(active.items()):
                                if addr not in desired:
                                    active.pop(addr, None)
                                    subs.pop(sid, None)
                                    req += 1
                                    method = "transactionUnsubscribe" if mode == "enhanced" else "logsUnsubscribe"
                                    await ws.send_json({"jsonrpc": "2.0", "id": req,
                                                        "method": method, "params": [sid]})
                            for _, (_, _, sent) in pending.items():
                                if now - sent > 10:
                                    raise RuntimeError("subscription acknowledgment timeout")
                        try:
                            msg = await ws.receive(timeout=1)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            rid = payload.get("id")
                            if rid in pending:
                                addr, mode, sent = pending.pop(rid)
                                if payload.get("error"):
                                    if mode == "enhanced":
                                        # Free/unentitled endpoints reject this method;
                                        # immediately retain the old confirmed fallback.
                                        LOG.warning("Enhanced WS unavailable; using confirmed logs wallet=%s",
                                                    shortened(addr))
                                        req += 1
                                        pending[req] = (addr, "logs", time.monotonic())
                                        await ws.send_json({
                                            "jsonrpc": "2.0", "id": req,
                                            "method": "logsSubscribe",
                                            "params": [{"mentions": [addr]}, {"commitment": "confirmed"}],
                                        })
                                        continue
                                    raise RuntimeError("standard logs subscription rejected")
                                sid = payload.get("result")
                                subs[sid] = (addr, mode)
                                active[addr] = (sid, mode)
                                LOG.info("WebSocket subscription acknowledged wallet=%s mode=%s active=%d",
                                         shortened(addr), mode, len(active))
                            elif payload.get("method") == "transactionNotification":
                                params = payload.get("params") or {}
                                info = subs.get(params.get("subscription"))
                                result = params.get("result") or {}
                                if info and info[1] == "enhanced":
                                    self.schedule_ws_transaction(info[0], result)
                            elif payload.get("method") == "logsNotification":
                                params = payload.get("params") or {}
                                info = subs.get(params.get("subscription"))
                                value = (params.get("result") or {}).get("value") or {}
                                if info and info[1] == "logs" and not value.get("err"):
                                    self.hot_addresses.add(info[0])
                                    self.ws_signal.set()
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("WebSocket unavailable (%s); polling remains active",
                            type(exc).__name__)
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)

    # ------------------------------------------------------------------ jupiter
    async def jupiter_order(self, source, destination, amount, wallet=None, slippage=None):
        params = {
            "inputMint": source,
            "outputMint": destination,
            "amount": str(int(amount)),
        }
        if wallet:
            params["taker"] = wallet
        if slippage is not None:
            params["slippageBps"] = str(int(slippage))
        headers = {"x-api-key": JUP_KEY} if JUP_KEY else {}

        async with self.session.get(
            JUP + "/order", params=params, headers=headers
        ) as response:
            data = await response.json(content_type=None)
            if response.status != 200:
                if 400 <= response.status < 500 and response.status != 429:
                    raise ValueError("Token unavailable")
                raise RuntimeError(f"Jupiter order HTTP {response.status}")
            if not isinstance(data, dict) or not data.get("outAmount"):
                raise ValueError("Token unavailable")
            if wallet:
                if not data.get("transaction") or not data.get("requestId"):
                    raise ValueError("Token unavailable")
                if data.get("inputMint") and data["inputMint"] != source:
                    raise ValueError("Trade details changed")
                if data.get("outputMint") and data["outputMint"] != destination:
                    raise ValueError("Trade details changed")
                if data.get("inAmount") and int(data["inAmount"]) > int(amount):
                    raise ValueError("Trade cost exceeded limit")
            return data

    async def sol_usd(self):
        now = time.monotonic()
        ts, cached = self.price_cache
        if cached is not None and now - ts < 12:
            return cached
        quote = await self.jupiter_order(WSOL, USDC, 100_000_000)
        value = Decimal(str(quote["outAmount"])) / Decimal(100_000)
        if value <= 0:
            raise RuntimeError("Invalid SOL/USD quote")
        self.price_cache = (now, value)
        return value

    async def jupiter_execute(self, order, key):
        if str(order.get("transactionVersion", "0")) not in ("0", "legacy"):
            raise ValueError("Trade rejected")
        tx = VersionedTransaction.from_bytes(base64.b64decode(order["transaction"]))
        required = tx.message.header.num_required_signatures
        signers = list(tx.message.account_keys[:required])
        if key.pubkey() not in signers:
            raise ValueError("Trade rejected")

        signatures = list(tx.signatures)
        message_data = to_bytes_versioned(tx.message)
        local_signature = key.sign_message(message_data)
        if not local_signature.verify(key.pubkey(), message_data):
            raise RuntimeError("Local signature verification failed")
        signatures[signers.index(key.pubkey())] = local_signature
        signed = VersionedTransaction.populate(tx.message, signatures)
        signed_signature = str(signed.signatures[0])
        if signed.signatures[0] == Signature.default():
            raise ValueError("Trade rejected")
        headers = {"x-api-key": JUP_KEY}
        return signed_signature, base64.b64encode(bytes(signed)).decode(), headers

    async def submit_jupiter(self, order, signed_b64, headers):
        async with self.session.post(
            JUP + "/execute",
            json={"signedTransaction": signed_b64, "requestId": order["requestId"]},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=40),
        ) as response:
            data = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError(f"Jupiter execute HTTP {response.status}")
            if not isinstance(data, dict):
                raise RuntimeError("Jupiter returned invalid execution result")
            return data

    # ------------------------------------------------------------------ classification
    def classify_swap(self, tx, master):
        """Return ((side, mint, fraction, venue), "") or (None, reason)."""
        if not tx or not tx.get("meta") or tx["meta"].get("err"):
            return None, "Failed or unavailable transaction"

        meta = tx["meta"]
        log_lines = meta.get("logMessages") or []
        logs = " ".join(log_lines).lower()
        programs = set()
        for line in log_lines:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "Program" and parts[2].startswith("invoke"):
                programs.add(parts[1])
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = [
            k.get("pubkey") if isinstance(k, dict) else k
            for k in msg.get("accountKeys", [])
        ]
        loaded = meta.get("loadedAddresses") or {}
        all_keys = set(keys) | set(loaded.get("writable") or []) | set(loaded.get("readonly") or [])
        programs |= {p for p in (PUMP_PROGRAM, PUMPSWAP_PROGRAM, JUP_PROGRAM) if p in all_keys}

        swap_words = (
            "instruction: swap", "instruction: route", "instruction: buy",
            "instruction: sell", "instruction: sharedaccountsroute",
            "instruction: exactoutroute", "swapbasein", "swapbaseout",
            "instruction: buyexactin", "instruction: sellall",
            "instruction: buyexactout", "instruction: buyexactsolin",
            "instruction: buy_exact_sol_in", "instruction: sell_exact_sol_out",
        )
        pump_present = bool(programs & {PUMP_PROGRAM, PUMPSWAP_PROGRAM})
        if not pump_present and not any(word in logs for word in swap_words):
            return None, "No recognized swap or buy/sell instruction"
        venue = detect_venue(programs)

        def owner_balances(entries):
            result = {}
            for item in entries or ():
                if item.get("owner") != master or not item.get("mint"):
                    continue
                amount = int((item.get("uiTokenAmount") or {}).get("amount", "0"))
                result[item["mint"]] = result.get(item["mint"], 0) + amount
            return result

        before = owner_balances(meta.get("preTokenBalances"))
        after = owner_balances(meta.get("postTokenBalances"))
        delta = {m: after.get(m, 0) - before.get(m, 0) for m in before.keys() | after.keys()}
        gained = [(m, n) for m, n in delta.items() if n > 0 and m not in QUOTE_MINTS]
        lost = [(m, n) for m, n in delta.items() if n < 0 and m not in QUOTE_MINTS]
        quote_spent = any(delta.get(q, 0) < 0 for q in QUOTE_MINTS)
        quote_gained = any(delta.get(q, 0) > 0 for q in QUOTE_MINTS)

        if master in keys:
            idx = keys.index(master)
            pre_s = meta.get("preBalances") or []
            post_s = meta.get("postBalances") or []
            if idx < len(pre_s) and idx < len(post_s):
                fee = int(meta.get("fee") or 0) if keys and keys[0] == master else 0
                sol_delta = int(post_s[idx]) - int(pre_s[idx]) + fee
                if sol_delta < -1_000_000:
                    quote_spent = True
                if sol_delta > 1_000_000:
                    quote_gained = True

        if len(gained) == 1 and len(lost) == 0 and quote_spent:
            return ("BUY", gained[0][0], None, venue), ""

        # Some Pump.fun purchases are funded by temporary/inner SOL accounts;
        # master wallet's net SOL change can look like zero. Do not treat a
        # token receipt alone as a buy: require a successful Pump invocation,
        # explicit Pump BUY instruction log, AND master as transaction signer.
        parsed_keys = msg.get("accountKeys") or []
        master_signed = any(
            isinstance(k, dict) and k.get("pubkey") == master and k.get("signer") is True
            for k in parsed_keys
        )
        pump_buy_log = any(re.search(
            r"instruction:\s*(?:buy|buyexactin|buyexactout|buyexactsolin|buy_exact_sol_in)\b",
            line, re.IGNORECASE
        ) for line in log_lines)
        if (len(gained) == 1 and not lost and pump_present and
                pump_buy_log and master_signed):
            LOG.info("Pump buy inferred from verified instruction, mint=%s", shortened(gained[0][0]))
            return ("BUY", gained[0][0], None, venue), ""

        if len(lost) == 1 and len(gained) == 0 and quote_gained:
            available_before = before.get(lost[0][0], 0)
            fraction = (
                Decimal(-lost[0][1]) / Decimal(available_before)
                if available_before > 0 else Decimal(1)
            )
            return ("SELL", lost[0][0], min(Decimal(1), fraction), venue), ""

        if len(gained) == 1 and len(lost) == 1:
            return None, "Token-to-token swap (not safely copyable yet)"
        return None, (
            f"Ambiguous swap: gained={len(gained)} lost={len(lost)} "
            f"quote_spent={quote_spent} quote_gained={quote_gained}"
        )

    # ------------------------------------------------------------------ reconciliation
    async def finalize_trade(self, tid, status, message):
        return await self.pool.fetchrow("""
            UPDATE copy_trades SET status=$2, message=$3
            WHERE id=$1 AND status = ANY($4)
            RETURNING user_id,side,mint,usd,signature,route,auto_exit_kind
        """, tid, status, message, UNRESOLVED)

    async def announce_trade(self, row, status):
        sig = row["signature"]
        link = f"https://solscan.io/tx/{sig}" if sig else ""
        if status == "CONFIRMED":
            verb = "Bought" if row["side"] == "BUY" else "Sold"
            if row["side"] == "SELL":
                verb = {"SL": "Stop loss sold", "TP": "Take profit sold"}.get(
                    row["auto_exit_kind"], verb)
            usd = f" (about ${row['usd']:.2f})" if row["usd"] else ""
            via = VIA_WORDS.get(row["route"] or "")
            text = (
                f"<b>{verb}</b>{usd}\n"
                f"Token: <code>{row['mint']}</code>\n"
                + (f"Via: {via}\n" if via else "")
                + (f"<a href=\"{link}\">View</a>" if link else "")
            )
        else:
            text = (
                f"<b>{row['side'].title()} failed</b>\n"
                f"Token: <code>{row['mint']}</code>\n"
                + (f"<a href=\"{link}\">View</a>" if link else "")
            )
        await self.notify(row["user_id"], text)

    async def record_confirmed_position(self, tid):
        """Idempotent, only count owner-held tokens actually received on-chain."""
        trade = await self.pool.fetchrow("SELECT * FROM copy_trades WHERE id=$1", tid)
        if not trade or not trade['signature']:
            return
        tx = await self.rpc('getTransaction', [trade['signature'], {
            'encoding': 'jsonParsed', 'commitment': 'confirmed',
            'maxSupportedTransactionVersion': 1,
        }])
        if not tx or (tx.get('meta') or {}).get('err') is not None:
            return
        if trade['side'] == 'BUY':
            u = await self.user(trade['user_id'])
            # Only manage positions opened after a risk feature was enabled.
            # Persist the first activation time so toggling a feature doesn't
            # lose the cost basis of a previously tracked position.
            since = [t for t in (u['take_profit_since'], u['stop_loss_since']) if t is not None]
            if not since or trade['created_at'] < min(since):
                return
            # Use the actual transaction signer, not the currently selected wallet.
            # A user can switch wallets after a buy has already confirmed.
            msg = tx.get('transaction', {}).get('message', {})
            account_keys = msg.get('accountKeys') or []
            first = account_keys[0] if account_keys else None
            wallet = first.get('pubkey') if isinstance(first, dict) else first
            if not wallet or not valid_address(wallet):
                LOG.warning('Position skipped: invalid transaction signer trade_id=%s', tid)
                return
            # Never register a position unless it belongs to this user's wallet.
            is_owned_wallet = await self.pool.fetchval('''
                SELECT EXISTS(SELECT 1 FROM copy_wallets
                              WHERE user_id=$1 AND address=$2)
            ''', trade['user_id'], wallet)
            if not is_owned_wallet:
                LOG.warning('Position skipped: signer not owned by user trade_id=%s', tid)
                return
            def owned(entries):
                value = 0
                dec = None
                for b in entries or ():
                    if b.get('owner') == wallet and b.get('mint') == trade['mint']:
                        token = b.get('uiTokenAmount') or {}
                        value += int(token.get('amount', 0))
                        dec = int(token['decimals'])
                return value, dec
            pre, _ = owned(tx['meta'].get('preTokenBalances'))
            post, dec = owned(tx['meta'].get('postTokenBalances'))
            received = post - pre
            if received <= 0 or dec is None or Decimal(trade['usd']) <= 0:
                LOG.warning('Position not tracked: missing token delta or cost trade_id=%s', tid)
                return
            await self.pool.execute('''
                INSERT INTO copy_positions(trade_id,user_id,wallet,mint,token_amount,decimals,cost_usd)
                VALUES($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT(trade_id) DO NOTHING
            ''',tid, trade['user_id'],wallet,trade['mint'],received,dec,trade['usd'])
            LOG.info('Position recorded trade_id=%s tokens=%s',tid,received)
        elif trade['side'] == 'SELL':
            # A confirmed manual/master sell may change holdings. Suspend old lots
            # rather than selling quantities no longer attributable to those lots.
            if trade['tp_position_id'] is not None:
                lot_id = trade['tp_position_id']
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        lot = await conn.fetchrow('''
                            SELECT token_amount,cost_usd,status,wallet FROM copy_positions
                            WHERE id=$1 FOR UPDATE
                        ''',lot_id)
                        if lot and lot['status'] == 'SELLING':
                            held = int(lot['token_amount'])
                            sold = int(trade['tp_raw_amount'] or 0)
                            if 0 < sold <= held:
                                lot_wallet = lot['wallet']
                                pre = sum(int((b.get('uiTokenAmount') or {}).get('amount', 0))
                                          for b in (tx['meta'].get('preTokenBalances') or [])
                                          if b.get('owner') == lot_wallet and b.get('mint') == trade['mint'])
                                post = sum(int((b.get('uiTokenAmount') or {}).get('amount', 0))
                                           for b in (tx['meta'].get('postTokenBalances') or [])
                                           if b.get('owner') == lot_wallet and b.get('mint') == trade['mint'])
                                if pre - post < sold:
                                    await conn.execute("UPDATE copy_positions SET status='REVIEW' WHERE id=$1", lot_id)
                                    return
                                remaining = held - sold
                                remaining_cost = (Decimal(lot['cost_usd'])
                                                  * Decimal(remaining) / Decimal(held))
                                await conn.execute('''
                                    UPDATE copy_positions
                                    SET token_amount=$2, cost_usd=$3, status=$4
                                    WHERE id=$1 AND status='SELLING'
                                ''',lot_id,remaining,remaining_cost,
                                    'CLOSED' if remaining == 0 else 'PARTIAL')
                            else:
                                await conn.execute("UPDATE copy_positions SET status='REVIEW' WHERE id=$1", lot_id)
            else:
                await self.pool.execute('''
                    UPDATE copy_positions SET status='REVIEW'
                    WHERE user_id=$1 AND mint=$2 AND status IN ('OPEN','PARTIAL','SELLING')
                ''',trade['user_id'],trade['mint'])

    async def reconcile_one(self, tid, signature, signed_at=None):
        result = await self.rpc("getSignatureStatuses", [
            [signature], {"searchTransactionHistory": True},
        ])
        state = ((result or {}).get("value") or [None])[0]
        if state and state.get("err") is not None:
            status, message = "FAILED", "On-chain transaction failed"
        elif state and state.get("confirmationStatus") in ("confirmed", "finalized"):
            status, message = "CONFIRMED", None
        # A missing signature alone does NOT prove failure. After a review
        # threshold, explain the persistent lock instead of saying "one minute".
        # REVIEW_REQUIRED remains blocked until a separately verified resolution.
        else:
            if signed_at is None:
                signed_at = await self.pool.fetchval(
                    "SELECT signed_at FROM copy_trades WHERE id=$1",tid)
            if signed_at and (datetime.now(timezone.utc) - signed_at).total_seconds() > 900:
                changed = await self.pool.fetchval("""
                    UPDATE copy_trades SET status='REVIEW_REQUIRED',
                        message='Submission could not be verified; independent review required'
                    WHERE id=$1 AND status IN ('SIGNED','SUBMITTED','UNKNOWN','SUBMITTING')
                    RETURNING user_id
                """, tid)
                if changed:
                    LOG.warning('Trade requires independent review trade_id=%s',tid)
                    await self.notify(changed,
                        'Transaction pending review.')
                return 'REVIEW_REQUIRED'
            return "UNKNOWN"
        row = await self.finalize_trade(tid, status, message)
        if row:
            if status == 'CONFIRMED':
                try:
                    await self.record_confirmed_position(tid)
                except Exception as exc:
                    LOG.warning('Position accounting failed trade_id=%s type=%s',tid,type(exc).__name__)
                # A master sell changes available token holdings. Block TP lots
                # even if RPC transaction indexing lags behind confirmation.
                traded = await self.pool.fetchrow(
                    "SELECT side,tp_position_id,user_id,mint FROM copy_trades WHERE id=$1",tid)
                if traded and traded['side'] == 'SELL' and traded['tp_position_id'] is None:
                    await self.pool.execute("""
                        UPDATE copy_positions SET status='REVIEW'
                        WHERE user_id=$1 AND mint=$2 AND status IN ('OPEN','PARTIAL','SELLING')
                    """, traded['user_id'],traded['mint'])
            elif status == 'FAILED':
                await self.pool.execute("UPDATE copy_positions SET status='REVIEW' WHERE id=(SELECT tp_position_id FROM copy_trades WHERE id=$1) AND status='SELLING'",tid)
            await self.announce_trade(row, status)
        return status

    def schedule_quick_reconcile(self, trade_id, signature):
        task = asyncio.create_task(self._quick_reconcile_trade(trade_id, signature))
        self.reconcile_inflight.add(task)
        task.add_done_callback(self.reconcile_inflight.discard)

    async def _quick_reconcile_trade(self, trade_id, signature):
        """Confirm in the background; never hold a trade-execution slot waiting."""
        for attempt in range(5):
            try:
                state = await self.reconcile_one(trade_id, signature, None)
                if state != "UNKNOWN":
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.debug("Fast reconciliation pending trade=%s type=%s",
                          trade_id, type(exc).__name__)
            await asyncio.sleep(0.35 if attempt < 2 else 0.7)

    async def reconcile_all(self):
        rows = await self.pool.fetch("""
            SELECT id,signature,signed_at FROM copy_trades
            WHERE status = ANY($1) AND signature IS NOT NULL
            ORDER BY id DESC LIMIT 100
        """, UNRESOLVED)
        for row in rows:
            try:
                await self.reconcile_one(row["id"], row["signature"], row["signed_at"])
            except Exception as exc:
                LOG.warning("Reconciliation failed trade=%s: %s", row["id"], type(exc).__name__)
        wrows = await self.pool.fetch("""
            SELECT id,signature,created_at FROM copy_withdrawals
            WHERE status IN ('SUBMITTING','SUBMITTED','UNKNOWN') AND signature IS NOT NULL
            ORDER BY id LIMIT 50
        """)
        for row in wrows:
            try:
                result = await self.rpc("getSignatureStatuses", [
                    [row["signature"]], {"searchTransactionHistory": True}
                ])
                state = ((result or {}).get("value") or [None])[0]
                new = None
                if state and state.get("err") is not None:
                    new = "FAILED"
                elif state and state.get("confirmationStatus") in ("confirmed", "finalized"):
                    new = "CONFIRMED"
                # Unknown withdrawal status is not proof of failure.
                # Preserve UNKNOWN until an actual on-chain result is found.
                if new:
                    await self.pool.execute(
                        "UPDATE copy_withdrawals SET status=$2 WHERE id=$1", row["id"], new
                    )
            except Exception as exc:
                LOG.warning("Withdrawal reconciliation failed: %s", type(exc).__name__)

    async def reconcile_loop(self):
        while True:
            try:
                await asyncio.sleep(8)
                await self.reconcile_all()
                # A queued TP sale may have been skipped by emergency pause.
                # Do not leave its lot permanently in SELLING without a signed tx.
                await self.pool.execute("""
                    UPDATE copy_positions p SET status='REVIEW'
                    WHERE p.status='SELLING'
                      AND (
                          SELECT t.status FROM copy_trades t
                          WHERE t.tp_position_id=p.id
                          ORDER BY t.id DESC LIMIT 1
                      ) IN ('SKIPPED','FAILED')
                """)
                # RPC indexing can lag behind confirmation. Retry position accounting
                # for recent confirmed buys when take-profit is enabled.
                recent = await self.pool.fetch("""
                    SELECT t.id FROM copy_trades t JOIN copy_users u ON u.id=t.user_id
                    LEFT JOIN copy_positions p ON p.trade_id=t.id
                    WHERE t.status='CONFIRMED' AND t.side='BUY' AND p.id IS NULL
                      AND (u.take_profit_since IS NOT NULL OR u.stop_loss_since IS NOT NULL)
                      AND t.created_at >= LEAST(
                          COALESCE(u.take_profit_since, 'infinity'::timestamptz),
                          COALESCE(u.stop_loss_since, 'infinity'::timestamptz))
                      AND t.created_at > now()-interval '1 hour'
                    ORDER BY t.id DESC LIMIT 10
                """)
                for entry in recent:
                    try:
                        await self.record_confirmed_position(entry['id'])
                    except Exception as exc:
                        LOG.debug('Position backfill pending trade=%s type=%s',entry['id'],type(exc).__name__)
                # A confirmed TP sell can be indexed after the signature status.
                # Retry allocation of the confirmed sale to its tracked lot.
                pending_tp = await self.pool.fetch("""
                    SELECT t.id FROM copy_trades t
                    JOIN copy_positions p ON p.id=t.tp_position_id
                    WHERE t.status='CONFIRMED' AND t.side='SELL'
                      AND p.status='SELLING'
                    ORDER BY t.id LIMIT 20
                """)
                for entry in pending_tp:
                    try:
                        await self.record_confirmed_position(entry['id'])
                    except Exception as exc:
                        LOG.warning('Confirmed TP accounting delayed trade=%s type=%s',entry['id'],type(exc).__name__)
                for uid in list(self.awaiting_resolution):
                    if not await self.has_unresolved(uid):
                        self.awaiting_resolution.discard(uid)
                        await self.notify(
                            uid,
                            "Transaction updated."
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Reconcile loop error")

    # ------------------------------------------------------------------ independent take-profit monitoring
    async def _run_take_profit_check(self, position):
        pid = position["id"]
        try:
            async with self.tp_sem:
                await self.check_take_profit(position)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.debug("Take-profit quote unavailable lot=%s type=%s", pid, type(exc).__name__)
        finally:
            self.tp_inflight.discard(pid)

    async def take_profit_loop(self):
        """Scan exits every 0.5s and quote multiple independent lots concurrently."""
        while True:
            try:
                rows = await self.pool.fetch("""
                    SELECT p.*,u.slip_bps FROM copy_positions p
                    JOIN copy_users u ON u.id=p.user_id
                    WHERE p.status IN ('OPEN','PARTIAL') AND u.enabled
                      AND (u.take_profit_enabled OR u.stop_loss_enabled)
                      AND p.id > $1
                    ORDER BY p.id LIMIT 24
                """, self.tp_cursor)
                if not rows:
                    self.tp_cursor = 0
                    rows = await self.pool.fetch("""
                        SELECT p.*,u.slip_bps FROM copy_positions p
                        JOIN copy_users u ON u.id=p.user_id
                        WHERE p.status IN ('OPEN','PARTIAL') AND u.enabled
                          AND (u.take_profit_enabled OR u.stop_loss_enabled)
                        ORDER BY p.id LIMIT 24
                    """)
                for position in rows:
                    self.tp_cursor = position["id"]
                    pid = position["id"]
                    if pid in self.tp_inflight:
                        continue
                    self.tp_inflight.add(pid)
                    task = asyncio.create_task(self._run_take_profit_check(position))
                    self.tp_check_tasks.add(task)
                    task.add_done_callback(self.tp_check_tasks.discard)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Take-profit monitor error")
            await asyncio.sleep(0.5)

    async def check_take_profit(self, p):
        """Monitor both exits using executable quotes, with a single sell claim.

        All amounts remain raw token units; cost_usd belongs to the remaining lot.
        No automatic sell is inferred from a quote that cannot be obtained.
        """
        uid, mint, wallet = p['user_id'], p['mint'], p['wallet']
        u = await self.user(uid)
        if (not u['enabled'] or u['address'] != wallet or
                not (u['take_profit_enabled'] or u['stop_loss_enabled'])):
            return
        # A submitted trade on another mint must not hold this exit back.
        # Only genuinely uncertain/review-required submissions pause all trading.
        if await self.has_unresolved(uid, ["UNKNOWN", "REVIEW_REQUIRED"]):
            return
        if await self.pool.fetchval("""
            SELECT EXISTS(SELECT 1 FROM copy_trades
                          WHERE user_id=$1 AND mint=$2 AND side='SELL'
                            AND status IN ('QUEUED','QUOTING','SIGNED','SUBMITTED',
                                           'UNKNOWN','SUBMITTING','REVIEW_REQUIRED'))
        """, uid, mint):
            return
        current_raw, dec = await self.token_balance(wallet, mint)
        held = int(p['token_amount'])
        if held <= 0 or dec != p['decimals'] or current_raw < held:
            await self.pool.execute("""
                UPDATE copy_positions SET status='REVIEW'
                WHERE id=$1 AND status IN ('OPEN','PARTIAL')
            """, p['id'])
            return
        if Decimal(p['cost_usd']) <= 0:
            return

        # Stop loss always targets 100% of the remaining lot. Check it first.
        # An accurate exit proceeds quote must exist before we create a sell.
        choice = None
        if u['stop_loss_enabled']:
            full_quote = await self.jupiter_order(mint, WSOL, held, wallet, int(u['slip_bps']))
            sol_price = await self.sol_usd()
            net = max(Decimal(0), Decimal(str(full_quote['outAmount'])) / Decimal(10**9)
                      - Decimal('0.003')) * sol_price
            floor = Decimal(p['cost_usd']) * (Decimal(1) - Decimal(u['stop_loss_pct']) / 100)
            if net <= floor:
                choice = ('SL', held, floor, net)
        if choice is None and u['take_profit_enabled']:
            raw = int(Decimal(held) * Decimal(u['take_profit_sell_pct']) / 100)
            if raw < 1:
                return
            quote = await self.jupiter_order(mint, WSOL, raw, wallet, int(u['slip_bps']))
            sol_price = await self.sol_usd()
            net = max(Decimal(0), Decimal(str(quote['outAmount'])) / Decimal(10**9)
                      - Decimal('0.003')) * sol_price
            target = (Decimal(p['cost_usd']) * Decimal(raw) / Decimal(held)
                      * (Decimal(1) + Decimal(u['take_profit_pct']) / 100))
            if net >= target:
                choice = ('TP', raw, target, net)
        if choice is None:
            return

        kind, raw, threshold, net = choice
        async with self.wallet_lock(wallet):
            u = await self.user(uid)
            if (not u['enabled'] or u['address'] != wallet or
                    not u['stop_loss_enabled'] and kind == 'SL' or
                    not u['take_profit_enabled'] and kind == 'TP'):
                return
            if await self.has_unresolved(uid):
                return
            if await self.pool.fetchval("""
                SELECT EXISTS(SELECT 1 FROM copy_trades
                WHERE user_id=$1 AND mint=$2 AND side='SELL'
                  AND status IN ('QUEUED','QUOTING','SIGNED','SUBMITTING',
                                 'SUBMITTED','UNKNOWN','REVIEW_REQUIRED'))
            """, uid, mint):
                return
            # Re-check holdings under the wallet lock, before claiming the lot.
            balance, balance_dec = await self.token_balance(wallet, mint)
            if balance_dec != p['decimals'] or balance < held:
                await self.pool.execute("""
                    UPDATE copy_positions SET status='REVIEW'
                    WHERE id=$1 AND status IN ('OPEN','PARTIAL')
                """, p['id'])
                return
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    claimed = await conn.fetchval("""
                        UPDATE copy_positions SET status='SELLING'
                        WHERE id=$1 AND status IN ('OPEN','PARTIAL')
                          AND token_amount=$2 RETURNING id
                    """, p['id'], held)
                    if not claimed:
                        return
                    await conn.execute("""
                        INSERT INTO copy_trades(user_id,master_address,master_signature,
                                                side,mint,status,fraction,venue,
                                                tp_position_id,tp_raw_amount,auto_exit_kind)
                        VALUES($1,$2,$3,'SELL',$4,'QUEUED',1,'pumpfun',$5,$6,$7)
                    """, uid, wallet,
                        kind.lower()+':'+str(p['id'])+':'+secrets.token_hex(6),
                        mint, p['id'], raw, kind)
        LOG.info('Auto exit queued uid=%s lot=%s type=%s quote=%s threshold=%s',
                 uid, p['id'], kind, net, threshold)
        self.trade_wake.set()

    # ------------------------------------------------------------------ execution
    async def trade_worker(self):
        """Bounded queue consumer: no more than TRADE_CONCURRENCY live tasks."""
        while True:
            try:
                capacity = max(0, TRADE_CONCURRENCY - len(self.inflight))
                if capacity:
                    rows = await self.pool.fetch("""
                        WITH picked AS (
                            SELECT id FROM copy_trades
                            WHERE status='QUEUED'
                            ORDER BY CASE WHEN side='SELL' THEN 0 ELSE 1 END, id
                            LIMIT $1
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE copy_trades AS t
                        SET status='QUOTING'
                        FROM picked
                        WHERE t.id=picked.id
                        RETURNING t.id,t.user_id,t.master_address,
                                  t.master_signature,t.side,t.mint,
                                  t.fraction,t.block_time,t.venue,t.created_at,t.tp_position_id,t.tp_raw_amount,t.auto_exit_kind
                    """, capacity)
                    for row in rows:
                        task = asyncio.create_task(self.run_trade(row))
                        self.inflight.add(task)
                        task.add_done_callback(self._trade_done)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Trade worker error")
            try:
                await asyncio.wait_for(self.trade_wake.wait(), timeout=0.4)
                self.trade_wake.clear()
            except asyncio.TimeoutError:
                pass

    def _trade_done(self, task):
        self.inflight.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            LOG.warning("Copy task cancelled during shutdown")
        except Exception:
            LOG.exception("Unexpected unhandled trade task error")

    async def run_trade(self, row):
        async with self.trade_sem:
            await self.execute_copy(row)

    async def skip_notice(self, uid, reason):
        if "not enough" not in reason.lower():
            return
        now = time.monotonic()
        if now - self.notice_throttle.get(uid, 0) < 600:
            return
        self.notice_throttle[uid] = now
        await self.notify(uid, f"Trade skipped: {html.escape(reason)}")

    async def execute_copy(self, row):
        tid = row["id"]
        uid = row["user_id"]
        side = row["side"]
        mint = row["mint"]
        fraction = row["fraction"]
        observed = float(row["block_time"] or row["created_at"].timestamp())
        phase = "PREPARING"
        signed_sig = None
        exec_started = time.monotonic()
        queued_delay = max(0.0, time.time() - row["created_at"].timestamp())
        LOG.info("Trade latency trade_id=%s side=%s queued_to_start=%.3fs",
                 tid, side, queued_delay)
        try:
            u = await self.user(uid)
            if not u["enabled"]:
                raise ValueError("Auto Trade off")
            if row["tp_position_id"] is None and time.time() - observed > int(u["max_signal_age_seconds"]):
                raise ValueError("Trade too old")
            if side == "BUY" and not u["copy_buys"]:
                raise ValueError("Copy buys off")
            if side == "SELL" and row["tp_position_id"] is None and not u["copy_sells"]:
                raise ValueError("Copy sells off")
            if not u["encrypted_secret"]:
                raise ValueError("No active wallet")
            if not valid_address(mint):
                raise ValueError("Invalid token")
            wallet = u["address"]
            slip = int(u["slip_bps"])
            if not 10 <= slip <= MAX_SLIPPAGE_BPS:
                raise ValueError("Slippage: 0.1–70%")

            async with self.wallet_lock(wallet):
                current = await self.user(uid)
                if not current["enabled"] or current["address"] != wallet:
                    raise ValueError("Auto Trade off or wallet changed")
                if row["tp_position_id"] is None and time.time() - observed > int(current["max_signal_age_seconds"]):
                    raise ValueError("Trade too old")
                if await self.has_unresolved(uid, ["UNKNOWN", "REVIEW_REQUIRED"]):
                    raise ValueError("Transaction pending")

                LOG.info("Copy preparing trade_id=%s uid=%s side=%s venue=%s", tid, uid, side, row["venue"])
                phase = "SIZING"
                decimals = None
                if side == "BUY" and current['prevent_repeat_buys']:
                    # The wallet lock serializes buys from every followed master.
                    # Failed/skipped trades do not permanently block the mint.
                    already_bought = await self.pool.fetchval("""
                        SELECT EXISTS(
                            SELECT 1 FROM copy_trades
                            WHERE user_id=$1 AND mint=$2 AND side='BUY'
                              AND id<>$3
                              AND status IN ('CONFIRMED','SIGNED','SUBMITTING',
                                             'SUBMITTED','UNKNOWN','REVIEW_REQUIRED')
                        )
                    """, uid, mint, tid)
                    if already_bought:
                        raise ValueError("Token already bought")
                if side == "BUY":
                    # Balance RPC and cached SOL/USD quote are independent; run
                    # them concurrently instead of adding their latencies.
                    balance_result, price = await asyncio.gather(
                        self.rpc("getBalance", [wallet]), self.sol_usd()
                    )
                    lamports = int(balance_result["value"])
                    available_sol = max(0, lamports - MIN_SOL_RESERVE)
                    usd_limit = Decimal(current["max_usd"])
                    max_lamports = int(
                        (usd_limit / price * Decimal(10**9)).to_integral_value(rounding=ROUND_DOWN)
                    )
                    if available_sol >= 1_000_000 and max_lamports >= 1_000_000:
                        source, dest = WSOL, mint
                        raw = min(available_sol, max_lamports)
                        usd = min(usd_limit, Decimal(raw) / Decimal(10**9) * price)
                        if usd < Decimal("0.50"):
                            raise ValueError("Not enough SOL: trade would be under $0.50")
                    elif current["allow_usdc_fallback"]:
                        available_usdc, _ = await self.token_balance(wallet, USDC)
                        if available_usdc < 500_000:
                            raise ValueError("Not enough SOL or USDC to copy this trade")
                        source, dest = USDC, mint
                        raw = min(available_usdc, int(usd_limit * 1_000_000))
                        usd = Decimal(raw) / Decimal(1_000_000)
                        if usd < Decimal("0.50"):
                            raise ValueError("Not enough USDC: trade would be under $0.50")
                    else:
                        raise ValueError(
                            "Not enough SOL to copy this trade. Add SOL to your wallet."
                        )
                else:
                    if fraction is None or not (Decimal(0) < Decimal(fraction) <= Decimal(1)):
                        raise ValueError("Sell amount unavailable")
                    fraction = Decimal(fraction)
                    had_buy = await self.pool.fetchval("""
                        SELECT EXISTS(SELECT 1 FROM copy_trades
                        WHERE user_id=$1 AND side='BUY' AND mint=$2
                          AND status='CONFIRMED')
                    """, uid, mint)
                    if not had_buy:
                        raise ValueError("No confirmed buy")
                    owned, decimals = 0, None
                    for _ in range(12):  # wait up to ~10s for a pending buy to land
                        owned, decimals = await self.token_balance(wallet, mint)
                        if owned > 0:
                            break
                        await asyncio.sleep(0.8)
                    if row['tp_position_id'] is None and await self.pool.fetchval("SELECT EXISTS(SELECT 1 FROM copy_trades WHERE user_id=$1 AND mint=$2 AND tp_position_id IS NOT NULL AND status IN ('QUEUED','QUOTING','SIGNED','SUBMITTING','SUBMITTED','UNKNOWN','REVIEW_REQUIRED'))",uid,mint):
                        raise ValueError('Sell in progress')
                    if row['tp_position_id'] is not None:
                        lot = await self.pool.fetchrow('SELECT * FROM copy_positions WHERE id=$1 AND user_id=$2',row['tp_position_id'],uid)
                        if not lot or lot['status'] != 'SELLING' or lot['wallet'] != wallet or lot['mint'] != mint:
                            raise ValueError('Position inactive')
                        if (row['auto_exit_kind'] == 'SL' and not current['stop_loss_enabled'] or
                                row['auto_exit_kind'] != 'SL' and not current['take_profit_enabled']):
                            raise ValueError('Auto exit disabled')
                        raw = min(int(owned),int(row['tp_raw_amount']))
                        if raw < int(row['tp_raw_amount']):
                            raise ValueError('Token balance changed')
                    else:
                        raw = int(Decimal(owned) * fraction)
                    if raw <= 0:
                        raise ValueError("No tokens to sell")
                    source, dest = mint, WSOL
                    usd = Decimal(0)

                await self.pool.execute(
                    "UPDATE copy_trades SET usd=$2 WHERE id=$1", tid, usd
                )
                LOG.info(
                    "Copy signal uid=%s side=%s mint=%s amount=%s source=%s venue=%s",
                    uid, side, shortened(mint), raw,
                    "SOL" if source == WSOL else shortened(source), row["venue"],
                )

                phase = "QUOTING"
                key = Keypair.from_bytes(
                    self.fernet.decrypt(current["encrypted_secret"].encode())
                )
                if str(key.pubkey()) != wallet:
                    raise ValueError("Wallet error")

                route = None
                order = None
                headers = None
                signed_sig = None
                signed_b64 = None
                prepare_started = time.monotonic()
                pump_venue = row["venue"] in ("pumpfun", "pumpswap")

                if current["use_pumpportal"] and pump_venue and row["tp_position_id"] is None and (side == "SELL" or source == WSOL):
                    try:
                        if side == "BUY":
                            amount_arg = Decimal(raw) / Decimal(10**9)
                            spend = raw
                        else:
                            spend = 0
                            amount_arg = Decimal(raw) / Decimal(10**decimals)
                        blob = await self.portal_transaction(wallet, mint, side, amount_arg, slip)
                        signed_sig, signed_b64 = self.sign_portal_transaction(
                            blob, key, side, mint, spend, slip
                        )
                        route = "pumpportal"
                    except (ValueError, RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                        LOG.warning(
                            "PumpPortal route rejected trade_id=%s (%s: %s); trying Jupiter",
                            tid, type(exc).__name__, str(exc)[:160]
                        )

                if route is None:
                    LOG.info("Copy quoting trade_id=%s via=jupiter mint=%s", tid, shortened(mint))
                    order = await self.jupiter_order(source, dest, raw, wallet, slip)
                    if row['tp_position_id'] is not None:
                        lot = await self.pool.fetchrow('SELECT cost_usd, token_amount FROM copy_positions WHERE id=$1',row['tp_position_id'])
                        if not lot or int(lot['token_amount']) <= 0:
                            raise ValueError('Position unavailable')
                        sol_price = await self.sol_usd()
                        expected_net = (Decimal(str(order['outAmount'])) / Decimal(10**9) - Decimal('0.003')) * sol_price
                        basis = Decimal(lot['cost_usd']) * Decimal(raw) / Decimal(lot['token_amount'])
                        if row['auto_exit_kind'] == 'SL':
                            threshold = basis * (Decimal(1) - Decimal(current['stop_loss_pct'])/100)
                            if expected_net > threshold:
                                raise ValueError('Loss limit not met')
                        else:
                            threshold = basis * (Decimal(1) + Decimal(current['take_profit_pct'])/100)
                            if expected_net < threshold:
                                raise ValueError('Profit target not met')
                    signed_sig, signed_b64, headers = await self.jupiter_execute(order, key)
                    route = "jupiter"
                    if side == "SELL":
                        try:
                            price = await self.sol_usd()
                            usd = Decimal(str(order["outAmount"])) / Decimal(10**9) * price
                        except Exception:
                            usd = Decimal(0)

                prepare_ms = (time.monotonic() - prepare_started) * 1000
                # Save the transaction ID BEFORE sending it. This is deliberately
                # retained so an uncertain broadcast can be reconciled safely.
                await self.pool.execute("""
                    UPDATE copy_trades
                    SET signature=$2,status='SIGNED',signed_at=now(),message=NULL,
                        route=$3,usd=$4
                    WHERE id=$1
                """, tid, signed_sig, route, usd)
                phase = "SUBMITTING"
                LOG.info("Copy signed trade_id=%s route=%s sig=%s", tid, route, shortened(signed_sig))

                broadcast_started = time.monotonic()
                if route == "pumpportal":
                    result_sig = await self.submit_portal(signed_b64)
                    if result_sig != signed_sig:
                        raise RuntimeError("Helius returned a different transaction signature")
                else:
                    result = await self.submit_jupiter(order, signed_b64, headers)
                    result_sig = result.get("signature")
                    if result_sig and result_sig != signed_sig:
                        raise RuntimeError("Jupiter returned a different transaction signature")
                    if result.get("status") not in ("Success", "Failed"):
                        raise RuntimeError("Jupiter execution outcome undetermined")
                    if result.get("status") == "Failed":
                        LOG.warning(
                            "Jupiter reported failure trade_id=%s code=%s; verifying on-chain",
                            tid, result.get("code")
                        )

                await self.pool.execute(
                    "UPDATE copy_trades SET status='SUBMITTED' WHERE id=$1 AND status='SIGNED'", tid
                )
                phase = "RECONCILING"
                broadcast_ms = (time.monotonic() - broadcast_started) * 1000
                ws_seen = self.ws_signal_times.pop((uid, row["master_signature"]), None)
                ws_to_submit = (time.monotonic() - ws_seen) * 1000 if ws_seen else None
                LOG.info("Copy submitted trade_id=%s route=%s", tid, route)
                LOG.info("Trade latency trade_id=%s side=%s worker_start_to_submit_ms=%.1f prepare_ms=%.1f broadcast_ms=%.1f ws_receive_to_submit_ms=%s",
                         tid, side, (time.monotonic()-exec_started)*1000,
                         prepare_ms, broadcast_ms,
                         f"{ws_to_submit:.1f}" if ws_to_submit is not None else "n/a")
                # Confirmation/accounting runs independently; do not occupy one
                # of the scarce trade workers while waiting for chain status.
                self.schedule_quick_reconcile(tid, signed_sig)

        except Exception as exc:
            if phase == "SUBMITTING" and signed_sig:
                await self.pool.execute("""
                    UPDATE copy_trades SET status='UNKNOWN',message=$2
                    WHERE id=$1 AND status IN ('SIGNED','SUBMITTED')
                """, tid, f"{phase}: {type(exc).__name__}")
                LOG.error("Submission uncertain trade_id=%s exc=%s", tid, type(exc).__name__)
                await self.pause_user(
                    uid,
                    "Trade pending confirmation.",
                    unresolved=True,
                )
            elif phase == "RECONCILING":
                LOG.warning("Reconcile error trade_id=%s exc=%s", tid, type(exc).__name__)
            elif isinstance(exc, ValueError):
                reason = str(exc)[:250]
                await self.pool.execute("""
                    UPDATE copy_trades SET status='SKIPPED',message=$2
                    WHERE id=$1 AND status IN ('QUEUED','QUOTING')
                """, tid, reason)
                if row['tp_position_id'] is not None:
                    # No signature means no automatic exit was broadcast; a
                    # price/quote/setting rejection can be retried on a new quote.
                    # An unexpected token balance is different: attribution is
                    # unsafe and must remain under review.
                    new_state = ('OPEN' if signed_sig is None and
                                 reason not in ('Token balance changed', 'Position inactive')
                                 else 'REVIEW')
                    await self.pool.execute("UPDATE copy_positions SET status=$2 WHERE id=$1 AND status='SELLING'",row['tp_position_id'],new_state)
                    # Unique per-attempt identifiers preserve historical skips and allow a later retry.
                LOG.info("Copy skipped trade_id=%s phase=%s reason=%s", tid, phase, reason)
                await self.skip_notice(uid, reason)
            else:
                await self.pool.execute("""
                    UPDATE copy_trades SET status='FAILED',message=$2
                    WHERE id=$1 AND status IN ('QUEUED','QUOTING')
                """, tid, "Couldn't complete this trade")
                if row['tp_position_id'] is not None:
                    # No signature persisted: safe to re-evaluate a fresh quote.
                    # A signed/unknown submission must never be retried here.
                    retry_status = 'OPEN' if signed_sig is None else 'REVIEW'
                    await self.pool.execute(
                        "UPDATE copy_positions SET status=$2 WHERE id=$1 AND status='SELLING'",
                        row['tp_position_id'], retry_status)
                LOG.error("Copy trade failed id=%s phase=%s exc=%s detail=%s",
                          tid, phase, type(exc).__name__, str(exc)[:160])
                await self.notify(
                    uid,
                    f"{side.title()} failed"
                )

    # ------------------------------------------------------------------ monitoring
    async def get_transaction(self, signature):
        for attempt in range(4):
            tx = await self.rpc("getTransaction", [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 1,
                },
            ])
            if tx is not None:
                return tx
            await asyncio.sleep(0.35)
        raise RuntimeError("RPC transaction details not ready")

    async def poll_master(self, address, max_age):
        lock = self.poll_locks.setdefault(address, asyncio.Lock())
        if lock.locked():
            return 0
        async with lock:
            return await self._poll_master(address, max_age)

    async def _poll_master(self, address, max_age):
        previous = await self.pool.fetchval(
            "SELECT signature FROM copy_cursors WHERE address=$1", address
        )
        if previous is None:
            entries = await self.rpc("getSignaturesForAddress", [
                address, {"limit": 1, "commitment": "confirmed"}
            ])
            if entries:
                await self.pool.execute("""
                    INSERT INTO copy_cursors(address,signature)
                    VALUES($1,$2) ON CONFLICT DO NOTHING
                """, address, entries[0]["signature"])
            return 0

        # Walk *all* pages back to the committed cursor. A single limit=100
        # response is NOT complete for a busy master wallet.
        entries = []
        before = None
        found_cursor = False
        for page in range(25):
            options = {"limit": 100, "until": previous, "commitment": "confirmed"}
            if before:
                options["before"] = before
            batch = await self.rpc("getSignaturesForAddress", [address, options])
            if not batch:
                found_cursor = True  # 'until' may legitimately yield no new entries
                break
            entries.extend(batch)
            # Older than the allowable copy window: no need to reach a stale cursor.
            # A missing blockTime is not evidence that a transaction is old.
            oldest = batch[-1].get("blockTime")
            if oldest is not None and int(oldest) < int(time.time()) - int(max_age) - 5:
                found_cursor = True
                break
            # The 'until' boundary is exclusive. A short page means we reached
            # that boundary (or the end of available history).
            if len(batch) < 100:
                found_cursor = True
                break
            before = batch[-1]["signature"]
        if not found_cursor:
            LOG.error(
                "Backlog >2500 signatures for %s: NOT advancing cursor; "
                "retry on next monitor pass",
                shortened(address),
            )
            return 0
        if not entries:
            return 0
        newest = entries[0]["signature"]
        LOG.info("Monitor batch wallet=%s count=%s", shortened(address), len(entries))

        for item in reversed(entries):  # oldest first
            if item.get("err"):
                continue
            signature = item["signature"]
            block_time = item.get("blockTime") or int(time.time())
            age = time.time() - block_time
            if not 0 <= age <= max_age:
                continue
            tx = await self.get_transaction(signature)
            swap, reason = self.classify_swap(tx, address)
            if not swap:
                LOG.info("Ignored tx %s: %s", shortened(signature), reason)
                continue
            side, mint, fraction, venue = swap
            followers = await self.pool.fetch("""
                SELECT m.user_id, u.enabled_since
                FROM copy_masters m
                JOIN copy_users u ON u.id=m.user_id
                WHERE m.address=$1 AND m.active AND u.enabled
            """, address)
            for follower in followers:
                since = follower["enabled_since"]
                if since is not None and block_time < since.timestamp():
                    continue
                inserted = await self.pool.fetchrow("""
                    INSERT INTO copy_trades
                    (user_id,master_address,master_signature,side,mint,status,
                     block_time,fraction,venue)
                    VALUES($1,$2,$3,$4,$5,'QUEUED',$6,$7,$8)
                    ON CONFLICT DO NOTHING RETURNING id
                """, follower["user_id"], address, signature, side, mint,
                    int(block_time), fraction, venue)
                if inserted:
                    LOG.info(
                        "Queued %s uid=%s mint=%s venue=%s",
                        side, follower["user_id"], shortened(mint), venue
                    )
                    self.trade_wake.set()

        await self.pool.execute("""
            INSERT INTO copy_cursors(address,signature) VALUES($1,$2)
            ON CONFLICT(address) DO UPDATE SET signature=EXCLUDED.signature
        """, address, newest)
        return len(entries)

    async def poll_one(self, master, next_poll, burst):
        async with self.monitor_sem:
            return await self._poll_one_limited(master, next_poll, burst)

    async def _poll_one_limited(self, master, next_poll, burst):
        address = master["address"]
        # WS hints provide immediacy; don't hammer HTTP for every master.
        next_poll[address] = time.monotonic() + max(3.0, float(master["interval"]))
        try:
            found = await self.poll_master(address, int(master["max_age"]))
            if burst.get(address, 0) > 0:
                if found == 0:
                    burst[address] -= 1
                    next_poll[address] = time.monotonic() + 3.0
                else:
                    burst[address] = 0
        except Exception as exc:
            # Back off this master, too, so notifications cannot restart a hot loop.
            self.poll_cooldowns[address] = time.monotonic() + 15.0
            next_poll[address] = max(next_poll[address], self.poll_cooldowns[address])
            LOG.error(
                "Wallet monitoring error wallet=%s type=%s detail=%s",
                shortened(address), type(exc).__name__,
                str(exc).split("?api-key=")[0][:150],
            )

    async def monitor(self):
        next_poll = {}
        burst = {}
        masters = []
        last_load = 0.0
        while True:
            try:
                now = time.monotonic()
                if now - last_load >= 1.0:
                    masters = await self.pool.fetch("""
                        SELECT m.address,
                               MIN(u.poll_interval_seconds) AS interval,
                               MAX(u.max_signal_age_seconds) AS max_age
                        FROM copy_masters m
                        JOIN copy_users u ON u.id=m.user_id
                        WHERE m.active AND u.enabled
                        GROUP BY m.address
                    """)
                    last_load = now
                    active = {m["address"] for m in masters}
                    for address in list(next_poll):
                        if address not in active:
                            next_poll.pop(address, None)
                            burst.pop(address, None)
                            self.poll_cooldowns.pop(address, None)
                hot = set(self.hot_addresses)
                self.hot_addresses.clear()
                for address in hot:
                    # Respect the shared Helius backoff even during WS bursts.
                    if (time.monotonic() >= self.rpc_cooldown_until and
                            time.monotonic() >= self.poll_cooldowns.get(address, 0)):
                        next_poll[address] = min(next_poll.get(address, 0), now)
                        burst[address] = 1
                due = [m for m in masters if now >= next_poll.get(m["address"], 0)]
                if due:
                    # Prioritize WebSocket-notified wallets and run at most two
                    # master polls concurrently. Per-wallet locks prevent overlaps.
                    due.sort(key=lambda m: (m["address"] not in hot,
                                            next_poll.get(m["address"], 0)))
                    await asyncio.gather(*(self.poll_one(m, next_poll, burst)
                                           for m in due))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Monitoring error")
            try:
                await asyncio.wait_for(self.ws_signal.wait(), timeout=0.25)
                self.ws_signal.clear()
            except asyncio.TimeoutError:
                pass


def main():
    service = BotService()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(service.startup)
        .post_shutdown(service.shutdown)
        .build()
    )
    app.add_handler(CommandHandler(["start", "menu"], service.start))
    app.add_handler(CommandHandler("resolve_trade", service.resolve_trade_command))
    async def show_wallets(update, ctx):
        if update.effective_chat.type == "private":
            await service.show(update, "wallet")

    async def send_command(update, ctx):
        if update.effective_chat.type != "private":
            return
        uid = update.effective_user.id
        user = await service.user(uid)
        if not user["address"]:
            return await service.show(update, "wallet", "Create or import a wallet first.")
        selected = await service.pool.fetchrow(
            """SELECT id FROM copy_wallets
               WHERE user_id=$1 AND address=$2 ORDER BY id LIMIT 1""",
            uid, user["address"],
        )
        if selected is None:
            return await service.show(update, "wallet", "Select a wallet first.")
        await service.show(update, f"walletview:{selected['id']}",
                           "Choose Send SOL or Send USDC.")

    async def help_command(update, ctx):
        if update.effective_chat.type != "private":
            return
        await update.message.reply_text(
            "Copy Trader commands\n\n"
            "/start or /menu — Dashboard\n"
            "/wallet — Manage wallets\n"
            "/send — Send SOL or USDC\n"
            "/masters — Manage master wallets\n"
            "/settings — Trading preferences\n"
            "/history — Recent trades\n"
            "/pause — Stop new trades\n"
            "/cancel — Cancel your current action\n"
            "/help — Show this guide",
            reply_markup=keyboard([[('🏠 Open Dashboard', 'home')]]),
        )

    app.add_handler(CommandHandler("wallet", show_wallets))
    app.add_handler(CommandHandler("send", send_command))
    app.add_handler(CommandHandler("help", help_command))
    for command, page in (
        ("masters", "masters"),
        ("settings", "settings"),
        ("history", "history"),
    ):
        async def open_page(update, ctx, target=page):
            if update.effective_chat.type == "private":
                await service.show(update, target)
        app.add_handler(CommandHandler(command, open_page))

    app.add_handler(CommandHandler("pause", service.pause_command))
    app.add_handler(CommandHandler("cancel", service.cancel))
    app.add_handler(CallbackQueryHandler(service.click))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, service.text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
