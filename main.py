"""
ZUDO OTP Support Bot
====================
Bot: @zudootpbot helper
- /addaccount  -> phone -> OTP -> 2FA (if any) -> session saved
- Logged-in userbot forwards every msg from @zudootpbot to @zudologs (-1003764994914)
  and deletes the original message after 3 seconds (only from source chat,
  NOT from the logs group).
- Userbot acts as "Zudo OTP Support Assistant" in groups:
    * Always-on : detects illegal / selling messages from non-admins, deletes
      them and tags the user with an English reason. (Warning message stays — NOT deleted)
    * /supportenable  -> userbot replies to every non-admin message as support
      (replies are sent with a 4-second delay between each message)
    * /supportdisable -> turns that off
    * /lock sticker   -> deletes stickers from non-admins
    * /unlock sticker -> stops deleting stickers
    * /help           -> shows all userbot commands
"""

import asyncio
import json
import logging
import os
import re
import base64
from pathlib import Path
from typing import Dict, Any, Optional

import aiohttp
from telethon import TelegramClient, events, Button
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
)
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaPhoto,
    ChannelParticipantsAdmins,
    PeerChannel,
)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
BOT_TOKEN  = "8610551934:AAEBU2a8aVnXLfAjklNL9evARL2DhhjOE2k"
API_ID     = 33628258
API_HASH   = "0850762925b9c1715b9b122f7b753128"
OWNER_ID   = 7661825494

LOG_GROUP_ID       = -1003764994914           # @zudologs
ZUDO_OTP_BOT_USER  = "zudootpbot"             # source bot to mirror
DELETE_AFTER_SEC   = 3                        # delete original after forward
REPLY_DELAY_SEC    = 4                        # delay before each support reply

SESSION_FILE   = "otpzudosessions.json"        # userbot sessions
SETTINGS_FILE  = "otpzudo_settings.json"       # per-account settings

# Groq AI
API_KEYS = [
    "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",
]
OPENROUTER_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL        = "llama-3.1-8b-instant"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
MAX_IMAGES   = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("zudo")

# ---------------------------------------------------------------------------
# PERSISTENT STORAGE
# ---------------------------------------------------------------------------
def _load(path: str) -> Dict[str, Any]:
    if not Path(path).exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("Cannot load %s: %s", path, e)
        return {}

