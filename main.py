"""
ZUDO OTP Support Bot
====================
Bot: @zudootpbot helper
- /addaccount  -> phone -> OTP -> 2FA (if any) -> session saved
- Logged-in userbot forwards every msg from @zudootpbot to @zudologs (-1003764994914)
  and deletes the original message after 3 seconds (only from source chat,
  NOT from the logs group).
- Userbot acts as "Zudo OTP Support Assistant" in groups:
    * Always-on : detects SELLING-only messages from non-admins, deletes
      them and tags the user with an English reason.
      (Buying messages are ALLOWED and never deleted.)
      (Warning message stays — NOT deleted)
    * /supportenable  -> userbot replies ONLY when:
          (a) someone REPLIES to the userbot's message, OR
          (b) someone TAGS the userbot via @username
      Replies are sent with a 4-second delay between each message.
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


API_KEYS = [os.getenv("GROQ_API_KEY")]

OPENROUTER_URL = "https://api.groq.com/openai/v1/chat/completions"
MODEL = "llama-3.1-8b-instant"
VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
MAX_IMAGES = 5

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
        "support_enabled": False,    # AI reply when tagged / replied-to
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

# IMPORTANT: Only SELLING is restricted. BUYING is fully allowed.
ILLEGAL_SYSTEM_PROMPT = (
    "You are a strict content moderator for a Telegram group called 'Zudo OTP Support'. "
    "ONLY the following are violations:\n"
    "  - SELLING something (offering accounts, OTPs, services, products for money/exchange)\n"
    "  - Advertising / promoting other services, channels, bots or websites\n"
    "  - Referral links, affiliate links, promo codes for OTHER services\n"
    "  - Scam / illegal content (carding, hacking-for-sale, stolen accounts, etc.)\n"
    "  - Sharing OTPs of other (non-Zudo) services for resale\n\n"
    "The following are NOT violations and must be allowed:\n"
    "  - BUYING / wanting to buy / asking 'how to buy' / 'I want to purchase' / 'need to buy' "
    "(buyers are customers, never punish them)\n"
    "  - Normal questions, greetings, support queries, complaints, doubts\n"
    "  - Talking about Zudo OTP service itself\n"
    "  - General chit-chat\n\n"
    "You must be CONFIDENT it is selling/promotion before flagging. "
    "If the message only expresses intent to BUY or asks about purchasing, set violation = false. "
    "When in doubt, set violation = false.\n\n"
    "Reply with ONLY a JSON object: "
    '{"violation": true/false, "reason": "short english reason"}. '
    "Do NOT add anything else outside the JSON."
)

SUPPORT_SYSTEM_PROMPT = (
    "You are 'Zudo OTP Support Assistant', a polite, helpful Telegram support agent for "
    "the Zudo OTP service. Reply briefly (1-3 sentences) in the same language the user wrote. "
    "Never promise refunds, never share private info, never reveal you are an AI. "
    "If the user asks something off-topic, gently redirect them to Zudo OTP support."
)

async def check_violation(text: str, image_b64: Optional[str] = None):
    """Return (is_violation: bool, reason: str). Only SELLING/promo is flagged."""
    if not text and not image_b64:
        return False, ""
    if image_b64:
        content = [
            {"type": "text", "text": f"Message text: {text or '(no text)'}\nAnalyze the image+text together. Remember: BUYING is allowed, only SELLING/promotion is a violation."},
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
        violation = bool(obj.get("violation"))
        reason = str(obj.get("reason", "")).strip()

        # Extra safety net: if the AI somehow flagged a clear buying intent, override to false
        if violation and text:
            low = text.lower()
            buy_signals = [
                "want to buy", "wanna buy", "i want buy", "i want to buy",
                "how to buy", "how can i buy", "where to buy", "kaise lu",
                "kaise lun", "kaise milega", "kharidna", "khareedna",
                "lena hai", "buy kar", "purchase kar", "need to buy",
                "i need to purchase", "i want to purchase",
            ]
            sell_signals = [
                "selling", "for sale", "sell ", "i sell", "i am selling",
                "dm to buy from me", "contact me to buy", "price list",
                "rate list", "available for sale", "bechna", "bech raha",
                "@", "t.me/", "https://", "http://",
            ]
            has_buy = any(b in low for b in buy_signals)
            has_sell = any(s in low for s in sell_signals)
            if has_buy and not has_sell:
                return False, ""

        return violation, reason
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
        "• /supportenable – AI replies ONLY when user replies to userbot OR tags @username (4s delay)\n"
        "• /supportdisable – turn off AI auto-reply\n"
        "• /lock sticker – delete stickers from non-admins\n"
        "• /unlock sticker – allow stickers again\n"
        "• /status – show current settings\n"
        "• /ping – check userbot is alive\n\n"
        "🛡 **Always-on protection:** SELLING / promo / illegal messages from "
        "non-admins are auto-deleted with a tagged English reason (warning stays). "
        "BUYING messages are allowed and never deleted.\n\n"
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
RUNNING: Dict[str, Dict[str, Any]] = {}   # {uid: {"client": ..., "name": ..., "username": ...}}
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

def _is_userbot_mentioned(ev, userbot_id: int, userbot_username: Optional[str]) -> bool:
    """
    Returns True if the message either:
      - is a reply to a message sent by the userbot, OR
      - contains @<userbot_username> in the text/entities
    """
    # 1) Reply to userbot
    try:
        if ev.is_reply:
            reply_msg = None
            # use cached reply if available
            try:
                reply_msg = ev.message.reply_to and ev.message.reply_to
            except Exception:
                reply_msg = None
            # we need the actual sender of the replied-to message
            # cheap check via reply_to_msg_id is not enough — fetch sender id
            # but to keep it lightweight, fall back to fetched message in handler.
    except Exception:
        pass

    # 2) Mention check via text
    if userbot_username:
        try:
            text = ev.raw_text or ""
            if re.search(r"(?<![A-Za-z0-9_])@" + re.escape(userbot_username) + r"(?![A-Za-z0-9_])", text, re.IGNORECASE):
                return True
        except Exception:
            pass
    return False

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

    userbot_id = int(uid)
    userbot_username = getattr(me, "username", None)

    RUNNING[uid] = {
        "client": client,
        "name": me.first_name,
        "username": userbot_username,
    }
    log.info(
        "✅ Userbot started: %s (%s) @%s",
        me.first_name, uid, userbot_username or "no-username",
    )

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
            "• /supportenable – AI replies ONLY when user replies to me or tags @username (4s delay)\n"
            "• /supportdisable – stop AI replies\n"
            "• /lock sticker – delete stickers from non-admins\n"
            "• /unlock sticker – allow stickers\n"
            "• /refreshadmins – refresh admin list for this chat\n\n"
            "🛡 SELLING / promo protection is **always on**. BUYING is allowed."
        )

    @client.on(events.NewMessage(pattern=r"^/ping$", outgoing=True))
    async def ub_ping(ev):
        await ev.edit("🏓 pong – userbot alive.")

    @client.on(events.NewMessage(pattern=r"^/status$", outgoing=True))
    async def ub_status(ev):
        s = get_settings(uid)
        uname = RUNNING.get(uid, {}).get("username") or "no-username"
        await ev.edit(
            "⚙️ **Status**\n"
            f"• Userbot                : @{uname}\n"
            f"• Support auto-reply : {'✅ ON (reply/tag only)' if s['support_enabled'] else '❌ OFF'}\n"
            f"• Sticker lock        : {'✅ ON' if s['lock_sticker'] else '❌ OFF'}\n"
            "• Selling filter         : ✅ ALWAYS ON (buying allowed)"
        )

    @client.on(events.NewMessage(pattern=r"^/supportenable$", outgoing=True))
    async def ub_sup_on(ev):
        update_setting(uid, "support_enabled", True)
        uname = RUNNING.get(uid, {}).get("username")
        tag_hint = f"@{uname}" if uname else "(set a username on this account)"
        await ev.edit(
            f"✅ Support auto-reply **enabled**.\n"
            f"I will reply ONLY when someone replies to my message or tags {tag_hint}. "
            f"(4s delay per reply)"
        )

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
    # 3) Group moderation (SELLING only) + reply-only AI support
    # ------------------------------------------------------------------
    @client.on(events.NewMessage(incoming=True))
    async def group_guard(ev):
        # Only act in groups / supergroups
        if not (ev.is_group or ev.is_channel):
            return
        # Never moderate the logs group itself
        if ev.chat_id == LOG_GROUP_ID:
            return
        sender = await ev.get_sender()
        if sender is None:
            return
        # Skip if sender is the userbot itself
        if getattr(sender, "id", None) == userbot_id:
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

        # ---- Violation detection (SELLING only, BUYING allowed) ----
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
                # Double-confirm before delete: must be CONFIRMED selling/promo, never buying.
                confirm_ok = True
                if text:
                    low = text.lower()
                    buy_signals = [
                        "want to buy", "wanna buy", "i want buy", "i want to buy",
                        "how to buy", "how can i buy", "where to buy", "kaise lu",
                        "kaise lun", "kaise milega", "kharidna", "khareedna",
                        "lena hai", "buy kar", "purchase kar", "need to buy",
                        "i need to purchase", "i want to purchase",
                    ]
                    if any(b in low for b in buy_signals):
                        confirm_ok = False  # buying → do NOT delete

                if not confirm_ok:
                    return  # buyer, leave message alone

                try:
                    await ev.delete()
                except Exception as e:
                    log.debug("delete violation failed: %s", e)
                mention = f"[{sender.first_name}](tg://user?id={sender.id})"
                reason = reason or "Selling / promotional content is not allowed in this group."
                try:
                    # Warning message — NOT auto-deleted (stays permanently)
                    await client.send_message(
                        ev.chat_id,
                        f"{mention} Why are you doing this? {reason} Please stop. (Only selling is restricted, buying is allowed.)",
                        parse_mode="md",
                    )
                except Exception as e:
                    log.debug("warn send failed: %s", e)
                return

        # ---- Support auto-reply ----
        # Only fires when:
        #   (a) message is a reply to userbot's own message, OR
        #   (b) message contains @<userbot_username>
        if not get_settings(uid)["support_enabled"]:
            return
        if not text:
            return

        # mention check
        mentioned = False
        if userbot_username:
            if re.search(
                r"(?<![A-Za-z0-9_])@" + re.escape(userbot_username) + r"(?![A-Za-z0-9_])",
                text, re.IGNORECASE,
            ):
                mentioned = True

        # reply-to-userbot check
        replied_to_userbot = False
        if ev.is_reply:
            try:
                rmsg = await ev.get_reply_message()
                if rmsg and getattr(rmsg, "sender_id", None) == userbot_id:
                    replied_to_userbot = True
            except Exception as e:
                log.debug("reply lookup failed: %s", e)

        if not (mentioned or replied_to_userbot):
            return  # silent, do not respond

        # Strip the @username from the text we send to the AI so it doesn't echo it
        clean_text = text
        if userbot_username:
            clean_text = re.sub(
                r"(?<![A-Za-z0-9_])@" + re.escape(userbot_username) + r"(?![A-Za-z0-9_])",
                "", clean_text, flags=re.IGNORECASE,
            ).strip()
        if not clean_text:
            clean_text = text  # fallback

        asyncio.create_task(_delayed_support_reply(ev, clean_text))

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
