import os
import re
import asyncio
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
# FLASK WEB SERVER (Keep-Alive Thread)
# -------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is active 24/7 with Multi-Owner & Admin Management System!", 200

def run_flask_in_background():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

# -------------------------------------------------------------
# ENVIRONMENT VARIABLES & DB SETUP
# -------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
WELCOME_LINK = os.getenv("WELCOME_LINK", "https://t.me")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# MongoDB Connection
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000) if MONGO_URI else None
    db = client['telegram_bot_db'] if client else None
    users_col = db['users'] if db is not None else None
    media_col = db['media_logs'] if db is not None else None
    stats_col = db['stats'] if db is not None else None
    admins_col = db['bot_admins'] if db is not None else None
except Exception as e:
    print(f"MongoDB Connection Error: {e}")

ALL_URL_REGEX = r'((https?://|www\.)[^\s]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/[^\s]*)?|t\.me/[^\s]+|telegram\.me/[^\s]+)'

# Default permissions for non-owner bot admins
DEFAULT_PERMISSIONS = {
    "can_broadcast_groups": False,
    "can_broadcast_dm": False,
    "can_promote_users": True,
    "can_add_users": True,
    "can_view_stats": True
}

# -------------------------------------------------------------
# MULTI-ID PARSING HELPER FUNCTIONS
# -------------------------------------------------------------
def get_owner_ids():
    raw = os.getenv("OWNER_ID", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip() and x.strip().replace('-', '').isdigit()]

def get_source_group_ids():
    raw = os.getenv("SOURCE_GROUP_ID", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip() and x.strip().replace('-', '').isdigit()]

def get_target_group_ids():
    raw = os.getenv("TARGET_GROUP_ID", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip() and x.strip().replace('-', '').isdigit()]

def is_owner(user_id: int) -> bool:
    return user_id in get_owner_ids()

def get_admin_permissions(user_id: int) -> dict:
    if is_owner(user_id):
        return {k: True for k in DEFAULT_PERMISSIONS.keys()}
    
    if admins_col is not None:
        admin_data = admins_col.find_one({"user_id": user_id})
        if admin_data:
            perms = DEFAULT_PERMISSIONS.copy()
            perms.update(admin_data.get("permissions", {}))
            return perms
    return {}

def is_bot_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    if admins_col is not None:
        return admins_col.find_one({"user_id": user_id}) is not None
    return False

# -------------------------------------------------------------
# 1. WELCOME & USER REGISTRATION
# -------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user and users_col is not None:
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {"user_id": user.id, "name": user.full_name, "joined_at": datetime.utcnow()}},
            upsert=True
        )
    await update.message.reply_text(
        f"Namaste {user.first_name}! Main Group Manager & Gemini AI Assistant Bot hoon.\n"
        f"Aap database mein registered hain!"
    )

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if result.old_chat_member.status in ["left", "kicked"] and result.new_chat_member.status == "member":
        user = result.new_chat_member.user
        if stats_col is not None:
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
# 2. GEMINI AI AUTO-REPLY SYSTEM
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
                model="gemini-1.5-flash",
                contents=prompt,
                config={
                    "system_instruction": "Aap ek friendly, smart aur helpful Telegram Assistant hain. Concise aur clear Hinglish mein jawab dein."
                }
            )
            if response.text:
                await msg.reply_text(response.text)
        except Exception as e:
            print(f"Gemini AI Error: {e}")
            await msg.reply_text("Kuch technical issue aa gaya, thodi der baad try karein!")

# -------------------------------------------------------------
# 3. OWNER & ADMIN CONTROL PANELS
# -------------------------------------------------------------
async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_owner(user_id):
        await update.message.reply_text("⛔ Yeh command sirf **BOT OWNER** ke liye reserved hai!", parse_mode="Markdown")
        return

    text = (
        "👑 **OWNER CONTROL PANEL**\n\n"
        "Aap yahan se Naye Bot Admins add/remove kar sakte hain aur unki permissions control kar sakte hain.\n\n"
        "**Owner Commands:**\n"
        "• `/add_admin <User_ID>` - User ko Bot Admin banana\n"
        "• `/remove_admin <User_ID>` - User ko Admin se hatana\n"
        "• `/set_perm <User_ID> <Perm_Name> <true/false>` - Specific permission dena\n\n"
        "**Available Permissions:**\n"
        "`can_broadcast_groups` | `can_broadcast_dm` | `can_promote_users` | `can_add_users` | `can_view_stats`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def add_bot_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/add_admin USER_ID`", parse_mode="Markdown")
        return
    
    target_id = int(context.args[0])
    if admins_col is not None:
        admins_col.update_one(
            {"user_id": target_id},
            {"$set": {"user_id": target_id, "permissions": DEFAULT_PERMISSIONS, "added_at": datetime.utcnow()}},
            upsert=True
        )
        await update.message.reply_text(f"✅ User `{target_id}` ko Bot Admin bana diya gaya hai!", parse_mode="Markdown")