def _save(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

SESSIONS: Dict[str, str]              = _load(SESSION_FILE)         # {user_id: string_session}
SETTINGS: Dict[str, Dict[str, Any]]   = _load(SETTINGS_FILE)        # per-userbot settings

def default_settings() -> Dict[str, Any]:
    return {
        "support_enabled": False,    # AI reply to everyone
        "lock_sticker": False,       # delete stickers from non-admins
    }

def get_settings(uid: str) -> Dict[str, Any]:
    s = SETTINGS.get(uid)
    if not s:
        s = default_settings()
        SETTINGS[uid] = s
        _save(SETTINGS_FILE, SETTINGS)
    # backfill new keys
    changed = False
    for k, v in default_settings().items():
        if k not in s:
            s[k] = v
            changed = True
    if changed:
        _save(SETTINGS_FILE, SETTINGS)
    return s

def update_setting(uid: str, key: str, value: Any) -> None:
    s = get_settings(uid)
    s[key] = value
    SETTINGS[uid] = s
    _save(SETTINGS_FILE, SETTINGS)

# ---------------------------------------------------------------------------
# GROQ AI HELPERS
# ---------------------------------------------------------------------------
async def groq_chat(messages, vision: bool = False) -> Optional[str]:
    """Call Groq chat completion. Returns text or None."""
    headers = {
        "Authorization": f"Bearer {API_KEYS[0]}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": VISION_MODEL if vision else MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 400,
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(OPENROUTER_URL, headers=headers, json=payload, timeout=40) as r:
                if r.status != 200:
                    body = await r.text()
                    log.warning("Groq %s: %s", r.status, body[:300])
                    return None
                data = await r.json()
                return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("Groq error: %s", e)
        return None

ILLEGAL_SYSTEM_PROMPT = (
    "You are a strict content moderator for a Telegram group called 'Zudo OTP Support'. "
    "Decide if the user's message is selling/buying, promoting other services, "
    "advertising, sharing illegal/scam content, asking for OTPs of other services, "
    "spam, referral links, or any commercial/illegal activity. "
    "Reply with ONLY a JSON object: "
    '{"violation": true/false, "reason": "short english reason"}. '
    "If it is just a normal question, greeting, or support query, violation must be false. "
    "Do NOT add anything else outside the JSON."
)

SUPPORT_SYSTEM_PROMPT = (
    "You are 'Zudo OTP Support Assistant', a polite, helpful Telegram support agent for "
    "the Zudo OTP service. Reply briefly (1-3 sentences) in the same language the user wrote. "
    "Never promise refunds, never share private info, never reveal you are an AI. "
    "If the user asks something off-topic, gently redirect them to Zudo OTP support."
)

async def check_violation(text: str, image_b64: Optional[str] = None):
    """Return (is_violation: bool, reason: str)."""
    if not text and not image_b64:
        return False, ""
    if image_b64:
        content = [
            {"type": "text", "text": f"Message text: {text or '(no text)'}\nAnalyze the image+text together."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ]
        messages = [
            {"role": "system", "content": ILLEGAL_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]
        raw = await groq_chat(messages, vision=True)
    else:
        messages = [
            {"role": "system", "content": ILLEGAL_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        raw = await groq_chat(messages, vision=False)

    if not raw:
        return False, ""
    # Extract JSON
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return False, ""
    try:
        obj = json.loads(m.group(0))
        return bool(obj.get("violation")), str(obj.get("reason", "")).strip()
    except Exception:
        return False, ""

async def support_reply(text: str) -> Optional[str]:
    messages = [
        {"role": "system", "content": SUPPORT_SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ]
    return await groq_chat(messages, vision=False)

# ---------------------------------------------------------------------------
# CONTROL BOT  (login flow)
# ---------------------------------------------------------------------------
bot = TelegramClient("zudo_control_bot", API_ID, API_HASH)

# in-memory login state per owner
LOGIN_STATE: Dict[int, Dict[str, Any]] = {}

# Per-chat reply queue lock so replies in the same chat are spaced by REPLY_DELAY_SEC
REPLY_LOCKS: Dict[int, asyncio.Lock] = {}

def _get_reply_lock(chat_id: int) -> asyncio.Lock:
    lock = REPLY_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        REPLY_LOCKS[chat_id] = lock
    return lock

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_cmd(event):
    if event.sender_id != OWNER_ID:
        return
    await event.reply(
        "👋 **Zudo OTP Support Bot**\n\n"
        "Available commands:\n"
        "• /addaccount – login a new userbot\n"
        "• /listaccounts – show logged-in accounts\n"
        "• /removeaccount – logout an account\n"
        "• /help – userbot command list",
        parse_mode="md",
    )

@bot.on(events.NewMessage(pattern=r"^/help$"))
async def help_cmd(event):
    if event.sender_id != OWNER_ID:
        return
    await event.reply(
        "🤖 **Control Bot Commands**\n"
        "• /addaccount – add a new userbot via OTP\n"
        "• /listaccounts – list all logged-in accounts\n"
        "• /removeaccount – logout an account\n\n"
        "👤 **Userbot Commands (inside groups, by the logged-in account)**\n"
        "• /help – show this menu inside the group\n"
        "• /supportenable – AI replies to every non-admin message (4s delay per reply)\n"
        "• /supportdisable – turn off AI auto-reply\n"
        "• /lock sticker – delete stickers from non-admins\n"
        "• /unlock sticker – allow stickers again\n"
        "• /status – show current settings\n"
        "• /ping – check userbot is alive\n\n"
        "🛡 **Always-on protection:** illegal / selling / promo messages from "
        "non-admins are auto-deleted with a tagged English reason (warning stays), "
        "regardless of support mode.\n\n"
        f"📥 All messages from @{ZUDO_OTP_BOT_USER} are auto-forwarded to the logs "
        "group and the original is deleted after 3 seconds.",
        parse_mode="md",
    )

@bot.on(events.NewMessage(pattern=r"^/addaccount$"))
async def add_account(event):
    if event.sender_id != OWNER_ID:
        return
    LOGIN_STATE[event.sender_id] = {"step": "phone"}
    await event.reply(
        "📱 Send the **phone number** (with country code, e.g. `+919876543210`) "
        "of the account you want to add.\n\nSend /cancel to abort.",
        parse_mode="md",
    )

@bot.on(events.NewMessage(pattern=r"^/cancel$"))
async def cancel_cmd(event):
    if event.sender_id != OWNER_ID:
        return
    if event.sender_id in LOGIN_STATE:
        st = LOGIN_STATE.pop(event.sender_id)
        client = st.get("client")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        await event.reply("❌ Login cancelled.")
    else:
        await event.reply("Nothing to cancel.")

@bot.on(events.NewMessage(pattern=r"^/listaccounts$"))
async def list_accounts(event):
    if event.sender_id != OWNER_ID:
        return
    if not SESSIONS:
        return await event.reply("No accounts logged in yet.")
    lines = ["📒 **Logged-in accounts:**"]
    for uid in SESSIONS:
        info = RUNNING.get(uid, {})
        name = info.get("name", "unknown")
        lines.append(f"• `{uid}` – {name}")
    await event.reply("\n".join(lines), parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/removeaccount(?:\s+(\d+))?$"))
async def remove_account(event):
    if event.sender_id != OWNER_ID:
        return
    m = event.pattern_match
    target = m.group(1)
    if not target:
        return await event.reply("Usage: `/removeaccount <user_id>`", parse_mode="md")
    if target not in SESSIONS:
        return await event.reply("That account is not logged in.")
    # disconnect
    info = RUNNING.pop(target, None)
    if info and info.get("client"):
        try:
            await info["client"].disconnect()
        except Exception:
            pass
    SESSIONS.pop(target, None)
    _save(SESSION_FILE, SESSIONS)
    await event.reply(f"✅ Account `{target}` removed.", parse_mode="md")

# Catch all owner messages for login flow
@bot.on(events.NewMessage())
async def login_flow(event):
    if event.sender_id != OWNER_ID:
        return
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        return  # handled elsewhere
    state = LOGIN_STATE.get(event.sender_id)
    if not state:
        return

    step = state.get("step")

    # ---------------- PHONE ----------------
    if step == "phone":
        phone = text.replace(" ", "")
        if not re.match(r"^\+?\d{6,15}$", phone):
            return await event.reply("❌ Invalid phone format. Try again or /cancel.")
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except FloodWaitError as e:
            await event.reply(f"⏳ Telegram says wait {e.seconds}s before trying again.")
            LOGIN_STATE.pop(event.sender_id, None)
            return
        except Exception as e:
            await event.reply(f"❌ Failed to send code: `{e}`", parse_mode="md")
            LOGIN_STATE.pop(event.sender_id, None)
            return
        state.update({
            "step": "code",
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "client": client,
        })
        await event.reply(
            "🔐 OTP sent.\n"
            "Send the code with **spaces between digits** "
            "(e.g. `1 2 3 4 5`) to bypass Telegram's auto-revoke.\n\n"
            "Send /cancel to abort.",
            parse_mode="md",
        )
        return

    # ---------------- CODE ----------------
    if step == "code":
        code = re.sub(r"\D", "", text)
        client: TelegramClient = state["client"]
        try:
            await client.sign_in(
                phone=state["phone"],
                code=code,
                phone_code_hash=state["phone_code_hash"],
            )
        except SessionPasswordNeededError:
            state["step"] = "password"
            return await event.reply("🔑 2FA is enabled. Send the **password**:", parse_mode="md")
        except PhoneCodeInvalidError:
            return await event.reply("❌ Wrong OTP. Try again or /cancel.")
        except PhoneCodeExpiredError:
            await event.reply("❌ OTP expired. /addaccount again.")
            await client.disconnect()
            LOGIN_STATE.pop(event.sender_id, None)
            return
        except Exception as e:
            await event.reply(f"❌ Login failed: `{e}`", parse_mode="md")
            await client.disconnect()
            LOGIN_STATE.pop(event.sender_id, None)
            return
        await _finalize_login(event, state)
        return

    # ---------------- 2FA PASSWORD ----------------
    if step == "password":
        client: TelegramClient = state["client"]
        try:
            await client.sign_in(password=text)
        except Exception as e:
            return await event.reply(f"❌ Wrong password: `{e}`\nTry again or /cancel.", parse_mode="md")
        await _finalize_login(event, state)
        return

async def _finalize_login(event, state):
    client: TelegramClient = state["client"]
    me = await client.get_me()
    uid = str(me.id)
    string = client.session.save()
    SESSIONS[uid] = string
    _save(SESSION_FILE, SESSIONS)
    get_settings(uid)  # init defaults
    await event.reply(
        f"✅ Logged in as **{me.first_name}** (`{uid}`).\n"
        "Userbot is now running.\n\n"
        "Add this account to your group with admin rights so it can delete messages.",
        parse_mode="md",
    )
    LOGIN_STATE.pop(event.sender_id, None)
    # Start userbot handlers without disconnecting
    await start_userbot(uid, client, me)

# ---------------------------------------------------------------------------
# USERBOT  (per-account)
# ---------------------------------------------------------------------------
RUNNING: Dict[str, Dict[str, Any]] = {}   # {uid: {"client": ..., "name": ...}}
ADMIN_CACHE: Dict[int, set] = {}          # {chat_id: {admin_ids}}

async def is_admin(client: TelegramClient, chat_id: int, user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    cache = ADMIN_CACHE.get(chat_id)
    if cache is None:
        cache = set()
        try:
            async for p in client.iter_participants(chat_id, filter=ChannelParticipantsAdmins):
                cache.add(p.id)
        except Exception as e:
            log.debug("admin fetch failed for %s: %s", chat_id, e)
        ADMIN_CACHE[chat_id] = cache
    return user_id in cache

async def refresh_admins(client: TelegramClient, chat_id: int) -> None:
    ADMIN_CACHE.pop(chat_id, None)
    await is_admin(client, chat_id, 0)  # repopulate

async def start_userbot(uid: str, client: Optional[TelegramClient], me=None):
    """Attach all userbot event handlers and keep it running."""
    if uid in RUNNING:
        log.info("Userbot %s already running.", uid)
        return

    if client is None:
        session = SESSIONS.get(uid)
        if not session:
            return
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("Session %s no longer authorized. Removing.", uid)
            SESSIONS.pop(uid, None)
            _save(SESSION_FILE, SESSIONS)
            return
        me = await client.get_me()

    RUNNING[uid] = {"client": client, "name": me.first_name}
    log.info("✅ Userbot started: %s (%s)", me.first_name, uid)

    # ------------------------------------------------------------------
    # 1) Mirror @zudootpbot messages -> @zudologs and delete after 3s
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(from_users=ZUDO_OTP_BOT_USER, incoming=True))
    async def mirror_otp_bot(ev):
        try:
            await client.forward_messages(LOG_GROUP_ID, ev.message)
        except Exception as e:
            log.error("Forward to logs failed: %s", e)
        # delete original after delay, only from source chat
        async def _later():
            await asyncio.sleep(DELETE_AFTER_SEC)
            try:
                if ev.chat_id != LOG_GROUP_ID:
                    await ev.delete()
            except Exception as e:
                log.debug("delete original failed: %s", e)
        asyncio.create_task(_later())

    # ------------------------------------------------------------------
    # 2) Userbot commands (only owner-of-userbot can use)
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(pattern=r"^/help$", outgoing=True))
    async def ub_help(ev):
        await ev.edit(
            "🤖 **Zudo OTP Support – Userbot Commands**\n"
            "• /help – this menu\n"
            "• /status – current settings\n"
            "• /ping – check alive\n"
            "• /supportenable – AI replies to non-admins (4s delay per reply)\n"
            "• /supportdisable – stop AI replies\n"
            "• /lock sticker – delete stickers from non-admins\n"
            "• /unlock sticker – allow stickers\n"
            "• /refreshadmins – refresh admin list for this chat\n\n"
            "🛡 Illegal / selling message protection is **always on**."
        )

    @client.on(events.NewMessage(pattern=r"^/ping$", outgoing=True))
    async def ub_ping(ev):
        await ev.edit("🏓 pong – userbot alive.")

    @client.on(events.NewMessage(pattern=r"^/status$", outgoing=True))
    async def ub_status(ev):
        s = get_settings(uid)
        await ev.edit(
            "⚙️ **Status**\n"
            f"• Support auto-reply : {'✅ ON' if s['support_enabled'] else '❌ OFF'}\n"
            f"• Sticker lock        : {'✅ ON' if s['lock_sticker'] else '❌ OFF'}\n"
            "• Illegal-msg filter  : ✅ ALWAYS ON"
        )

    @client.on(events.NewMessage(pattern=r"^/supportenable$", outgoing=True))
    async def ub_sup_on(ev):
        update_setting(uid, "support_enabled", True)
        await ev.edit("✅ Support auto-reply **enabled** (4s delay per reply).")

    @client.on(events.NewMessage(pattern=r"^/supportdisable$", outgoing=True))
    async def ub_sup_off(ev):
        update_setting(uid, "support_enabled", False)
        await ev.edit("❌ Support auto-reply **disabled**.")

    @client.on(events.NewMessage(pattern=r"^/lock sticker$", outgoing=True))
    async def ub_lock_st(ev):
        update_setting(uid, "lock_sticker", True)
        await ev.edit("🔒 Stickers are now **locked** for non-admins.")

    @client.on(events.NewMessage(pattern=r"^/unlock sticker$", outgoing=True))
    async def ub_unlock_st(ev):
        update_setting(uid, "lock_sticker", False)
        await ev.edit("🔓 Stickers are now **unlocked**.")

    @client.on(events.NewMessage(pattern=r"^/refreshadmins$", outgoing=True))
    async def ub_refresh(ev):
        if ev.is_private:
            return await ev.edit("Use this inside a group.")
        await refresh_admins(client, ev.chat_id)
        await ev.edit("🔄 Admin list refreshed.")

    # ------------------------------------------------------------------
    # 3) Group moderation (illegal/selling) + optional AI support reply
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True))
    async def group_guard(ev):
        # Only act in groups / supergroups
        if not (ev.is_group or ev.is_channel):
            return
        # Never moderate the logs group itself
        if ev.chat_id == LOG_GROUP_ID:
            return
        # Ignore messages from the source bot (handled by mirror)
        sender = await ev.get_sender()
        if sender is None:
            return
        # Skip if sender is the userbot itself
        if getattr(sender, "id", None) == int(uid):
            return
        # Skip admins
        try:
            if await is_admin(client, ev.chat_id, sender.id):
                return
        except Exception:
            pass

        text = ev.raw_text or ""

        # ---- Sticker lock ----
        if get_settings(uid)["lock_sticker"] and ev.message.sticker:
            try:
                await ev.delete()
                mention = f"[{sender.first_name}](tg://user?id={sender.id})"
                # Warning message — NOT auto-deleted (stays permanently)
                await client.send_message(
                    ev.chat_id,
                    f"{mention} stickers are not allowed here. Please don't send stickers.",
                    parse_mode="md",
                )
            except Exception as e:
                log.debug("sticker delete failed: %s", e)
            return

        # ---- Violation detection ----
        image_b64 = None
        if isinstance(ev.message.media, MessageMediaPhoto):
            try:
                data = await ev.message.download_media(bytes)
                if data:
                    image_b64 = base64.b64encode(data).decode()
            except Exception:
                image_b64 = None

        if text or image_b64:
            try:
                violation, reason = await check_violation(text, image_b64)
            except Exception as e:
                log.error("violation check error: %s", e)
                violation, reason = False, ""
            if violation:
                try:
                    await ev.delete()
                except Exception as e:
                    log.debug("delete violation failed: %s", e)
                mention = f"[{sender.first_name}](tg://user?id={sender.id})"
                reason = reason or "Selling / promotional / illegal content is not allowed in this group."
                try:
                    # Warning message — NOT auto-deleted (stays permanently)
                    await client.send_message(
                        ev.chat_id,
                        f"{mention} Why are you doing this? {reason} Please stop.",
                        parse_mode="md",
                    )
                except Exception as e:
                    log.debug("warn send failed: %s", e)
                return

        # ---- Support auto-reply (if enabled) ----
        # Each reply in the same chat is queued and sent with a 4-second delay
        # so messages don't go out back-to-back.
        if get_settings(uid)["support_enabled"] and text:
            asyncio.create_task(_delayed_support_reply(ev, text))

    async def _delayed_support_reply(ev, text: str):
        """Queue per-chat so each reply waits REPLY_DELAY_SEC before sending."""
        lock = _get_reply_lock(ev.chat_id)
        async with lock:
            try:
                await asyncio.sleep(REPLY_DELAY_SEC)
                reply = await support_reply(text)
                if reply:
                    await ev.reply(reply)
            except Exception as e:
                log.debug("support reply failed: %s", e)

    # keep running
    asyncio.create_task(client.run_until_disconnected())


async def _auto_clean(msg, delay: int):
    """Kept for backward compatibility; warnings no longer use this."""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# BOOTSTRAP
# ---------------------------------------------------------------------------
async def main():
    await bot.start(bot_token=BOT_TOKEN)
    log.info("🤖 Control bot started.")

    # Resume all saved userbots
    for uid in list(SESSIONS.keys()):
        try:
            await start_userbot(uid, None)
        except Exception as e:
            log.error("Could not resume %s: %s", uid, e)

    log.info("✅ All userbots resumed. Awaiting events …")
    await bot.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down.")
