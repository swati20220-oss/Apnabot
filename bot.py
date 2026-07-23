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
# FLASK WEB SERVER (Background Thread For Render 24/7 Alive)
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
SOURCE_GROUP_ID = int(os.getenv("SOURCE_GROUP_ID", "0"))
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))
WELCOME_LINK = os.getenv("WELCOME_LINK", "https://t.me")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Gemini Client Initialization
ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# MongoDB Database Connection
client = MongoClient(MONGO_URI)
db = client['telegram_bot_db']
users_col = db['users']
media_col = db['media_logs']
stats_col = db['stats']

# URL Regex Pattern
URL_REGEX = r'(https?://[^\s]+|www\.[^\s]+|t\.me/[^\s]+)'

# Helper function to parse multiple target group IDs (comma separated)
def get_target_group_ids():
    raw = os.getenv("TARGET_GROUP_ID", "")
    if not raw:
        return []
    ids = []
    for x in raw.split(","):
        x = x.strip()
        if x:
            try:
                ids.append(int(x))
            except ValueError:
                pass
    return ids

# -------------------------------------------------------------
# 1. WELCOME & USER REGISTRATION
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
        f"Namaste {user.first_name}! Main aapka Multi-Group Manager & Gemini AI Assistant Bot hoon.\n"
        f"Aap ab bot database mein successfully registered hain!"
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
# 2. GEMINI AI CHATBOT HANDLER
# -------------------------------------------------------------
async def handle_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text or not ai_client:
        return

    bot_username = context.bot.username
    text = msg.text

    # Trigger conditions: Bot Tagged OR Reply to Bot
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

        # Show typing indicator in chat
        await context.bot.send_chat_action(chat_id=msg.chat_id, action="typing")

        try:
            response = ai_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config={
                    "system_instruction": "Aap ek polite, smart aur helpful Telegram Group Manager aur Assistant hain. Fast, accurate aur friendly Hinglish mein concise (chote) jawab dein."
                }
            )
            if response.text:
                await msg.reply_text(response.text)
        except Exception as e:
            print(f"Gemini AI Error: {e}")
            await msg.reply_text("Thoda technical issue aa gaya hai, kripya 1 minute baad try karein!")

# -------------------------------------------------------------
# 3. LINK PROTECTOR & MESSAGE FILTER
# -------------------------------------------------------------
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat.id
    user_id = msg.from_user.id

    # Check for AI Trigger First
    bot_username = context.bot.username
    is_ai_trigger = (f"@{bot_username}" in msg.text) or (
        msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == context.bot.id
    )

    if is_ai_trigger:
        await handle_ai_chat(update, context)
        return

    # Check for Group Admins (Exempt from link deletion)
    try:
        chat_member = await context.bot.get_chat_member(chat_id, user_id)
        if chat_member.status in ["administrator", "creator"]:
            return
    except Exception:
        pass

    # Link Auto-Deletion System
    if re.search(URL_REGEX, msg.text):
        log_text = (
            f"⚠️ **Link Deleted Alert**\n"
            f"👤 **User:** {msg.from_user.full_name} (`{user_id}`)\n"
            f"📍 **Group ID:** `{chat_id}`\n"
            f"📝 **Message Content:**\n{msg.text}"
        )
        if LOG_GROUP_ID != 0:
            try:
                await context.bot.send_message(chat_id=LOG_GROUP_ID, text=log_text, parse_mode="Markdown")
            except Exception as e:
                print(f"Log group error: {e}")

        try:
            await msg.delete()
        except Exception as e:
            print(f"Delete Error: {e}")

# -------------------------------------------------------------
# 4. MULTI-GROUP MEDIA AUTOMATION CRON
# -------------------------------------------------------------
async def fetch_source_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.chat.id == SOURCE_GROUP_ID and (msg.photo or msg.video):
        media_id = msg.photo[-1].file_id if msg.photo else msg.video.file_id
        media_type = "photo" if msg.photo else "video"
        
        media_col.update_one(
            {"media_id": media_id},
            {"$set": {"media_id": media_id, "type": media_type, "sent": False, "added_at": datetime.utcnow()}},
            upsert=True
        )

