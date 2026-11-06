"""Solana Telegram Copy Trader — single-file application."""
import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import secrets
import struct
import sys
from getpass import getpass
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import aiohttp
import asyncpg
import base58
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
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
RPC = os.getenv("HELIUS_RPC_URL", "").strip()

JUP = "https://api.jup.ag/swap/v2"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
USDT = "Es9vMFrzaCER1LEJzKSSGfYxj7ZLyuuz3QfbD7cGoE"
QUOTE_MINTS = {USDC, USDT, WSOL}
# User-configurable trading settings are persisted in PostgreSQL, not .env.
# Only credentials/infrastructure values belong in /etc/tg-bot.env.
DEFAULT_SLIPPAGE_BPS = 100
MIN_SOL_RESERVE = 5_000_000  # Fees and rent for up to two standard token accounts
MAX_SLIPPAGE_BPS = 7000
PUMPPORTAL_FEE_RECIPIENT = "7FeFBYbewCqXG7LP6gC8Fnqzk7hmQFMtntXVRqnXi4g6"
PUMPPORTAL_FEE_BPS = 50
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
    PUMP_PROGRAM, PUMPSWAP_PROGRAM, ATA_PROGRAM_ID,
    TOKEN_PROGRAM_ID, TOKEN_2022_ID, SYSTEM_ID, COMPUTE_BUDGET_ID,
}
# Anchor instruction discriminators from the published Pump and PumpSwap IDLs.
SWAP_NAMES = {
    hashlib.sha256(f"global:{name}".encode()).digest()[:8]: name
    for name in ("buy", "sell", "buy_exact_sol_in", "buy_exact_quote_in",
                 "buy_v2", "sell_v2", "buy_exact_quote_in_v2")
}


RAY_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
DIRECT_SWAP_PROGRAMS = {
    RAY_V4,
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpool
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
}
DIRECT_SWAP_DISCRIMINATORS = {
    hashlib.sha256(f"global:{name}".encode()).digest()[:8]
    for name in ("swap", "swap_v2", "swap_base_input", "swap_base_output",
                 "swap_exact_out", "swap2", "swap_exact_out2", "two_hop_swap", "two_hop_swap_v2")
}


def token_account(owner, mint, program):
    seeds = [bytes(Pubkey.from_string(k)) for k in (owner, program, mint)]
    return str(Pubkey.find_program_address(seeds, Pubkey.from_string(ATA_PROGRAM_ID))[0])


PRIORITY_FEE_SOL = Decimal("0.0001")
MAX_PRIORITY_LAMPORTS = 5_000_000
PORTAL_RENT_ALLOWANCE = 10_000_000
UNRESOLVED = ["SIGNED", "SUBMITTED", "UNKNOWN", "SUBMITTING"]
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
    "EXPIRED": "Expired (no receipt)",
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
 slip_bps INTEGER NOT NULL DEFAULT 100,
 copy_buys BOOLEAN NOT NULL DEFAULT TRUE,
 copy_sells BOOLEAN NOT NULL DEFAULT TRUE,
 address TEXT,
 encrypted_secret TEXT,
 poll_interval_seconds NUMERIC(4,1) NOT NULL DEFAULT 1.5,
 max_signal_age_seconds INTEGER NOT NULL DEFAULT 90,
 allow_usdc_fallback BOOLEAN NOT NULL DEFAULT FALSE,
 use_pumpportal BOOLEAN NOT NULL DEFAULT FALSE,
 enabled_since TIMESTAMPTZ
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

MIGRATIONS = (
    "ALTER TABLE copy_users ALTER COLUMN copy_sells SET DEFAULT TRUE",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS poll_interval_seconds NUMERIC(4,1) NOT NULL DEFAULT 1.5",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS max_signal_age_seconds INTEGER NOT NULL DEFAULT 90",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS allow_usdc_fallback BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS use_pumpportal BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE copy_users ADD COLUMN IF NOT EXISTS enabled_since TIMESTAMPTZ",
    "ALTER TABLE copy_users ALTER COLUMN slip_bps SET DEFAULT 100",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS block_time BIGINT",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS fraction NUMERIC",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS venue TEXT",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS route TEXT",
    "ALTER TABLE copy_masters ADD COLUMN IF NOT EXISTS active_since TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS wallet_address TEXT",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS recent_blockhash TEXT",
    "ALTER TABLE copy_trades ADD COLUMN IF NOT EXISTS signed_at TIMESTAMPTZ",
    "CREATE INDEX IF NOT EXISTS copy_trades_status_idx ON copy_trades(status)",
)


class PreflightRejected(Exception):
    """The RPC rejected a transaction in preflight, before forwarding it."""


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
        raise ValueError("Please enter a valid number.")


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
    raise ValueError("That private key doesn't look right. Please check it and try again.")


def import_cipher(private, peer):
    shared = private.exchange(X25519PublicKey.from_public_bytes(peer))
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=b"copybot-wallet-import-v1").derive(shared)
    return Fernet(base64.urlsafe_b64encode(derived))


