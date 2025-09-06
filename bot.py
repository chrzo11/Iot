
import asyncio
import os
import re
import random
import string
from datetime import datetime, timezone
from dataclasses import dataclass

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = set(map(int, filter(None, (os.getenv("ADMIN_IDS") or "").split(","))))
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL")  # like @mychannel (public) or ID for private
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID" or "0"))

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN missing in .env")

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# --------------- Utilities ---------------
TICKET_LEN = 6
ALPHABET = string.ascii_uppercase + string.digits

FIRST_MESSAGE_DEFAULT = (
    "<b>🎟️ Welcome to Free Lottery Bot!</b>\n\n"
    "• You get a <b>free ticket</b> as a welcome reward (one per device).\n"
    "• Invite friends – after they verify & receive their welcome ticket, <b>you</b> get an extra ticket.\n"
    "• Next draw: {draw_time}.\n"
    "• Total prize this round: ₹{reward}.\n\n"
    "Join our channel: {channel}\n"
    "Then tap <b>Verify</b> below to complete device check & claim your ticket."
)

@dataclass
class Settings:
    draw_time: str
    reward_amount: int
    first_message: str


MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🎟️ My Tickets"), KeyboardButton(text="👥 Refer")],
        [KeyboardButton(text="👤 Profile"), KeyboardButton(text="📢 Join Channel")],
        [KeyboardButton(text="🏆 Earnings Leaderboard"), KeyboardButton(text="👑 Refer Leader")],
        [KeyboardButton(text="💳 Change UPI"), KeyboardButton(text="💸 Withdraw")],
        [KeyboardButton(text="❓ How to get extra tickets")],
    ],
    resize_keyboard=True,
)

ADMIN_MENU = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats"),
            InlineKeyboardButton(text="📣 Broadcast", callback_data="admin:broadcast"),
        ],
        [
            InlineKeyboardButton(text="✉️ DM User", callback_data="admin:dm"),
            InlineKeyboardButton(text="💰 Add Balance", callback_data="admin:addbal"),
            InlineKeyboardButton(text="💳 Remove Balance", callback_data="admin:rmbal"),
        ],
        [
            InlineKeyboardButton(text="➕ Add Ticket", callback_data="admin:addticket"),
            InlineKeyboardButton(text="🗑️ Remove All Tickets", callback_data="admin:cleartickets"),
        ],
        [
            InlineKeyboardButton(text="🔚 End Round (clear ALL)", callback_data="admin:endround"),
        ],
        [
            InlineKeyboardButton(text="🏅 Select Winner(s)", callback_data="admin:pick"),
        ],
        [
            InlineKeyboardButton(text="⏰ Change Draw Time", callback_data="admin:drawtime"),
            InlineKeyboardButton(text="🏦 Change Prize", callback_data="admin:prize"),
        ],
        [
            InlineKeyboardButton(text="📝 Change First Message", callback_data="admin:firstmsg"),
        ],
    ]
)

# --------------- FSM States ---------------
class CaptureUPI(StatesGroup):
    waiting_for_upi = State()

class AdminFSM(StatesGroup):
    broadcast_text = State()
    dm_user_id = State()
    dm_text = State()
    bal_user_id = State()
    bal_amount = State()
    rmbal_user_id = State()
    rmbal_amount = State()
    addticket_user_id = State()
    pick_count = State()
    pick_prize = State()
    draw_time = State()
    prize_amount = State()
    first_message = State()


# --------------- DB ---------------
DB_PATH = "lottery.db"

INIT_SQL = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  joined_at TEXT,
  ref_by INTEGER,
  upi TEXT UNIQUE,
  balance_cents INTEGER DEFAULT 0,
  total_won_cents INTEGER DEFAULT 0,
  last_win_round INTEGER,
  device_hash TEXT,
  welcomes_given INTEGER DEFAULT 0 -- 0 not given, 1 given
);

CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  code TEXT UNIQUE,
  round INTEGER,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS referrals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  referrer INTEGER,
  referee INTEGER,
  created_at TEXT,
  valid INTEGER DEFAULT 0 -- set to 1 when referee successfully gets welcome ticket
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS withdrawals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  amount_cents INTEGER,
  upi TEXT,
  created_at TEXT,
  status TEXT DEFAULT 'pending'
);
"""

async def db()
:
    return await aiosqlite.connect(DB_PATH)

async def ensure_db():
    async with aiosqlite.connect(DB_PATH) as con:
        await con.executescript(INIT_SQL)
        # default settings
        cur = await con.execute("SELECT value FROM settings WHERE key='draw_time'")
        row = await cur.fetchone()
        if not row:
            await con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('draw_time', ?)",
                              ("Every day at 8:00 PM",))
        cur = await con.execute("SELECT value FROM settings WHERE key='reward_amount'")
        row = await cur.fetchone()
        if not row:
            await con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('reward_amount', ?)",
                              (str(5000),))  # ₹5000 default
        cur = await con.execute("SELECT value FROM settings WHERE key='first_message'")
        row = await cur.fetchone()
        if not row:
            await con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('first_message', ?)",
                              (FIRST_MESSAGE_DEFAULT,))
        await con.commit()

async def get_settings() -> Settings:
    async with aiosqlite.connect(DB_PATH) as con:
        m = {}
        async with con.execute("SELECT key, value FROM settings") as cur:
            async for k, v in cur:
                m[k] = v
        return Settings(
            draw_time=m.get('draw_time', 'Every day at 8:00 PM'),
            reward_amount=int(m.get('reward_amount', '5000')),
            first_message=m.get('first_message', FIRST_MESSAGE_DEFAULT),
        )

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as con:
        await con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        await con.commit()

# --------------- Core helpers ---------------

def gen_ticket_code() -> str:
    return ''.join(random.choices(ALPHABET, k=TICKET_LEN))

async def unique_ticket(con) -> str:
    while True:
        code = gen_ticket_code()
        cur = await con.execute("SELECT 1 FROM tickets WHERE code=?", (code,))
        if not await cur.fetchone():
            return code

async def get_or_create_user(con, user_id: int, ref_by: int | None = None):
    cur = await con.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    if row:
        return
    await con.execute(
        "INSERT INTO users(user_id, joined_at, ref_by) VALUES(?,?,?)",
        (user_id, datetime.now(timezone.utc).isoformat(), ref_by),
    )

async def is_in_channel(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in {"member", "administrator", "creator"}
    except Exception:
        return False

# Stub: integrate your own device check here (e.g., external API), return True if device already used
async def same_device_detected(user_id: int) -> bool:
    # TODO: Replace with real device-fingerprint logic
    return False

async def award_welcome_ticket(con, user_id: int) -> bool:
    # returns True if ticket awarded
    cur = await con.execute("SELECT welcomes_given FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    if not row:
        return False
    if row[0] == 1:
        return False
    if await same_device_detected(user_id):
        # mark as considered to prevent repeated attempts
        await con.execute("UPDATE users SET welcomes_given=0 WHERE user_id=?", (user_id,))
        await con.commit()
        return False
    code = await unique_ticket(con)
    # Use current max round as round id; store 1 if empty
    cur2 = await con.execute("SELECT COALESCE(MAX(round), 0) FROM tickets")
    (max_round,) = await cur2.fetchone()
    current_round = max_round if max_round > 0 else 1
    await con.execute(
        "INSERT INTO tickets(user_id, code, round, created_at) VALUES(?,?,?,?)",
        (user_id, code, current_round, datetime.now(timezone.utc).isoformat()),
    )
    await con.execute("UPDATE users SET welcomes_given=1 WHERE user_id=?", (user_id,))
    await con.commit()
    return True

async def try_award_referral(con, referee_id: int):
    # When referee got welcome ticket, credit referrer if eligible
    cur = await con.execute("SELECT ref_by FROM users WHERE user_id=?", (referee_id,))
    row = await cur.fetchone()
    if not row or row[0] is None:
        return
    referrer = row[0]
    # device check across both accounts: if same device, abort (policy)
    if await same_device_detected(referee_id) or await same_device_detected(referrer):
        return
    # mark referral valid & award 1 ticket to referrer
    await con.execute(
        "UPDATE referrals SET valid=1 WHERE referee=? AND referrer=?",
        (referee_id, referrer),
    )
    code = await unique_ticket(con)
    cur2 = await con.execute("SELECT COALESCE(MAX(round), 0) FROM tickets")
    (max_round,) = await cur2.fetchone()
    current_round = max_round if max_round > 0 else 1
    await con.execute(
        "INSERT INTO tickets(user_id, code, round, created_at) VALUES(?,?,?,?)",
        (referrer, code, current_round, datetime.now(timezone.utc).isoformat()),
    )
    await con.commit()

# --------------- Handlers ---------------
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await ensure_db()
    args = (m.text or "").split(maxsplit=1)
    ref_by = None
    if len(args) == 2 and args[1].startswith("ref_"):
        try:
            ref_by = int(args[1][4:])
        except ValueError:
            ref_by = None
    async with aiosqlite.connect(DB_PATH) as con:
        await get_or_create_user(con, m.from_user.id, ref_by)
        if ref_by and ref_by != m.from_user.id:
            await con.execute(
                "INSERT INTO referrals(referrer, referee, created_at) VALUES(?,?,?)",
                (ref_by, m.from_user.id, datetime.now(timezone.utc).isoformat()),
            )
            await con.commit()
    settings = await get_settings()
    text = settings.first_message.format(
        draw_time=settings.draw_time, reward=settings.reward_amount, channel=REQUIRED_CHANNEL
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Verify & Claim", callback_data="verify")
    kb.button(text="📢 Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL.removeprefix('@')}")
    kb.adjust(1)
    await m.answer(text, reply_markup=kb.as_markup())


@dp.callback_query(F.data == "verify")
async def verify_join_and_device(cq: CallbackQuery):
    user_id = cq.from_user.id
    if not await is_in_channel(user_id):
        await cq.answer("Please join the channel first.", show_alert=True)
        return
    async with aiosqlite.connect(DB_PATH) as con:
        awarded = await award_welcome_ticket(con, user_id)
        if awarded:

await try_award_referral(con, user_id)
            await cq.message.answer("🎉 Welcome ticket granted!", reply_markup=MENU)
        else:
            await cq.message.answer(
                "No welcome ticket granted (already claimed or same device detected). You can still refer and earn.",
                reply_markup=MENU,
            )
    await cq.answer()


# -------- User menu commands --------
@dp.message(F.text == "🎟️ My Tickets")
async def my_tickets(m: Message):
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute(
            "SELECT code FROM tickets WHERE user_id=? ORDER BY id DESC", (m.from_user.id,)
        )
        rows = await cur.fetchall()
        settings = await get_settings()
    if not rows:
        await m.answer(
            f"You have no tickets yet. Next draw: {settings.draw_time}. Invite friends to earn tickets!"
        )
        return
    codes = "\n".join([f"<code>{r[0]}</code>" for r in rows])
    await m.answer(
        f"<b>Your Tickets</b> (total: {len(rows)})\nNext draw: {settings.draw_time}\n\n{codes}"
    )

@dp.message(F.text == "👥 Refer")
async def refer(m: Message):
    link = f"https://t.me/{(await bot.me()).username}?start=ref_{m.from_user.id}"
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute(
            "SELECT COUNT(1) FROM referrals WHERE referrer=? AND valid=1",
            (m.from_user.id,)
        )
        (valid_count,) = await cur.fetchone()
    await m.answer(
        "Share your link:\n"
        f"<code>{link}</code>\n\n"
        f"You get 1 ticket for each friend who verifies and receives their welcome ticket.\n"
        f"Total valid referrals: <b>{valid_count}</b>"
    )

@dp.message(F.text == "👤 Profile")
async def profile(m: Message):
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute("SELECT upi, balance_cents, total_won_cents, last_win_round FROM users WHERE user_id=?", (m.from_user.id,))
        upi, bal, won, last_round = await cur.fetchone() if (await cur.fetchone()) else (None,0,0,None)
    # The above fetchone was consumed; fix by requery
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute("SELECT upi, balance_cents, total_won_cents, last_win_round FROM users WHERE user_id=?", (m.from_user.id,))
        row = await cur.fetchone()
    upi = row[0] if row else None
    bal = row[1] if row else 0
    won = row[2] if row else 0
    last_round = row[3] if row else None

    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute("SELECT COUNT(1) FROM tickets WHERE user_id=?", (m.from_user.id,))
        (ticket_count,) = await cur.fetchone()
    await m.answer(
        "<b>Your Profile</b>\n"
        f"UPI: <code>{upi or 'Not set'}</code>\n"
        f"Balance: ₹{bal/100:.2f}\n"
        f"Total Won: ₹{won/100:.2f}\n"
        f"Last Round Won: {last_round or '-'}\n"
        f"Total Tickets: {ticket_count}"
    )

@dp.message(F.text == "📢 Join Channel")
async def join_channel(m: Message):
    await m.answer(
        f"Join our channel: {REQUIRED_CHANNEL}\nAfter joining, tap <b>Verify & Claim</b> in /start message.")

@dp.message(F.text == "🏆 Earnings Leaderboard")
async def earnings_leaderboard(m: Message):
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute(
            "SELECT user_id, total_won_cents FROM users WHERE total_won_cents>0 ORDER BY total_won_cents DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        await m.answer("No winners yet.")
        return
    lines = []
    for i, (uid, cents) in enumerate(rows, 1):
        lines.append(f"{i}. <a href='tg://user?id={uid}'>User {uid}</a> – ₹{cents/100:.2f}")
    await m.answer("<b>Top 10 Earnings</b>\n" + "\n".join(lines))

@dp.message(F.text == "👑 Refer Leader")
async def refer_leader(m: Message):
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute(
            "SELECT referrer, SUM(valid) as cnt FROM referrals GROUP BY referrer ORDER BY cnt DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        await m.answer("No referrals yet.")
        return
    lines = []
    for i, (uid, cnt) in enumerate(rows, 1):
        lines.append(f"{i}. <a href='tg://user?id={uid}'>User {uid}</a> – {cnt} valid")
    await m.answer("<b>Top Referrers</b>\n" + "\n".join(lines))

@dp.message(F.text == "💳 Change UPI")
async def change_upi(m: Message, state: FSMContext):
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute("SELECT upi FROM users WHERE user_id=?", (m.from_user.id,))
        (upi,) = await cur.fetchone() if (await cur.fetchone()) else (None,)
    # Fix double fetch
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute("SELECT upi FROM users WHERE user_id=?", (m.from_user.id,))
        row = await cur.fetchone()
    cur_upi = row[0] if row else None
    await m.answer(f"Your current UPI: <code>{cur_upi or 'Not set'}</code>\nSend the new UPI ID.")
    await state.set_state(CaptureUPI.waiting_for_upi)

@dp.message(CaptureUPI.waiting_for_upi)
async def capture_upi(m: Message, state: FSMContext):
    new_upi = (m.text or "").strip()
    # Basic validation
    if not re.match(r"^[\w._-]+@[\w.-]+$", new_upi):
        await m.answer("That doesn't look like a UPI ID. Try again (e.g., name@bank).")
        return
    async with aiosqlite.connect(DB_PATH) as con:
        # enforce uniqueness across all time: if ever linked, don't allow reuse
        cur = await con.execute("SELECT user_id FROM users WHERE upi=?", (new_upi,))
        row = await cur.fetchone()
        if row and row[0] != m.from_user.id:
            await m.answer("This UPI is already linked to another account. Choose a different one.")
            return
        await con.execute("UPDATE users SET upi=? WHERE user_id=?", (new_upi, m.from_user.id))
        await con.commit()
    await state.clear()
    await m.answer("✅ UPI updated.")

@dp.message(F.text == "💸 Withdraw")
async def withdraw(m: Message):
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute("SELECT upi, balance_cents FROM users WHERE user_id=?", (m.from_user.id,))
        row = await cur.fetchone()
    if not row:
        await m.answer("Profile not found.")
        return
    upi, cents = row
    if not upi:
        await m.answer("Link your UPI first via <b>Change UPI</b>.")
        return
    if cents < 100:
        await m.answer("Minimum balance for withdrawal is ₹1.00.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Confirm Withdraw", callback_data="wd:confirm")
    kb.button(text="❌ Cancel", callback_data="wd:cancel")
    kb.adjust(1)
    await m.answer(f"Withdraw full balance?\nUPI: <code>{upi}</code>\nAmount: ₹{cents/100:.2f}", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("wd:"))
async def wd_actions(cq: CallbackQuery):
    action = cq.data.split(":",1)[1]
    async with aiosqlite.connect(DB_PATH) as con:
        cur = await con.execute("SELECT upi, balance_cents FROM users WHERE user_id=
