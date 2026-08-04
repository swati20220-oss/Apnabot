import os
import re
import asyncio
import hashlib
import threading
from datetime import datetime
from flask import Flask
from google import genai
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ChatMemberHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from pymongo import MongoClient

# -------------------------------------------------------------
# 1. FLASK KEEP-ALIVE SERVER (Port 8099 / Render / Replit)
# -------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is alive and running 24/7!", 200

def run_flask_in_background():
    port = int(os.environ.get("PORT", 8099))
    flask_app.run(host="0.0.0.0", port=port)

# -------------------------------------------------------------
# 2. ENVIRONMENT VARIABLES & DATABASE INITIALIZATION
# -------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
WELCOME_LINK = os.getenv("WELCOME_LINK", "https://t.me")
INITIAL_ADMIN = os.getenv("ADMIN_ID", "0")

# Gemini AI Client Init
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# MongoDB Connection
client = MongoClient(MONGO_URI)
db = client['telegram_bot_db']

# Database Collections
users_col = db['users']
media_col = db['media_logs']
stats_col = db['stats']
config_col = db['bot_config']
badwords_col = db['badwords']
admin_perms_col = db['admin_permissions']

# Initial DB Config Setup for Initial Admin/Owner
if INITIAL_ADMIN != "0" and INITIAL_ADMIN.replace('-', '').isdigit():
    config_col.update_one({"_id": "owners"}, {"$addToSet": {"ids": int(INITIAL_ADMIN)}}, upsert=True)

# Regex Pattern for ALL URLs & Links
ALL_URL_REGEX = r'((https?://|www\.)[^\s]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/[^\s]*)?|t\.me/[^\s]+|telegram\.me/[^\s]+)'

# -------------------------------------------------------------
# 3. HELPER FUNCTIONS FOR DYNAMIC CONFIGURATION
# -------------------------------------------------------------
def get_db_ids(config_key):
    doc = config_col.find_one({"_id": config_key})
    return doc.get("ids", []) if doc else []

def add_db_id(config_key, group_id):
    config_col.update_one({"_id": config_key}, {"$addToSet": {"ids": group_id}}, upsert=True)

def del_db_id(config_key, group_id):
    config_col.update_one({"_id": config_key}, {"$pull": {"ids": group_id}})

def is_owner(user_id):
    owners = get_db_ids("owners")
    return user_id in owners

def get_custom_caption():
    doc = config_col.find_one({"_id": "branding_caption"})
    return doc.get("text", "") if doc else ""

def has_admin_permission(user_id, perm_name):
    if is_owner(user_id):
        return True
    perm_doc = admin_perms_col.find_one({"user_id": user_id})
    if perm_doc and perm_doc.get("permissions", {}).get(perm_name, False):
        return True
    return False

# -------------------------------------------------------------
# 4. WELCOME & USER REGISTRATION SYSTEM
# -------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {"user_id": user.id, "name": user.full_name, "joined_at": datetime.utcnow()}},
            upsert=True
        )
    await update.message.reply_text(
        f"Namaste {user.first_name}! Main aapka Dynamic Telegram Group Manager & Gemini AI Assistant Bot hoon.\n\n"
        f"Aap bot database mein successfully registered hain!"
    )

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if result.old_chat_member.status in ["left", "kicked"] and result.new_chat_member.status == "member":
        user = result.new_chat_member.user
        stats_col.update_one({"_id": "total_joins"}, {"$inc": {"count": 1}}, upsert=True)

        user_mention = f'<a href="tg://user?id={user.id}">{user.full_name}</a>'
        welcome_text = (
            f"Aapka swagat hai {user_mention}! 🎉\n\n"
            f"Group rules follow karein aur niche button par click karke bot ko DM mein START karein!"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(text="🔗 Official Link", url=WELCOME_LINK)],
            [InlineKeyboardButton(text="🤖 Bot Ko Start Karein", url=f"https://t.me/{context.bot.username}?start=welcome")]
        ])
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=welcome_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