def seal_wallet(public_hex):
    if not sys.stdin.isatty():
        raise ValueError("Run wallet import in an interactive terminal")
    key = import_key(getpass("Wallet key (hidden): "))
    ephemeral = X25519PrivateKey.generate()
    cipher = import_cipher(ephemeral, bytes.fromhex(public_hex))
    print(ephemeral.public_key().public_bytes_raw().hex() + "." + cipher.encrypt(bytes(key)).decode())


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
    if programs & DIRECT_SWAP_PROGRAMS:
        return "dex"
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
        self.rpc_limit = asyncio.Semaphore(4)
        self.trade_sem = asyncio.Semaphore(TRADE_CONCURRENCY)
        self.ws_signal = asyncio.Event()
        self.trade_wake = asyncio.Event()
        self.alert_queues = [asyncio.Queue(maxsize=128) for _ in range(4)]
        self.alert_workers = []
        self.hot_addresses = set()
        self.poll_locks = {}
        self.wallet_locks = {}
        self.price_cache = (0.0, None)
        self.inflight = set()
        self.awaiting_resolution = set()
        self.notice_throttle = {}

    # ------------------------------------------------------------------ lifecycle
    async def startup(self, app):

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
                SELECT user_id FROM copy_trades WHERE status = ANY($1)
            ) RETURNING id
        """, UNRESOLVED)
        for row in paused:
            self.awaiting_resolution.add(row["id"])
            await self.notify(
                row["id"],
                "🛑 <b>Auto trading paused</b>\nA trade was still being checked when the "
                "bot restarted. I'll message you as soon as it's sorted out."
            )
        self.alert_workers = [asyncio.create_task(self.alert_worker(q)) for q in self.alert_queues]
        self.monitor_task = asyncio.create_task(self.monitor())
        self.ws_task = asyncio.create_task(self.websocket_monitor())
        self.cleanup_task = asyncio.create_task(self.cleanup_keys())
        self.worker_task = asyncio.create_task(self.trade_worker())
        self.reconcile_task = asyncio.create_task(self.reconcile_loop())
        await app.bot.set_my_commands([
            BotCommand("start", "Open dashboard"),
            BotCommand("wallet", "Manage wallets"),
            BotCommand("send", "Send funds"),
            BotCommand("masters", "Master wallets"),
            BotCommand("settings", "Trading settings"),
            BotCommand("history", "Trade history"),
            BotCommand("pause", "Stop new trades now"),
            BotCommand("cancel", "Cancel action"),
        ])
        LOG.info("Solana live copy bot started (WS + poll monitoring, DB-backed queue)")

    async def shutdown(self, app):
        for task in (self.monitor_task, self.cleanup_task, self.ws_task,
                     self.worker_task, self.reconcile_task, *self.alert_workers):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.inflight:
            done, pending = await asyncio.wait(self.inflight, timeout=20)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        if self.session:
            await self.session.close()
        if self.pool:
            await self.pool.close()

    # ------------------------------------------------------------------ rpc + data
    async def rpc(self, method, params, endpoint=None):
        async with self.rpc_limit:
            async with self.session.post(endpoint or RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": method, "params": params
            }) as response:
                if response.status != 200:
                    raise RuntimeError(f"Helius RPC {method} returned HTTP {response.status}")
                data = await response.json()
                if data.get("error"):
                    error = data["error"]
                    details = error.get("data")
                    if (method == "sendTransaction" and error.get("code") == -32002
                            and isinstance(details, dict) and details.get("err") is not None):
                        raise PreflightRejected(str(details["err"])[:140])
                    raise RuntimeError(
                        f"Helius RPC {method} failed (code {error.get('code', 'unknown')})"
                    )
                return data.get("result")

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
        return await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM copy_trades WHERE user_id=$1 AND status = ANY($2))",
            uid, statuses or UNRESOLVED
        )

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
                    raise ValueError("This wallet is already added.")
                count = await conn.fetchval(
                    "SELECT count(*) FROM copy_wallets WHERE user_id=$1", uid
                )
                if count >= 10:
                    raise ValueError("You can have up to 10 wallets.")
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
        await self.notify(uid, f"🛑 <b>Auto trading paused</b>\n{html.escape(reason)}")

    # ------------------------------------------------------------------ transfers
    async def confirm_signature(self, signature, timeout):
        deadline = time.monotonic() + timeout
        while True:
            result = await self.rpc("getSignatureStatuses", [
                [signature], {"searchTransactionHistory": True}
            ])
            state = ((result or {}).get("value") or [None])[0]
            if state and state.get("confirmationStatus") in ("confirmed", "finalized"):
                return "FAILED" if state.get("err") is not None else "CONFIRMED"
            if time.monotonic() >= deadline:
                return "PENDING"
            await asyncio.sleep(1.0)

    async def send_funds(self, uid, wid, asset, recipient, amount):
        w = await self.wallet(uid, wid)
        if not w:
            raise ValueError("Wallet not found.")
        if not valid_address(recipient):
            raise ValueError("That wallet address isn't valid.")
        if recipient == w["address"]:
            raise ValueError("You can't send to the same wallet.")
        if asset not in ("SOL", "USDC"):
            raise ValueError("Unsupported asset.")
        decimals = 9 if asset == "SOL" else 6
        raw = amount * (10**decimals)
        if amount <= 0 or not amount.is_finite() or raw != raw.to_integral_value():
            raise ValueError("That amount isn't valid.")
        raw = int(raw)
        if raw >= 2**64:
            raise ValueError("That amount is too large.")

        async with self.wallet_lock(w["address"]):
            u = await self.user(uid)
            if u["enabled"] and u["address"] == w["address"]:
                raise ValueError("Pause auto trading before sending funds.")
            if await self.has_unresolved(uid):
                raise ValueError("A trade is still being checked. Try again in a minute.")
            sender = Pubkey.from_string(w["address"])
            dest = Pubkey.from_string(recipient)
            key = Keypair.from_bytes(
                self.fernet.decrypt(w["encrypted_secret"].encode())
            )
            balance = await self.rpc("getBalance", [w["address"]])
            lamports = int(balance["value"])
            if asset == "SOL":
                if raw + 2_000_000 > lamports:
                    raise ValueError("Not enough SOL. Keep 0.002 SOL for fees.")
                instructions = [transfer(TransferParams(
                    from_pubkey=sender, to_pubkey=dest, lamports=raw
                ))]
            else:
                if lamports < 3_000_000:
                    raise ValueError("You need at least 0.003 SOL to cover fees.")
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
                        "maxRetries": 3,
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
                "🤖 <b>Solana Copy Trader</b>\n\n"
                "🔴 <b>LIVE</b>\n\n"
                f"Auto Trade: <b>{state}</b>\n"
                f"Master Wallets: <b>{len(ms)} / 3</b>\n"
                f"Max Trade: <b>${u['max_usd']:.2f}</b>\n"
                f"Slippage: <b>{pct(u['slip_bps'])}</b>\n"
                f"Active Wallet: <code>{address}</code>{balances}"
            )
            rows = [
                [("⏸ Pause Auto Trade" if u["enabled"] else "▶️ Enable Auto Trade", "toggle")],
            ]
            if u["enabled"]:
                rows.append([("🛑 Stop Now", "estop")])
            rows += [
                [("👀 Master Wallets", "masters"), ("👛 My Wallets", "wallet")],
                [("⚙️ Settings", "settings"), ("📜 Trade History", "history")],
            ]
            if u["address"]:
                rows.append([("📋 Copy Address", "copy", u["address"])])
            rows.append([("🔄 Refresh", "home")])
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
                lines.append("\nNo wallets yet.")
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
                lines.append("\nNo master wallets yet.")
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
            )
            return text, keyboard([
                [("💵 Max Trade", "edit:max"),
                 ("📉 Slippage", "edit:slip")],
                [("🔁 Buys", "setting:buy"),
                 ("🔁 Sells", "setting:sell")],
                [("🛑 Stop Now", "estop")],
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
                if t["message"] and t["status"] in ("SKIPPED", "UNKNOWN", "FAILED", "CONFIRMED", "EXPIRED"):
                    line += f"\n<i>{html.escape(str(t['message'])[:110])}</i>"
                rows.append([("Master transaction", f"url:https://solscan.io/tx/{t['master_signature']}")])
                lines.append(line)
                if t["signature"]:
                    rows.append([
                        ("📋 Copy ID", "copy", t["signature"]),
                        ("🔎 View", f"url:https://solscan.io/tx/{t['signature']}"),
                    ])
            if not trades:
                lines.append("No trades yet.")
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
        await self.show(update, "home", "Stopped. No new trades will start.")

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
            return await self.show(update, "home", "Stopped. No new trades will start.")

        if data == "generate":
            try:
                wid = await self.save_wallet(uid, Keypair())
                return await self.show(update, f"walletview:{wid}", "Wallet created.")
            except ValueError as exc:
                return await self.show(update, "wallet", str(exc))

        if data == "import":
            ephemeral = X25519PrivateKey.generate()
            public = ephemeral.public_key().public_bytes_raw().hex()
            ctx.user_data["pending"] = ("import", ephemeral, time.monotonic())
            return await q.edit_message_text(
                "On your own computer, run this project command and enter the wallet key "
                "at its hidden prompt. Send only the encrypted output here within five minutes.\n\n"
                f"python main.py --seal-wallet {public}"
            )

        if data.startswith("walletview:"):
            ctx.user_data.pop("pending", None)
            return await self.show(update, data)

        if data.startswith("key:"):
            return await self.show(update, "wallet", "Wallet keys are kept encrypted on the server.")

        if data.startswith("active:"):
            wid = int(data.split(":")[1])
            await self.select_wallet(uid, wid)
            return await self.show(
                update, f"walletview:{wid}",
                "Active wallet changed. Auto trading is paused."
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
                "Transfer remaining funds before deleting this wallet.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard([
                    [("✅ Delete", f"delconfirm:{nonce}")],
                    [("❌ Keep", f"walletview:{wid}")],
                ])
            )

        if data.startswith("delconfirm:"):
            pending = ctx.user_data.pop("delete", None)
            if not pending or pending[1] != data.split(":")[1]:
                return await self.show(update, "wallet", "That confirmation expired.")
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
                return await self.show(update, "wallet", "That confirmation expired.")
            if time.monotonic() > confirmation["expires"]:
                return await self.show(update, "wallet", "That confirmation expired.")
            await q.edit_message_text("📤 Sending...")
            try:
                signature, outcome = await self.send_funds(
                    uid, confirmation["wid"], confirmation["asset"],
                    confirmation["destination"], confirmation["amount"]
                )
                title = {
                    "CONFIRMED": "✅ <b>Sent</b>",
                    "FAILED": "❌ <b>The transfer failed</b>",
                    "PENDING": "⏳ <b>Sent — still waiting for confirmation</b>",
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
                    "⚠️ I couldn't confirm that transfer. Check your wallet before "
                    f"trying again.\n\n<code>{html.escape(sig)}</code>",
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
                    "❌ Something went wrong and the transfer wasn't sent. "
                    "Please check your wallet before trying again.",
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
                    "A trade is still being checked. Try again in a minute."
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
            }.get(data.split(":")[1])
            if column:
                await self.pool.execute(
                    f"UPDATE copy_users SET {column}=NOT {column} WHERE id=$1", uid
                )
            return await self.show(update, "settings")

        if data.startswith("edit:"):
            mode = data.split(":")[1]
            ctx.user_data["pending"] = mode
            prompts = {
                "max": "How much (in USD) should each trade be? Enter 0.50 to 1000.",
                "slip": "Enter your slippage as a percentage, from 0.1 to 70. The default is 20.",
                "poll": "How often should I check for new trades, in seconds? Enter 0.5 to 10.",
                "age": "Ignore master trades older than how many seconds? Enter 10 to 180.",
            }
            return await q.edit_message_text(prompts.get(mode, "Unknown setting"))

        if data == "addmaster":
            if len(await self.masters(uid)) >= 3:
                return await self.show(update, "masters", "You can add up to 3 masters.")
            ctx.user_data["pending"] = "master"
            return await q.edit_message_text(
                "Paste the wallet address of the trader you want to copy."
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
            return await q.edit_message_text("Enter a new nickname for this master.")

        if data.startswith("masterreplace:"):
            ctx.user_data["pending"] = ("mreplace", int(data.split(":")[1]))
            return await q.edit_message_text(
                "Paste the new wallet address for this master."
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
                if time.monotonic() - pending[2] > 300:
                    ctx.user_data.pop("pending", None)
                    raise ValueError("Import expired. Tap Import again.")
                try:
                    peer, payload = value.split(".", 1)
                    cipher = import_cipher(pending[1], bytes.fromhex(peer))
                    key = Keypair.from_bytes(cipher.decrypt(payload.encode(), ttl=300))
                except (InvalidToken, ValueError) as exc:
                    raise ValueError("Invalid encrypted import. Use the local seal-wallet command.") from exc
                wid = await self.save_wallet(uid, key)
                ctx.user_data.pop("pending", None)
                return await self.show(update, f"walletview:{wid}", "Wallet added.")

            if isinstance(pending, tuple) and pending[0] == "recipient":
                _, wid, asset = pending
                if not valid_address(value):
                    raise ValueError("That wallet address isn't valid.")
                w = await self.wallet(uid, wid)
                if not w or w["address"] == value:
                    raise ValueError("Please enter a different wallet address.")
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
                    raise ValueError("That amount isn't valid or has too many decimals.")
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
                    raise ValueError("That wallet address isn't valid.")
                if await self.pool.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM copy_wallets WHERE user_id=$1 AND address=$2)",
                    uid, value
                ):
                    raise ValueError("You can't copy your own wallet.")
                if pending == "master":
                    if len(await self.masters(uid)) >= 3:
                        raise ValueError("You can add up to 3 masters.")
                    if await self.pool.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM copy_masters WHERE user_id=$1 AND address=$2)",
                        uid, value
                    ):
                        raise ValueError("That master is already in your list.")
                    ctx.user_data["pending"] = ("nickname", value)
                    return await update.message.reply_text("Enter a nickname for this master.")
                await self.pool.execute(
                    "UPDATE copy_masters SET address=$3,active_since=now() WHERE id=$1 AND user_id=$2",
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

            if pending in ("max", "slip", "poll", "age"):
                n = amount_number(value)
                if pending == "poll":
                    if not Decimal("0.5") <= n <= Decimal("10"):
                        raise ValueError("Please enter a number from 0.5 to 10.")
                    if n != n.quantize(Decimal("0.1")):
                        raise ValueError("Use steps of 0.1 (for example 1.5).")
                    await self.pool.execute(
                        "UPDATE copy_users SET poll_interval_seconds=$2 WHERE id=$1", uid, n
                    )
                elif pending == "age":
                    if n != n.to_integral_value() or not 10 <= n <= 180:
                        raise ValueError("Please enter a whole number from 10 to 180.")
                    await self.pool.execute(
                        "UPDATE copy_users SET max_signal_age_seconds=$2 WHERE id=$1",
                        uid, int(n)
                    )
                elif pending == "max":
                    if not Decimal("0.5") <= n <= Decimal("1000"):
                        raise ValueError("Please enter an amount from $0.50 to $1,000.")
                    await self.pool.execute(
                        "UPDATE copy_users SET max_usd=$2 WHERE id=$1",
                        uid, n.quantize(Decimal(".01"))
                    )
                else:
                    if not Decimal(".1") <= n <= Decimal("70"):
                        raise ValueError("Please enter a number from 0.1 to 70.")
                    await self.pool.execute(
                        "UPDATE copy_users SET slip_bps=$2 WHERE id=$1",
                        uid, int((n * 100).to_integral_value())
                    )
                ctx.user_data.pop("pending", None)
                return await self.show(update, "settings", "Saved.")
        except asyncpg.UniqueViolationError:
            await update.message.reply_text("❌ That master is already in your list.")
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
            "amount": format(amount, "f"),
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

    async def lookup_keys(self, blob):
        lookups = list(getattr(VersionedTransaction.from_bytes(blob).message, "address_table_lookups", ()))
        if not lookups:
            return []
        result = await self.rpc("getMultipleAccounts", [
            [str(table.account_key) for table in lookups], {"encoding": "base64", "commitment": "confirmed"}
        ])
        writable, readonly = [], []
        for table, account in zip(lookups, result["value"], strict=True):
            if not account or account["owner"] != "AddressLookupTab1e1111111111111111111111111":
                raise ValueError("Invalid lookup table")
            raw = base64.b64decode(account["data"][0], validate=True)
            if len(raw) < 56 or raw[:4] != bytes([1, 0, 0, 0]) or (len(raw) - 56) % 32:
                raise ValueError("Invalid lookup table data")
            addresses = [str(Pubkey.from_bytes(raw[i:i+32])) for i in range(56, len(raw), 32)]
            for indices, target in ((table.writable_indexes, writable), (table.readonly_indexes, readonly)):
                for index in indices:
                    if index >= len(addresses):
                        raise ValueError("Invalid lookup index")
                    target.append(addresses[index])
        return writable + readonly

    async def curve_spot(self, mint):
        program = Pubkey.from_string(PUMP_PROGRAM)
        address, _ = Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))], program)
        result = await self.rpc("getAccountInfo", [str(address), {"encoding": "base64", "commitment": "confirmed"}])
        account = result["value"]
        if not account or account["owner"] != PUMP_PROGRAM:
            raise ValueError("Curve unavailable")
        data = base64.b64decode(account["data"][0], validate=True)
        if (len(data) < 49 or data[:8] != hashlib.sha256(b"account:BondingCurve").digest()[:8]
                or data[48] or any(data[83:115])):
            raise ValueError("Unsupported or completed SOL curve")
        token, sol = struct.unpack_from("<QQ", data, 8)
        if not token or not sol:
            raise ValueError("Empty curve")
        return Decimal(sol) / token

    def validate_portal_tx(self, blob, key, side, mint, spend_lamports, slip_bps, resolved=(), fee_limit=None):
        """Strict validation of the third-party transaction. Raises ValueError if unsafe."""
        tx = VersionedTransaction.from_bytes(blob)
        msg = tx.message
        keys = [str(k) for k in msg.account_keys]
        wallet = str(key.pubkey())
        if msg.header.num_required_signatures != 1 or not keys or keys[0] != wallet:
            raise ValueError("PumpPortal transaction has unexpected signer/fee payer")
        lookups = list(getattr(msg, "address_table_lookups", []) or [])
        if len(resolved) != sum(len(t.writable_indexes) + len(t.readonly_indexes) for t in lookups):
            raise ValueError("Missing lookup keys")
        keys.extend(resolved)
        if mint not in keys:
            raise ValueError("PumpPortal transaction does not reference the target mint")

        wrapped = token_account(wallet, WSOL, TOKEN_PROGRAM_ID)
        if fee_limit is None:
            fee_limit = spend_lamports * PUMPPORTAL_FEE_BPS // 10_000 if side == "BUY" else 0
        service_fee = 0
        swap_input = 0
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
                name = SWAP_NAMES.get(data[:8], "")
                if saw_pump or len(data) < 24 or not name:
                    raise ValueError("Unexpected swap direction or instruction")
                if program == PUMPSWAP_PROGRAM:
                    if name not in ("buy", "sell", "buy_exact_quote_in") or len(accts) < 13:
                        raise ValueError("Unsupported PumpSwap layout")
                    user, base, dest, token_program = accts[1], accts[3], accts[5], accts[11]
                    direction = side.lower()
                    quote_mint, quote_account = accts[4], accts[6]
                    if accts[3] == WSOL and accts[4] == mint:
                        base, dest, token_program = accts[4], accts[6], accts[12]
                        quote_mint, quote_account = accts[3], accts[5]
                        direction = "sell" if side == "BUY" else "buy"
                    if not name.startswith(direction) or quote_mint != WSOL or quote_account != wrapped:
                        raise ValueError("Unexpected PumpSwap quote account")
                elif name.endswith("_v2"):
                    if len(accts) < 16 or accts[2] != WSOL:
                        raise ValueError("Unsupported curve quote")
                    user, base, dest, token_program = accts[13], accts[1], accts[14], accts[3]
                else:
                    if name not in ("buy", "sell", "buy_exact_sol_in") or len(accts) < 10:
                        raise ValueError("Unsupported curve instruction")
                    user, base, dest = accts[6], accts[2], accts[5]
                    token_program = accts[9 if side == "SELL" else 8]
                if program == PUMP_PROGRAM and not name.startswith(side.lower()):
                    raise ValueError("Unexpected curve swap direction")
                if (user != wallet or base != mint or token_program not in (TOKEN_PROGRAM_ID, TOKEN_2022_ID)
                        or dest != token_account(wallet, mint, token_program)):
                    raise ValueError("Unexpected Pump wallet or mint")
                first, second = struct.unpack_from("<QQ", data, 8)
                maximum_input = second if name in ("buy", "buy_v2") else first
                if min(first, second) == 0 or maximum_input > spend_lamports:
                    raise ValueError("Swap exceeds input budget or lacks an output minimum")
                swap_input = maximum_input
                saw_pump = True
            elif program == SYSTEM_ID:
                kind = int.from_bytes(data[:4], "little")
                if kind == 2 and accts == [wallet, wrapped] and side == "BUY":
                    if not accts or accts[0] != wallet or len(data) < 12:
                        raise ValueError("System instruction does not originate from wallet")
                    system_out += int.from_bytes(data[4:12], "little")
                elif kind == 2 and len(data) == 12 and accts == [wallet, PUMPPORTAL_FEE_RECIPIENT]:
                    service_fee += int.from_bytes(data[4:12], "little")
                    if service_fee > fee_limit:
                        raise ValueError("PumpPortal service fee exceeds budget")
                else:
                    raise ValueError(f"Disallowed System instruction type {kind}")
            elif program in (TOKEN_PROGRAM_ID, TOKEN_2022_ID):
                op = data[0] if data else -1
                if op == 17 and accts == [wrapped]:
                    pass
                elif op == 9:
                    if accts != [wrapped, wallet, wallet]:
                        raise ValueError("Token close sends funds to a foreign account")
                else:
                    raise ValueError(f"Disallowed token instruction {op}")
            elif program == ATA_PROGRAM_ID:
                if (len(accts) < 6 or accts[0] != wallet or accts[2] != wallet
                        or accts[3] not in (mint, WSOL) or accts[5] not in (TOKEN_PROGRAM_ID, TOKEN_2022_ID)
                        or accts[1] != token_account(wallet, accts[3], accts[5])
                        or data not in (b"", b"\x00", b"\x01")):
                    raise ValueError("Unexpected associated account")
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
            cap = spend_lamports
        else:
            cap = PORTAL_RENT_ALLOWANCE
        if system_out > cap or (side == "BUY" and max(swap_input, system_out) + service_fee > cap):
            raise ValueError("Builder transaction moves more SOL than allowed")
        return msg

    def sign_portal_transaction(self, blob, key, side, mint, spend_lamports, slip_bps, resolved=(), fee_limit=None):
        msg = self.validate_portal_tx(blob, key, side, mint, spend_lamports, slip_bps, resolved, fee_limit)
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
                "maxRetries": 3,
            },
        ])

    # ------------------------------------------------------------------ websocket
    async def websocket_monitor(self):
        """Dispatch confirmed signatures directly. Polling covers disconnects and queue overflow."""
        backoff = 1
        while True:
            try:
                ws_url = RPC.replace("https://", "wss://", 1)
                async with self.session.ws_connect(ws_url, heartbeat=25) as ws:
                    LOG.info("WebSocket connected")
                    backoff = 1
                    subs = {}      # subscription id -> address
                    active = {}    # address -> subscription id
                    pending = {}   # request id -> (address, sent_at)
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
                            in_flight = {a for a, _ in pending.values()}
                            for addr in desired - set(active) - in_flight:
                                req += 1
                                pending[req] = (addr, now)
                                await ws.send_json({
                                    "jsonrpc": "2.0", "id": req,
                                    "method": "logsSubscribe",
                                    "params": [
                                        {"mentions": [addr]},
                                        {"commitment": "confirmed"},
                                    ],
                                })
                            for addr in list(active):
                                if addr not in desired:
                                    sid = active.pop(addr)
                                    subs.pop(sid, None)
                                    req += 1
                                    await ws.send_json({
                                        "jsonrpc": "2.0", "id": req,
                                        "method": "logsUnsubscribe", "params": [sid],
                                    })
                            for _, (addr, sent) in pending.items():
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
                                addr, _ = pending.pop(rid)
                                if payload.get("error"):
                                    raise RuntimeError("subscription rejected by server")
                                sid = payload.get("result")
                                subs[sid] = addr
                                active[addr] = sid
                                LOG.info(
                                    "WebSocket subscription acknowledged wallet=%s active=%d",
                                    shortened(addr), len(active)
                                )
                            elif payload.get("method") == "logsNotification":
                                params = payload.get("params") or {}
                                addr = subs.get(params.get("subscription"))
                                value = (params.get("result") or {}).get("value") or {}
                                if addr and value.get("signature") and value.get("err") is None:
                                    queue = self.alert_queues[hash(addr) % len(self.alert_queues)]
                                    try:
                                        queue.put_nowait((addr, value["signature"]))
                                    except asyncio.QueueFull:
                                        self.hot_addresses.add(addr)
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
                LOG.warning(
                    "WebSocket unavailable (%s); polling remains active",
                    type(exc).__name__
                )
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
                if response.status in (401, 403):
                    raise ValueError("Jupiter access denied. Check JUPITER_API_KEY on the server.")
                if 400 <= response.status < 500 and response.status != 429:
                    raise ValueError("This token can't be traded right now")
                raise RuntimeError(f"Jupiter order HTTP {response.status}")
            if not isinstance(data, dict) or not data.get("outAmount"):
                raise ValueError("This token can't be traded right now")
            if (data.get("inputMint") != source or data.get("outputMint") != destination
                    or int(data.get("inAmount") or 0) != int(amount) or int(data["outAmount"]) <= 0):
                raise ValueError("Jupiter quote does not match the requested swap")
            if wallet:
                if not data.get("transaction") or not data.get("requestId"):
                    reason = str(data.get("errorMessage") or "no executable route")[:120]
                    raise ValueError(f"Jupiter {data.get('router', 'order')}: {reason}")
                if data.get("taker") != wallet or not 0 <= int(data.get("slippageBps", -1)) <= slippage:
                    raise ValueError("Unexpected Jupiter signer or slippage")
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
            raise ValueError("Trade was rejected for safety")
        tx = VersionedTransaction.from_bytes(base64.b64decode(order["transaction"]))
        required = tx.message.header.num_required_signatures
        signers = list(tx.message.account_keys[:required])
        if key.pubkey() not in signers:
            raise ValueError("Trade was rejected for safety")

        signatures = list(tx.signatures)
        message_data = to_bytes_versioned(tx.message)
        local_signature = key.sign_message(message_data)
        if not local_signature.verify(key.pubkey(), message_data):
            raise RuntimeError("Local signature verification failed")
        signatures[signers.index(key.pubkey())] = local_signature
        signed = VersionedTransaction.populate(tx.message, signatures)
        signed_signature = str(signed.signatures[0])
        if signed.signatures[0] == Signature.default():
            raise ValueError("Trade was rejected for safety")
        headers = {"x-api-key": JUP_KEY} if JUP_KEY else {}
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
        msg = (tx.get("transaction") or {}).get("message") or {}
        keys = [
            k.get("pubkey") if isinstance(k, dict) else k
            for k in msg.get("accountKeys", [])
        ]
        if not any(isinstance(k, dict) and k.get("pubkey") == master and k.get("signer")
                   for k in msg.get("accountKeys", [])):
            return None, "Master did not sign"
        # Match instruction logs to the invoking program, including CPI swaps.
        stack = []
        venues = set()
        for line in log_lines:
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "Program" and parts[2] == "invoke":
                stack.append(parts[1])
            elif stack and line.startswith(f"Program {stack[-1]} success"):
                stack.pop()
            elif stack and line.startswith("Program log: Instruction: "):
                name = line.split(": ")[-1].lower().replace("_", "")
                if ((stack[-1] in (PUMP_PROGRAM, PUMPSWAP_PROGRAM) and name in
                     ("buy", "sell", "buyv2", "sellv2", "buyexactsolin", "buyexactquotein", "buyexactquoteinv2"))
                        or (stack[-1] == JUP_PROGRAM and "route" in name)):
                    venues.add(stack[-1])


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
                if sol_delta < 0:
                    quote_spent = True
                if sol_delta > 0:
                    quote_gained = True

        # Bind the owner's changed token account to the swap itself. An unrelated
        # transfer in the same signature must never select the copied mint.
        instructions = list(msg.get("instructions") or [])
        for group in meta.get("innerInstructions") or []:
            instructions.extend(group.get("instructions") or [])
        candidate = gained or lost
        if len(candidate) != 1:
            return None, "Multiple or missing target mints"
        target = candidate[0][0]
        side = "BUY" if gained else "SELL"
        owned_accounts = set()
        for field in ("preTokenBalances", "postTokenBalances"):
            for entry in meta.get(field) or []:
                idx = entry.get("accountIndex")
                if (entry.get("owner") == master and entry.get("mint") == target
                        and isinstance(idx, int) and 0 <= idx < len(keys)):
                    owned_accounts.add(keys[idx])
        bound = False
        for ix in instructions:
            program = ix.get("programId")
            accts = ix.get("accounts") or []
            if master not in accts:
                continue
            if program in DIRECT_SWAP_PROGRAMS and owned_accounts.intersection(accts):
                raw = base58.b58decode(ix.get("data", ""))
                if program == RAY_V4:
                    matches = (len(raw) == 17 and raw[0] in (9, 11)
                               and len(accts) >= 3 and accts[-1] == master
                               and accts[-2 if side == "BUY" else -3] in owned_accounts)
                else:
                    matches = raw[:8] in DIRECT_SWAP_DISCRIMINATORS
                if matches:
                    venues.add(program)
                    bound = True
                continue
            if program not in venues and program not in (PUMP_PROGRAM, PUMPSWAP_PROGRAM):
                continue
            if program == JUP_PROGRAM:
                if owned_accounts.intersection(accts):
                    bound = True
                continue
            data = base58.b58decode(ix.get("data", ""))
            name = SWAP_NAMES.get(data[:8], "")
            direction = side.lower()
            if program == PUMPSWAP_PROGRAM:
                user_i, mint_i, account_i = 1, 3, 5
                if len(accts) > 6 and accts[4] == target and accts[3] in QUOTE_MINTS:
                    direction = "sell" if side == "BUY" else "buy"
                    mint_i, account_i = 4, 6
            elif name.endswith("_v2"):
                user_i, mint_i, account_i = 13, 1, 14
            else:
                user_i, mint_i, account_i = 6, 2, 5
            if (name.startswith(direction) and len(accts) > max(user_i, mint_i, account_i) and accts[user_i] == master
                    and accts[mint_i] == target and accts[account_i] in owned_accounts):
                bound = True
                venues.add(program)
        venue = detect_venue(venues)
        if not bound:
            return None, "Target token account could not be tied to the swap instruction"

        if len(gained) == 1 and len(lost) == 0 and quote_spent:
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
    async def finalize_trade(self, tid, status, message, signature):
        return await self.pool.fetchrow("""
            UPDATE copy_trades SET status=$2, message=$3
            WHERE id=$1 AND status = ANY($4) AND signature=$5
            RETURNING user_id,side,mint,usd,signature,route
        """, tid, status, message, UNRESOLVED, signature)

    async def announce_trade(self, row, status):
        sig = row["signature"]
        link = f"https://solscan.io/tx/{sig}" if sig else ""
        if status == "CONFIRMED":
            verb = "Bought" if row["side"] == "BUY" else "Sold"
            usd = f" (about ${row['usd']:.2f})" if row["usd"] else ""
            via = VIA_WORDS.get(row["route"] or "")
            text = (
                f"✅ <b>{verb}</b>{usd}\n"
                f"Token: <code>{row['mint']}</code>\n"
                + (f"Via: {via}\n" if via else "")
                + (f"<a href=\"{link}\">View</a>" if link else "")
            )
        elif status == "EXPIRED":
            text = f"Copy order expired without a transaction receipt. Token: <code>{row['mint']}</code>"
        else:
            text = (
                f"❌ <b>The {row['side'].lower()} didn't go through</b>\n"
                f"Token: <code>{row['mint']}</code>\n"
                + (f"<a href=\"{link}\">View</a>" if link else "")
            )
        await self.notify(row["user_id"], text)

    async def reconcile_one(self, tid, signature, signed_at=None, blockhash=None):
        result = await self.rpc("getSignatureStatuses", [
            [signature], {"searchTransactionHistory": True},
        ])
        state = ((result or {}).get("value") or [None])[0]
        if state and state.get("confirmationStatus") in ("confirmed", "finalized") and state.get("err") is not None:
            status, message = "FAILED", "On-chain transaction failed"
        elif state and state.get("confirmationStatus") in ("confirmed", "finalized"):
            trade = await self.pool.fetchrow(
                "SELECT wallet_address, side, mint FROM copy_trades WHERE id=$1", tid
            )
            tx = await self.get_transaction(signature)
            meta = tx.get("meta") or {}
            if meta.get("err") is not None or signature != tx["transaction"]["signatures"][0]:
                return "UNKNOWN"
            owner = trade["wallet_address"]
            if owner is None:
                signers = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"] if k.get("signer")]
                if len(signers) != 1:
                    return "UNKNOWN"
                owner = signers[0]

            def total(field):
                return sum(int(item["uiTokenAmount"]["amount"]) for item in meta[field]
                           if item.get("owner") == owner and item["mint"] == trade["mint"])
            delta = total("postTokenBalances") - total("preTokenBalances")
            if delta * (1 if trade["side"] == "BUY" else -1) <= 0:
                changed = await self.pool.fetchval("""
                    UPDATE copy_trades SET status='UNKNOWN', message='Confirmed signature, unexpected mint delta'
                    WHERE id=$1 AND status IN ('SIGNED','SUBMITTED') RETURNING user_id
                """, tid)
                if changed is not None:
                    await self.pause_user(changed, "On-chain token movement needs review. Check History.", unresolved=True)
                return "UNKNOWN"
            status, message = "CONFIRMED", f"Verified token delta (raw units): {delta}"
        elif (state is None and result and result.get("value") == [None] and blockhash and signed_at
              and (datetime.now(timezone.utc) - signed_at).total_seconds() >= 180):
            validity = await self.rpc("isBlockhashValid", [blockhash, {"commitment": "finalized"}])
            if validity.get("value") is not False:
                return "UNKNOWN"
            receipt = await self.rpc("getTransaction", [signature, {
                "encoding": "jsonParsed", "commitment": "finalized", "maxSupportedTransactionVersion": 1,
            }])
            if receipt is not None:
                return "UNKNOWN"
            status, message = "EXPIRED", "Blockhash expired. RPC returned no finalized receipt."
        else:
            return "UNKNOWN"
        row = await self.finalize_trade(tid, status, message, signature)
        if row:
            self.trade_wake.set()
            await self.announce_trade(row, status)
        return status

    async def reconcile_all(self):
        rows = await self.pool.fetch("""
            SELECT id,signature,signed_at,recent_blockhash FROM copy_trades
            WHERE status = ANY($1) AND signature IS NOT NULL
            ORDER BY id LIMIT 100
        """, UNRESOLVED)
        for row in rows:
            try:
                await self.reconcile_one(row["id"], row["signature"], row["signed_at"], row["recent_blockhash"])
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
                if state and state.get("confirmationStatus") in ("confirmed", "finalized"):
                    new = "FAILED" if state.get("err") is not None else "CONFIRMED"
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
                for uid in list(self.awaiting_resolution):
                    if not await self.has_unresolved(uid):
                        self.awaiting_resolution.discard(uid)
                        await self.notify(
                            uid,
                            "✅ Everything is sorted out now. Have a look at your wallet, "
                            "then turn Auto Trade back on when you're ready."
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Reconcile loop error")

    # ------------------------------------------------------------------ execution
    async def trade_worker(self):
        while True:
            try:
                rows = await self.pool.fetch("""
                    UPDATE copy_trades SET status='QUOTING'
                    WHERE id IN (
                        SELECT q.id FROM copy_trades q WHERE q.status='QUEUED'
                          AND NOT EXISTS (
                              SELECT 1 FROM copy_trades earlier
                              WHERE earlier.user_id=q.user_id AND earlier.id<>q.id
                                AND (earlier.status IN ('SIGNED','SUBMITTED','UNKNOWN','SUBMITTING','QUOTING')
                                     OR (earlier.status='QUEUED' AND earlier.id<q.id))
                          )
                        ORDER BY q.id LIMIT 10 FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id,user_id,master_address,master_signature,side,mint,
                              fraction,block_time,venue,created_at
                """)
                for row in rows:
                    task = asyncio.create_task(self.run_trade(row))
                    self.inflight.add(task)
                    task.add_done_callback(self._trade_done)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Trade worker error")
            try:
                await asyncio.wait_for(self.trade_wake.wait(), timeout=1.0)
                self.trade_wake.clear()
            except asyncio.TimeoutError:
                pass

    def _trade_done(self, task):
        self.inflight.discard(task)
        self.trade_wake.set()
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
        await self.notify(uid, f"⚠️ A trade was skipped: {html.escape(reason)}")

    async def execute_copy(self, row, *, jupiter_only=False):
        tid = row["id"]
        uid = row["user_id"]
        side = row["side"]
        mint = row["mint"]
        fraction = row["fraction"]
        observed = float(row["block_time"] or row["created_at"].timestamp())
        phase = "PREPARING"
        signed_sig = None
        try:
            u = await self.user(uid)
            if not u["enabled"]:
                raise ValueError("Auto trading is paused")
            if time.time() - observed > int(u["max_signal_age_seconds"]):
                raise ValueError("The master's trade was too old")
            if side == "BUY" and not u["copy_buys"]:
                raise ValueError("Copy buys is turned off")
            if side == "SELL" and not u["copy_sells"]:
                raise ValueError("Copy sells is turned off")
            if not u["encrypted_secret"]:
                raise ValueError("No active wallet")
            if not valid_address(mint):
                raise ValueError("Invalid token")
            wallet = u["address"]
            slip = int(u["slip_bps"])
            if not 10 <= slip <= MAX_SLIPPAGE_BPS:
                raise ValueError("Slippage must be between 0.1% and 70%")

            async with self.wallet_lock(wallet):
                current = await self.user(uid)
                if not current["enabled"] or current["address"] != wallet:
                    raise ValueError("Auto trading was paused or the wallet changed")
                if time.time() - observed > int(current["max_signal_age_seconds"]):
                    raise ValueError("The master's trade was too old by the time it was ready")
                if await self.has_unresolved(uid):
                    raise ValueError("An earlier trade is still being checked")

                phase = "SIZING"
                sizing_cap = current["max_usd"]
                decimals = None
                if side == "BUY":
                    balance, price = await asyncio.gather(self.rpc("getBalance", [wallet]), self.sol_usd())
                    available_sol = max(0, int(balance["value"]) - MIN_SOL_RESERVE)
                    usd_limit = Decimal(current["max_usd"])
                    max_lamports = int(
                        (usd_limit / price * Decimal(10**9)).to_integral_value(rounding=ROUND_DOWN)
                    )
                    if available_sol > 0 and max_lamports > 0:
                        source, dest = WSOL, mint
                        raw = min(available_sol, max_lamports)
                        usd = min(usd_limit, Decimal(raw) / Decimal(10**9) * price)
                        if raw < max_lamports and usd < Decimal("0.50"):
                            raise ValueError("Not enough SOL: trade would be under $0.50")
                    else:
                        raise ValueError(
                            "Not enough SOL to copy this trade. Add SOL to your wallet."
                        )
                else:
                    if fraction is None or not (Decimal(0) < Decimal(fraction) <= Decimal(1)):
                        raise ValueError("Couldn't work out how much to sell")
                    fraction = Decimal(fraction)
                    owned, decimals = await self.token_balance(wallet, mint)
                    raw = int(Decimal(owned) * fraction)
                    if raw <= 0:
                        raise ValueError("No tokens available to sell")
                    # Value sells conservatively using an exact-input quote.
                    if row["venue"] == "pumpfun":
                        try:
                            sell_lamports = Decimal(raw) * await self.curve_spot(mint)
                        except ValueError:
                            sell_lamports = Decimal((await self.jupiter_order(mint, WSOL, raw))["outAmount"])
                    else:
                        sell_lamports = Decimal((await self.jupiter_order(mint, WSOL, raw))["outAmount"])
                    value = sell_lamports * await self.sol_usd() / Decimal(10**9)
                    if value <= 0:
                        raise ValueError("Cannot value sell")
                    if value > current["max_usd"]:
                        raw = int(Decimal(raw) * current["max_usd"] / value)
                    if raw <= 0:
                        raise ValueError("Sell is below one token unit")
                    source, dest = mint, WSOL
                    usd = min(value, current["max_usd"])

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
                    raise ValueError("Wallet problem; trade skipped")

                route = None
                order = None
                headers = None
                signed_sig = None
                signed_b64 = None
                pump_venue = row["venue"] in ("pumpfun", "pumpswap")

                if pump_venue and not jupiter_only:
                    try:
                        if side == "BUY":
                            nominal = raw * 10000 // (10000 + slip + PUMPPORTAL_FEE_BPS)
                            fee_limit = (nominal * PUMPPORTAL_FEE_BPS + 9999) // 10000
                            amount_arg = Decimal(nominal) / Decimal(10**9)
                            spend = raw
                        else:
                            spend = raw
                            fee_limit = int(sell_lamports * PUMPPORTAL_FEE_BPS / 10000 * usd / value) + 1
                            amount_arg = Decimal(raw) / Decimal(10**decimals)
                        blob = await self.portal_transaction(wallet, mint, side, amount_arg, slip)
                        signed_sig, signed_b64 = self.sign_portal_transaction(
                            blob, key, side, mint, spend, slip, await self.lookup_keys(blob), fee_limit
                        )
                        route = "pumpportal"
                    except (ValueError, IndexError, RuntimeError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                        LOG.warning(
                            "PumpPortal route rejected trade_id=%s (%s: %s); trying Jupiter",
                            tid, type(exc).__name__, str(exc).split("?")[0][:160]
                        )

                if route is None:
                    order = await self.jupiter_order(source, dest, raw, wallet, slip)
                    signed_sig, signed_b64, headers = await self.jupiter_execute(order, key)
                    route = "jupiter"
                    if side == "SELL":
                        try:
                            price = await self.sol_usd()
                            usd = Decimal(str(order["outAmount"])) / Decimal(10**9) * price
                        except Exception:
                            usd = Decimal(0)

                current = await self.user(uid)
                if (not current["enabled"] or current["address"] != wallet
                        or current["max_usd"] < sizing_cap or current["slip_bps"] < slip
                        or not current["copy_buys" if side == "BUY" else "copy_sells"]
                        or time.time() - observed > current["max_signal_age_seconds"]
                        or not await self.pool.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM copy_masters WHERE user_id=$1 AND address=$2 AND active)",
                            uid, row["master_address"])):
                    raise ValueError("Copy settings changed or signal expired")
                signed_tx = VersionedTransaction.from_bytes(base64.b64decode(signed_b64))
                blockhash = str(signed_tx.message.recent_blockhash)
                # Save the transaction ID BEFORE sending it.
                await self.pool.execute("""
                    UPDATE copy_trades
                    SET signature=$2,status='SIGNED',signed_at=now(),message=NULL,
                        route=$3,usd=$4,wallet_address=$5,recent_blockhash=$6
                    WHERE id=$1
                """, tid, signed_sig, route, usd, wallet, blockhash)
                phase = "SUBMITTING"

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

            # Lock released; confirm on-chain (background checker continues if still pending).
            chain = "UNKNOWN"
            for _ in range(10):
                chain = await self.reconcile_one(tid, signed_sig, None)
                if chain != "UNKNOWN":
                    break
                await asyncio.sleep(0.8)
            if chain == "CONFIRMED":
                LOG.info("Copy confirmed trade_id=%s sig=%s", tid, shortened(signed_sig))
            elif chain == "FAILED":
                LOG.warning("On-chain trade failed trade_id=%s", tid)
            else:
                LOG.info("Trade %s submitted; awaiting confirmation via reconciler", tid)

        except PreflightRejected as exc:
            # This was the only submission of this signature. An explicit RPC
            # simulation rejection is distinct from a timeout after transmission.
            reset = await self.pool.fetchval("""
                UPDATE copy_trades SET status='QUOTING',signature=NULL,signed_at=NULL,
                    message=$3 WHERE id=$1 AND signature=$2 AND status='SIGNED'
                RETURNING id
            """, tid, signed_sig, f"Pump preflight rejected: {exc}")
            if reset is not None:
                LOG.info("Pump preflight rejected trade=%s error=%s; requesting Jupiter route", tid, exc)
                await self.execute_copy(row, jupiter_only=True)
        except Exception as exc:
            if phase == "SUBMITTING" and signed_sig:
                await self.pool.execute("""
                    UPDATE copy_trades SET status='UNKNOWN',message=$2
                    WHERE id=$1 AND status IN ('SIGNED','SUBMITTED')
                """, tid, f"{phase}: {type(exc).__name__}")
                LOG.error("Submission uncertain trade_id=%s exc=%s", tid, type(exc).__name__)
                await self.pause_user(
                    uid,
                    "I wasn't sure whether a trade went through, so I stopped to be safe. "
                    "I'm checking it now and I won't send it again. "
                    "I'll message you when it's sorted out.",
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
                LOG.info("Copy skipped trade_id=%s phase=%s reason=%s", tid, phase, reason)
                await self.skip_notice(uid, reason)
            else:
                await self.pool.execute("""
                    UPDATE copy_trades SET status='FAILED',message=$2
                    WHERE id=$1 AND status IN ('QUEUED','QUOTING')
                """, tid, "Couldn't complete this trade")
                LOG.error("Copy trade failed id=%s phase=%s exc=%s", tid, phase, type(exc).__name__)
                await self.notify(
                    uid,
                    f"❌ <b>Couldn't copy a {side.lower()}.</b> No money was spent."
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
        options = {"limit": 100, "commitment": "confirmed"}
        if previous:
            options["until"] = previous
        entries = await self.rpc("getSignaturesForAddress", [address, options])
        if not entries:
            return 0
        page = list(entries)
        while len(page) == 100 and (page[-1].get("blockTime") or 0) >= time.time() - max_age:
            options["before"] = page[-1]["signature"]
            page = await self.rpc("getSignaturesForAddress", [address, options])
            if page and page[-1]["signature"] == options["before"]:
                raise RuntimeError("RPC history pagination did not advance")
            entries.extend(page)
        newest = entries[0]["signature"]

        for item in reversed(entries):
            if item.get("err") or item.get("blockTime") is None:
                continue
            if -5 <= time.time() - item["blockTime"] <= max_age:
                signature = item["signature"]
                tx = await self.get_transaction(signature)
                await self.enqueue_signal(address, signature, tx, item["blockTime"])

        await self.pool.execute("""
            INSERT INTO copy_cursors(address,signature) VALUES($1,$2)
            ON CONFLICT(address) DO UPDATE SET signature=EXCLUDED.signature
        """, address, newest)
        return len(entries)

    async def alert_worker(self, queue):
        while True:
            address, signature = await queue.get()
            try:
                tx = await self.get_transaction(signature)
                if tx.get("blockTime") is not None:
                    await self.enqueue_signal(address, signature, tx, tx["blockTime"])
            except Exception as exc:
                LOG.warning("Alert deferred to polling master=%s signature=%s error=%s",
                            address, signature, type(exc).__name__)
            finally:
                queue.task_done()

    async def enqueue_signal(self, address, signature, tx, block_time):
        swap, reason = self.classify_swap(tx, address)
        if not swap:
            LOG.info("Ignored master=%s signature=%s reason=%s", address, signature, reason)
            return
        side, mint, fraction, venue = swap
        followers = await self.pool.fetch("""
            SELECT m.user_id, GREATEST(u.enabled_since,m.active_since) AS enabled_since, u.max_signal_age_seconds
            FROM copy_masters m
            JOIN copy_users u ON u.id=m.user_id
            WHERE m.address=$1 AND m.active AND u.enabled
        """, address)
        for follower in followers:
            since = follower["enabled_since"]
            if (since is None or block_time < int(since.timestamp())
                    or time.time() - block_time > follower["max_signal_age_seconds"]):
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
                    "Queued %s uid=%s mint=%s venue=%s master_sig=%s",
                    side, follower["user_id"], mint, venue, signature
                )
                self.trade_wake.set()

    async def poll_one(self, master, next_poll, burst):
        address = master["address"]
        next_poll[address] = time.monotonic() + float(master["interval"])
        try:
            found = await self.poll_master(address, int(master["max_age"]))
            if burst.get(address, 0) > 0:
                if found == 0:
                    burst[address] -= 1
                    next_poll[address] = time.monotonic() + 0.3
                else:
                    burst[address] = 0
        except Exception as exc:
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
                hot = set(self.hot_addresses)
                self.hot_addresses.clear()
                for address in hot:
                    next_poll[address] = 0
                    burst[address] = 3
                due = [m for m in masters if now >= next_poll.get(m["address"], 0)]
                if due:
                    await asyncio.gather(*(self.poll_one(m, next_poll, burst) for m in due))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Monitoring error")
            try:
                await asyncio.wait_for(self.ws_signal.wait(), timeout=0.25)
                self.ws_signal.clear()
            except asyncio.TimeoutError:
                pass


async def inspect_master_trade(address, signature):
    if not valid_address(address):
        raise SystemExit("Enter a valid master wallet address")
    try:
        Signature.from_string(signature)
    except ValueError:
        raise SystemExit("Enter a valid transaction signature") from None
    service = BotService()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=True) as session:
        service.session = session
        result = await service.rpc("getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 1,
        }], endpoint=RPC or "https://api.mainnet-beta.solana.com")
    swap, reason = service.classify_swap(result, address)
    if swap:
        side, mint, fraction, venue = swap
        print(f"{side} mint={mint} venue={venue} master_fraction={fraction}")
    else:
        print(f"Skipped: {reason}")


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--inspect-trade":
        asyncio.run(inspect_master_trade(*sys.argv[2:]))
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--seal-wallet":
        seal_wallet(sys.argv[2])
        return
    if not TOKEN or not DSN or not RPC:
        raise SystemExit("Set TELEGRAM_TOKEN, DATABASE_URL and HELIUS_RPC_URL")
    parsed_rpc = urlsplit(RPC)
    if (parsed_rpc.scheme != "https" or
            parsed_rpc.hostname != "mainnet.helius-rpc.com" or
            not parse_qs(parsed_rpc.query).get("api-key", [""])[0]):
        raise SystemExit("HELIUS_RPC_URL must be a valid Helius mainnet HTTPS RPC URL")
    service = BotService()
    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(service.startup)
        .post_shutdown(service.shutdown)
        .build()
    )
    async def on_error(update, ctx):
        LOG.error("Telegram handler failed: %s", type(ctx.error).__name__)

    app.add_error_handler(on_error)
    app.add_handler(CommandHandler(["start", "menu"], service.start))
    for command, page in (
        ("wallet", "wallet"),
        ("send", "wallet"),
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
