
"""Solana Telegram Copy Trader — single-file application."""
import asyncio
import base64
import html
import json
import logging
import os
import secrets
import struct
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

import aiohttp
import asyncpg
import base58
from cryptography.fernet import Fernet
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.hash import Hash
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
QUOTE_MINTS = {USDC, WSOL}
MAX_SIGNAL_AGE = 30

TOKEN_PROGRAM = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
ATA_PROGRAM = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
USDC_MINT = Pubkey.from_string(USDC)

DDL = """
CREATE TABLE IF NOT EXISTS copy_users (
 id BIGINT PRIMARY KEY,
 enabled BOOLEAN NOT NULL DEFAULT FALSE,
 max_usd NUMERIC(12,2) NOT NULL DEFAULT 10,
 slip_bps INTEGER NOT NULL DEFAULT 100,
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
    raise ValueError("Invalid private key. Use a Solana base58 secret or 64-byte JSON array.")

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
        self.busy = set()
        self.last_monitor_alert = {}

    async def startup(self, app):
        self.app = app
        self.fernet = encryption_key()
        self.pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
        await self.pool.execute(DDL)
        await self.pool.execute("""
            INSERT INTO copy_wallets(user_id,name,address,encrypted_secret)
            SELECT id,'Wallet 1',address,encrypted_secret
            FROM copy_users
            WHERE address IS NOT NULL AND encrypted_secret IS NOT NULL
            ON CONFLICT(user_id,address) DO NOTHING
        """)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=18))
        self.monitor_task = asyncio.create_task(self.monitor())
        self.cleanup_task = asyncio.create_task(self.cleanup_keys())
        await app.bot.set_my_commands([
            BotCommand("start", "Open dashboard"),
            BotCommand("wallet", "Manage wallets"),
            BotCommand("send", "Send funds"),
            BotCommand("masters", "Master wallets"),
            BotCommand("settings", "Trading settings"),
            BotCommand("history", "Trade history"),
            BotCommand("cancel", "Cancel action"),
        ])
        LOG.info("Solana copy bot started")

    async def shutdown(self, app):
        for task in (self.monitor_task, self.cleanup_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self.session:
            await self.session.close()
        if self.pool:
            await self.pool.close()

    async def rpc(self, method, params):
        # Do not emit the authenticated RPC URL into error messages or logs.
        async with self.rpc_limit:
            async with self.session.post(RPC, json={
                "jsonrpc": "2.0", "id": 1,
                "method": method, "params": params
            }) as response:
                if response.status != 200:
                    raise RuntimeError(f"Helius RPC {method} returned HTTP {response.status}")
                data = await response.json()
                if data.get("error"):
                    error = data["error"]
                    raise RuntimeError(
                        f"Helius RPC {method} failed (code {error.get('code', 'unknown')})"
                    )
                return data.get("result")

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
                    raise ValueError("This wallet is already imported.")
                count = await conn.fetchval(
                    "SELECT count(*) FROM copy_wallets WHERE user_id=$1", uid
                )
                if count >= 10:
                    raise ValueError("You can manage up to 10 wallets.")
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
            "This message will be deleted in one hour. "
            "Anyone with this key can control your funds.",
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

    async def send_funds(self, uid, wid, asset, recipient, amount):
        w = await self.wallet(uid, wid)
        if not w:
            raise ValueError("Wallet not found.")
        if not valid_address(recipient):
            raise ValueError("Invalid recipient address.")
        if recipient == w["address"]:
            raise ValueError("Recipient cannot equal sender.")
        if asset not in ("SOL", "USDC"):
            raise ValueError("Unsupported asset.")
        decimals = 9 if asset == "SOL" else 6
        raw = amount * (10**decimals)
        if amount <= 0 or not amount.is_finite() or raw != raw.to_integral_value():
            raise ValueError("Invalid transfer amount.")
        raw = int(raw)
        if raw >= 2**64:
            raise ValueError("Transfer amount too large.")

        async with self.lock:
            u = await self.user(uid)
            if u["enabled"] and u["address"] == w["address"]:
                raise ValueError("Pause auto trading before sending funds.")
            sender = Pubkey.from_string(w["address"])
            dest = Pubkey.from_string(recipient)
            key = Keypair.from_bytes(
                self.fernet.decrypt(w["encrypted_secret"].encode())
            )
            balance = await self.rpc("getBalance", [w["address"]])
            lamports = int(balance["value"])
            if asset == "SOL":
                if raw + 2_000_000 > lamports:
                    raise ValueError("Insufficient SOL. Leave 0.002 SOL for fees.")
                instructions = [transfer(TransferParams(
                    from_pubkey=sender, to_pubkey=dest, lamports=raw
                ))]
            else:
                if lamports < 3_000_000:
                    raise ValueError("At least 0.003 SOL is needed for fees.")
                available, _ = await self.token_balance(w["address"])
                if raw > available:
                    raise ValueError("Insufficient USDC.")
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
                raise RuntimeError(
                    "Submission uncertain. Check transaction " + signature
                ) from exc
            await self.pool.execute(
                "UPDATE copy_withdrawals SET status='SUBMITTED' WHERE id=$1",
                transfer_id
            )
            return signature

    async def screen(self, uid, page="home"):
        u = await self.user(uid)
        if page == "home":
            ms = await self.masters(uid)
            active = "🟢 ON" if u["enabled"] else "⏸ OFF"
            address = u["address"] or "Not created"
            text = (
                "🤖 <b>Solana Copy Trader</b>\n\n"
                f"Auto trade: <b>{active}</b>\n"
                f"Masters: <b>{len(ms)}/3</b>\n"
                f"Max trade: <b>${u['max_usd']:.2f}</b>\n"
                f"Slippage: <b>{Decimal(u['slip_bps'])/100}%</b>\n"
                f"Wallet: <code>{address}</code>\n\n"
                "Copies eligible master trades using your USDC balance."
            )
            rows = [
                [("⏸ Pause" if u["enabled"] else "▶️ Enable Auto Trade", "toggle")],
                [("👀 Masters", "masters"), ("⚙️ Settings", "settings")],
                [("👛 My Wallets", "wallet"), ("📜 History", "history")],
            ]
            if u["address"]:
                rows.append([("📋 Copy Address", "copy", u["address"])])
            rows.append([("🔄 Refresh", "home")])
            return text, keyboard(rows)

        if page == "wallet":
            ws = await self.wallets(uid)
            lines = ["👛 <b>My Wallets</b>", "Choose a wallet to manage."]
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
                lines.append("\nNo saved wallets.")
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
                balances = "Balances temporarily unavailable."
            is_active = w["address"] == u["address"]
            text = (
                f"👛 <b>{html.escape(w['name'])}</b>"
                f"{' — Active' if is_active else ''}\n\n"
                f"<code>{w['address']}</code>\n\n{balances}\n\n"
                "Private-key messages expire after one hour."
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
                    f"<b>{html.escape(m['name'])}</b> "
                    f"{'🟢' if m['active'] else '⏸'}\n"
                    f"<code>{m['address']}</code>"
                )
                rows.append([("📋 Copy Address", "copy", m["address"])])
                rows.append([
                    ("⏯ Pause/Resume", f"mastertoggle:{m['id']}"),
                    ("🗑 Remove", f"masterdelete:{m['id']}")
                ])
            if len(ms) < 3:
                rows.append([("➕ Add Master", "addmaster")])
            rows.append([("🏠 Home", "home")])
            return "\n".join(lines), keyboard(rows)

        if page == "settings":
            text = (
                "⚙️ <b>Trade Settings</b>\n\n"
                f"Max trade: ${u['max_usd']:.2f}\n"
                f"Slippage: {Decimal(u['slip_bps'])/100}%\n"
                f"Copy buys: {'ON' if u['copy_buys'] else 'OFF'}\n"
                f"Copy sells: {'ON' if u['copy_sells'] else 'OFF'}"
            )
            return text, keyboard([
                [("💵 Max Trade", "edit:max"),
                 ("📉 Slippage", "edit:slip")],
                [("🔁 Buys", "setting:buy"),
                 ("🔁 Sells", "setting:sell")],
                [("🏠 Home", "home")],
            ])

        if page == "history":
            trades = await self.pool.fetch("""
                SELECT * FROM copy_trades WHERE user_id=$1
                ORDER BY id DESC LIMIT 8
            """, uid)
            lines = ["📜 <b>Recent Trades</b>"]
            rows = []
            for t in trades:
                lines.append(
                    f"{t['side']} <code>{t['mint']}</code> "
                    f"${t['usd']:.2f} — {html.escape(t['status'])}"
                )
                if t["signature"]:
                    rows.append([
                        ("📋 Copy Signature", "copy", t["signature"])
                    ])
                    rows.append([
                        ("🔎 View TX", f"url:https://solscan.io/tx/{t['signature']}")
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
                    text, reply_markup=markup, parse_mode=ParseMode.HTML
                )
            except BadRequest as exc:
                if "not modified" not in str(exc).lower():
                    raise
        else:
            await update.message.reply_text(
                text, reply_markup=markup, parse_mode=ParseMode.HTML
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

        if data in ("home", "wallet", "masters", "settings", "history"):
            ctx.user_data.pop("pending", None)
            return await self.show(update, data)

        if data == "generate":
            try:
                wid = await self.save_wallet(uid, Keypair())
                return await self.show(update, f"walletview:{wid}", "Wallet generated.")
            except ValueError as exc:
                return await self.show(update, "wallet", str(exc))

        if data == "import":
            ctx.user_data["pending"] = ("import",)
            return await q.edit_message_text(
                "📥 Send your Solana private key in this private chat.\n"
                "The original message will be deleted where possible.\n"
                "Use /cancel to abort."
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
                "Active wallet changed. Auto trading paused."
            )

        if data.startswith("del:"):
            wid = int(data.split(":")[1])
            w = await self.wallet(uid, wid)
            if not w:
                return await self.show(update, "wallet")
            nonce = secrets.token_hex(6)
            ctx.user_data["delete"] = (wid, nonce)
            return await q.edit_message_text(
                "⚠️ <b>Delete Wallet?</b>\n\n"
                f"<code>{w['address']}</code>\n\n"
                "Back up your private key first. "
                "Deleting this wallet does not move its funds.",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard([
                    [("✅ Delete", f"delconfirm:{nonce}")],
                    [("❌ Keep", f"walletview:{wid}")],
                ])
            )

        if data.startswith("delconfirm:"):
            pending = ctx.user_data.pop("delete", None)
            if not pending or pending[1] != data.split(":")[1]:
                return await self.show(update, "wallet", "Confirmation expired.")
            wid = pending[0]
            w = await self.wallet(uid, wid)
            if not w:
                return await self.show(update, "wallet")
            async with self.lock:
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
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
            return await self.show(update, "wallet", "Wallet removed.")

        if data.startswith("send:"):
            _, wid, asset = data.split(":")
            ctx.user_data["pending"] = ("recipient", int(wid), asset)
            return await q.edit_message_text(
                f"📤 <b>Send {asset}</b>\n\n"
                "Paste the recipient's Solana wallet address.",
                parse_mode=ParseMode.HTML
            )

        if data.startswith("confirm_send:"):
            confirmation = ctx.user_data.pop("transfer", None)
            if not confirmation or confirmation["nonce"] != data.split(":")[1]:
                return await self.show(update, "wallet", "Confirmation expired.")
            if time.monotonic() > confirmation["expires"]:
                return await self.show(update, "wallet", "Confirmation expired.")
            await q.edit_message_text("📤 Submitting transfer...")
            try:
                signature = await self.send_funds(
                    uid, confirmation["wid"], confirmation["asset"],
                    confirmation["destination"], confirmation["amount"]
                )
                await q.edit_message_text(
                    "✅ <b>Transfer submitted</b>\n\n"
                    f"<code>{signature}</code>\n\n"
                    "Confirm the transaction on-chain before retrying.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard([
                        [("📋 Copy Signature", "copy", signature)],
                        [("🔎 View TX", f"url:https://solscan.io/tx/{signature}")],
                    ])
                )
            except Exception as exc:
                LOG.warning("Transfer failed: %s", type(exc).__name__)
                await q.edit_message_text(
                    "⚠️ Transfer was not confirmed.\n\n"
                    f"{html.escape(str(exc))[:300]}\n"
                    "Check your wallet before trying again."
                )
            return

        if data == "toggle":
            if not u["enabled"] and (
                not u["encrypted_secret"] or not await self.masters(uid)
            ):
                return await self.show(
                    update, "home",
                    "Create a wallet and add a master first."
                )
            await self.pool.execute(
                "UPDATE copy_users SET enabled=NOT enabled WHERE id=$1", uid
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
            prompt = (
                "Enter max USD per trade (0.50–1000)."
                if mode == "max" else
                "Enter slippage percentage (0.1–5)."
            )
            return await q.edit_message_text(prompt)

        if data == "addmaster":
            if len(await self.masters(uid)) >= 3:
                return await self.show(update, "masters", "Maximum 3 masters.")
            ctx.user_data["pending"] = "master"
            return await q.edit_message_text(
                "Paste the master wallet's public Solana address."
            )

        if data.startswith("mastertoggle:"):
            mid = int(data.split(":")[1])
            await self.pool.execute("""
                UPDATE copy_masters SET active=NOT active
                WHERE id=$1 AND user_id=$2
            """, mid, uid)
            return await self.show(update, "masters")

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
                return await self.show(
                    update, f"walletview:{wid}", "Wallet imported."
                )

            if isinstance(pending, tuple) and pending[0] == "recipient":
                _, wid, asset = pending
                if not valid_address(value):
                    raise ValueError("Invalid recipient address.")
                w = await self.wallet(uid, wid)
                if not w or w["address"] == value:
                    raise ValueError("Recipient must be a different wallet.")
                ctx.user_data["pending"] = ("amount", wid, asset, value)
                return await update.message.reply_text(
                    f"How much {asset} would you like to send?"
                )

            if isinstance(pending, tuple) and pending[0] == "amount":
                _, wid, asset, destination = pending
                amount = amount_number(value)
                decimals = 9 if asset == "SOL" else 6
                scaled = amount * (10**decimals)
                if amount <= 0 or scaled != scaled.to_integral_value():
                    raise ValueError("Invalid amount or too many decimal places.")
                nonce = secrets.token_hex(8)
                ctx.user_data.pop("pending", None)
                ctx.user_data["transfer"] = {
                    "nonce": nonce,
                    "wid": wid, "asset": asset,
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
                        [("✅ Confirm & Send", f"confirm_send:{nonce}")],
                        [("❌ Cancel", f"walletview:{wid}")],
                        [("📋 Copy Recipient", "copy", destination)],
                    ])
                )

            if pending == "master":
                if not valid_address(value):
                    raise ValueError("Invalid Solana wallet address.")
                if len(await self.masters(uid)) >= 3:
                    raise ValueError("Maximum 3 masters.")
                ctx.user_data["pending"] = ("nickname", value)
                return await update.message.reply_text(
                    "Enter a nickname for this master."
                )

            if isinstance(pending, tuple) and pending[0] == "nickname":
                await self.pool.execute("""
                    INSERT INTO copy_masters(user_id,address,name)
                    VALUES($1,$2,$3)
                """, uid, pending[1], value[:24] or "Trader")
                ctx.user_data.pop("pending", None)
                return await self.show(update, "masters", "Master added.")

            if pending in ("max", "slip"):
                n = amount_number(value)
                if pending == "max":
                    if not Decimal("0.5") <= n <= Decimal("1000"):
                        raise ValueError("Amount must be between $0.50 and $1,000.")
                    await self.pool.execute("""
                        UPDATE copy_users SET max_usd=$2 WHERE id=$1
                    """, uid, n.quantize(Decimal(".01")))
                else:
                    if not Decimal(".1") <= n <= Decimal("5"):
                        raise ValueError("Slippage must be between 0.1 and 5%.")
                    await self.pool.execute("""
                        UPDATE copy_users SET slip_bps=$2 WHERE id=$1
                    """, uid, int(n*100))
                ctx.user_data.pop("pending", None)
                return await self.show(update, "settings", "Saved.")
        except (ValueError, asyncpg.UniqueViolationError) as exc:
            await update.message.reply_text(f"❌ {html.escape(str(exc))}")

    async def cancel(self, update, ctx):
        ctx.user_data.clear()
        await self.show(update, "home", "Cancelled.")

    async def jupiter_order(self, source, destination, amount, wallet, slippage):
        headers = {"x-api-key": JUP_KEY} if JUP_KEY else {}
        params = {
            "inputMint": source, "outputMint": destination,
            "amount": str(amount), "taker": wallet,
            "slippageBps": str(slippage),
        }
        async with self.session.get(
            JUP+"/order", params=params, headers=headers
        ) as response:
            data = await response.json(content_type=None)
            if response.status != 200 or not data.get("transaction"):
                raise RuntimeError("Jupiter order unavailable.")
            return data

    async def jupiter_execute(self, order, key):
        tx = VersionedTransaction.from_bytes(
            base64.b64decode(order["transaction"])
        )
        required = tx.message.header.num_required_signatures
        signers = list(tx.message.account_keys[:required])
        if key.pubkey() not in signers:
            raise RuntimeError("Unexpected transaction signer.")
        signatures = list(tx.signatures)
        signatures[signers.index(key.pubkey())] = key.sign_message(
            bytes(tx.message)
        )
        signed = VersionedTransaction.populate(tx.message, signatures)
        headers = {"x-api-key": JUP_KEY} if JUP_KEY else {}
        async with self.session.post(
            JUP+"/execute",
            json={
                "signedTransaction": base64.b64encode(
                    bytes(signed)
                ).decode(),
                "requestId": order["requestId"],
            },
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=35),
        ) as response:
            data = await response.json(content_type=None)
            if response.status != 200:
                raise RuntimeError("Jupiter execution failed.")
            return data

    def classify_swap(self, tx, master):
        """Conservative balance-delta detection from standard Solana RPC getTransaction.

        Validates that an actual swap instruction ran, then compares the
        watched owner's SPL and native SOL balances, not the fee payer.
        The bot only executes USDC-funded copies, even when master spent SOL.
        """
        if not tx or not tx.get("meta") or tx["meta"].get("err"):
            return None, "Transaction failed or unavailable"
        meta = tx["meta"]
        logs = " ".join(meta.get("logMessages") or []).lower()
        # Avoid treating incoming gifts, outgoing transfers, and mints as swaps.
        if not any(word in logs for word in (
            "instruction: swap", "instruction: route", "instruction: buy",
            "instruction: sell", "instruction: sharedaccountsroute",
            "instruction: exactoutroute", "swapbasein", "swapbaseout",
        )):
            return None, "No recognizable swap instruction"

        def owner_amounts(entries):
            balances = {}
            for item in entries or []:
                if item.get("owner") != master:
                    continue
                mint = item.get("mint")
                if not mint:
                    continue
                raw = int((item.get("uiTokenAmount") or {}).get("amount", "0"))
                balances[mint] = balances.get(mint, 0) + raw
            return balances

        before = owner_amounts(meta.get("preTokenBalances"))
        after = owner_amounts(meta.get("postTokenBalances"))
        changes = {m: after.get(m, 0) - before.get(m, 0)
                   for m in before.keys() | after.keys()}
        pos = [(m, v) for m, v in changes.items() if v > 0 and m not in QUOTE_MINTS]
        neg = [(m, v) for m, v in changes.items() if v < 0 and m not in QUOTE_MINTS]
        spent_quote = any(changes.get(q, 0) < 0 for q in QUOTE_MINTS)
        gained_quote = any(changes.get(q, 0) > 0 for q in QUOTE_MINTS)

        # The owner can spend native SOL, including when the fee payer differs.
        keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys", [])
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
        if master in keys:
            i = keys.index(master)
            pre_sol = (meta.get("preBalances") or [])[i]
            post_sol = (meta.get("postBalances") or [])[i]
            fee = int(meta.get("fee") or 0) if keys and keys[0] == master else 0
            sol_delta = int(post_sol) - int(pre_sol) + fee
            if sol_delta < -1_000_000:
                spent_quote = True
            if sol_delta > 1_000_000:
                gained_quote = True

        if len(pos) == 1 and not neg and spent_quote:
            return ("BUY", pos[0][0]), ""
        if len(neg) == 1 and not pos and gained_quote:
            return ("SELL", neg[0][0]), ""
        if len(pos) == 1 and len(neg) == 1:
            return None, "Token-to-token swap requires explicit copying policy"
        return None, "Swap could not be classified unambiguously"

    async def notify(self, uid, message):
        try:
            await self.app.bot.send_message(
                uid, message, parse_mode=ParseMode.HTML
            )
        except (BadRequest, Forbidden):
            pass

    async def execute_copy(self, uid, master, signature, side, mint, observed):
        row = await self.pool.fetchrow("""
            INSERT INTO copy_trades
            (user_id,master_address,master_signature,side,mint,status)
            VALUES($1,$2,$3,$4,$5,'QUEUED')
            ON CONFLICT DO NOTHING RETURNING id
        """, uid, master, signature, side, mint)
        if not row:
            return
        tid = row["id"]
        try:
            u = await self.user(uid)
            if not u["enabled"] or time.time() - observed > MAX_SIGNAL_AGE:
                raise ValueError("Trading disabled or signal expired.")
            if side == "BUY" and not u["copy_buys"]:
                raise ValueError("Buy copying disabled.")
            if side == "SELL" and not u["copy_sells"]:
                raise ValueError("Sell copying disabled.")
            if not u["encrypted_secret"]:
                raise ValueError("No trading wallet.")
            key = Keypair.from_bytes(
                self.fernet.decrypt(u["encrypted_secret"].encode())
            )
            if side == "BUY":
                available, _ = await self.token_balance(u["address"])
                raw = min(
                    available, int(Decimal(u["max_usd"]) * 1_000_000)
                )
                if raw < 500_000:
                    raise ValueError("Insufficient USDC.")
                source, dest = USDC, mint
                usd = Decimal(raw)/1_000_000
            else:
                raw, _ = await self.token_balance(u["address"], mint)
                if not raw:
                    raise ValueError("No tokens to sell.")
                source, dest = mint, USDC
                usd = Decimal(0)
            await self.pool.execute("""
                UPDATE copy_trades SET status='SUBMITTING',usd=$2
                WHERE id=$1
            """, tid, usd)
            async with self.lock:
                order = await self.jupiter_order(
                    source, dest, raw, u["address"], u["slip_bps"]
                )
                result = await self.jupiter_execute(order, key)
            tx_sig = result.get("signature")
            if result.get("status") != "Success" or not tx_sig:
                raise RuntimeError("Swap outcome uncertain.")
            await self.pool.execute("""
                UPDATE copy_trades
                SET status='CONFIRMED',signature=$2 WHERE id=$1
            """, tid, tx_sig)
            await self.notify(
                uid, f"✅ Copied {side}\n"
                f"<a href='https://solscan.io/tx/{tx_sig}'>View transaction</a>"
            )
        except ValueError as exc:
            await self.pool.execute("""
                UPDATE copy_trades SET status='SKIPPED',message=$2
                WHERE id=$1
            """, tid, str(exc)[:250])
            await self.notify(
                uid, f"⚠️ Copy {side} skipped: {html.escape(str(exc))}"
            )
        except Exception as exc:
            await self.pool.execute("""
                UPDATE copy_trades SET status='UNKNOWN',message=$2
                WHERE id=$1
            """, tid, str(exc)[:250])
            LOG.exception("Copy transaction outcome uncertain")
            await self.notify(
                uid, "⚠️ Trade outcome uncertain. Check your wallet history."
            )

    async def poll_master(self, address):
        previous = await self.pool.fetchval(
            "SELECT signature FROM copy_cursors WHERE address=$1", address
        )
        entries = await self.rpc("getSignaturesForAddress", [
            address, {"limit": 50, "commitment": "confirmed"}
        ])
        if not entries:
            return
        newest = entries[0]["signature"]
        if previous is None:
            # First observation establishes a safe baseline: do not replay history.
            await self.pool.execute("""
                INSERT INTO copy_cursors(address,signature)
                VALUES($1,$2) ON CONFLICT DO NOTHING
            """, address, newest)
            return
        seen = any(x["signature"] == previous for x in entries)
        if not seen:
            LOG.warning("History gap for monitored wallet %s; automatic replay blocked", shortened(address))
            # Don't silently skip an unknown volume of trades; pause followers.
            await self.pool.execute("""
                UPDATE copy_users SET enabled=FALSE
                WHERE id IN (SELECT m.user_id FROM copy_masters m
                             WHERE m.address=$1 AND m.active)
            """, address)
            await self.alert_monitor(address, "History gap detected. Auto trading paused; review wallet activity.")
            await self.pool.execute("""
                UPDATE copy_cursors SET signature=$2 WHERE address=$1
            """, address, newest)
            return

        fresh = []
        for item in entries:
            if item["signature"] == previous:
                break
            if not item.get("err"):
                fresh.append(item)

        # Do not advance checkpoint when an API error occurs. On return the
        # oldest unprocessed signature remains eligible for the next poll.
        for item in reversed(fresh):
            signature = item["signature"]
            age = time.time() - (item.get("blockTime") or 0)
            if 0 <= age <= MAX_SIGNAL_AGE:
                tx = await self.rpc("getTransaction", [signature, {
                    "encoding": "jsonParsed", "commitment": "confirmed",
                    "maxSupportedTransactionVersion": 0
                }])
                if tx is None:
                    raise RuntimeError("RPC transaction details not ready")
                swap, reason = self.classify_swap(tx, address)
                if swap:
                    side, mint = swap
                    followers = await self.pool.fetch("""
                        SELECT m.user_id FROM copy_masters m
                        JOIN copy_users u ON u.id=m.user_id
                        WHERE m.address=$1 AND m.active AND u.enabled
                    """, address)
                    for follower in followers:
                        uid = follower["user_id"]
                        if uid in self.busy:
                            # Busy signals are not safely requeued by this baseline.
                            await self.notify(uid, "⚠️ New trade detected but another copy is running.")
                            continue
                        self.busy.add(uid)
                        try:
                            await self.execute_copy(
                                uid, address, signature, side, mint,
                                item.get("blockTime") or time.time()
                            )
                        finally:
                            self.busy.discard(uid)
                else:
                    LOG.info("Ignored transaction %s: %s", shortened(signature), reason)
            else:
                LOG.info("Stale transaction %s: not copied", shortened(signature))
            # Durable progress after successful parsing, or explicit skip.
            await self.pool.execute("""
                INSERT INTO copy_cursors(address,signature) VALUES($1,$2)
                ON CONFLICT(address) DO UPDATE SET signature=EXCLUDED.signature
            """, address, signature)

    async def alert_monitor(self, address, message):
        now = time.monotonic()
        if now - self.last_monitor_alert.get(address, 0) < 300:
            return
        self.last_monitor_alert[address] = now
        rows = await self.pool.fetch("""
            SELECT DISTINCT m.user_id FROM copy_masters m
            JOIN copy_users u ON u.id=m.user_id
            WHERE m.address=$1 AND m.active
        """, address)
        for row in rows:
            await self.notify(row["user_id"], "⚠️ Wallet monitoring: " + message)

    async def monitor(self):
        while True:
            try:
                masters = await self.pool.fetch("""
                    SELECT DISTINCT m.address FROM copy_masters m
                    JOIN copy_users u ON u.id=m.user_id
                    WHERE m.active AND u.enabled
                """)
                for master in masters:
                    try:
                        await self.poll_master(master["address"])
                    except Exception as exc:
                        LOG.error("Wallet monitoring error (%s)", type(exc).__name__)
                        await self.alert_monitor(
                            master["address"],
                            "Transaction processing failed. New trades may be missed; check monitoring."
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Monitoring error")
            await asyncio.sleep(4)

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
    app.add_handler(CommandHandler("cancel", service.cancel))
    app.add_handler(CallbackQueryHandler(service.click))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, service.text
    ))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
