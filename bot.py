import os
import logging
import random
import string
import time
import asyncio
import urllib.parse
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.errors import UserNotParticipant
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
    CallbackQuery, InlineQueryResultArticle,
    InputTextMessageContent
)
from pymongo import MongoClient
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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("pymongo").setLevel(logging.WARNING)

# --- Load Environment Variables ---
load_dotenv(".env")

# --- Configuration ---
try:
    API_ID = int(os.environ.get("API_ID"))
    API_HASH = os.environ.get("API_HASH")
    BOT_TOKEN = os.environ.get("BOT_TOKEN")
    MONGO_URI = os.environ.get("MONGO_URI")
    LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))
    ADMINS = [int(admin_id.strip()) for admin_id in os.environ.get("ADMINS", "").split(',') if admin_id.strip()]
    FORCE_CHANNELS = [channel.strip() for channel in os.environ.get("FORCE_CHANNELS", "").split(',') if channel.strip()]
except (ValueError, TypeError) as e:
    logging.error(f"❌ Error in environment variables: {e}")
    exit()

# --- Database Setup ---
try:
    client = MongoClient(MONGO_URI)
    db = client['file_link_bot_ultimate']
    logging.info("✅ MongoDB connected successfully!")
except Exception as e:
    logging.error(f"❌ Failed to connect to MongoDB: {e}")
    exit()

# --- Pyrogram Client ---
app = Client(
    "FileLinkBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

# --- Helper Functions ---
def generate_random_string(length=6):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

async def get_user_full_name(user):
    """Safely gets the user's full name, handling cases where it's not available."""
    if user:
        if user.first_name and user.last_name:
            return f"{user.first_name} {user.last_name}"
        return user.first_name if user.first_name else f"User_{user.id}"
    return "Unknown User"

async def is_user_member_all_channels(client: Client, user_id: int, channels: list) -> list:
    missing_channels = []
    if not channels:
        return []
    for channel in channels:
        try:
            await client.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
        except UserNotParticipant:
            missing_channels.append(channel)
        except Exception as e:
            logging.error(f"Error checking membership for {user_id} in @{channel}: {e}")
            missing_channels.append(channel)
    return missing_channels

async def get_bot_mode(db) -> str:
    setting = db.settings.find_one({"_id": "bot_mode"})
    if setting:
        return setting.get("mode", "public")
    db.settings.update_one({"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True)
    return "public"

def force_join_check(func):
    """
    Decorator to check if a user is a member of all required channels.
    """
    async def wrapper(client, message):
        if not FORCE_CHANNELS:
            return await func(client, message)
        
        user_id = message.from_user.id
        missing_channels = await is_user_member_all_channels(client, user_id, FORCE_CHANNELS)
        
        if missing_channels:
            join_buttons = [[InlineKeyboardButton(f"🔗 Join @{ch}", url=f"https://t.me/{ch}")] for ch in missing_channels]
            join_buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data="check_join_force")])
            
            await message.reply(
                "👋 **Hello!**\n\nTo use this command, you must first join the following channels:",
                reply_markup=InlineKeyboardMarkup(join_buttons),
                quote=True
            )
            return
        
        return await func(client, message)
    return wrapper

