import os
import logging
import random
import string
import time
import asyncio
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant, FloodWait
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery,
    InputMediaPhoto, InputMediaDocument, InlineQueryResultArticle, InputTextMessageContent
)
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from flask import Flask, request, jsonify
from threading import Thread

# --- Flask Web Server (To keep the bot alive) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive!", 200

@flask_app.route('/api/webhook', methods=['POST'])
def webhook_receiver():
    data = request.json
    # Process webhook data here
    print(f"Received webhook data: {json.dumps(data, indent=2)}")
    return jsonify({"status": "success"}), 200

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port)

# --- Basic Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pymongo").setLevel(logging.WARNING)

# --- Load Environment Variables ---
load_dotenv()

# --- Configuration ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL")
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMINS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]
API_KEY = os.environ.get("API_KEY") # For a new API feature

# --- Global Dictionaries for temporary storage ---
# To handle multi-step processes like custom captions
custom_caption_states = {}

# --- Database Setup ---
try:
    client = MongoClient(MONGO_URI)
    db = client['file_link_bot']
    files_collection = db['files']
    settings_collection = db['settings']
    users_collection = db['users']
    logging.info("Connected to MongoDB successfully!")

    # Ensure indexes for faster lookups
    files_collection.create_index([("link_id", 1)], unique=True)
    files_collection.create_index([("expires_at", 1)], expireAfterSeconds=0)
    users_collection.create_index([("user_id", 1)], unique=True)
except ConnectionFailure as e:
    logging.error(f"Error connecting to MongoDB: {e}")
    exit()

# --- Pyrogram Client ---
app = Client(
    "FileLinkBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=enums.ParseMode.HTML,
    max_concurrent_transfers=5 # For better performance with multiple uploads
)

# --- Helper Functions ---
def generate_random_string(length=10):
    """Generates a secure, random string."""
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def human_readable_size(size):
    """Converts bytes to a human-readable format."""
    if size < 1024:
        return f"{size} B"
    for unit in ['KB', 'MB', 'GB', 'TB']:
        size /= 1024
        if size < 1024:
            return f"{size:.2f} {unit}"

def progress_callback(current, total, msg_obj, start_time):
    """Callback function for showing a real-time progress bar during uploads."""
    percentage = current * 100 / total
    progress_bar = "".join(["■" for _ in range(int(percentage / 10))])
    empty_bar = "".join(["□" for _ in range(10 - int(percentage / 10))])
    
    elapsed_time = time.time() - start_time
    if elapsed_time > 0:
        speed = current / elapsed_time
        speed_text = f"Speed: {human_readable_size(speed)}/s"
    else:
        speed_text = ""

    try:
        msg_obj.edit_text(
            f"**UPLOADING...**\n"
            f"[{progress_bar}{empty_bar}] {percentage:.1f}%\n"
            f"**Uploaded:** {human_readable_size(current)} / {human_readable_size(total)}\n"
            f"{speed_text}"
        )
    except Exception as e:
        logging.error(f"Progress bar update error: {e}")
        pass

async def is_user_member(client: Client, user_id: int) -> bool:
    """Checks if a user is a member of the update channel."""
    if not UPDATE_CHANNEL:
        return True
    try:
        await client.get_chat_member(chat_id=f"@{UPDATE_CHANNEL}", user_id=user_id)
        return True
    except UserNotParticipant:
        return False
    except Exception as e:
        logging.error(f"Error checking membership for {user_id}: {e}")
        return False

