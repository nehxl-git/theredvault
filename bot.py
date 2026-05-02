import asyncio
import logging
import random
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
from redgifs.aio import API as RedGifsAPI
from telethon import TelegramClient, events, Button
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator

# ================= CONFIG =================

API_ID = 19274214
API_HASH = "bf87adfbc2c24c66904f3c36f3c0af3a"
BOT_TOKEN = "8666941152:AAGu644t5mCpvuiCFjZhwjVpdcRjDzjnIKs"

FORCE_CHANNEL = "theredvault"
BOT_USERNAME = "TheRedVBot"
OWNER_ID = 2104057670
CONTACT_BOT = "TheRedHelpBot"
DUMP_GROUP_ID = -1003778907156
FREE_DAILY_LIMIT = 5
REFERRAL_BONUS = 3
AUTO_DELETE_MINUTES = 15

DB_FILE = "botdata.db"

# ================= LOGGING =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("TalkMazeBot")

# ================= DATABASE =================

def db():
    return sqlite3.connect(DB_FILE)

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        username TEXT,
        joined_on TEXT,
        daily_used INTEGER DEFAULT 0,
        referred_by INTEGER DEFAULT 0,
        referral_count INTEGER DEFAULT 0,
        bonus_videos INTEGER DEFAULT 0,
        premium_until TEXT DEFAULT '',
        banned INTEGER DEFAULT 0
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sent_videos(
        user_id INTEGER PRIMARY KEY,
        message_id INTEGER
    )
    """)

    conn.commit()
    conn.close()

async def add_user(user):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))
    exists = cur.fetchone()

    if not exists:
        uname = user.username if user.username else ""
        cur.execute(
            "INSERT INTO users(user_id,name,username,joined_on) VALUES(?,?,?,?)",
            (user.id, user.first_name, uname, str(datetime.now()))
        )
        conn.commit()
        conn.close()
        await notify_new_user(user)
    else:
        conn.close()

def get_user(user_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    data = cur.fetchone()
    conn.close()
    return data

def update_query(query, params=()):
    conn = db()
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    conn.close()

def total_users():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    x = cur.fetchone()[0]
    conn.close()
    return x

# ================= PREMIUM / LIMIT =================

def is_premium(user_id):
    user = get_user(user_id)
    if not user:
        return False
    premium_until = user[8]
    if not premium_until:
        return False
    try:
        return datetime.now() < datetime.fromisoformat(premium_until)
    except:
        return False

def get_remaining_videos(user_id):
    user = get_user(user_id)
    if not user:
        return FREE_DAILY_LIMIT
    return FREE_DAILY_LIMIT + user[7] - user[4]

def consume_video(user_id):
    update_query("UPDATE users SET daily_used = daily_used + 1 WHERE user_id=?", (user_id,))

def add_bonus(user_id, amount):
    update_query("UPDATE users SET bonus_videos = bonus_videos + ? WHERE user_id=?", (amount,))

async def reset_daily_limits():
    while True:
        now = datetime.now()
        next_reset = datetime.combine(now.date() + timedelta(days=1), datetime.min.time())
        wait = (next_reset - now).total_seconds()
        await asyncio.sleep(wait)
        update_query("UPDATE users SET daily_used=0")

def await_sleep(seconds):
    return asyncio.sleep(seconds)

async def notify_new_user(user):
    try:
        uname = f"@{user.username}" if user.username else "No Username"
        msg = (
            "<b>📥 New User Joined Bot</b>\n\n"
            f"<b>Name:</b> {user.first_name}\n"
            f"<b>Username:</b> {uname}\n"
            f"<b>User ID:</b> <code>{user.id}</code>\n"
            f"<b>Total Users:</b> {total_users()}"
        )
        await client.send_message(DUMP_GROUP_ID, msg, parse_mode="html")
    except Exception as e:
        LOGGER.error(f"Dump notify failed: {e}")

# ================= FORCE JOIN =================

async def is_joined(client, user_id):
    try:
        await client(GetParticipantRequest(FORCE_CHANNEL, user_id))
        return True
    except:
        return False

async def force_join_message(event):
    await event.respond(
        "<b>🔒 Channel Membership Required</b>\n\n"
        "Please join our updates channel before using this bot.",
        parse_mode="html",
        buttons=[
            [Button.url("📢 Join Channel", f"https://t.me/{FORCE_CHANNEL}")],
            [Button.inline("✅ I Joined", b"checkjoin")]
        ]
    )

# ================= REDGIFS SERVICE =================

class RedGifsService:
    def __init__(self, default_tag="amateur", result_count=40):
        self.default_tag = default_tag
        self.result_count = result_count
        self.api: Optional[RedGifsAPI] = None
        self._lock = asyncio.Lock()

    async def start(self):
        async with self._lock:
            if self.api is not None:
                return
            self.api = RedGifsAPI()
            await self.api.login()
            LOGGER.info("Redgifs API connected")

    async def close(self):
        async with self._lock:
            if self.api:
                await self.api.close()
                self.api = None

    async def get_random_video(self, tag=None):
        query = (tag or self.default_tag).strip()

        selected = await self._pick_from_tag(query)
        if selected is None:
            selected = await self._pick_from_trending()

        video_url = selected.urls.hd or selected.urls.sd
        caption = f"<b>🎬 Your Video is Ready</b>\n<i>Enjoy premium content.</i>"
        return query, video_url, caption

    async def _pick_from_tag(self, query):
        creators = await self.api.search_creators(tags=[query])
        creator_items = creators.items or []

        if creator_items:
            creator = random.choice(creator_items)
            creator_result = await self.api.search_creator(creator.username, count=self.result_count)
            gifs = [gif for gif in creator_result.gifs if gif.urls.sd]
            if gifs:
                return random.choice(gifs)

        result = await self.api.search(query, count=self.result_count)
        gifs = [gif for gif in (result.gifs or []) if gif.urls.sd]
        if gifs:
            return random.choice(gifs)

        return None

    async def _pick_from_trending(self):
        top_week = await self.api.get_top_this_week(count=200)
        gifs = [gif for gif in (top_week.gifs or []) if gif.urls.sd]
        return random.choice(gifs)

async def download_video(url: str) -> Path:
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
                while True:
                    chunk = await response.content.read(1024 * 256)
                    if not chunk:
                        break
                    tmp.write(chunk)
                return Path(tmp.name)
            
    # ================= CLIENT =================

redgifs = RedGifsService()
client = TelegramClient("Bot", API_ID, API_HASH)

# ================= HELPERS =================

async def delete_after(chat_id, msg_id, delay=AUTO_DELETE_MINUTES * 60):
    await asyncio.sleep(delay)
    try:
        await client.delete_messages(chat_id, msg_id)
    except:
        pass

async def delete_previous_video(user_id, chat_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT message_id FROM sent_videos WHERE user_id=?", (user_id,))
    old = cur.fetchone()
    conn.close()

    if old:
        try:
            await client.delete_messages(chat_id, old[0])
        except:
            pass

def save_last_video(user_id, msg_id):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM sent_videos WHERE user_id=?", (user_id,))
    cur.execute("INSERT INTO sent_videos(user_id,message_id) VALUES(?,?)", (user_id, msg_id))
    conn.commit()
    conn.close()

# ================= START =================

@client.on(events.NewMessage(pattern=r"^/start"))
async def start_cmd(event):
    user = await event.get_sender()
    await add_user(user)

    args = event.raw_text.split()

    if len(args) > 1:
        ref = args[1]
        if ref.isdigit():
            ref = int(ref)
            if ref != user.id:
                u = get_user(user.id)
                if u and u[5] == 0:
                    update_query("UPDATE users SET referred_by=? WHERE user_id=?", (ref, user.id))
                    update_query("UPDATE users SET referral_count = referral_count + 1 WHERE user_id=?", (ref,))
                    add_bonus(ref, REFERRAL_BONUS)

    if not await is_joined(client, user.id):
        return await force_join_message(event)

    text = (
        "<b>🎬 Welcome to The Red Vault</b>\n"
        "<i>Your private daily premium video provider.</i>\n\n"
        "<u>Available Commands</u>\n"
        "• /send - Get random video\n"
        "• /send (genre) - Get specific video\n"
        "• /plans - View premium plans\n"
        "• /profile - Your usage profile\n"
        "• /contact - Purchase premium access"
    )
    await event.respond(text, parse_mode="html")

# ================= CHECK JOIN BUTTON =================

@client.on(events.CallbackQuery(data=b"checkjoin"))
async def check_join(event):
    if await is_joined(client, event.sender_id):
        await event.edit("<b>✅ Membership Verified.</b>\nNow use /start", parse_mode="html")
    else:
        await event.answer("You have not joined yet.", alert=True)

# ================= PLANS =================

@client.on(events.NewMessage(pattern=r"^/plans$"))
async def plans_cmd(event):
    if not await is_joined(client, event.sender_id):
        return await force_join_message(event)

    txt = (
        "<b>💎 Premium Subscription Plans</b>\n\n"
        "<u>Choose your access duration:</u>\n\n"
        "• <b>1 Day</b> — ₹20\n"
        "• <b>3 Days</b> — ₹30\n"
        "• <b>1 Week</b> — ₹50\n"
        "• <b>1 Month</b> — ₹99\n\n"
        "<i>Unlimited premium media access included.</i>\n\n"
        f"Purchase via @{CONTACT_BOT}"
    )
    await event.respond(txt, parse_mode="html")

@client.on(events.NewMessage(pattern=r"^/info (\d+)"))
async def info_cmd(event):
    if not is_admin(event.sender_id):
        return

    uid = int(event.pattern_match.group(1))
    user = get_user(uid)

    if not user:
        return await event.respond("<b>User not found.</b>", parse_mode="html")

    premium = user[8] if user[8] else "No Premium"
    banned = "Yes" if user[9] == 1 else "No"
    uname = f"@{user[2]}" if user[2] else "None"

    txt = (
        "<b>📌 User Information</b>\n\n"
        f"<b>Name:</b> {user[1]}\n"
        f"<b>Username:</b> {uname}\n"
        f"<b>User ID:</b> <code>{user[0]}</code>\n"
        f"<b>Joined On:</b> {user[3][:19]}\n"
        f"<b>Videos Used Today:</b> {user[4]}\n"
        f"<b>Referred By:</b> {user[5]}\n"
        f"<b>Referral Count:</b> {user[6]}\n"
        f"<b>Bonus Videos:</b> {user[7]}\n"
        f"<b>Premium Until:</b> {premium}\n"
        f"<b>Banned:</b> {banned}"
    )

    await event.respond(txt, parse_mode="html")

# ================= CONTACT =================

@client.on(events.NewMessage(pattern=r"^/contact$"))
async def contact_cmd(event):
    if not await is_joined(client, event.sender_id):
        return await force_join_message(event)

    await event.respond(
        f"<b>📞 Premium Support Contact</b>\n\n"
        f"For premium upgrades, payment or help contact:\n@{CONTACT_BOT}",
        parse_mode="html"
    )

# ================= PROFILE =================

@client.on(events.NewMessage(pattern=r"^/profile$"))
async def profile_cmd(event):
    if not await is_joined(client, event.sender_id):
        return await force_join_message(event)

    user = get_user(event.sender_id)
    rem = get_remaining_videos(event.sender_id)
    premium = "Active ✅" if is_premium(event.sender_id) else "Free User"

    txt = (
        "<b>👤 Your Profile</b>\n\n"
        f"<b>User ID:</b> <code>{event.sender_id}</code>\n"
        f"<b>Remaining Videos Today:</b> {rem}\n"
        f"<b>Referral Count:</b> {user[6]}\n"
        f"<b>Bonus Videos:</b> {user[7]}\n"
        f"<b>Status:</b> {premium}\n\n"
        f"<b>Your Referral Link:</b>\n"
        f"https://t.me/{BOT_USERNAME}?start={event.sender_id}"
    )
    await event.respond(txt, parse_mode="html")

# ================= SEND VIDEO =================

@client.on(events.NewMessage(pattern=r"^/send(?:\s+(.+))?$"))
async def send_cmd(event):
    user = await event.get_sender()
    await add_user(user)

    if not await is_joined(client, user.id):
        return await force_join_message(event)

    data = get_user(user.id)
    if data[9] == 1:
        return await event.respond("<b>🚫 You are banned from using this bot.</b>", parse_mode="html")

    if not is_premium(user.id):
        if get_remaining_videos(user.id) <= 0:
            return await event.respond(
                f"<b>❌ Daily Free Limit Reached</b>\n\n"
                f"You have used all {FREE_DAILY_LIMIT} free videos today.\n\n"
                f"• Contact @{CONTACT_BOT} to buy premium\n"
                f"• Or refer 1 member and earn +3 videos",
                parse_mode="html"
            )

    tag = event.pattern_match.group(1)
    status = await event.respond("<i>Fetching your premium media...</i>", parse_mode="html")
    temp_path = None

    try:
        query, video_url, caption = await redgifs.get_random_video(tag)
        temp_path = await download_video(video_url)

        await delete_previous_video(user.id, event.chat_id)

        msg = await client.send_file(
            event.chat_id,
            file=str(temp_path),
            caption=caption,
            parse_mode="html",
            supports_streaming=True
        )

        save_last_video(user.id, msg.id)
        asyncio.create_task(delete_after(event.chat_id, msg.id))

        if not is_premium(user.id):
            consume_video(user.id)

        await status.delete()

    except Exception as e:
        LOGGER.exception("Video send failed")
        await status.edit(f"<b>Failed:</b> {e}", parse_mode="html")
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink(missing_ok=True)

    # ================= ADMIN CHECK =================

def is_admin(user_id):
    return user_id == OWNER_ID

# ================= ADMIN PANEL =================

@client.on(events.NewMessage(pattern=r"^/admin$"))
async def admin_cmd(event):
    if not is_admin(event.sender_id):
        return

    txt = (
        "<b>🛠 Admin Control Panel</b>\n\n"
        "• /stats - bot statistics\n"
        "• /broadcast your_message\n"
        "• /ban user_id\n"
        "• /unban user_id\n"
        "• /prem user_id 1d/1w/1m"
    )
    await event.respond(txt, parse_mode="html")

# ================= STATS =================

@client.on(events.NewMessage(pattern=r"^/stats$"))
async def stats_cmd(event):
    if not is_admin(event.sender_id):
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE banned=1")
    banned = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM users WHERE premium_until != ''")
    premium = cur.fetchone()[0]
    conn.close()

    txt = (
        "<b>📊 Bot Statistics</b>\n\n"
        f"Total Users: {total_users()}\n"
        f"Premium Users: {premium}\n"
        f"Banned Users: {banned}"
    )
    await event.respond(txt, parse_mode="html")

# ================= BROADCAST =================

@client.on(events.NewMessage(pattern=r"^/broadcast (.+)"))
async def broadcast_cmd(event):
    if not is_admin(event.sender_id):
        return

    msg = event.pattern_match.group(1)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    conn.close()

    sent = 0
    for uid in users:
        try:
            await client.send_message(uid[0], msg, parse_mode="html")
            sent += 1
        except:
            pass

    await event.respond(f"<b>Broadcast sent to {sent} users.</b>", parse_mode="html")

# ================= BAN =================

@client.on(events.NewMessage(pattern=r"^/ban (\d+)"))
async def ban_cmd(event):
    if not is_admin(event.sender_id):
        return

    uid = int(event.pattern_match.group(1))
    update_query("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    await event.respond(f"<b>User {uid} banned.</b>", parse_mode="html")

@client.on(events.NewMessage(pattern=r"^/unban (\d+)"))
async def unban_cmd(event):
    if not is_admin(event.sender_id):
        return

    uid = int(event.pattern_match.group(1))
    update_query("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await event.respond(f"<b>User {uid} unbanned.</b>", parse_mode="html")

# ================= PREMIUM ADD =================

def parse_duration(x):
    if x.endswith("d"):
        return timedelta(days=int(x[:-1]))
    if x.endswith("w"):
        return timedelta(weeks=int(x[:-1]))
    if x.endswith("m"):
        return timedelta(days=30 * int(x[:-1]))
    return timedelta(days=0)

@client.on(events.NewMessage(pattern=r"^/prem (\d+) (\S+)"))
async def prem_cmd(event):
    if not is_admin(event.sender_id):
        return

    uid = int(event.pattern_match.group(1))
    dur = event.pattern_match.group(2)

    td = parse_duration(dur)
    expiry = datetime.now() + td

    update_query("UPDATE users SET premium_until=? WHERE user_id=?", (expiry.isoformat(), uid))

    await event.respond(
        f"<b>Premium activated for {uid}</b>\nExpires: {expiry.strftime('%d-%m-%Y %H:%M')}",
        parse_mode="html"
    )

# ================= MAIN STARTUP =================

async def main():
    init_db()
    await redgifs.start()
    asyncio.create_task(reset_daily_limits())

    await client.start(bot_token=BOT_TOKEN)
    LOGGER.info("TheRedVault Started Successfully")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
