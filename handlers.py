import asyncio
import html
import logging
import random
import secrets

from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
from config import (
    ADS_REQUIRED,
    BACKUP_CHANNEL,
    BASE_URL,
    CHANNEL_URL,
    LINK_BASE_URL,
    OWNER_ID,
    PRIMARY_CHANNEL,
    SUPPORT_URL,
    VERIFY_REQUIRED,
)
from delivery import send_file

log = logging.getLogger("handlers")
HTML = ParseMode.HTML

owner_only = filters.User(user_id=OWNER_ID) & filters.ChatType.PRIVATE

# Reactions the bot is allowed to use on messages
REACTIONS = ["🔥", "⚡", "🎉", "🤩", "💯", "🏆", "😎", "👀"]

_tasks: set = set()


def bg(coro):
    """Fire-and-forget so replies never wait on side effects."""
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def react(msg, emoji: str | None = None):
    try:
        await msg.set_reaction(emoji or random.choice(REACTIONS))
    except TelegramError:
        pass


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


async def edit(message, text: str, kb=None):
    try:
        await message.edit_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("edit failed: %s", e)
    except TelegramError as e:
        log.warning("edit failed: %s", e)


# ===================== SECONDARY (delivery) BOTS =====================
def home_text(user) -> str:
    name = html.escape((user.first_name if user else None) or "friend")
    return (
        f"🎉🔥 <b>Welcome, {name}!</b> 🔥🎉\n\n"
        "📦 Tap any <b>permanent file link</b> and I'll deliver your file right here ⚡️🚀\n"
        "💸 100% free • 🛡 Safe • 🏎 Super fast\n\n"
        "👇✨ Pick an option below ✨👇"
    )


HOW_TEXT = (
    "📖✨ <b>How it works</b> ✨📖\n\n"
    "1️⃣ Open a permanent link 🔗\n"
    "2️⃣ Tap <b>Verify & Get Video</b> 🔐\n"
    "3️⃣ Watch one short ad 📺\n"
    "4️⃣ Your file lands in this chat 🎁🚀💎"
)
FAQ_TEXT = (
    "❓🤔 <b>FAQ</b> 🤔❓\n\n"
    "💸 <b>Is it free?</b> Yes, totally! 🎉\n"
    "📺 <b>Why an ad?</b> It keeps this service running 🛠\n"
    "😕 <b>Ad not showing?</b> Turn off AdBlock / Private DNS 🛡 and retry 🔄\n"
    "⏳ <b>File didn't arrive?</b> Wait a few seconds, then tap your link again ⚡️"
)
HELP_TEXT = (
    "🆘🛟 <b>Need help?</b> 🛟🆘\n\n"
    "1️⃣ Open your file link again 🔗\n"
    "2️⃣ Tap <b>Verify & Get Video</b> and finish the ad 📺\n"
    "3️⃣ Still stuck? Reach out below 👇💬"
)
WHY_ALERT = "📺 One short ad keeps this service free for everyone!\n🎁 After it, your file drops straight into this chat ⚡"


def kb_home() -> InlineKeyboardMarkup:
    rows = [[btn("📖 How it works", "how"), btn("❓ FAQ", "faq")], [btn("🆘 Help", "help")]]
    extra = []
    if CHANNEL_URL:
        extra.append(InlineKeyboardButton("📢 Updates", url=CHANNEL_URL))
    if SUPPORT_URL:
        extra.append(InlineKeyboardButton("💬 Support", url=SUPPORT_URL))
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(rows)


def kb_back(*more: InlineKeyboardButton) -> InlineKeyboardMarkup:
    rows = []
    if more:
        rows.append(list(more))
    rows.append([btn("🏠 Home", "home")])
    return InlineKeyboardMarkup(rows)


def kb_help() -> InlineKeyboardMarkup:
    more = [InlineKeyboardButton("💬 Contact support", url=SUPPORT_URL)] if SUPPORT_URL else []
    return kb_back(*more)