async def remove_bot_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/remove_admin USER_ID`", parse_mode="Markdown")
        return
    
    target_id = int(context.args[0])
    if admins_col is not None:
        admins_col.delete_one({"user_id": target_id})
        await update.message.reply_text(f"❌ User `{target_id}` ko Bot Admin se hata diya gaya hai!", parse_mode="Markdown")

async def set_permission_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Sirf OWNER hi permissions badal sakta hai!")
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/set_perm USER_ID PERMISSION_NAME true/false`\n\n"
            "Example: `/set_perm 123456789 can_broadcast_dm true`",
            parse_mode="Markdown"
        )
        return

    target_id = int(context.args[0])
    perm_name = context.args[1]
    value = context.args[2].lower() == "true"

    if perm_name not in DEFAULT_PERMISSIONS:
        await update.message.reply_text("❌ Invalid Permission Name! Check `/owner` for full list.")
        return

    if admins_col is not None:
        admins_col.update_one(
            {"user_id": target_id},
            {"$set": {f"permissions.{perm_name}": value}},
            upsert=True
        )
        await update.message.reply_text(f"✅ User `{target_id}` ki permission `{perm_name}` = `{value}` set kar di gayi!", parse_mode="Markdown")

async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_bot_admin(user_id):
        await update.message.reply_text("⛔ Aapke paas Bot Panel Access nahi hai.")
        return

    perms = get_admin_permissions(user_id)
    
    dm_users = users_col.count_documents({}) if users_col is not None else 0
    joins_data = (stats_col.find_one({"_id": "total_joins"}) if stats_col is not None else None) or {"count": 0}
    media_pending = media_col.count_documents({"sent": False}) if media_col is not None else 0

    text = (
        f"📊 **ADMIN DASHBOARD** {'(OWNER)' if is_owner(user_id) else ''}\n\n"
        f"👥 **Total Group Joins:** `{joins_data['count']}`\n"
        f"💬 **Registered Users:** `{dm_users}`\n"
        f"🖼️ **Pending Media:** `{media_pending}`\n"
        f"🤖 **AI Status:** `{'Active ✅' if ai_client else 'Inactive ❌'}`\n\n"
        f"🔒 **Aapki Permissions:**\n"
    )

    buttons = []
    for perm, allowed in perms.items():
        text += f"• `{perm}`: {'✅' if allowed else '❌'}\n"

    if perms.get("can_broadcast_dm"):
        buttons.append([InlineKeyboardButton("📢 Broadcast Users (DM)", callback_data="bc_users")])
    if perms.get("can_broadcast_groups"):
        buttons.append([InlineKeyboardButton("📢 Broadcast Groups", callback_data="bc_group")])

    keyboard = InlineKeyboardMarkup(buttons) if buttons else None
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

# -------------------------------------------------------------
# 4. BROADCAST & ADMIN ACTIONS
# -------------------------------------------------------------
async def promote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    perms = get_admin_permissions(user_id)
    if not perms.get("can_promote_users"):
        await update.message.reply_text("⛔ Aapke paas Member Promote karne ki permission nahi hai!")
        return

    msg = update.message
    if not msg.reply_to_message:
        await update.message.reply_text("Usage: Target user ke message par **reply** karke `/promote` likhein.", parse_mode="Markdown")
        return

    target_user = msg.reply_to_message.from_user
    try:
        await context.bot.promote_chat_member(
            chat_id=msg.chat_id,
            user_id=target_user.id,
            can_change_info=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True
        )
        await msg.reply_text(f"✅ {target_user.full_name} ko Admin bana diya gaya!")
    except Exception as e:
        await msg.reply_text(f"❌ Promote Error: {e}")