# -------------------------------------------------------------
# 5. GEMINI AI AUTO-REPLY SYSTEM
# -------------------------------------------------------------
async def handle_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text or not ai_client:
        return

    bot_username = context.bot.username
    text = msg.text

    is_tagged = f"@{bot_username}" in text
    is_reply = (
        msg.reply_to_message 
        and msg.reply_to_message.from_user 
        and msg.reply_to_message.from_user.id == context.bot.id
    )

    if is_tagged or is_reply:
        prompt = text.replace(f"@{bot_username}", "").strip()
        if not prompt:
            await msg.reply_text("Haan ji, boliye! Main aapki kya help kar sakta hoon?")
            return

        await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")

        try:
            response = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={
                    "system_instruction": "Aap ek friendly, smart aur helpful Telegram Assistant hain. Concise aur clear Hinglish mein answer dein."
                }
            )
            if response.text:
                await msg.reply_text(response.text)
        except Exception as e:
            print(f"Gemini AI Error: {e}")
            await msg.reply_text("Kuch technical issue aa gaya, thodi der baad try karein!")

# -------------------------------------------------------------
# 6. ADVANCED ADMIN CONTROL COMMANDS
# -------------------------------------------------------------
async def promote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    if not has_admin_permission(user.id, "can_promote"):
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
        admin_perms_col.update_one(
            {"user_id": target_user_id},
            {"$set": {"is_admin": True, "promoted_at": datetime.utcnow()}},
            upsert=True
        )
        await msg.reply_text(f"✅ User `{target_user_id}` ko Admin bana diya gaya hai!", parse_mode="Markdown")
    except Exception as e:
        await msg.reply_text(f"❌ Promote Error: {e}")

async def demote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    if not is_owner(user.id):
        await msg.reply_text("⛔ Sirf Bot Owner hi kisi ko demote kar sakta hai!")
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
        admin_perms_col.delete_one({"user_id": target_user_id})
        await msg.reply_text(f"✅ User `{target_user_id}` ko Demote kar diya gaya hai!", parse_mode="Markdown")
    except Exception as e:
        await msg.reply_text(f"❌ Demote Error: {e}")