async def deliver_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    bg(react(msg))

    if not context.args:
        await msg.reply_text(home_text(user), parse_mode=HTML, reply_markup=kb_home())
        return

    token = context.args[0]
    if chat.type != "private":
        link = f"https://t.me/{context.bot.username}?start={token}"
        await msg.reply_text(
            "🔒💬 Please open this link in a <b>private chat</b> with me 👇",
            parse_mode=HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Open private chat", url=link)]]),
        )
        return

    status = await msg.reply_text("🔎⚡ <b>Checking your link…</b>", parse_mode=HTML)

    doc = await db.get_file(token)
    if not doc:
        await edit(
            status,
            "❌😵 <b>Invalid or expired link</b>\n\nDouble-check the link and try again 🔗",
            kb_back(btn("🆘 Help", "help")),
        )
        return

    manager = context.application.bot_data["manager"]
    retry = InlineKeyboardButton("🔄 Try again", url=f"https://t.me/{context.bot.username}?start={token}")

    if not VERIFY_REQUIRED:
        await edit(status, "🎁🚀 <b>Sending your file…</b>")
        ok = await send_file(context.bot, chat.id, doc, manager)
        if ok:
            await edit(status, "✅🎉 <b>Delivered!</b> Enjoy 🍿🔥", kb_back())
        else:
            await edit(
                status,
                "😕💤 <b>File temporarily unavailable</b>\n\nPlease try again in a few seconds ⏳",
                kb_back(retry),
            )
        return

    sid = secrets.token_urlsafe(12)
    await db.create_session(
        sid=sid,
        token=token,
        user_id=user.id,
        chat_id=chat.id,
        bot_id=context.application.bot_data["bot_id"],
        required=ADS_REQUIRED,
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Verify & Get Video 🎬", web_app=WebAppInfo(url=f"{BASE_URL}/app?sid={sid}"))],
            [btn("❓ Why an ad?", "why"), btn("🆘 Help", "help")],
        ]
    )
    await edit(
        status,
        "🔐🎬 <b>Almost there!</b> 🎬🔐\n\n"
        "📺 Tap the button and watch one short ad ⚡️\n"
        "🎁 Your file drops right into this chat 🚀💎",
        kb,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data
    if data == "why":
        await q.answer(WHY_ALERT, show_alert=True)
        return
    await q.answer()  # instant feedback, no spinner

    screens = {
        "home": (home_text(q.from_user), kb_home()),
        "how": (HOW_TEXT, kb_back(btn("❓ FAQ", "faq"))),
        "faq": (FAQ_TEXT, kb_back(btn("📖 How it works", "how"))),
        "help": (HELP_TEXT, kb_help()),
    }
    screen = screens.get(data)
    if not screen:
        return
    text, kb = screen
    m = q.message
    try:
        if getattr(m, "text", None):  # text message → edit in place (smooth)
            await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
        else:  # e.g. buttons under a delivered video → send a fresh message
            await m.reply_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("callback edit failed: %s", e)
    except TelegramError as e:
        log.warning("callback failed: %s", e)


async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    bg(react(msg, "👀"))
    await msg.reply_text(
        "🤔💭 I only understand <b>file links</b>!\n\nTap a permanent link to get your file 🔗🎁",
        parse_mode=HTML,
        reply_markup=kb_home(),
    )


def register_delivery_handlers(app: Application):
    app.add_handler(CommandHandler("start", deliver_start))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^(home|how|faq|help|why)$"))
    app.add_handler(MessageHandler((filters.TEXT | filters.COMMAND) & filters.ChatType.PRIVATE, fallback_text))


# ===================== MAIN (manager) BOT — 100% buttons =====================
# Owner koi bhi /command type nahi karta. Sab kuch inline buttons se hota hai.
# Sirf 2 jagah text bhejna padta hai: bot token (Add Bot) aur file (Upload).
PAGE_SIZE = 5
STATUS_ICON = {"active": "🟢", "pending": "🟡", "dead": "🔴", "disabled": "⚪"}
AWAIT_KEY = "await"  # context.user_data[AWAIT_KEY] == "token" -> next text = bot token


def menu_text(stats: str = "") -> str:
    return (
        "🛠⚡ <b>Control Center</b> ⚡🛠\n\n"
        f"{stats}"
        "📤 Koi bhi file/video/photo bhejo → upload + backup + permanent link 🔗🔥\n\n"
        "👇 Neeche ke buttons se sab manage karo"
    )


def kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("🤖 My Bots", "m_bots"), btn("➕ Add Bot", "m_add")],
            [btn("📁 My Files", "f_list:0"), btn("📤 Upload", "m_up")],
            [btn("🩺 Health Check", "m_health"), btn("📊 Stats", "m_stats")],
        ]
    )