async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    perms = get_admin_permissions(user_id)
    if not perms.get("can_add_users"):
        await update.message.reply_text("⛔ Aapke paas Members Add karne ki permission nahi hai!")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/add @username` ya `/add USER_ID`", parse_mode="Markdown")
        return

    try:
        await context.bot.add_chat_members(chat_id=update.effective_chat.id, user_ids=[context.args[0]])
        await update.message.reply_text(f"✅ User {context.args[0]} add ho gaya!")
    except Exception as e:
        await update.message.reply_text(f"❌ Add Error: {e}")

async def broadcast_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    perms = get_admin_permissions(user_id)
    if not perms.get("can_broadcast_dm"):
        await update.message.reply_text("⛔ Permission Denied: DM Broadcast Access Not Allowed.")
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: `/send_users Message Text`", parse_mode="Markdown")
        return

    users = users_col.find({}) if users_col is not None else []
    count = 0
    for u in users:
        try:
            await context.bot.send_message(chat_id=u['user_id'], text=text)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await update.message.reply_text(f"✅ Sent to {count} DM users.")

async def broadcast_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    perms = get_admin_permissions(user_id)
    if not perms.get("can_broadcast_groups"):
        await update.message.reply_text("⛔ Permission Denied: Group Broadcast Access Not Allowed.")
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: `/send_group Message Text`", parse_mode="Markdown")
        return

    targets = get_target_group_ids()
    sent_count = 0
    for target_id in targets:
        try:
            await context.bot.send_message(chat_id=target_id, text=text)
            sent_count += 1
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Group Broadcast Error: {e}")

    await update.message.reply_text(f"✅ Group Broadcast sent to {sent_count}/{len(targets)} groups.")

# -------------------------------------------------------------
# 5. LINK ERASER & MEDIA AUTO-FORWARDING
# -------------------------------------------------------------
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = update.effective_chat.id
    user_id = msg.from_user.id

    # 1. AI Check
    bot_username = context.bot.username
    is_ai_trigger = (f"@{bot_username}" in msg.text) or (
        msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == context.bot.id
    )

    if is_ai_trigger:
        await handle_ai_chat(update, context)
        return

    # 2. Skip Link deletion if User is Group Admin/Owner
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        if member.status in ["administrator", "creator"]:
            return
    except Exception:
        pass

    # 3. Erase Links
    if re.search(ALL_URL_REGEX, msg.text, re.IGNORECASE):
        if LOG_GROUP_ID != 0:
            try:
                await context.bot.forward_message(chat_id=LOG_GROUP_ID, from_chat_id=chat_id, message_id=msg.message_id)
                log_info = (
                    f"⚠️ **Deleted Link Alert**\n\n"
                    f"📢 **Group:** `{update.effective_chat.title}`\n"
                    f"👤 **User:** {msg.from_user.full_name} (`{user_id}`)"
                )
                await context.bot.send_message(chat_id=LOG_GROUP_ID, text=log_info, parse_mode="Markdown")
            except Exception as log_err:
                print(f"Log Error: {log_err}")

        try:
            await msg.delete()
        except Exception as e:
            print(f"Delete Error: {e}")

async def fetch_source_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    source_ids = get_source_group_ids()

    if msg and msg.chat.id in source_ids and (msg.photo or msg.video) and media_col is not None:
        media_id = msg.photo[-1].file_id if msg.photo else msg.video.file_id
        media_type = "photo" if msg.photo else "video"
        
        media_col.update_one(
            {"media_id": media_id},
            {"$set": {"media_id": media_id, "type": media_type, "sent": False, "added_at": datetime.utcnow()}},
            upsert=True
        )

async def auto_post_media_job(context: ContextTypes.DEFAULT_TYPE):
    target_ids = get_target_group_ids()
    if not target_ids or media_col is None:
        return

    unsent_media = list(media_col.find({"sent": False}).limit(10))
    for media in unsent_media:
        try:
            for target_id in target_ids:
                try:
                    if media['type'] == 'photo':
                        await context.bot.send_photo(chat_id=target_id, photo=media['media_id'])
                    elif media['type'] == 'video':
                        await context.bot.send_video(chat_id=target_id, video=media['media_id'])
                    await asyncio.sleep(1)
                except Exception as group_err:
                    print(f"Error Target {target_id}: {group_err}")

            media_col.update_one({"_id": media["_id"]}, {"$set": {"sent": True}})
            await asyncio.sleep(3)
        except Exception as e:
            print(f"Cron Error: {e}")

# -------------------------------------------------------------
# BUTTON CLICK CALLBACK HANDLER
# -------------------------------------------------------------
async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "bc_users":
        await query.message.reply_text("DM Broadcast Command: `/send_users Your Text`", parse_mode="Markdown")
    elif query.data == "bc_group":
        await query.message.reply_text("Group Broadcast Command: `/send_group Your Text`", parse_mode="Markdown")

# -------------------------------------------------------------
# MAIN BOOTSTRAP
# -------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN missing!")
        return

    threading.Thread(target=run_flask_in_background, daemon=True).start()
    print("🌐 Background Flask Web Server Started!")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("owner", owner_panel))
    app.add_handler(CommandHandler("add_admin", add_bot_admin_cmd))
    app.add_handler(CommandHandler("remove_admin", remove_bot_admin_cmd))
    app.add_handler(CommandHandler("set_perm", set_permission_cmd))
    app.add_handler(CommandHandler("dashboard", admin_dashboard))
    app.add_handler(CommandHandler("send_users", broadcast_users))
    app.add_handler(CommandHandler("send_group", broadcast_group))
    app.add_handler(CommandHandler("promote", promote_user))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CallbackQueryHandler(button_click_handler))
    
    # Events & Handlers
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), handle_messages))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.PHOTO | filters.VIDEO), fetch_source_media))

    # Cron Job (Har 5 Min Media Sync)
    if app.job_queue:
        app.job_queue.run_repeating(auto_post_media_job, interval=300, first=10)

    print("🤖 Telegram Bot Polling Started!")
    app.run_polling(allowed_updates=["chat_member", "message", "callback_query"], stop_signals=None)

if __name__ == '__main__':
    main()
