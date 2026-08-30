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
# 1. FLASK KEEP-ALIVE SERVER (Port 8099 / Render / Replit)
# -------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is alive and running 24/7 on Neon!", 200

def run_flask_in_background():
    port = int(os.environ.get("PORT", 8099))
    flask_app.run(host="0.0.0.0", port=port)

# -------------------------------------------------------------
# 2. ENVIRONMENT VARIABLES & DATABASE INITIALIZATION
# -------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")  # YAHAN NEON DB KA URL AAYEGA
WELCOME_LINK = os.getenv("WELCOME_LINK", "https://t.me")
INITIAL_ADMIN = os.getenv("ADMIN_ID", "0")

# Regex Pattern for ALL URLs & Links
ALL_URL_REGEX = r'((https?://|www\.)[^\s]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/[^\s]*)?|t\.me/[^\s]+|telegram\.me/[^\s]+)'

# -------------------------------------------------------------
# 3. HELPER FUNCTIONS FOR DYNAMIC CONFIGURATION & PERMISSIONS (NEON)
# -------------------------------------------------------------
def get_db_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute('''
                CREATE TABLE IF NOT EXISTS bot_config (config_type VARCHAR(50), group_id BIGINT, UNIQUE(config_type, group_id));
                CREATE TABLE IF NOT EXISTS badwords (word VARCHAR(255) PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS captions (id VARCHAR(50) PRIMARY KEY, text TEXT);
                CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, name TEXT, joined_at TIMESTAMP);
                CREATE TABLE IF NOT EXISTS stats (id VARCHAR(50) PRIMARY KEY, count BIGINT);
                CREATE TABLE IF NOT EXISTS admin_perms (user_id BIGINT PRIMARY KEY, permissions JSONB);
            ''')
            # Initial Admin Setup
            if INITIAL_ADMIN != "0" and INITIAL_ADMIN.replace('-', '').isdigit():
                cur.execute("INSERT INTO bot_config (config_type, group_id) VALUES ('owners', %s) ON CONFLICT DO NOTHING", (int(INITIAL_ADMIN),))

def get_db_ids(config_key):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT group_id FROM bot_config WHERE config_type = %s", (config_key,))
            return [row[0] for row in cur.fetchall()]

def add_db_id(config_key, group_id):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO bot_config (config_type, group_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (config_key, group_id))

def del_db_id(config_key, group_id):
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM bot_config WHERE config_type = %s AND group_id = %s", (config_key, group_id))

def is_owner(user_id):
    owners = get_db_ids("owners")
    return user_id in owners

def get_custom_caption():
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT text FROM captions WHERE id = 'branding_caption'")
            res = cur.fetchone()
            return res[0] if res else ""

def can_run_command(user_id: int, required_perm: str) -> bool:
    if is_owner(user_id):
        return True
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT permissions FROM admin_perms WHERE user_id = %s", (user_id,))
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
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (user_id, name, joined_at) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET name = EXCLUDED.name", 
                            (user.id, user.full_name, datetime.now(timezone.utc)))
    await update.message.reply_text(
        f"Namaste {user.first_name}! Main aapka Dynamic Telegram Group Manager Bot hoon.\n\n"
        f"Aap bot database mein successfully registered hain!"
    )

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.new_chat_members:
        return

    for new_member in msg.new_chat_members:
        if new_member.id == context.bot.id:
            continue

        with get_db_conn() as conn:
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
            await msg.reply_text(
                text=welcome_text,
                parse_mode="HTML",
                reply_markup=keyboard,
                disable_web_page_preview=True
            )
        except Exception as e:
            print(f"Welcome Message Error: {e}")