def kb_menu_only() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[btn("🏠 Menu", "m_home")]])


def kb_after(*more: InlineKeyboardButton) -> InlineKeyboardMarkup:
    rows = [list(more)] if more else []
    rows.append([btn("🤖 My Bots", "m_bots"), btn("🏠 Menu", "m_home")])
    return InlineKeyboardMarkup(rows)


async def stats_line() -> str:
    bots = await db.list_bots()
    active = sum(1 for b in bots if b["status"] == "active")
    n_files = await db.count_files()
    return f"🤖 Bots: <b>{active}</b> active / {len(bots)} total   •   📁 Files: <b>{n_files}</b>\n\n"


# ---------- bots ----------
async def bots_screen():
    items = await db.list_bots()
    rows = []
    if not items:
        text = "🤖💤 <b>Abhi koi bot nahi hai</b>\n\n➕ <b>Add Bot</b> dabao aur token bhejo."
    else:
        text = "🤖📋 <b>Your bots</b>\n\nKisi bot par tap karo → Enable / Disable / Remove 👇"
        for b in items:
            rows.append([btn(f"{STATUS_ICON.get(b['status'], '❔')} @{b['username']} — {b['status']}", f"b:{b['bot_id']}")])
    rows.append([btn("➕ Add Bot", "m_add"), btn("🔄 Refresh", "m_bots")])
    rows.append([btn("🏠 Menu", "m_home")])
    return text, InlineKeyboardMarkup(rows)


async def bot_screen(bot_id: str):
    b = await db.get_bot(bot_id)
    if not b:
        return "❌🔍 Ye bot registry me nahi mila.", kb_after()
    icon = STATUS_ICON.get(b["status"], "❔")
    text = f"🤖 <b>@{html.escape(b['username'])}</b>\n\nStatus: {icon} <b>{b['status']}</b>"
    row = []
    if b["status"] == "disabled":
        row.append(btn("🟢 Enable", f"be:{bot_id}"))
    elif b["status"] in ("active", "pending"):
        row.append(btn("⚪ Disable", f"bd:{bot_id}"))
    else:  # dead
        row.append(btn("🟢 Re-check", f"be:{bot_id}"))
    row.append(btn("🗑 Remove", f"br:{bot_id}"))
    return text, InlineKeyboardMarkup([row, [btn("⬅️ Bots", "m_bots"), btn("🏠 Menu", "m_home")]])