async def delete_files_after_delay(client: Client, chat_id: int, message_ids: list):
    """Deletes a list of messages after a 60-minute delay."""
    await asyncio.sleep(3600)  # Wait for 60 minutes
    try:
        await client.delete_messages(chat_id=chat_id, message_ids=message_ids)
        logging.info(f"Successfully deleted messages {message_ids} for user {chat_id}.")
    except Exception as e:
        logging.error(f"Failed to delete messages {message_ids} for user {chat_id}: {e}")

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user_name = await get_user_full_name(message.from_user)
    db.users.update_one({"_id": message.from_user.id}, {"$set": {"name": user_name}}, upsert=True)

    if len(message.command) > 1:
        file_id_str = message.command[1]
        
        # Check for force join channels associated with the specific file
        file_record = db.files.find_one({"_id": file_id_str})
        multi_file_record = db.multi_files.find_one({"_id": file_id_str})
        
        force_channels_for_file = []
        if file_record and file_record.get('force_channel'):
            force_channels_for_file.append(file_record['force_channel'])
        elif multi_file_record and multi_file_record.get('force_channel'):
            force_channels_for_file.append(multi_file_record['force_channel'])
        
        # Add global force channels if any
        all_channels_to_check = list(set(force_channels_for_file + FORCE_CHANNELS))

        missing_channels = await is_user_member_all_channels(client, message.from_user.id, all_channels_to_check)
        
        if missing_channels:
            join_buttons = [[InlineKeyboardButton(f"🔗 Join @{ch}", url=f"https://t.me/{ch}")] for ch in missing_channels]
            join_buttons.append([InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")])

            await message.reply(
                f"👋 **Hello, {user_name}!**\n\nTo access this file, you must first join the following channels:",
                reply_markup=InlineKeyboardMarkup(join_buttons),
                quote=True
            )
            return

        if file_record:
            try:
                sent_message = await client.copy_message(chat_id=message.from_user.id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                # Start timed deletion for the sent message
                asyncio.create_task(delete_files_after_delay(client, message.from_user.id, [sent_message.id]))
            except Exception as e:
                await message.reply(f"❌ An error occurred while sending the file.\n`Error: {e}`")
            return

        if multi_file_record:
            sent_message_ids = []
            for msg_id in multi_file_record['message_ids']:
                try:
                    sent_message = await client.copy_message(chat_id=message.from_user.id, from_chat_id=LOG_CHANNEL, message_id=msg_id)
                    sent_message_ids.append(sent_message.id)
                    time.sleep(0.5)
                except Exception as e:
                    logging.error(f"Error sending multi-file message {msg_id}: {e}")
            await message.reply(f"✅ All {len(sent_message_ids)} files from the bundle have been sent successfully!")
            # Start timed deletion for the sent messages
            asyncio.create_task(delete_files_after_delay(client, message.from_user.id, sent_message_ids))
            return
        
        await message.reply("🤔 File or bundle not found! The link might be wrong or expired.")
    else:
        buttons = [
            [InlineKeyboardButton("📚 About", callback_data="about"),
             InlineKeyboardButton("💡 How to Use?", callback_data="help")],
            [InlineKeyboardButton("🔗 Join Channels", callback_data="join_channels")]
        ]
        
        await message.reply(
            f"**Hello, {message.from_user.first_name}! I'm a powerful File-to-Link Bot!** 🤖\n\n"
            "Just send me any file, or a bundle of files, and I'll give you a **permanent, shareable link** for it. "
            "It's fast, secure, and super easy! ✨",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

# Command to set a force join channel for the next file upload
@app.on_message(filters.command("create_link") & filters.private)
@force_join_check
async def create_link_handler(client: Client, message: Message):
    if len(message.command) < 2:
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp"},
            {"$set": {"state": "single_link", "force_channel": None}},
            upsert=True
        )
        await message.reply("Okay! Now send me a single file to generate a link.")
        return
        
    force_channel = message.command[1].replace('@', '').strip()
    try:
        chat = await client.get_chat(force_channel)
        if chat.type != 'channel':
            await message.reply("❌ That is not a valid public channel username. Please provide a public channel username starting with '@' or just the username.")
            return
        
        await client.get_chat_member(chat_id=f"@{force_channel}", user_id=(await client.get_me()).id)
        
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp"},
            {"$set": {"state": "single_link", "force_channel": force_channel}},
            upsert=True
        )
        
        await message.reply(f"✅ Force join channel set to **@{force_channel}**. Now send me a file to get its link.")
        
    except Exception as e:
        await message.reply(f"❌ I could not find that channel or I'm not an admin there. Please make sure the channel is public and I have admin rights.\n`Error: {e}`")

@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
@force_join_check
async def file_handler(client: Client, message: Message):
    bot_mode = await get_bot_mode(db)
    if bot_mode == "private" and message.from_user.id not in ADMINS:
        await message.reply("😔 **Sorry!** Only Admins can upload files in private mode.")
        return

    user_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp"})
    
    # Handle multi_link mode
    if user_state and user_state.get("state") == "multi_link":
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp"},
            {"$push": {"message_ids": message.id}}
        )
        await message.reply("File added to bundle. Send more or use `/done` to finish.", quote=True)
        return
    
    status_msg = await message.reply("⏳ Uploading file... Please wait a moment.", quote=True)
    
    try:
        forwarded_message = await message.forward(LOG_CHANNEL)
        file_id_str = generate_random_string()
        
        # Added File Categorization
        file_name = "Untitled"
        file_type = "unknown"
        if message.document:
            file_name = message.document.file_name
            file_type = "document"
        elif message.video:
            file_name = message.video.file_name
            file_type = "video"
        elif message.photo:
            file_name = f"photo_{message.photo.file_id}.jpg"
            file_type = "photo"
        elif message.audio:
            file_name = message.audio.file_name
            file_type = "audio"

        # Check for user-set force channel
        force_channel = user_state.get("force_channel") if user_state and user_state.get("state") == "single_link" else None
        
        db.files.insert_one({
            '_id': file_id_str,
            'message_id': forwarded_message.id,
            'user_id': message.from_user.id,
            'file_name': file_name,
            'file_type': file_type,
            'force_channel': force_channel,
            'created_at': time.time()
        })
        
        db.settings.delete_one({"_id": message.from_user.id, "type": "temp"})
        
        bot_username = (await client.get_me()).username
        share_link = f"https://t.me/{bot_username}?start={file_id_str}"
        
        share_button = InlineKeyboardButton("🔗 Share Link", url=f"https://t.me/share/url?url={share_link}")
        
        await status_msg.edit_text(
            f"✅ **Link Generated Successfully!**\n\n"
            f"🔗 **Your Permanent Link:** `{share_link}`\n\n"
            f"**Note:** This link will always be active.",
            reply_markup=InlineKeyboardMarkup([[share_button]]),
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"File handling error: {e}")
        await status_msg.edit_text(f"❌ **Error!**\n\nSomething went wrong. Please try again.\n`Details: {e}`")

@app.on_message(filters.command("multi_link") & filters.private)
@force_join_check
async def multi_link_handler(client: Client, message: Message):
    if len(message.command) > 1:
        force_channel = message.command[1].replace('@', '').strip()
        try:
            chat = await client.get_chat(force_channel)
            if chat.type != 'channel':
                await message.reply("❌ That is not a valid public channel username. Please provide a public channel username.")
                return
            await client.get_chat_member(chat_id=f"@{force_channel}", user_id=(await client.get_me()).id)
            
            db.settings.update_one(
                {"_id": message.from_user.id, "type": "temp"},
                {"$set": {"state": "multi_link", "message_ids": [], "force_channel": force_channel}},
                upsert=True
            )
            await message.reply(f"✅ Force join channel set to **@{force_channel}**. Now, forward me the files you want to bundle together. When finished, send `/done`.")
            return
            
        except Exception as e:
            await message.reply(f"❌ I could not find that channel or I'm not an admin there. Please make sure the channel is public and I have admin rights.\n`Error: {e}`")
            return
    
    db.settings.update_one(
        {"_id": message.from_user.id, "type": "temp"},
        {"$set": {"state": "multi_link", "message_ids": [], "force_channel": None}},
        upsert=True
    )
    
    await message.reply("Okay! Now, forward me the files you want to bundle together. When you're finished, send the command `/done`.")

@app.on_message(filters.command("done") & filters.private)
@force_join_check
async def done_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_state = db.settings.find_one({"_id": user_id, "type": "temp"})
    
    if user_state and user_state.get("state") == "multi_link":
        message_ids = user_state.get("message_ids", [])
        if not message_ids:
            await message.reply("You haven't added any files to the bundle yet. Please forward them first.")
            return
            
        status_msg = await message.reply(f"⏳ Processing {len(message_ids)} files... Please wait.")
        
        try:
            forwarded_msg_ids = []
            for msg_id in message_ids:
                try:
                    forwarded_msg = await client.copy_message(chat_id=LOG_CHANNEL, from_chat_id=user_id, message_id=msg_id)
                    forwarded_msg_ids.append(forwarded_msg.id)
                except Exception as e:
                    logging.error(f"Error forwarding message {msg_id}: {e}")
            
            multi_file_id = generate_random_string(8)
            force_channel = user_state.get("force_channel")
            db.multi_files.insert_one({
                '_id': multi_file_id, 
                'message_ids': forwarded_msg_ids,
                'force_channel': force_channel,
                'created_at': time.time()
            })
            
            bot_username = (await client.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={multi_file_id}"
            
            share_button = InlineKeyboardButton("🔗 Share Link", url=f"https://t.me/share/url?url={share_link}")
            
            await status_msg.edit_text(
                f"✅ **Multi-File Link Generated!**\n\n"
                f"**This link contains {len(message_ids)} files.**\n\n"
                f"🔗 **Your Permanent Link:** `{share_link}`",
                reply_markup=InlineKeyboardMarkup([[share_button]]),
                disable_web_page_preview=True
            )
            
            db.settings.delete_one({"_id": user_id, "type": "temp"})
            
        except Exception as e:
            logging.error(f"Multi-file handling error: {e}")
            await status_msg.edit_text(f"❌ **Error!**\n\nSomething went wrong. Please try again.\n`Details: {e}`")
    else:
        await message.reply("You are not in multi-link mode. Send `/multi_link` to start.")

@app.on_message(filters.command("myfiles") & filters.private)
async def my_files_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_files = list(db.files.find({"user_id": user_id}).sort("created_at", -1).limit(10))
    
    if not user_files:
        await message.reply("You haven't uploaded any files yet.")
        return

    text = "📂 **Your Recently Uploaded Files:**\n\n"
    for i, file_record in enumerate(user_files):
        file_name = file_record.get('file_name', 'Unnamed File')
        file_id_str = file_record['_id']
        bot_username = (await client.get_me()).username
        share_link = f"https://t.me/{bot_username}?start={file_id_str}"
        
        text += f"**{i+1}.** [{file_name}]({share_link})\n"
        
    text += "\n_Only your last 10 files are shown._"
    
    await message.reply(text, disable_web_page_preview=True)

@app.on_message(filters.command("delete") & filters.private)
async def delete_file_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("Please provide the file link or ID to delete. Example: `/delete abcdef`")
        return

    file_id_str = message.command[1]
    file_record = db.files.find_one({"_id": file_id_str, "user_id": message.from_user.id})

    if not file_record:
        await message.reply("🤔 File not found or you don't have permission to delete it.")
        return

    delete_button = InlineKeyboardButton("Confirm Delete", callback_data=f"confirm_delete_{file_id_str}")
    cancel_button = InlineKeyboardButton("Cancel", callback_data="cancel_delete")
    keyboard = InlineKeyboardMarkup([[delete_button, cancel_button]])

    await message.reply(
        f"Are you sure you want to delete the file **`{file_record.get('file_name', 'Unnamed File')}`**?",
        reply_markup=keyboard,
        quote=True
    )

@app.on_message(filters.command("admin") & filters.private & filters.user(ADMINS))
async def admin_panel_handler(client: Client, message: Message):
    buttons = [
        [InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
         InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")]
    ]
    await message.reply(
        "**👑 Welcome to the Admin Panel!**\n\n"
        "Select an option below to manage the bot.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_message(filters.command("stats") & filters.private & filters.user(ADMINS))
async def stats_handler(client: Client, message: Message):
    user_count = db.users.count_documents({})
    single_files_count = db.files.count_documents({})
    multi_files_count = db.multi_files.count_documents({})
    
    # Detailed stats added
    today_start = time.time() - (24 * 60 * 60)
    today_users = db.users.count_documents({"created_at": {"$gte": today_start}})
    today_files = db.files.count_documents({"created_at": {"$gte": today_start}})

    file_types = db.files.aggregate([{"$group": {"_id": "$file_type", "count": {"$sum": 1}}}])
    file_types_text = "\n".join([f"  • {ft['_id'].capitalize()}: {ft['count']}" for ft in file_types])
    
    await message.reply(
        f"📊 **Bot Statistics**\n\n"
        f"**👥 Total Users:** `{user_count}`\n"
        f"**🗓️ Today's Users:** `{today_users}`\n"
        f"**📄 Single Files:** `{single_files_count}`\n"
        f"**📦 Multi-File Bundles:** `{multi_files_count}`\n"
        f"**🗓️ Today's Files:** `{today_files}`\n\n"
        f"**📁 Files by Type:**\n"
        f"{file_types_text}"
    )

@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMINS))
async def broadcast_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("Please provide a message to broadcast. Example: `/broadcast Hello everyone!`")
        return

    message_to_send = message.text.split(" ", 1)[1]
    users = db.users.find({}, {"_id": 1})
    
    success_count = 0
    failed_count = 0
    
    status_msg = await message.reply("⏳ Starting broadcast...")
    
    for user in users:
        try:
            await client.send_message(chat_id=user['_id'], text=message_to_send)
            success_count += 1
            time.sleep(0.1)
        except Exception as e:
            failed_count += 1
            logging.error(f"Failed to broadcast to user {user['_id']}: {e}")
    
    await status_msg.edit_text(
        f"✅ **Broadcast Complete!**\n\n"
        f"**Success:** `{success_count}`\n"
        f"**Failed:** `{failed_count}`"
    )

@app.on_message(filters.command("settings") & filters.private & filters.user(ADMINS))
async def settings_handler(client: Client, message: Message):
    current_mode = await get_bot_mode(db)
    
    public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
    private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button]])
    
    await message.reply(
        f"⚙️ **Bot Settings**\n\n"
        f"The current file upload mode is **{current_mode.upper()}**.\n\n"
        f"**Public:** Anyone can upload files and get a link.\n"
        f"**Private:** Only admins can upload files.\n\n"
        f"Select a new mode below:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex("^(about|help|join_channels|start_menu)$"))
async def general_callback_handler(client: Client, callback_query: CallbackQuery):
    query = callback_query.data
    
    if query == "about":
        await callback_query.message.edit_text(
            "📚 **About Me**\n\n"
            "I'm a powerful bot designed to help you create permanent shareable links for your files.\n\n"
            "**Key Features:**\n"
            "✨ **File-to-Link:** Convert any file into a unique link.\n"
            "📦 **Multi-File Bundles:** Generate a single link for multiple files.\n"
            "🔒 **Secure:** Your files are stored securely.\n"
            "🚀 **Fast & Reliable:** Get your link in seconds.\n"
            "🔗 **Permanent:** Links won't expire.\n\n"
            "Made with ❤️ by [Your Name or Bot Creator's Name].",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")]])
        )
    elif query == "help":
        await callback_query.message.edit_text(
            "💡 **How to Use?**\n\n"
            "1. **Single File:** Send me any document, photo, video, or audio file.\n"
            "2. **Multi-File Bundle:** Use the command `/multi_link` and then forward me a series of files. Send `/done` when you're finished.\n\n"
            "3. **Get Your Link:** I will instantly process your file(s) and reply with a unique link.\n"
            "4. **Share:** You can share this link with anyone! When they click it, the file(s) will be sent directly to them.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")]])
        )
    elif query == "join_channels":
        join_buttons = [[InlineKeyboardButton(f"🔗 Join @{ch}", url=f"https://t.me/{ch}")] for ch in FORCE_CHANNELS]
        join_buttons.append([InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")])
        
        await callback_query.message.edit_text(
            "To unlock all features and support the creators, please join our channels:",
            reply_markup=InlineKeyboardMarkup(join_buttons)
        )
    elif query == "start_menu":
        user_name = await get_user_full_name(callback_query.from_user)
        buttons = [
            [InlineKeyboardButton("📚 About", callback_data="about"),
             InlineKeyboardButton("💡 How to Use?", callback_data="help")],
            [InlineKeyboardButton("🔗 Join Channels", callback_data="join_channels")]
        ]
        await callback_query.message.edit_text(
            f"**Hello, {user_name}! I'm a powerful File-to-Link Bot!** 🤖\n\n"
            "Just send me any file, and I'll give you a **permanent, shareable link** for it. "
            "It's fast, secure, and super easy! ✨",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^check_join_"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    file_id_str = callback_query.data.split("_", 2)[2]

    file_record = db.files.find_one({"_id": file_id_str})
    multi_file_record = db.multi_files.find_one({"_id": file_id_str})

    force_channels_for_file = []
    if file_record and file_record.get('force_channel'):
        force_channels_for_file.append(file_record['force_channel'])
    elif multi_file_record and multi_file_record.get('force_channel'):
        force_channels_for_file.append(multi_file_record['force_channel'])
    
    all_channels_to_check = list(set(force_channels_for_file + FORCE_CHANNELS))
    missing_channels = await is_user_member_all_channels(client, user_id, all_channels_to_check)

    if not missing_channels:
        await callback_query.answer("Thanks for joining! Sending files now...", show_alert=True)
        
        if file_record:
            try:
                sent_message = await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                await callback_query.message.delete()
                # Start timed deletion for the sent message
                asyncio.create_task(delete_files_after_delay(client, user_id, [sent_message.id]))
            except Exception as e:
                await callback_query.message.edit_text(f"❌ An error occurred while sending the file.\n`Error: {e}`")
        
        if multi_file_record:
            sent_message_ids = []
            for msg_id in multi_file_record['message_ids']:
                try:
                    sent_message = await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=msg_id)
                    sent_message_ids.append(sent_message.id)
                    time.sleep(0.5)
                except Exception as e:
                    logging.error(f"Error sending multi-file message {msg_id}: {e}")
            await callback_query.message.edit_text(f"✅ All {len(sent_message_ids)} files from the bundle have been sent successfully!")
            asyncio.create_task(delete_files_after_delay(client, user_id, sent_message_ids))
    else:
        await callback_query.answer("You have not joined all the channels. Please join them and try again.", show_alert=True)
        join_buttons = [[InlineKeyboardButton(f"🔗 Join @{ch}", url=f"https://t.me/{ch}")] for ch in missing_channels]
        join_buttons.append([InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")])
        keyboard = InlineKeyboardMarkup(join_buttons)
        await callback_query.message.edit_text(
            f"Please join the remaining channels to continue:",
            reply_markup=keyboard
        )

@app.on_callback_query(filters.regex(r"^set_mode_"))
async def set_mode_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id not in ADMINS:
        await callback_query.answer("Permission Denied!", show_alert=True)
        return
        
    new_mode = callback_query.data.split("_")[2]
    
    db.settings.update_one(
        {"_id": "bot_mode"},
        {"$set": {"mode": new_mode}},
        upsert=True
    )
    
    await callback_query.answer(f"Mode successfully set to {new_mode.upper()}!", show_alert=True)
    
    public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
    private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
    keyboard = InlineKeyboardMarkup([[public_button], [private_button]])
    
    await callback_query.message.edit_text(
        f"⚙️ **Bot Settings**\n\n"
        f"✅ File upload mode is now **{new_mode.upper()}**.\n\n"
        f"Select a new mode:",
        reply_markup=keyboard
    )
    
@app.on_callback_query(filters.regex(r"^confirm_delete_"))
async def confirm_delete_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    file_id_str = callback_query.data.split("_", 2)[2]

    file_record = db.files.find_one({"_id": file_id_str, "user_id": user_id})

    if not file_record:
        await callback_query.answer("File not found or already deleted.", show_alert=True)
        await callback_query.message.edit_text("❌ File could not be deleted. It might be a bad link or already gone.")
        return

    try:
        # Delete from log channel
        await client.delete_messages(chat_id=LOG_CHANNEL, message_ids=file_record['message_id'])
        
        # Delete from database
        db.files.delete_one({"_id": file_id_str})

        await callback_query.answer("File deleted successfully!", show_alert=True)
        await callback_query.message.edit_text("✅ File has been permanently deleted.")
    except Exception as e:
        logging.error(f"Failed to delete file {file_id_str}: {e}")
        await callback_query.answer("An error occurred while deleting the file.", show_alert=True)
        await callback_query.message.edit_text("❌ An error occurred while trying to delete the file. Please try again later.")

@app.on_callback_query(filters.regex(r"^cancel_delete"))
async def cancel_delete_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer("Deletion cancelled.", show_alert=True)
    await callback_query.message.edit_text("🗑️ Deletion cancelled. Your file is safe.")

@app.on_inline_query()
async def inline_search(client, inline_query):
    query = inline_query.query.strip().lower()
    
    if not query:
        results = [
            InlineQueryResultArticle(
                title="Search for a file",
                description="Type a filename or keyword to find a link.",
                input_message_content=InputTextMessageContent(
                    message_text="🤔 Search for a file here!"
                )
            )
        ]
        await client.answer_inline_query(inline_query.id, results)
        return

    files_found = db.files.find(
        {"file_name": {"$regex": query, "$options": "i"}}
    ).limit(15)

    articles = []
    bot_username = (await client.get_me()).username
    
    for file_record in files_found:
        file_name = file_record.get('file_name', 'Unnamed File')
        file_id_str = file_record['_id']
        share_link = f"https://t.me/{bot_username}?start={file_id_str}"
        
        articles.append(
            InlineQueryResultArticle(
                title=file_name,
                description="Click to share the permanent link for this file.",
                input_message_content=InputTextMessageContent(
                    message_text=f"🔗 **Here is your file link:** `{share_link}`",
                    disable_web_page_preview=True
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Share Link", url=f"https://t.me/share/url?url={share_link}")]])
            )
        )

    await client.answer_inline_query(
        inline_query.id,
        results=articles,
        cache_time=5
    )

# --- Main Bot Runner ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("⚠️ WARNING: ADMINS is not set. Admin commands will not work.")
    if not FORCE_CHANNELS:
        logging.warning("⚠️ WARNING: FORCE_CHANNELS is not set. Force join feature will be disabled.")
        
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("Bot is starting...")
    app.start()
    idle()
    app.stop()
    logging.info("Bot has stopped.")