# -------------------------------------------------------------
# 5. ADVANCED ADMIN CONTROL COMMANDS
# -------------------------------------------------------------
async def promote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    if not can_run_command(user.id, "can_promote"):
        await msg.reply_text("⛔ Access Denied! Aapke paas `can_promote` permission nahi hai.")
        return

    if not msg.reply_to_message and not context.args:
        await msg.reply_text("Usage: Reply to message with `/promote` OR `/promote <user_id>`", parse_mode="Markdown")
        return

    target_user_id = msg.reply_to_message.from_user.id if msg.reply_to_message else int(context.args[0])
    try:
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=target_user_id,
            can_change_info=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_promote_members=False
        )
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO admin_perms (user_id, permissions) VALUES (%s, %s::jsonb)
                    ON CONFLICT (user_id) DO UPDATE SET permissions = EXCLUDED.permissions
                """, (target_user_id, json.dumps({"is_admin": True, "promoted_at": str(datetime.now(timezone.utc))})))
        await msg.reply_text(f"✅ User `{target_user_id}` ko Admin bana diya gaya hai!", parse_mode="Markdown")
    except Exception as e:
        await msg.reply_text(f"❌ Promote Error: {e}")

async def demote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    if not can_run_command(user.id, "can_demote"):
        await msg.reply_text("⛔ Access Denied! Aapke paas `can_demote` permission nahi hai.")
        return

    if not msg.reply_to_message and not context.args:
        await msg.reply_text("Usage: Reply with `/demote` OR `/demote <user_id>`", parse_mode="Markdown")
        return

    target_user_id = msg.reply_to_message.from_user.id if msg.reply_to_message else int(context.args[0])
    try:
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=target_user_id,
            can_change_info=False,
            can_delete_messages=False,
            can_invite_users=False,
            can_restrict_members=False,
            can_pin_messages=False,
            can_promote_members=False
        )
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM admin_perms WHERE user_id = %s", (target_user_id,))
        await msg.reply_text(f"✅ User `{target_user_id}` ko Demote kar diya gaya hai!", parse_mode="Markdown")
    except Exception as e:
        await msg.reply_text(f"❌ Demote Error: {e}")

async def set_permission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not is_owner(update.effective_user.id):
        await msg.reply_text("⛔ Sirf Owner permissions set kar sakta hai!")
        return

    if len(context.args) < 3:
        await msg.reply_text("Usage: `/set_perm <user_id> <permission_name> true/false`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
        perm_name = context.args[1].lower()
        val = context.args[2].lower() == "true"

        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO admin_perms (user_id, permissions) VALUES (%s, %s::jsonb)
                    ON CONFLICT (user_id) DO UPDATE SET permissions = admin_perms.permissions || %s::jsonb
                """, (target_id, json.dumps({perm_name: val}), json.dumps({perm_name: val})))
        await msg.reply_text(f"✅ Permission `{perm_name}` = `{val}` set for `{target_id}`", parse_mode="Markdown")
    except Exception as e:
        await msg.reply_text(f"❌ Error setting permission: {e}")

# -------------------------------------------------------------
# 6. DYNAMIC LIVE GROUP & OWNER CONFIGURATION
# -------------------------------------------------------------
async def list_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_config"):
        await update.message.reply_text("⛔ Access Denied!")
        return
    
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT word FROM badwords")
            words = [row[0] for row in cur.fetchall()]

    text = (
        f"📋 **DYNAMIC BOT CONFIGURATION**\n\n"
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

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid ID! Enter a numeric ID.")
        return

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
# 7. BADWORD FILTER & BRANDING CAPTION COMMANDS
# -------------------------------------------------------------
async def add_badword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_badwords"):
        await update.message.reply_text("⛔ Access Denied!")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/add_badword <word>`", parse_mode="Markdown")
        return
    
    word = context.args[0].lower().strip()
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO badwords (word) VALUES (%s) ON CONFLICT DO NOTHING", (word,))
    await update.message.reply_text(f"✅ Badword added: `{word}`", parse_mode="Markdown")

async def del_badword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_badwords"):
        await update.message.reply_text("⛔ Access Denied!")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/del_badword <word>`", parse_mode="Markdown")
        return
    
    word = context.args[0].lower().strip()
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM badwords WHERE word = %s", (word,))
    await update.message.reply_text(f"✅ Badword removed: `{word}`", parse_mode="Markdown")

async def set_caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_caption"):
        await update.message.reply_text("⛔ Access Denied!")
        return
    caption_text = " ".join(context.args)
    if not caption_text:
        await update.message.reply_text("Usage: `/set_caption <Your Text/Links>`", parse_mode="Markdown")
        return

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO captions (id, text) VALUES ('branding_caption', %s) ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text", (caption_text,))
    await update.message.reply_text(f"✅ Custom Caption Set:\n\n{caption_text}")

async def reset_caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_caption"):
        await update.message.reply_text("⛔ Access Denied!")
        return
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM captions WHERE id = 'branding_caption'")
    await update.message.reply_text("✅ Custom Caption Reset To Default.")

# -------------------------------------------------------------
# 8. ALL LINKS & BADWORD ERASER + MULTI-LOG FORWARDING
# -------------------------------------------------------------
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat = update.effective_chat
    chat_id = chat.id
    chat_title = chat.title or "Unknown Group"
    user_id = msg.from_user.id

    # Admin Status Check
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status in ["administrator", "creator"] or is_owner(user_id):
            return
    except Exception:
        pass

    with get_db_conn() as conn:
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
                await context.bot.forward_message(
                    chat_id=log_id,
                    from_chat_id=chat_id,
                    message_id=msg.message_id
                )
                log_info = (
                    f"⚠️ **Deleted Message Alert ({reason})**\n\n"
                    f"📢 **Group:** `{chat_title}`\n"
                    f"📍 **Group ID:** `{chat_id}`\n"
                    f"👤 **User:** {msg.from_user.full_name} (`{user_id}`)"
                )
                await context.bot.send_message(chat_id=log_id, text=log_info, parse_mode="Markdown")
            except Exception as e:
                print(f"Log Error ({log_id}): {e}")

        try:
            await msg.delete()
        except Exception as del_err:
            print(f"Delete Error: {del_err}")

# -------------------------------------------------------------
# 9. INSTANT MULTI-SOURCE MEDIA FORWARDING (NO 5-MIN DELAY)
# -------------------------------------------------------------
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
            except Exception as group_err:
                print(f"Target Error {target_id}: {group_err}")

# -------------------------------------------------------------
# 10. STATS, DASHBOARD & BROADCAST SYSTEM
# -------------------------------------------------------------
async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not can_run_command(user_id, "can_stats"):
        await update.message.reply_text("⛔ Access Denied!")
        return

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            dm_users = cur.fetchone()[0]
            cur.execute("SELECT count FROM stats WHERE id = 'total_joins'")
            joins_data = cur.fetchone()
            joins_count = joins_data[0] if joins_data else 0

    text = (
        f"📊 **BOT SYSTEM DASHBOARD & STATS**\n\n"
        f"👑 **Owners Active:** `{len(get_db_ids('owners'))}`\n"
        f"👥 **Total Group Joins:** `{joins_count}`\n"
        f"💬 **Registered DM Users:** `{dm_users}`\n"
        f"📢 **Log Groups:** `{len(get_db_ids('log_groups'))}`\n"
        f"📥 **Source Groups:** `{len(get_db_ids('source_groups'))}`\n"
        f"📤 **Target Groups:** `{len(get_db_ids('target_groups'))}`\n"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Broadcast Users (DM)", callback_data="bc_users")],
        [InlineKeyboardButton("📢 Broadcast Target Groups", callback_data="bc_group")]
    ])
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "bc_users":
        await query.message.reply_text("DM Broadcast: `/send_users Message`", parse_mode="Markdown")
    elif query.data == "bc_group":
        await query.message.reply_text("Group Broadcast: `/send_group Message`", parse_mode="Markdown")