async def get_bot_mode() -> str:
    """Retrieves the current bot operation mode from the database."""
    setting = settings_collection.find_one({"_id": "bot_mode"})
    if setting:
        return setting.get("mode", "public")
    settings_collection.update_one({"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True)
    return "public"

async def log_user_action(user_id, action, details=None):
    """Logs user actions to a private channel."""
    if LOG_CHANNEL:
        log_text = (
            f"👤 **User Action Log**\n"
            f"**User:** <a href='tg://user?id={user_id}'>{user_id}</a>\n"
            f"**Action:** `{action}`\n"
            f"**Time:** `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
        )
        if details:
            log_text += f"\n**Details:** `{details}`"
        
        try:
            await app.send_message(LOG_CHANNEL, log_text, disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"Failed to send log message: {e}")

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    """Handles the /start command, including deep links for files."""
    user_id = message.from_user.id
    
    # Track user in database
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"last_seen": datetime.now(), "is_banned": False}},
        upsert=True
    )
    
    if len(message.command) > 1:
        link_id = message.command[1]

        if UPDATE_CHANNEL and not await is_user_member(client, user_id):
            join_button = InlineKeyboardButton("🔗 Join Channel", url=f"https://t.me/{UPDATE_CHANNEL}")
            joined_button = InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{link_id}")
            keyboard = InlineKeyboardMarkup([[join_button], [joined_button]])
            
            await message.reply(
                f"👋 **Hello, {message.from_user.first_name}!**\n\nTo access this content, you must first join our channel. Please click the button below to continue.",
                reply_markup=keyboard
            )
            return

        file_record = files_collection.find_one({"_id": link_id})
        if file_record:
            await log_user_action(user_id, "file_access", details=f"Link ID: {link_id}")
            try:
                if 'message_ids' in file_record:
                    # Handle batch files
                    status_msg = await message.reply("⏳ Fetching your files...", quote=True)
                    for msg_id in file_record['message_ids']:
                        await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=msg_id)
                    await status_msg.edit_text("✅ All files have been sent!")
                elif 'message_id' in file_record:
                    # Handle single file
                    await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
            except Exception as e:
                await message.reply(f"❌ Sorry, an error occurred while sending the file.\n`Error: {e}`")
        else:
            await message.reply("🤔 Content not found! The link may be incorrect or has expired.")
    else:
        welcome_image_url = "https://envs.sh/L6I.jpg/IMG20250922630.jpg"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❓ How to Use", callback_data="help_info")],
            [InlineKeyboardButton("⚙️ Bot Settings", callback_data="open_settings") if user_id in ADMINS else InlineKeyboardButton("🚀 More", url="https://t.me/your_channel_link")],
        ])
        
        await message.reply_photo(
            photo=welcome_image_url,
            caption=f"👋 **Hello, {message.from_user.first_name}!**\n\n"
                    f"I'm your personal cloud link generator. Simply send me any file or a group of files, and I'll give you a clean, shareable link!",
            reply_markup=keyboard
        )

# --- Admin Command Handlers ---

@app.on_message(filters.command("settings") & filters.private & filters.user(ADMINS))
async def settings_panel_handler(client: Client, message: Message):
    """Admin-only command to open the bot settings panel."""
    current_mode = await get_bot_mode()
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Current Mode: {current_mode.upper()} 🔄", callback_data="toggle_mode")],
        [InlineKeyboardButton("Manage Banned Users 🚫", callback_data="manage_bans")],
        [InlineKeyboardButton("Edit Welcome Message ✍️", callback_data="edit_welcome")],
        [InlineKeyboardButton("Close ❌", callback_data="close_settings")],
    ])
    
    await message.reply("⚙️ **Admin Settings Panel**", reply_markup=keyboard)

@app.on_message(filters.command("status") & filters.private & filters.user(ADMINS))
async def status_handler(client: Client, message: Message):
    """Admin command to check bot status."""
    db_status = "Connected ✅"
    try:
        client.mongo_client.admin.command('ping')
    except Exception:
        db_status = "Disconnected ❌"
    
    current_mode = await get_bot_mode()
    total_users = users_collection.count_documents({})
    total_files = files_collection.count_documents({})
    
    status_text = (
        "📊 **Bot Status**\n\n"
        f"**Database:** {db_status}\n"
        f"**File Upload Mode:** `{current_mode.upper()}`\n"
        f"**Total Users:** {total_users}\n"
        f"**Total Files Stored:** {total_files}\n"
        f"**Admins:** {len(ADMINS)}"
    )
    
    await message.reply(status_text)
    await log_user_action(message.from_user.id, "check_status")

@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMINS))
async def broadcast_handler(client: Client, message: Message):
    """Admin command to broadcast a message to all users."""
    if len(message.command) < 2:
        await message.reply("❌ **Usage:** `/broadcast [message]`")
        return

    broadcast_message = " ".join(message.command[1:])
    user_count = 0
    
    await message.reply("⏳ Starting broadcast...")
    await log_user_action(message.from_user.id, "broadcast", details=f"Message: {broadcast_message[:50]}")
    
    for user in users_collection.find():
        try:
            await app.send_message(user['user_id'], broadcast_message)
            user_count += 1
            await asyncio.sleep(0.1) # Avoid flood waits
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            logging.error(f"Failed to send message to user {user['user_id']}: {e}")
    
    await message.reply(f"✅ Broadcast complete! Sent message to {user_count} users.")

@app.on_message(filters.command("ban") & filters.private & filters.user(ADMINS))
async def ban_user_handler(client: Client, message: Message):
    """Admin command to ban a user."""
    if len(message.command) < 2 or not message.command[1].isdigit():
        await message.reply("❌ **Usage:** `/ban [user_id]`")
        return
    
    user_id_to_ban = int(message.command[1])
    if user_id_to_ban in ADMINS:
        await message.reply("❌ You cannot ban another admin.")
        return

    users_collection.update_one({"user_id": user_id_to_ban}, {"$set": {"is_banned": True}})
    await message.reply(f"🚫 User `{user_id_to_ban}` has been banned.")
    await log_user_action(message.from_user.id, "ban_user", details=f"Banned user ID: {user_id_to_ban}")

