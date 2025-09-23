import os
import logging
import random
import string
import time
import asyncio
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant, FloodWait
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
)
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError, InvalidOperation
from flask import Flask
from threading import Thread

# --- Flask Web Server (To keep the bot alive) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive!", 200

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

# --- Database Setup ---
try:
    client = MongoClient(MONGO_URI)
    db = client['file_link_bot']
    files_collection = db['files']
    users_collection = db['users'] # New collection for user tracking
    settings_collection = db['settings']
    logging.info("Connected to MongoDB successfully!")

    # **ADVANCED FEATURE**: Create indexes for faster lookups and to prevent errors
    files_collection.create_index([("_id", 1)], unique=True)
    users_collection.create_index([("user_id", 1)], unique=True)
    logging.info("MongoDB indexes created/verified successfully!")

except ConnectionFailure as e:
    logging.error(f"Error connecting to MongoDB: {e}")
    exit()
except InvalidOperation as e:
    logging.error(f"MongoDB InvalidOperation error: {e}. The fix is to ensure `create_index` is called immediately after connection.")
    exit()

# --- Pyrogram Client ---
app = Client(
    "FileLinkBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=enums.ParseMode.HTML
)

# --- Helper Functions ---
def generate_random_string(length=8):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def human_readable_size(size):
    if size < 1024:
        return f"{size} B"
    for unit in ['KB', 'MB', 'GB', 'TB']:
        size /= 1024
        if size < 1024:
            return f"{size:.2f} {unit}"

def progress_callback(current, total, msg_obj, start_time):
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
            f"**Progress:**\n"
            f"[{progress_bar}{empty_bar}] {percentage:.1f}%\n"
            f"**Uploaded:** {human_readable_size(current)} / {human_readable_size(total)}\n"
            f"{speed_text}"
        )
    except Exception as e:
        logging.error(f"Progress bar update error: {e}")
        pass

async def is_user_member(client: Client, user_id: int) -> bool:
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
    setting = settings_collection.find_one({"_id": "bot_mode"})
    if setting:
        return setting.get("mode", "public")
    settings_collection.update_one({"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True)
    return "public"

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    # **ADVANCED FEATURE**: Store user info and check for ban status
    user_id = message.from_user.id
    users_collection.update_one(
        {"user_id": user_id},
        {"$set": {"last_seen": time.time(), "is_banned": False}},
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
            if 'message_ids' in file_record:
                status_msg = await message.reply("⏳ Fetching your files...", quote=True)
                for msg_id in file_record['message_ids']:
                    try:
                        await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=msg_id)
                    except Exception as e:
                        logging.error(f"Error copying file with ID {msg_id}: {e}")
                await status_msg.edit_text("✅ All files have been sent!")
            elif 'message_id' in file_record:
                try:
                    await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                except Exception as e:
                    await message.reply(f"❌ Sorry, an error occurred while sending the file.\n`Error: {e}`")
        else:
            await message.reply("🤔 Content not found! The link may be incorrect or has expired.")
    else:
        welcome_image_url = "https://envs.sh/L6I.jpg/IMG20250922630.jpg"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("❓ How to Use", callback_data="help_info")]
        ])
        
        await message.reply_photo(
            photo=welcome_image_url,
            caption=f"👋 **Hello, {message.from_user.first_name}!**\n\n"
                    f"I'm your personal cloud link generator. Simply send me any file or a group of files, and I'll give you a clean, shareable link!",
            reply_markup=keyboard
        )

# **NEW ADVANCED FEATURE**: Status, Ban, Unban, and Broadcast commands
@app.on_message(filters.command("status") & filters.private & filters.user(ADMINS))
async def status_handler(client: Client, message: Message):
    db_status = "Connected ✅"
    try:
        client.mongo_client.admin.command('ping')
    except Exception:
        db_status = "Disconnected ❌"
    
    current_mode = await get_bot_mode()
    total_users = users_collection.count_documents({})
    
    status_text = (
        "📊 **Bot Status**\n\n"
        f"**Database:** {db_status}\n"
        f"**File Upload Mode:** `{current_mode.upper()}`\n"
        f"**Total Users:** {total_users}\n"
        f"**Admins:** {len(ADMINS)}"
    )
    
    await message.reply(status_text)

@app.on_message(filters.command("ban") & filters.private & filters.user(ADMINS))
async def ban_user_handler(client: Client, message: Message):
    if len(message.command) < 2 or not message.command[1].isdigit():
        await message.reply("❌ **Usage:** `/ban [user_id]`")
        return
    
    user_id_to_ban = int(message.command[1])
    if user_id_to_ban in ADMINS:
        await message.reply("❌ You cannot ban another admin.")
        return

    users_collection.update_one({"user_id": user_id_to_ban}, {"$set": {"is_banned": True}})
    await message.reply(f"🚫 User `{user_id_to_ban}` has been banned.")