# ---------- files ----------
async def files_screen(page: int):
    total = await db.count_files()
    pages = max(1, -(-total // PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    items = await db.list_files(page * PAGE_SIZE, PAGE_SIZE)
    if not items:
        return "📁💤 <b>Abhi koi file nahi hai</b>\n\nKoi file/video bhejo, wo yahan aa jayegi.", kb_menu_only()
    rows = []
    for f in items:
        cap = (f.get("caption") or "").strip().replace("\n", " ")[:28] or "(no caption)"
        rows.append([btn(f"📄 {cap} • {f['token']}", f"f:{f['token']}")])
    nav = []
    if page > 0:
        nav.append(btn("⬅️ Prev", f"f_list:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("Next ➡️", f"f_list:{page + 1}"))
    rows.append(nav)
    rows.append([btn("🏠 Menu", "m_home")])
    return f"📁 <b>My Files</b> ({total})\n\nFile par tap karo → link / delete 👇", InlineKeyboardMarkup(rows)


async def file_screen(token: str):
    f = await db.get_file(token)
    if not f:
        return "❌🔍 File nahi mili (shayad delete ho chuki).", InlineKeyboardMarkup([[btn("📁 Files", "f_list:0")]])
    link = f"{LINK_BASE_URL}/f/{token}"
    cap = html.escape((f.get("caption") or "").strip()[:200]) or "<i>(no caption)</i>"
    text = f"📄 <b>File</b> <code>{token}</code>\n\n📝 {cap}\n\n🔗 <code>{html.escape(link)}</code>"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔗 Open", url=link), InlineKeyboardButton("📋 Copy", copy_text=CopyTextButton(text=link))],
            [btn("🗑 Delete", f"fd:{token}")],
            [btn("⬅️ Files", "f_list:0"), btn("🏠 Menu", "m_home")],
        ]
    )
    return text, kb


async def delete_file_everywhere(bot, token: str) -> bool:
    f = await db.get_file(token)
    if not f:
        return False
    for loc in f.get("locations", []):
        try:
            await bot.delete_message(loc["channel"], loc["msg"])
        except TelegramError as e:
            log.warning("channel msg delete failed %s: %s", loc, e)
    await db.delete_file(token)
    return True


# ---------- screens / navigation ----------
async def show(q, text: str, kb: InlineKeyboardMarkup):
    try:
        await q.edit_message_text(text, parse_mode=HTML, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("owner callback failed: %s", e)
    except TelegramError as e:
        log.warning("owner callback failed: %s", e)


async def send_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(AWAIT_KEY, None)
    await update.effective_message.reply_text(menu_text(await stats_line()), parse_mode=HTML, reply_markup=kb_menu())


async def owner_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bg(react(update.effective_message, "🔥"))
    await send_menu(update, context)


async def on_owner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("🚫 Owner only", show_alert=True)
        return
    data = q.data
    manager = context.application.bot_data["manager"]

    if data == "noop":
        await q.answer()
        return
    if data != "m_add":
        context.user_data.pop(AWAIT_KEY, None)  # koi bhi aur button = token input cancel

    if data == "m_home":
        await q.answer()
        await show(q, menu_text(await stats_line()), kb_menu())
    elif data == "m_stats":
        await q.answer()
        await show(q, "📊 <b>Stats</b>\n\n" + await stats_line(), kb_menu_only())
    elif data == "m_up":
        await q.answer()
        await show(
            q,
            "📤💾 <b>Upload a file</b>\n\n"
            "Bas koi bhi video, photo ya document yahan bhej do 🎬📸📄\n"
            "Main dono channels me save karke permanent link de dunga 🔗🔥",
            kb_menu_only(),
        )
    elif data == "m_add":
        await q.answer()
        context.user_data[AWAIT_KEY] = "token"
        await show(
            q,
            "➕🤖 <b>Add a bot</b>\n\n"
            "1️⃣ @BotFather me bot banao 🧪\n"
            "2️⃣ Uska <b>token yahan chat me bhej do</b> 🔑\n"
            "3️⃣ Bot ko dono channels me admin banao 👑\n\n"
            "🔒 Token message main turant delete kar dunga.\n\n"
            "👇 Ab token bhejo (ya Cancel dabao)",
            InlineKeyboardMarkup([[btn("❌ Cancel", "m_bots")]]),
        )
    elif data == "m_bots":
        await q.answer()
        await show(q, *await bots_screen())
    elif data == "m_health":
        await q.answer("🩺 Checking…")
        await manager.check_all()
        await manager.refresh()
        await show(q, *await bots_screen())
    elif data.startswith("b:"):
        await q.answer()
        await show(q, *await bot_screen(data[2:]))
    elif data.startswith("bd:"):  # disable
        b = await db.get_bot(data[3:])
        if b:
            await db.set_status(b["bot_id"], "disabled")
            await manager.release_bot(b)
            await manager.refresh()
        await q.answer("⚪ Disabled")
        await show(q, *await bot_screen(data[3:]))
    elif data.startswith("be:"):  # enable / re-check
        b = await db.get_bot(data[3:])
        if b:
            await db.set_status(b["bot_id"], "pending")
            await manager.check_all()
        await manager.refresh()
        await q.answer("🟢 Checked — thodi der me live", show_alert=True)
        await show(q, *await bot_screen(data[3:]))
    elif data.startswith("br:"):  # remove -> confirm
        b = await db.get_bot(data[3:])
        await q.answer()
        if not b:
            await show(q, *await bots_screen())
            return
        await show(
            q,
            f"🗑⚠️ <b>@{html.escape(b['username'])}</b> ko remove karna hai?\n\nYe registry se hat jayega.",
            InlineKeyboardMarkup(
                [[btn("✅ Haan, remove", f"brc:{b['bot_id']}"), btn("❌ Nahi", f"b:{b['bot_id']}")]]
            ),
        )
    elif data.startswith("brc:"):
        b = await db.get_bot(data[4:])
        if b:
            await manager.release_bot(b)
            await db.delete_bot(b["bot_id"])
            await manager.refresh()
        await q.answer("🗑 Removed")
        await show(q, *await bots_screen())
    elif data.startswith("f_list:"):
        await q.answer()
        await show(q, *await files_screen(int(data.split(":")[1])))
    elif data.startswith("f:"):
        await q.answer()
        await show(q, *await file_screen(data[2:]))
    elif data.startswith("fd:"):  # delete file -> confirm
        token = data[3:]
        await q.answer()
        await show(
            q,
            f"🗑⚠️ File <code>{token}</code> delete karni hai?\n\n"
            "Channels (primary + backup) se bhi hat jayegi aur link kaam karna band kar dega.",
            InlineKeyboardMarkup([[btn("✅ Haan, delete", f"fdc:{token}"), btn("❌ Nahi", f"f:{token}")]]),
        )
    elif data.startswith("fdc:"):
        await delete_file_everywhere(context.bot, data[4:])
        await q.answer("🗑 Deleted")
        await show(q, *await files_screen(0))
    else:
        await q.answer()


# ---------- token input (text) ----------
async def owner_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if context.user_data.get(AWAIT_KEY) == "token":
        context.user_data.pop(AWAIT_KEY, None)
        token = (msg.text or "").strip()
        try:
            await msg.delete()  # token chat me na rahe
        except TelegramError:
            pass
        status = await context.bot.send_message(msg.chat_id, "⏳🔑 <b>Checking token…</b>", parse_mode=HTML)
        manager = context.application.bot_data["manager"]
        result = await manager.add_bot(token)
        await edit(status, html.escape(result), kb_after(btn("➕ Add another", "m_add")))
        return
    bg(react(msg, "🤔"))
    await send_menu(update, context)


async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    context.user_data.pop(AWAIT_KEY, None)
    status = await msg.reply_text("⏳💾 <b>Uploading & backing up…</b>", parse_mode=HTML)

    token = secrets.token_urlsafe(6)
    original_caption = (msg.caption or "")[:900]
    channel_caption = f"{original_caption}\n\nID: {token}".strip()

    try:
        a = await context.bot.copy_message(
            chat_id=PRIMARY_CHANNEL,
            from_chat_id=msg.chat_id,
            message_id=msg.message_id,
            caption=channel_caption,
        )
    except TelegramError as e:
        bg(react(msg, "😢"))
        await edit(
            status,
            f"❌😢 <b>Primary channel upload failed</b>\n<code>{html.escape(str(e))}</code>\n\n👑 Make the bot an admin of the channel.",
            kb_after(),
        )
        return

    locations = [{"channel": PRIMARY_CHANNEL, "msg": a.message_id}]
    warn = ""
    try:
        b = await context.bot.copy_message(
            chat_id=BACKUP_CHANNEL,
            from_chat_id=PRIMARY_CHANNEL,
            message_id=a.message_id,
        )
        locations.append({"channel": BACKUP_CHANNEL, "msg": b.message_id})
    except TelegramError as e:
        warn = f"\n\n⚠️ Backup copy failed: <code>{html.escape(str(e))}</code>"

    await db.save_file(token, locations, original_caption)
    link = f"{LINK_BASE_URL}/f/{token}"
    bg(react(msg, "🔥"))
    await edit(
        status,
        f"✅🎉 <b>Saved & backed up!</b> 🔥💾\n\n🔗 <code>{html.escape(link)}</code>{warn}",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔗 Open", url=link), InlineKeyboardButton("📋 Copy", copy_text=CopyTextButton(text=link))],
                [btn("📄 Manage file", f"f:{token}"), btn("🏠 Menu", "m_home")],
            ]
        ),
    )


def register_main_handlers(app: Application):
    # /start Telegram ka built-in entry hai (pehli baar chat kholne par). Uske baad sab buttons.
    app.add_handler(CommandHandler("start", owner_start, filters=owner_only))
    app.add_handler(CallbackQueryHandler(on_owner_callback))
    app.add_handler(MessageHandler(owner_only & filters.ATTACHMENT & ~filters.COMMAND, upload))
    # koi bhi aur /command ya text -> bas menu dikha do
    app.add_handler(MessageHandler(owner_only & filters.TEXT & ~filters.COMMAND, owner_text))
    app.add_handler(MessageHandler(owner_only & filters.COMMAND, owner_start))