@app.on_message(filters.command("unban") & filters.private & filters.user(ADMINS))
async def unban_user_handler(client: Client, message: Message):
    """Admin command to unban a user."""
    if len(message.command) < 2 or not message.command[1].isdigit():
        await message.reply("❌ **Usage:** `/unban [user_id]`")
        return
    
    user_id_to_unban = int(message.command[1])
    users_collection.update_one({"user_id": user_id_to_unban}, {"$set": {"is_banned": False}})
    await message.reply(f"✅ User `{user_id_to_unban}` has been unbanned.")
    await log_user_action(message.from_user.id, "unban_user", details=f"Unbanned user ID: {user_id_to_unban}")

# --- Callback Query Handlers ---

@app.on_callback_query(filters.regex("help_info"))
async def help_callback_handler(client: Client, callback_query: CallbackQuery):
    """Callback for the 'How to Use' button."""
    await callback_query.answer("Here's how to use me!", show_alert=True)
    help_text = (
        "**How I Work:**\n\n"
        "**1. Send a file:** Just send me a photo, video, document, or any other file.\n"
        "**2. Get a link:** I'll reply with a permanent, direct link.\n"
        "**3. Share:** Anyone can use the link to get the file from me.\n\n"
        "**Tip:** You can send multiple files as an album, and I'll create a single link for all of them!"
    )
    await callback_query.message.edit_caption(help_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_to_start")]]))

@app.on_callback_query(filters.regex("back_to_start"))
async def back_to_start_callback(client: Client, callback_query: CallbackQuery):
    """Brings the user back to the main start message."""
    welcome_image_url = "https://envs.sh/L6I.jpg/IMG20250922630.jpg"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❓ How to Use", callback_data="help_info")],
        [InlineKeyboardButton("⚙️ Bot Settings", callback_data="open_settings") if callback_query.from_user.id in ADMINS else InlineKeyboardButton("🚀 More", url="https://t.me/your_channel_link")],
    ])
    
    await callback_query.message.edit_media(
        media=InputMediaPhoto(welcome_image_url, caption=f"👋 **Hello, {callback_query.from_user.first_name}!**\n\nI'm your personal cloud link generator. Simply send me any file or a group of files, and I'll give you a clean, shareable link!"),
        reply_markup=keyboard
    )
    await callback_query.answer()

@app.on_callback_query(filters.regex("toggle_mode") & filters.user(ADMINS))
async def toggle_mode_callback(client: Client, callback_query: CallbackQuery):
    """Toggles the bot's operation mode."""
    current_mode = await get_bot_mode()
    new_mode = "private" if current_mode == "public" else "public"
    
    settings_collection.update_one({"_id": "bot_mode"}, {"$set": {"mode": new_mode}}, upsert=True)
    
    await callback_query.answer(f"Mode set to {new_mode.upper()}!", show_alert=True)
    
    await log_user_action(callback_query.from_user.id, "toggle_mode", details=f"New mode: {new_mode}")
    
    current_mode = await get_bot_mode()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Current Mode: {current_mode.upper()} 🔄", callback_data="toggle_mode")],
        [InlineKeyboardButton("Manage Banned Users 🚫", callback_data="manage_bans")],
        [InlineKeyboardButton("Edit Welcome Message ✍️", callback_data="edit_welcome")],
        [InlineKeyboardButton("Close ❌", callback_data="close_settings")],
    ])
    
    await callback_query.message.edit_text(f"⚙️ **Admin Settings Panel**", reply_markup=keyboard)