@app.on_message(filters.command("unban") & filters.private & filters.user(ADMINS))
async def unban_user_handler(client: Client, message: Message):
    if len(message.command) < 2 or not message.command[1].isdigit():
        await message.reply("❌ **Usage:** `/unban [user_id]`")
        return
    
    user_id_to_unban = int(message.command[1])
    users_collection.update_one({"user_id": user_id_to_unban}, {"$set": {"is_banned": False}})
    await message.reply(f"✅ User `{user_id_to_unban}` has been unbanned.")

@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMINS))
async def broadcast_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("❌ **Usage:** `/broadcast [message]`")
        return

    broadcast_message = " ".join(message.command[1:])
    user_count = 0
    
    await message.reply("⏳ Starting broadcast...")
    
    for user in users_collection.find():
        try:
            await app.send_message(user['user_id'], broadcast_message)
            user_count += 1
            await asyncio.sleep(0.1)
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception as e:
            logging.error(f"Failed to send message to user {user['user_id']}: {e}")
    
    await message.reply(f"✅ Broadcast complete! Sent message to {user_count} users.")


@app.on_callback_query(filters.regex("help_info"))
async def help_callback_handler(client: Client, callback_query: CallbackQuery):
    await callback_query.answer("Here's how to use me!", show_alert=True)
    help_text = (
        "**How I Work:**\n\n"
        "**1. Send a file:** Just send me a photo, video, document, or any other file.\n"
        "**2. Get a link:** I'll reply with a permanent, direct link.\n"
        "**3. Share:** Anyone can use the link to get the file from me.\n\n"
        "**Tip:** You can send multiple files as an album, and I'll create a single link for all of them!"
    )
    await callback_query.message.edit_caption(help_text)

@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
async def file_handler(client: Client, message: Message):
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
            media_group_messages = []
            async for msg in client.get_chat_history(message.chat.id, limit=20):
                if msg.media_group_id == message.media_group_id and (msg.document or msg.video or msg.photo or msg.audio):
                    media_group_messages.append(msg)
            
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
                'message_ids': forwarded_message_ids
            })
            
            bot_username = (await client.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={link_id}"
            
            await status_msg.edit_text(
                f"✅ **Link Generated!**\n\n🔗 Your Link: `{share_link}`\n\n"
                f"**Note:** This link contains all **{len(forwarded_message_ids)}** files.",
                disable_web_page_preview=True
            )

        except DuplicateKeyError:
            # **ADVANCED FEATURE**: Handle rare duplicate key errors gracefully
            await status_msg.edit_text("❌ A temporary issue occurred. Please try sending the files again.")
        except Exception as e:
            logging.error(f"Album handling error: {e}")
            await status_msg.edit_text(f"❌ **Error!**\n\nSomething went wrong. Please try again.\n`Details: {e}`")

    else: # Single file processing
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
            files_collection.insert_one({'_id': file_id_str, 'message_id': forwarded_message.id})
            bot_username = (await client.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={file_id_str}"
            
            await status_msg.edit_text(
                f"✅ **Link Generated!**\n\n🔗 Your Link: `{share_link}`",
                disable_web_page_preview=True
            )
        except DuplicateKeyError:
            await status_msg.edit_text("❌ A temporary issue occurred. Please try sending the file again.")
        except Exception as e:
            logging.error(f"File handling error: {e}")
            await status_msg.edit_text(f"❌ **Error!**\n\nSomething went wrong. Please try again.\n`Details: {e}`")


@app.on_callback_query(filters.regex(r"^set_mode_"))
async def set_mode_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id not in ADMINS:
        await callback_query.answer("Permission Denied!", show_alert=True)
        return
        
    new_mode = callback_query.data.split("_")[2]
    
    settings_collection.update_one(
        {"_id": "bot_mode"},
        {"$set": {"mode": new_mode}},
        upsert=True
    )
    
    await callback_query.answer(f"Mode set to {new_mode.upper()}!", show_alert=True)
    
    public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
    private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button]])
    
    await callback_query.message.edit_text(
        f"⚙️ **Bot Settings**\n\n"
        f"✅ The file upload mode is now **{new_mode.upper()}**.\n\n"
        f"Select a new mode:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^check_join_"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
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

# --- Start the Bot ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("WARNING: ADMIN_IDS is not set. The settings and admin commands will not work.")
    
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("Bot is starting...")
    app.run()
    logging.info("Bot has stopped.")
