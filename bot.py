import os
import re
import asyncio
import threading
from datetime import datetime
from functools import wraps
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
# FLASK WEB SERVER (Render 24/7 Alive Thread)
# -------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is alive and running 24/7!", 200

def run_flask_in_background():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# -------------------------------------------------------------
# ENVIRONMENT VARIABLES & CONFIGURATION
# -------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
WELCOME_LINK = os.getenv("WELCOME_LINK", "https://t.me")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Gemini Client Init
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# MongoDB Connection
client = MongoClient(MONGO_URI)
db = client['telegram_bot_db']
users_col = db['users']
media_col = db['media_logs']
stats_col = db['stats']
admin_rights_col = db['admin_rights']  # NEW: Custom Admin Rights Collection

# Broad RegEx Pattern (SABHI TERAH KI LINKS & DOMAINS KO CATCH KARNE KE LIYE)
ALL_URL_REGEX = r'((https?://|www\.)[^\s]+|[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(/[^\s]*)?|t\.me/[^\s]+|telegram\.me/[^\s]+)'

# Helper Functions for Multi-Group Parsing
def get_source_group_ids():
    raw = os.getenv("SOURCE_GROUP_ID", "")
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip() and x.strip().replace('-', '').isdigit()]

def get_target_group_ids():
    raw = os.getenv("TARGET_GROUP_ID", "")
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip() and x.strip().replace('-', '').isdigit()]

# -------------------------------------------------------------
# BOT-LEVEL PERMISSION HELPER & DECORATOR (RBAC SYSTEM)
# -------------------------------------------------------------
async def check_user_right(user_id: int, required_right: str) -> bool:
    """Check if the user is Main Admin OR has the specific granted right in DB."""
    if user_id == ADMIN_ID:
        return True
    
    user_permissions = admin_rights_col.find_one({"user_id": user_id})
    if user_permissions and user_permissions.get("rights", {}).get(required_right, False):
        return True
    
    return False

def require_permission(required_right: str):
    """Decorator to restrict command execution based on dynamic bot rights."""
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            if not user:
                return
            
            has_access = await check_user_right(user.id, required_right)
            if not has_access:
                await update.effective_message.reply_text(
                    f"❌ **Access Denied!** Aapke paas `{required_right}` right nahi hai is command ko chalane ke liye.",
                    parse_mode="Markdown"
                )
                return
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

