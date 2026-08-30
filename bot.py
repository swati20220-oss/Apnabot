import os
import re
import asyncio
import threading
import json
from datetime import datetime, timezone
from flask import Flask
import psycopg2
from telegram import (
    Update, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    BotCommand, 
    BotCommandScopeDefault, 
    BotCommandScopeAllChatAdministrators
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -------------------------------------------------------------
# 1. FLASK KEEP-ALIVE SERVER
# -------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is alive and running on Neon DB 24/7!", 200

def run_flask_in_background():
    port = int(os.environ.get("PORT", 8099))
    flask_app.run(host="0.0.0.0", port=port)

# -------------------------------------------------------------
# 2. ENVIRONMENT VARIABLES
# -------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") # NEON DB URL YAHAN AAYEGA
WELCOME_LINK = os.getenv("WELCOME_LINK", "https://t.me")
INITIAL_ADMIN = os.getenv("ADMIN_ID", "0")

ALL_URL_REGEX = r'((https?://|www\.)[^\s]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/[^\s]*)?|t\.me/[^\s]+|telegram\.me/[^\s]+)'

# -------------------------------------------------------------
# 3. DATABASE HELPER FUNCTIONS (NEON POSTGRESQL)
# -------------------------------------------------------------
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS bot_config (config_type VARCHAR(50), group_id BIGINT, UNIQUE(config_type, group_id));
                CREATE TABLE IF NOT EXISTS badwords (word VARCHAR(255) PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS captions (id VARCHAR(50) PRIMARY KEY, text TEXT);
                CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, name TEXT, joined_at TIMESTAMP);
                CREATE TABLE IF NOT EXISTS stats (id VARCHAR(50) PRIMARY KEY, count BIGINT);
                CREATE TABLE IF NOT EXISTS admin_perms (user_id BIGINT PRIMARY KEY, perms JSONB);
            ''')
            if INITIAL_ADMIN != "0" and INITIAL_ADMIN.replace('-', '').isdigit():
                cur.execute("INSERT INTO bot_config (config_type, group_id) VALUES ('owners', %s) ON CONFLICT DO NOTHING", (int(INITIAL_ADMIN),))

def get_db_ids(config_type):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM bot_config WHERE config_type = %s", (config_type,))
            return [row[0] for row in cur.fetchall()]

def add_db_id(config_type, group_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO bot_config (config_type, group_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (config_type, group_id))

def del_db_id(config_type, group_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_config WHERE config_type = %s AND group_id = %s", (config_type, group_id))

def is_owner(user_id):
    return user_id in get_db_ids("owners")

def get_custom_caption():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT text FROM captions WHERE id = 'branding_caption'")
            res = cur.fetchone()
            return res[0] if res else ""

def can_run_command(user_id: int, required_perm: str) -> bool:
    if is_owner(user_id):
        return True
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT perms FROM admin_perms WHERE user_id = %s", (user_id,))
            res = cur.fetchone()
            if res and res[0] and res[0].get(required_perm, False):
                return True
    return False

# -------------------------------------------------------------
# 4. WELCOME & USER REGISTRATION SYSTEM 
# -------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (user_id, name, joined_at) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name", 
                            (user.id, user.full_name, datetime.now(timezone.utc)))
    await update.message.reply_text(
        f"Namaste {user.first_name}! Main aapka Dynamic Telegram Group Manager Bot hoon.\n\n"
        f"Aap bot database (Neon) mein successfully registered hain!"
    )

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.new_chat_members:
        return

    for new_member in msg.new_chat_members:
        if new_member.id == context.bot.id:
            continue

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO stats (id, count) VALUES ('total_joins', 1) ON CONFLICT (id) DO UPDATE SET count = stats.count + 1")

        user_mention = f'<a href="tg://user?id={new_member.id}">{new_member.full_name}</a>'
        welcome_text = (
            f"🎉 <b>Welcome to Our Telegram Group!</b> 🎉\n\n"
            f"👋 Hey {user_mention}, aapka hamare group me dil se swagat hai! 💙\n\n"
            f"✨ Ab aap hamari awesome GROUP ka hissa hain.\n"
            f"📢 Group explore karo, participate karo aur active raho.\n\n"
            f"📌 <b>Group Rules:</b>\n"
            f"✅ ONLY ACTIVE USER \n"
            f"🚫 No Spam\n"
            f"💙 DAILY SEND VIDEO OR PHOTO \n\n"
            f"🤝 NO ACTIVE OR NO VIDEO SEND , I REMOVE USER \n"
            f"🙏 Thanks for joining and enjoy your stay! 🎊"
        )
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(text="🔗 Official Link", url=WELCOME_LINK)],
            [InlineKeyboardButton(text="🤖 Bot Ko Start Karein", url=f"https://t.me/{context.bot.username}?start=welcome")]
        ])

        try:
            await msg.reply_text(text=welcome_text, parse_mode="HTML", reply_markup=keyboard, disable_web_page_preview=True)
        except Exception:
            pass

# -------------------------------------------------------------
# 5. ADMIN CONTROL & DYNAMIC CONFIGURATION
# -------------------------------------------------------------
async def set_permission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf Owner permissions set kar sakta hai!")
        return

    if len(context.args) < 3:
        await update.message.reply_text("Usage: `/set_perm <user_id> <permission_name> true/false`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
        perm_name = context.args[1].lower()
        val = context.args[2].lower() == "true"

        perm_data = json.dumps({perm_name: val})
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO admin_perms (user_id, perms) VALUES (%s, %s::jsonb)
                    ON CONFLICT (user_id) DO UPDATE SET perms = admin_perms.perms || %s::jsonb
                """, (target_id, perm_data, perm_data))
        
        await update.message.reply_text(f"✅ Permission `{perm_name}` = `{val}` set for `{target_id}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def list_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_config"):
        await update.message.reply_text("⛔ Access Denied!")
        return
    
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT word FROM badwords")
            words = [row[0] for row in cur.fetchall()]

    text = (
        f"📋 **DYNAMIC BOT CONFIGURATION (NEON)**\n\n"
        f"👑 **Owners:** `{get_db_ids('owners')}`\n"
        f"📥 **Source Groups:** `{get_db_ids('source_groups')}`\n"
        f"📤 **Target Groups:** `{get_db_ids('target_groups')}`\n"
        f"📢 **Log Groups:** `{get_db_ids('log_groups')}`\n"
        f"🚫 **Badwords List:** `{words}`\n"
        f"🏷️ **Branding Caption:** `{get_custom_caption() or 'Not Set'}`\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def manage_dynamic_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    cmd = update.message.text.split()[0].lower()

    if cmd in ["/add_owner", "/del_owner"] and not is_owner(user_id):
        await update.message.reply_text("⛔ Sirf Owner hi Owner add/remove kar sakta hai!")
        return

    if not can_run_command(user_id, "can_config"):
        await update.message.reply_text("⛔ Access Denied!")
        return

    if not context.args:
        await update.message.reply_text(f"Usage: `{cmd} <ID>`", parse_mode="Markdown")
        return

    target_id = int(context.args[0])
    mapping = {
        "/add_target": ("target_groups", True, "Target Group Added"),
        "/del_target": ("target_groups", False, "Target Group Removed"),
        "/add_source": ("source_groups", True, "Source Group Added"),
        "/del_source": ("source_groups", False, "Source Group Removed"),
        "/add_log": ("log_groups", True, "Log Group Added"),
        "/del_log": ("log_groups", False, "Log Group Removed"),
        "/add_owner": ("owners", True, "Owner Added"),
        "/del_owner": ("owners", False, "Owner Removed"),
    }

    if cmd in mapping:
        key, is_add, msg_text = mapping[cmd]
        if is_add:
            add_db_id(key, target_id)
        else:
            del_db_id(key, target_id)
        await update.message.reply_text(f"✅ {msg_text}: `{target_id}`", parse_mode="Markdown")

# -------------------------------------------------------------
# 6. BADWORD FILTER & BRANDING CAPTION COMMANDS
# -------------------------------------------------------------
async def add_badword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_badwords"):
        return
    word = context.args[0].lower().strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO badwords (word) VALUES (%s) ON CONFLICT DO NOTHING", (word,))
    await update.message.reply_text(f"✅ Badword added: `{word}`", parse_mode="Markdown")

async def del_badword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_badwords"):
        return
    word = context.args[0].lower().strip()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM badwords WHERE word = %s", (word,))
    await update.message.reply_text(f"✅ Badword removed: `{word}`", parse_mode="Markdown")

async def set_caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_caption"):
        return
    caption_text = " ".join(context.args)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO captions (id, text) VALUES ('branding_caption', %s) ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text", (caption_text,))
    await update.message.reply_text(f"✅ Custom Caption Set:\n\n{caption_text}")

# -------------------------------------------------------------
# 7. SPAM MODERATION & INSTANT MEDIA FORWARDING
# -------------------------------------------------------------
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat.id
    user_id = msg.from_user.id

    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status in ["administrator", "creator"] or is_owner(user_id):
            return
    except Exception:
        pass

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT word FROM badwords")
            badwords = [row[0] for row in cur.fetchall()]
    
    has_badword = any(w in msg.text.lower() for w in badwords)
    has_link = bool(re.search(ALL_URL_REGEX, msg.text, re.IGNORECASE))

    if has_link or has_badword:
        reason = "Promotional Link" if has_link else "Blocked/Abusive Word"
        log_ids = get_db_ids("log_groups")
        for log_id in log_ids:
            try:
                await context.bot.forward_message(chat_id=log_id, from_chat_id=chat_id, message_id=msg.message_id)
            except Exception:
                pass
        try:
            await msg.delete()
        except Exception:
            pass

async def fetch_source_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    source_ids = get_db_ids("source_groups")
    target_ids = get_db_ids("target_groups")

    if msg and msg.chat.id in source_ids and (msg.photo or msg.video):
        custom_caption = get_custom_caption()
        for target_id in target_ids:
            try:
                await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=msg.chat.id,
                    message_id=msg.message_id,
                    caption=custom_caption if custom_caption else msg.caption
                )
            except Exception as e:
                print(f"Target Error {target_id}: {e}")

# -------------------------------------------------------------
# 8. STATS & DASHBOARD
# -------------------------------------------------------------
async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_stats"):
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            dm_users = cur.fetchone()[0]
            cur.execute("SELECT count FROM stats WHERE id = 'total_joins'")
            joins_data = cur.fetchone()
            joins_count = joins_data[0] if joins_data else 0

    text = (
        f"📊 **NEON DB DASHBOARD**\n\n"
        f"👑 **Owners Active:** `{len(get_db_ids('owners'))}`\n"
        f"👥 **Total Group Joins:** `{joins_count}`\n"
        f"💬 **Registered DM Users:** `{dm_users}`\n"
        f"📢 **Log Groups:** `{len(get_db_ids('log_groups'))}`\n"
        f"📥 **Source Groups:** `{len(get_db_ids('source_groups'))}`\n"
        f"📤 **Target Groups:** `{len(get_db_ids('target_groups'))}`\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

# -------------------------------------------------------------
# 9. MAIN BOOTSTRAP
# -------------------------------------------------------------
async def post_init(application: Application):
    await application.bot.set_my_commands([BotCommand("start", "Bot ko start karein")], scope=BotCommandScopeDefault())
    print("✅ Command Scopes setup completed successfully!")

def main():
    if not BOT_TOKEN or not DATABASE_URL:
        print("Error: BOT_TOKEN or DATABASE_URL missing!")
        return

    init_db()  # Tables create karega Neon Database mein
    
    threading.Thread(target=run_flask_in_background, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("set_perm", set_permission))
    app.add_handler(CommandHandler("list_groups", list_groups_command))
    for cmd in ["add_target", "del_target", "add_source", "del_source", "add_log", "del_log", "add_owner", "del_owner"]:
        app.add_handler(CommandHandler(cmd, manage_dynamic_config))

    app.add_handler(CommandHandler("add_badword", add_badword))
    app.add_handler(CommandHandler("del_badword", del_badword))
    app.add_handler(CommandHandler("set_caption", set_caption_command))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stats", admin_dashboard))

    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), handle_messages))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.PHOTO | filters.VIDEO), fetch_source_media))

    print("🤖 Telegram Bot Polling Started with Neon PostgreSQL!")
    app.run_polling(allowed_updates=["message"])

if __name__ == '__main__':
    main()