async def broadcast_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_broadcast"):
        await update.message.reply_text("⛔ Access Denied!")
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: `/send_users Text`", parse_mode="Markdown")
        return

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            users = [row[0] for row in cur.fetchall()]
            
    count = 0
    for u_id in users:
        try:
            await context.bot.send_message(chat_id=u_id, text=text)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast sent to {count} DM users.")

async def broadcast_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_run_command(update.effective_user.id, "can_broadcast"):
        await update.message.reply_text("⛔ Access Denied!")
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: `/send_group Text`", parse_mode="Markdown")
        return

    targets = get_db_ids("target_groups")
    sent_count = 0
    for target_id in targets:
        try:
            await context.bot.send_message(chat_id=target_id, text=text)
            sent_count += 1
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Broadcast error {target_id}: {e}")

    await update.message.reply_text(f"✅ Broadcast sent to {sent_count}/{len(targets)} Target Groups!")

# -------------------------------------------------------------
# 11. MAIN BOOTSTRAP & COMMAND SCOPES
# -------------------------------------------------------------
async def post_init(application: Application):
    user_commands = [
        BotCommand("start", "Bot ko start karein"),
    ]
    await application.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    admin_commands = [
        BotCommand("start", "Bot ko start karein"),
        BotCommand("promote", "Group member ko Promote karein"),
        BotCommand("demote", "Admin ko Demote karein"),
        BotCommand("stats", "Dashboard aur Stats dekhein"),
    ]
    await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeAllChatAdministrators())

    print("✅ Command Scopes setup completed successfully!")

def main():
    if not BOT_TOKEN or not DATABASE_URL:
        print("Error: BOT_TOKEN or DATABASE_URL missing!")
        return

    # Initialize Neon Database Tables
    init_db()

    threading.Thread(target=run_flask_in_background, daemon=True).start()
    print("🌐 Background Flask Server Started (Port 8099)!")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # Admin Control Commands
    app.add_handler(CommandHandler("promote", promote_user))
    app.add_handler(CommandHandler("demote", demote_user))
    app.add_handler(CommandHandler("set_perm", set_permission))

    # Dynamic Management Commands
    app.add_handler(CommandHandler("list_groups", list_groups_command))
    for cmd in ["add_target", "del_target", "add_source", "del_source", "add_log", "del_log", "add_owner", "del_owner"]:
        app.add_handler(CommandHandler(cmd, manage_dynamic_config))

    # Branding & Filter Commands
    app.add_handler(CommandHandler("add_badword", add_badword))
    app.add_handler(CommandHandler("del_badword", del_badword))
    app.add_handler(CommandHandler("set_caption", set_caption_command))
    app.add_handler(CommandHandler("reset_caption", reset_caption_command))

    # General & Stats Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("stats", admin_dashboard))
    app.add_handler(CommandHandler("dashboard", admin_dashboard))
    app.add_handler(CommandHandler("send_users", broadcast_users))
    app.add_handler(CommandHandler("send_group", broadcast_group))
    app.add_handler(CallbackQueryHandler(button_click_handler))

    # Event Handlers
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), handle_messages))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.PHOTO | filters.VIDEO), fetch_source_media))

    print("🤖 Telegram Bot Polling Started!")
    app.run_polling(allowed_updates=["message", "callback_query"], stop_signals=None)

if __name__ == '__main__':
    main()