# -------------------------------------------------------------
# 1. WELCOME & USER REGISTRATION (WITH DEEP LINKING GROUP ADD)
# -------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user:
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {"user_id": user.id, "name": user.full_name, "joined_at": datetime.utcnow()}},
            upsert=True
        )
    
    bot_username = context.bot.username
    deep_link_add_url = f"https://t.me/{bot_username}?startgroup=true"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(text="➕ Add Me To Your Group", url=deep_link_add_url)],
        [InlineKeyboardButton(text="🔗 Official Community", url=WELCOME_LINK)]
    ])
    
    await update.message.reply_text(
        f"Namaste {user.first_name}! Main aapka Group Manager & Gemini AI Assistant Bot hoon.\n\n"
        f"✅ Aap bot database mein successfully registered hain!\n"
        f"👇 Niche button par click karke mujhe apne kisi bhi group mein add karein:",
        reply_markup=keyboard
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
# 2. BOT-LEVEL DYNAMIC RIGHTS MANAGEMENT (GRANT & REVOKE)
# -------------------------------------------------------------
async def grant_right(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Sirf Bot Main Owner is command ko use kar sakta hai!")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "⚠️ **Usage:** `/grant_right <USER_ID> <RIGHT_NAME>`\n\n"
            "**Available Rights:**\n"
            "• `broadcast` (Can send /send_users & /send_group)\n"
            "• `promote` (Can execute Telegram /promote & /add)\n"
            "• `dashboard` (Can view /dashboard)",
            parse_mode="Markdown"
        )
        return

    try:
        target_user_id = int(context.args[0])
        right_name = context.args[1].lower()

        admin_rights_col.update_one(
            {"user_id": target_user_id},
            {"$set": {f"rights.{right_name}": True, "updated_at": datetime.utcnow()}},
            upsert=True
        )
        await update.message.reply_text(f"✅ User `{target_user_id}` ko `{right_name}` right de diya gaya hai!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!")

async def revoke_right(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Sirf Bot Main Owner is command ko use kar sakta hai!")
        return

    if len(context.args) < 2:
        await update.message.reply_text("⚠️ **Usage:** `/revoke_right <USER_ID> <RIGHT_NAME>`", parse_mode="Markdown")
        return

    try:
        target_user_id = int(context.args[0])
        right_name = context.args[1].lower()

        admin_rights_col.update_one(
            {"user_id": target_user_id},
            {"$set": {f"rights.{right_name}": False, "updated_at": datetime.utcnow()}},
            upsert=True
        )
        await update.message.reply_text(f"🚫 User `{target_user_id}` se `{right_name}` right wapas le liya gaya hai!", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Invalid User ID!")

# -------------------------------------------------------------
# 3. GEMINI AI AUTO-REPLY SYSTEM
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
                    "system_instruction": "Aap ek friendly, smart aur helpful Telegram Assistant hain. Koi bhi question puchne par concise (chota) aur clear Hinglish mein jawab dein."
                }
            )
            if response.text:
                await msg.reply_text(response.text)
        except Exception as e:
            print(f"Gemini AI Error: {e}")
            await msg.reply_text("Kuch technical issue aa gaya, thodi der baad try karein!")

# -------------------------------------------------------------
# 4. ADVANCED ADMIN ACTIONS (Make Admin & Add Member)
# -------------------------------------------------------------
@require_permission("promote")
async def promote_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    user = update.effective_user

    member = await chat.get_member(user.id)
    if member.status not in ["administrator", "creator"]:
        await msg.reply_text("⚠️ Ye command sirf Group Admins use kar sakte hain!")
        return

    if not msg.reply_to_message:
        await msg.reply_text("Usage: Jis user ko Admin banana hai uske message par **reply** karke `/promote` likhein.", parse_mode="Markdown")
        return

    target_user = msg.reply_to_message.from_user
    try:
        await context.bot.promote_chat_member(
            chat_id=chat.id,
            user_id=target_user.id,
            can_change_info=True,
            can_delete_messages=True,
            can_invite_users=True,
            can_restrict_members=True,
            can_pin_messages=True,
            can_promote_members=False
        )
        await msg.reply_text(f"✅ {target_user.full_name} ko iss group ka Admin bana diya gaya hai!")
    except Exception as e:
        await msg.reply_text(f"❌ Admin banane mein error: {e}\n(Dhyaan rahe bot ke paas 'Add New Admins' right hona chahiye).")

@require_permission("promote")
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat = update.effective_chat
    
    if not context.args:
        await msg.reply_text("Usage: `/add @username` ya `/add USER_ID`", parse_mode="Markdown")
        return

    user_identifier = context.args[0]
    try:
        await context.bot.add_chat_members(chat_id=chat.id, user_ids=[user_identifier])
        await msg.reply_text(f"✅ Member {user_identifier} group mein add ho gaya!")
    except Exception as e:
        await msg.reply_text(f"❌ User add nahi ho saka: {e}\n(User ki privacy settings block kar sakti hain).")

# -------------------------------------------------------------
# 5. ALL LINKS ERASER & LOG FORWARDING HANDLER
# -------------------------------------------------------------
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat = update.effective_chat
    chat_id = chat.id
    chat_title = chat.title or "Unknown Group"
    user_id = msg.from_user.id

    # 1. AI Trigger Check (@Bot Tag or Reply)
    bot_username = context.bot.username
    is_ai_trigger = (f"@{bot_username}" in msg.text) or (
        msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == context.bot.id
    )

    if is_ai_trigger:
        await handle_ai_chat(update, context)
        return

    # 2. Check Admin Status (Admins can send links)
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status in ["administrator", "creator"]:
            return
    except Exception:
        pass

    # 3. ALL LINKS Detection Logic
    if re.search(ALL_URL_REGEX, msg.text, re.IGNORECASE):
        if LOG_GROUP_ID != 0:
            try:
                # Step A: Original Message Forward
                await context.bot.forward_message(
                    chat_id=LOG_GROUP_ID,
                    from_chat_id=chat_id,
                    message_id=msg.message_id
                )
                
                # Step B: Detailed Log with Group Name & ID
                log_info = (
                    f"⚠️ **Deleted Link Alert**\n\n"
                    f"📢 **Group Name:** `{chat_title}`\n"
                    f"📍 **Group ID:** `{chat_id}`\n"
                    f"👤 **User:** {msg.from_user.full_name} (`{user_id}`)"
                )
                await context.bot.send_message(chat_id=LOG_GROUP_ID, text=log_info, parse_mode="Markdown")
            except Exception as log_err:
                print(f"Log forwarding error: {log_err}")

        # Step C: Delete message from group
        try:
            await msg.delete()
        except Exception as del_err:
            print(f"Delete Error: {del_err}")

# -------------------------------------------------------------
# 6. MULTI-SOURCE TO MULTI-TARGET MEDIA CRON
# -------------------------------------------------------------
async def fetch_source_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    source_ids = get_source_group_ids()

    if msg and msg.chat.id in source_ids and (msg.photo or msg.video):
        media_id = msg.photo[-1].file_id if msg.photo else msg.video.file_id
        media_type = "photo" if msg.photo else "video"
        
        media_col.update_one(
            {"media_id": media_id},
            {"$set": {"media_id": media_id, "type": media_type, "sent": False, "added_at": datetime.utcnow()}},
            upsert=True
        )

async def auto_post_media_job(context: ContextTypes.DEFAULT_TYPE):
    target_ids = get_target_group_ids()
    if not target_ids:
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
# 7. DASHBOARD & BROADCAST SYSTEM
# -------------------------------------------------------------
@require_permission("dashboard")
async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dm_users = users_col.count_documents({})
    joins_data = stats_col.find_one({"_id": "total_joins"}) or {"count": 0}
    media_pending = media_col.count_documents({"sent": False})
    sources = get_source_group_ids()
    targets = get_target_group_ids()

    text = (
        f"📊 **ADMIN DASHBOARD**\n\n"
        f"👥 **Total Group Joins:** `{joins_data['count']}`\n"
        f"💬 **Registered DM Users:** `{dm_users}`\n"
        f"📥 **Source Groups:** `{len(sources)}`\n"
        f"📤 **Target Groups:** `{len(targets)}`\n"
        f"🖼️ **Pending Unsent Media:** `{media_pending}`\n"
        f"🤖 **Gemini AI:** `{'Active ✅' if ai_client else 'Inactive ❌'}`\n"
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

@require_permission("broadcast")
async def broadcast_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await update.message.reply_text(f"✅ Sent to {count} DM users.")

@require_permission("broadcast")
async def broadcast_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: `/send_group Text`", parse_mode="Markdown")
        return

    targets = get_target_group_ids()
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
# MAIN BOOTSTRAP
# -------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN missing!")
        return

    threading.Thread(target=run_flask_in_background, daemon=True).start()
    print("🌐 Background Flask Server Started!")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("dashboard", admin_dashboard))
    app.add_handler(CommandHandler("send_users", broadcast_users))
    app.add_handler(CommandHandler("send_group", broadcast_group))
    app.add_handler(CommandHandler("promote", promote_user))
    app.add_handler(CommandHandler("add", add_user))
    app.add_handler(CommandHandler("grant_right", grant_right))
    app.add_handler(CommandHandler("revoke_right", revoke_right))
    app.add_handler(CallbackQueryHandler(button_click_handler))
    
    # Events
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), handle_messages))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.PHOTO | filters.VIDEO), fetch_source_media))

    # Job Queue (Every 5 mins)
    if app.job_queue:
        app.job_queue.run_repeating(auto_post_media_job, interval=300, first=10)

    print("🤖 Telegram Bot Polling Started!")
    app.run_polling(allowed_updates=["chat_member", "message", "callback_query"], stop_signals=None)

if __name__ == '__main__':
    main()