async def set_permission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await msg.reply_text("⛔ Sirf Owner permissions set kar sakta hai!")
        return

    if len(context.args) < 3:
        await update.message.reply_text("Usage: `/set_perm <user_id> <permission_name> true/false`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
        perm_name = context.args[1].lower()
        val = context.args[2].lower() == "true"

        admin_perms_col.update_one(
            {"user_id": target_id},
            {"$set": {f"permissions.{perm_name}": val}},
            upsert=True
        )
        await update.message.reply_text(f"✅ Permission `{perm_name}` = `{val}` set for `{target_id}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Error setting permission: {e}")

# -------------------------------------------------------------
# 7. DYNAMIC LIVE GROUP & OWNER CONFIGURATION
# -------------------------------------------------------------
async def list_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    
    badwords_doc = badwords_col.find_one({"_id": "word_list"})
    words = badwords_doc.get("words", []) if badwords_doc else []

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
    if not is_owner(update.effective_user.id):
        return

    cmd = update.message.text.split()[0].lower()
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
# 8. BADWORD FILTER & BRANDING CAPTION COMMANDS
# -------------------------------------------------------------
async def add_badword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/add_badword <word>`", parse_mode="Markdown")
        return
    
    word = context.args[0].lower().strip()
    badwords_col.update_one({"_id": "word_list"}, {"$addToSet": {"words": word}}, upsert=True)
    await update.message.reply_text(f"✅ Badword added: `{word}`", parse_mode="Markdown")

async def del_badword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/del_badword <word>`", parse_mode="Markdown")
        return
    
    word = context.args[0].lower().strip()
    badwords_col.update_one({"_id": "word_list"}, {"$pull": {"words": word}})
    await update.message.reply_text(f"✅ Badword removed: `{word}`", parse_mode="Markdown")

async def set_caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    caption_text = " ".join(context.args)
    if not caption_text:
        await update.message.reply_text("Usage: `/set_caption <Your Text/Links>`", parse_mode="Markdown")
        return

    config_col.update_one({"_id": "branding_caption"}, {"$set": {"text": caption_text}}, upsert=True)
    await update.message.reply_text(f"✅ Custom Caption Set:\n\n{caption_text}")

async def reset_caption_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    config_col.delete_one({"_id": "branding_caption"})
    await update.message.reply_text("✅ Custom Caption Reset To Default.")

# -------------------------------------------------------------
# 9. ALL LINKS & BADWORD ERASER + MULTI-LOG FORWARDING
# -------------------------------------------------------------
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat = update.effective_chat
    chat_id = chat.id
    chat_title = chat.title or "Unknown Group"
    user_id = msg.from_user.id

    # 1. Gemini AI Trigger Check
    bot_username = context.bot.username
    is_ai_trigger = (f"@{bot_username}" in msg.text) or (
        msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == context.bot.id
    )

    if is_ai_trigger:
        await handle_ai_chat(update, context)
        return

    # 2. Admin Status Check
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status in ["administrator", "creator"] or is_owner(user_id):
            return
    except Exception:
        pass

    # 3. Check Badwords & Links
    badwords_doc = badwords_col.find_one({"_id": "word_list"})
    badwords = badwords_doc.get("words", []) if badwords_doc else []
    
    has_badword = any(w in msg.text.lower() for w in badwords)
    has_link = bool(re.search(ALL_URL_REGEX, msg.text, re.IGNORECASE))

    if has_link or has_badword:
        reason = "Promotional Link" if has_link else "Blocked/Abusive Word"
        log_ids = get_db_ids("log_groups")
        
        for log_id in log_ids:
            try:
                # Forward original msg to Log Channel
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
# 10. MULTI-SOURCE MEDIA FETCHING & ANTI-DUPLICATE HASHING
# -------------------------------------------------------------
async def fetch_source_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    source_ids = get_db_ids("source_groups")

    if msg and msg.chat.id in source_ids and (msg.photo or msg.video):
        media_id = msg.photo[-1].file_id if msg.photo else msg.video.file_id
        media_type = "photo" if msg.photo else "video"
        
        # Anti-Duplicate Unique Hash
        unique_hash = hashlib.md5(f"{media_type}_{media_id[-20:]}".encode()).hexdigest()

        if media_col.find_one({"hash": unique_hash}):
            return

        media_col.update_one(
            {"hash": unique_hash},
            {"$set": {
                "media_id": media_id, 
                "type": media_type, 
                "hash": unique_hash, 
                "sent": False, 
                "added_at": datetime.utcnow()
            }},
            upsert=True
        )

async def auto_post_media_job(context: ContextTypes.DEFAULT_TYPE):
    target_ids = get_db_ids("target_groups")
    if not target_ids:
        return

    custom_caption = get_custom_caption()
    unsent_media = list(media_col.find({"sent": False}).limit(10))

    for media in unsent_media:
        try:
            for target_id in target_ids:
                try:
                    if media['type'] == 'photo':
                        await context.bot.send_photo(chat_id=target_id, photo=media['media_id'], caption=custom_caption)
                    elif media['type'] == 'video':
                        await context.bot.send_video(chat_id=target_id, video=media['media_id'], caption=custom_caption)
                    await asyncio.sleep(1)
                except Exception as group_err:
                    print(f"Target Error {target_id}: {group_err}")

            media_col.update_one({"_id": media["_id"]}, {"$set": {"sent": True}})
            await asyncio.sleep(3)
        except Exception as e:
            print(f"Cron Error: {e}")

# -------------------------------------------------------------
# 11. STATS, DASHBOARD & BROADCAST SYSTEM
# -------------------------------------------------------------
async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        return

    dm_users = users_col.count_documents({})
    joins_data = stats_col.find_one({"_id": "total_joins"}) or {"count": 0}
    media_pending = media_col.count_documents({"sent": False})

    text = (
        f"📊 **BOT SYSTEM DASHBOARD & STATS**\n\n"
        f"👑 **Owners Active:** `{len(get_db_ids('owners'))}`\n"
        f"👥 **Total Group Joins:** `{joins_data['count']}`\n"
        f"💬 **Registered DM Users:** `{dm_users}`\n"
        f"📢 **Log Groups:** `{len(get_db_ids('log_groups'))}`\n"
        f"📥 **Source Groups:** `{len(get_db_ids('source_groups'))}`\n"
        f"📤 **Target Groups:** `{len(get_db_ids('target_groups'))}`\n"
        f"🖼️ **Pending Media Jobs:** `{media_pending}`\n"
        f"🤖 **Gemini AI Status:** `{'Active ✅' if ai_client else 'Inactive ❌'}`\n"
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
    if not is_owner(update.effective_user.id):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: `/send_users Text`", parse_mode="Markdown")
        return

    users = users_col.find({})
    count = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u['user_id'], text=text)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast sent to {count} DM users.")

async def broadcast_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
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
# MAIN APPLICATION BOOTSTRAP
# -------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN missing!")
        return

    threading.Thread(target=run_flask_in_background, daemon=True).start()
    print("🌐 Background Flask Server Started (Port 8099)!")

    app = Application.builder().token(BOT_TOKEN).build()

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
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), handle_messages))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.PHOTO | filters.VIDEO), fetch_source_media))

    # Job Queue Scheduler (Every 5 mins)
    if app.job_queue:
        app.job_queue.run_repeating(auto_post_media_job, interval=300, first=10)

    print("🤖 Telegram Bot Polling Started!")
    app.run_polling(allowed_updates=["chat_member", "message", "callback_query"], stop_signals=None)