async def auto_post_media_job(context: ContextTypes.DEFAULT_TYPE):
    target_group_ids = get_target_group_ids()
    if not target_group_ids:
        return

    unsent_media = list(media_col.find({"sent": False}).limit(10))
    for media in unsent_media:
        try:
            for target_id in target_group_ids:
                try:
                    if media['type'] == 'photo':
                        await context.bot.send_photo(chat_id=target_id, photo=media['media_id'])
                    elif media['type'] == 'video':
                        await context.bot.send_video(chat_id=target_id, video=media['media_id'])
                    await asyncio.sleep(1)  # Gap to avoid Telegram flood limits
                except Exception as group_err:
                    print(f"Error posting to group {target_id}: {group_err}")

            # Mark sent after attempting all target groups
            media_col.update_one({"_id": media["_id"]}, {"$set": {"sent": True}})
            await asyncio.sleep(3)
        except Exception as e:
            print(f"Media loop error: {e}")

# -------------------------------------------------------------
# 5. ADMIN DASHBOARD & BROADCAST SYSTEM
# -------------------------------------------------------------
async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    dm_users = users_col.count_documents({})
    joins_data = stats_col.find_one({"_id": "total_joins"}) or {"count": 0}
    media_pending = media_col.count_documents({"sent": False})
    targets = get_target_group_ids()

    text = (
        f"📊 **ADMIN DASHBOARD**\n\n"
        f"👥 **Total Group Joins:** `{joins_data['count']}`\n"
        f"💬 **Registered DM Users:** `{dm_users}`\n"
        f"🎯 **Target Groups Connected:** `{len(targets)}`\n"
        f"🖼️ **Pending Unsent Media:** `{media_pending}`\n"
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
        await query.message.reply_text("DM Broadcast ke liye likhein:\n`/send_users Aapka Message`", parse_mode="Markdown")
    elif query.data == "bc_group":
        await query.message.reply_text("Multi-Group Broadcast ke liye likhein:\n`/send_group Aapka Message`", parse_mode="Markdown")

async def broadcast_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: `/send_users Hello everyone!`", parse_mode="Markdown")
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
    await update.message.reply_text(f"✅ DM Broadcast sent to {count} users.")

async def broadcast_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: `/send_group Hello Groups!`", parse_mode="Markdown")
        return

    targets = get_target_group_ids()
    sent_count = 0
    for target_id in targets:
        try:
            await context.bot.send_message(chat_id=target_id, text=text)
            sent_count += 1
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Broadcast error for group {target_id}: {e}")

    await update.message.reply_text(f"✅ Broadcast sent to {sent_count}/{len(targets)} Target Groups!")

# -------------------------------------------------------------
# MAIN BOOTSTRAP
# -------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN missing!")
        return

    # 1. Start Flask Server in Background Thread
    threading.Thread(target=run_flask_in_background, daemon=True).start()
    print("🌐 Background Flask Server Started!")

    # 2. Build Telegram Bot Application
    app = Application.builder().token(BOT_TOKEN).build()

    # Register Command Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("dashboard", admin_dashboard))
    app.add_handler(CommandHandler("send_users", broadcast_users))
    app.add_handler(CommandHandler("send_group", broadcast_group))
    app.add_handler(CallbackQueryHandler(button_click_handler))
    
    # Event Handlers
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), handle_messages))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.PHOTO | filters.VIDEO), fetch_source_media))

    # Job Queue (Cron - Every 5 minutes / 300 seconds)
    if app.job_queue:
        app.job_queue.run_repeating(auto_post_media_job, interval=300, first=10)

    print("🤖 Telegram Bot Polling Started Successfully On Main Thread!")
    app.run_polling(allowed_updates=["chat_member", "message", "callback_query"], stop_signals=None)

if __name__ == '__main__':
    main()