@app.on_callback_query(filters.regex("check_join_"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
    """Handles the 'I Have Joined' button after force join."""
    user_id = callback_query.from_user.id
    link_id = callback_query.data.split("_", 2)[2]

    if await is_user_member(client, user_id):
        await callback_query.answer("Thanks for joining! Sending files...", show_alert=True)
        file_record = files_collection.find_one({"_id": link_id})
        
        if file_record:
            try:
                if 'message_ids' in file_record:
                    for msg_id in file_record['message_ids']:
                        await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=msg_id)
                elif 'message_id' in file_record:
                    await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                
                await callback_query.message.delete()
            except Exception as e:
                await callback_query.message.edit_text(f"❌ An error occurred while sending the file.\n`Error: {e}`")
        else:
            await callback_query.message.edit_text("🤔 Content not found!")
    else:
        await callback_query.answer("You haven't joined the channel yet. Please join and try again.", show_alert=True)

# --- File Handler ---

@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
async def file_handler(client: Client, message: Message):
    """Main file handling logic, supporting single files and albums."""
    user_id = message.from_user.id
    user_status = users_collection.find_one({"user_id": user_id})
    if user_status and user_status.get("is_banned"):
        await message.reply("🚫 You are banned from using this bot.")
        return

    bot_mode = await get_bot_mode()
    if bot_mode == "private" and user_id not in ADMINS:
        await message.reply("😔 **Sorry!** At the moment, only admins can upload files.")
        return

    if message.media_group_id:
        if files_collection.find_one({"media_group_id": message.media_group_id}):
            return

        status_msg = await message.reply("⏳ Processing your files...", quote=True)
        try:
            # A more robust way to get all messages in an album
            media_group_messages = []
            album_start_time = time.time()
            while time.time() - album_start_time < 5: # Wait for 5 seconds for all album parts to arrive
                async for msg in client.get_chat_history(message.chat.id, limit=20):
                    if msg.media_group_id == message.media_group_id and msg not in media_group_messages:
                        media_group_messages.append(msg)
                if len(media_group_messages) >= 10: # Max 10 files per album
                    break
                await asyncio.sleep(0.5)

            if not media_group_messages:
                await status_msg.edit_text("❌ An error occurred while processing the album. Please try again.")
                return

            forwarded_message_ids = []
            for msg in media_group_messages:
                forwarded_message = await msg.forward(LOG_CHANNEL)
                forwarded_message_ids.append(forwarded_message.id)

            link_id = f"batch_{generate_random_string()}"
            files_collection.insert_one({
                '_id': link_id,
                'media_group_id': message.media_group_id,
                'message_ids': forwarded_message_ids,
                'created_at': datetime.now(),
                'expires_at': datetime.now() + timedelta(days=365) # Links expire in 1 year
            })
            
            bot_username = (await client.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={link_id}"
            
            await status_msg.edit_text(
                f"✅ **Link Generated!**\n\n🔗 Your Link: `{share_link}`\n\n"
                f"**Note:** This link contains all **{len(forwarded_message_ids)}** files.",
                disable_web_page_preview=True
            )
            await log_user_action(user_id, "album_link_generated", details=f"Link ID: {link_id}")

        except Exception as e:
            logging.error(f"Album handling error: {e}")
            await status_msg.edit_text(f"❌ **Error!**\n\nSomething went wrong. Please try again.\n`Details: {e}`")
    else:
        # Single file processing
        status_msg = await message.reply("⏳ Preparing to upload...", quote=True)
        try:
            start_time = time.time()
            forwarded_message = await client.copy_message(
                chat_id=LOG_CHANNEL,
                from_chat_id=message.chat.id,
                message_id=message.id,
                progress=progress_callback,
                progress_args=(status_msg, start_time)
            )
            
            file_id_str = f"file_{generate_random_string()}"
            files_collection.insert_one({
                '_id': file_id_str,
                'message_id': forwarded_message.id,
                'created_at': datetime.now(),
                'expires_at': datetime.now() + timedelta(days=365)
            })
            
            bot_username = (await client.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={file_id_str}"
            
            await status_msg.edit_text(
                f"✅ **Link Generated!**\n\n🔗 Your Link: `{share_link}`",
                disable_web_page_preview=True
            )
            await log_user_action(user_id, "single_file_link_generated", details=f"Link ID: {file_id_str}")
        except Exception as e:
            logging.error(f"Single file handling error: {e}")
            await status_msg.edit_text(f"❌ **Error!**\n\nSomething went wrong. Please try again.\n`Details: {e}`")

# --- Inline Mode Handler ---
@app.on_inline_query()
async def inline_query_handler(client: Client, inline_query):
    """Allows users to search for files in inline mode."""
    results = []
    query = inline_query.query.strip().lower()

    if not query:
        results.append(
            InlineQueryResultArticle(
                title="Type to search for files...",
                input_message_content=InputTextMessageContent("Start typing to search for files stored by the bot.")
            )
        )
    else:
        # A simple search based on file name or other metadata
        # This part requires more advanced database queries to be truly effective
        # For now, it's a basic example.
        # You'd need to store file names in your database.
        files = files_collection.find({"_id": {"$regex": f".*{query}.*", "$options": "i"}}).limit(20)
        
        for file in files:
            link_id = file['_id']
            bot_username = (await client.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={link_id}"
            results.append(
                InlineQueryResultArticle(
                    title=f"File Link: {link_id}",
                    description=f"Click to share this file link.",
                    input_message_content=InputTextMessageContent(share_link)
                )
            )
    
    await client.answer_inline_query(inline_query.id, results=results, cache_time=5)

# --- Start the Bot ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("WARNING: ADMIN_IDS is not set. Admin commands will not work.")
    
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("Bot is starting...")
    app.run()
    logging.info("Bot has stopped.")