# -------------------------------------------------------------
# MAIN APPLICATION BOOTSTRAP (MENU BUTTON INTEGRATED)
# -------------------------------------------------------------
from telegram import BotCommand

async def post_init(application: Application):
    # Dynamic Menu Commands list
    commands = [
        BotCommand("start", "Bot ko start ya restart karein"),
        BotCommand("list_groups", "Saari active groups aur config list karein"),
        BotCommand("add_target", "Target Group add karein"),
        BotCommand("del_target", "Target Group remove karein"),
        BotCommand("add_source", "Source Group add karein"),
        BotCommand("del_source", "Source Group remove karein"),
        BotCommand("add_log", "Log Group add karein"),
        BotCommand("del_log", "Log Group remove karein"),
        BotCommand("add_owner", "Owner add karein"),
        BotCommand("del_owner", "Owner remove karein"),
        BotCommand("add_badword", "Word block karein"),
        BotCommand("del_badword", "Word unblock karein"),
        BotCommand("set_caption", "Custom Caption set karein"),
        BotCommand("reset_caption", "Caption reset karein"),
        BotCommand("stats", "Dashboard aur System Stats dekhein"),
        BotCommand("promote", "Member ko Admin banaayein"),
        BotCommand("demote", "Admin ko Demote karein"),
    ]
    await application.bot.set_my_commands(commands)
    print("✅ Bot Menu Commands successfully set for all users!")

def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN missing!")
        return

    threading.Thread(target=run_flask_in_background, daemon=True).start()
    print("🌐 Background Flask Server Started (Port 8099)!")

    # post_init link ho gaya hai
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
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), handle_messages))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.PHOTO | filters.VIDEO), fetch_source_media))

    # Job Queue Scheduler (Every 5 mins)
    if app.job_queue:
        app.job_queue.run_repeating(auto_post_media_job, interval=300, first=10)

    print("🤖 Telegram Bot Polling Started!")
    app.run_polling(allowed_updates=["chat_member", "message", "callback_query"], stop_signals=None)

if __name__ == '__main__':
    main()
