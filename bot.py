import os
import re
import asyncio
from datetime import datetime
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

# Environment Variables Read Karein
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # Aapki apni Telegram User ID
SOURCE_GROUP_ID = int(os.getenv("SOURCE_GROUP_ID", "0"))  # Jahan se media uthana hai
TARGET_GROUP_ID = int(os.getenv("TARGET_GROUP_ID", "0"))  # Jahan media post hona hai
LOG_GROUP_ID = int(os.getenv("LOG_GROUP_ID", "0"))  # Deleted links log group
WELCOME_LINK = os.getenv("WELCOME_LINK", "https://t.me")

# MongoDB Database Connection
client = MongoClient(MONGO_URI)
db = client['telegram_bot_db']
users_col = db['users']
media_col = db['media_logs']
stats_col = db['stats']

# URL Detect karne ke liye RegEx
URL_REGEX = r'(https?://[^\s]+|www\.[^\s]+|t\.me/[^\s]+)'

# -------------------------------------------------------------
# 1. WELCOME & USER REGISTRATION
# -------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User Jab Bot ke DM mein /start dabayega"""
    user = update.effective_user
    if user:
        users_col.update_one(
            {"user_id": user.id},
            {"$set": {"user_id": user.id, "name": user.full_name, "joined_at": datetime.utcnow()}},
            upsert=True
        )
    await update.message.reply_text(
        f"Namaste {user.first_name}! Main aapka Group Manager Bot hoon.\n"
        f"Aap ab bot database mein registered hain!"
    )

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Naye Group Member ko Welcome karna"""
    result = update.chat_member
    if result.old_chat_member.status in ["left", "kicked"] and result.new_chat_member.status == "member":
        user = result.new_chat_member.user
        
        # Total joins count update karein
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
# 2. LINK DELETE & LOG SYSTEM
# -------------------------------------------------------------
async def handle_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Non-admin links delete karna aur source/log group mein bhejna"""
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = msg.chat.id
    user_id = msg.from_user.id

    # Admin check
    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    if chat_member.status in ["administrator", "creator"]:
        return  # Admin allowed

    # Agar normal user link bhejta hai
    if re.search(URL_REGEX, msg.text):
        # 1. Private Log/Source Group mein Copy Bhejo
        log_text = (
            f"⚠️ **Link Deleted Alert**\n"
            f"👤 **User:** {msg.from_user.full_name} (`{user_id}`)\n"
            f"📍 **Group ID:** `{chat_id}`\n"
            f"📝 **Message Content:**\n{msg.text}"
        )
        if LOG_GROUP_ID != 0:
            await context.bot.send_message(chat_id=LOG_GROUP_ID, text=log_text, parse_mode="Markdown")

        # 2. Original Message Delete Karo
        try:
            await msg.delete()
        except Exception as e:
            print(f"Delete Error: {e}")

# -------------------------------------------------------------
# 3. SOURCE TO TARGET AUTOMATED MEDIA CRON (Every 24 Hours)
# -------------------------------------------------------------
async def fetch_source_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Source group se media register karna (Source Group Admin / Save)"""
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
    """Har 24 Ghante mein 20 Unsent Media Post Karega"""
    if TARGET_GROUP_ID == 0:
        return

    unsent_media = list(media_col.find({"sent": False}).limit(20))
    for media in unsent_media:
        try:
            if media['type'] == 'photo':
                await context.bot.send_photo(chat_id=TARGET_GROUP_ID, photo=media['media_id'])
            elif media['type'] == 'video':
                await context.bot.send_video(chat_id=TARGET_GROUP_ID, video=media['media_id'])
            
            # Database mein 'Sent' Mark Karo (Taaki repeat na ho)
            media_col.update_one({"_id": media["_id"]}, {"$set": {"sent": True}})
            await asyncio.sleep(3)  # Rate-limit safety gap
        except Exception as e:
            print(f"Media post error: {e}")

# -------------------------------------------------------------
# 4. IN-BOT DASHBOARD & DUAL BROADCAST SYSTEM
# -------------------------------------------------------------
async def admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin Panel Command: /dashboard"""
    if update.effective_user.id != ADMIN_ID:
        return

    dm_users = users_col.count_documents({})
    joins_data = stats_col.find_one({"_id": "total_joins"}) or {"count": 0}
    media_pending = media_col.count_documents({"sent": False})

    text = (
        f"📊 **ADMIN DASHBOARD**\n\n"
        f"👥 **Total Joins (Group):** `{joins_data['count']}`\n"
        f"💬 **Registered DM Users (For Broadcast):** `{dm_users}`\n"
        f"🖼️ **Pending Unsent Media:** `{media_pending}`\n"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Broadcast Users (DM)", callback_data="bc_users")],
        [InlineKeyboardButton("📢 Broadcast Target Group", callback_data="bc_group")]
    ])

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

async def button_click_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dashboard Buttons Click Handler"""
    query = update.callback_query
    await query.answer()

    if query.data == "bc_users":
        await query.message.reply_text("Direct Broadcast ke liye likhein:\n`/send_users Aapka Message`", parse_mode="Markdown")
    elif query.data == "bc_group":
        await query.message.reply_text("Group Broadcast ke liye likhein:\n`/send_group Aapka Message`", parse_mode="Markdown")

async def broadcast_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Direct Messages Broadcast: /send_users <msg>"""
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
    await update.message.reply_text(f"✅ Successful DM Broadcast: {count} users.")

async def broadcast_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Target Group Broadcast: /send_group <msg>"""
    if update.effective_user.id != ADMIN_ID:
        return

    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("Usage: `/send_group Hello Group!`", parse_mode="Markdown")
        return

    try:
        await context.bot.send_message(chat_id=TARGET_GROUP_ID, text=text)
        await update.message.reply_text("✅ Target Group Broadcast Sent!")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")

# -------------------------------------------------------------
# MAIN APP BOOTSTRAP
# -------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        print("Error: BOT_TOKEN missing!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("dashboard", admin_dashboard))
    app.add_handler(CommandHandler("send_users", broadcast_users))
    app.add_handler(CommandHandler("send_group", broadcast_group))
    app.add_handler(CallbackQueryHandler(button_click_handler))
    
    # Event Handlers
    app.add_handler(ChatMemberHandler(welcome_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & (~filters.COMMAND), handle_messages))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (filters.PHOTO | filters.VIDEO), fetch_source_media))

    # Daily 24 Hours Media Auto Poster Job (86400 seconds)
    if app.job_queue:
        app.job_queue.run_repeating(auto_post_media_job, interval=86400, first=10)

    print("Bot Successfully Started!")
    app.run_polling(allowed_updates=["chat_member", "message", "callback_query"])

if __name__ == '__main__':
    main()
