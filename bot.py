import os
import logging
import random
import string
import time
import asyncio
import urllib.parse
from dotenv import load_dotenv
from pyrogram import Client, filters, idle
from pyrogram.errors import UserNotParticipant, ChatAdminRequired
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message,
    CallbackQuery, InlineQueryResultArticle,
    InputTextMessageContent, ChatPermissions
)
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from flask import Flask
from threading import Thread
from datetime import datetime, timedelta

# --- Flask Web Server (To keep the bot alive) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive! 🚀", 200

def run_flask():
    """Runs the Flask web server."""
    port = int(os.environ.get('PORT', 8080))
    # Use Threaded to handle multiple requests smoothly
    flask_app.run(host='0.0.0.0', port=port, threaded=True)

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
    GROUP_LOG_CHANNEL = int(os.environ.get("GROUP_LOG_CHANNEL")) 
    OWNER_ID = int(os.environ.get("OWNER_ID", "7524032836")) # OWNER_ID should be from env if possible
    
    # Safely parse ADMINS, defaulting to OWNER_ID if not set
    admin_list = os.environ.get("ADMINS", str(OWNER_ID)).split(',')
    ADMINS = [OWNER_ID] + [int(admin_id.strip()) for admin_id in admin_list if admin_id.strip() and admin_id.strip().isdigit()]
    ADMINS = list(set(ADMINS)) # Remove duplicates
    
    FORCE_CHANNELS = [channel.strip() for channel in os.environ.get("FORCE_CHANNELS", "").split(',') if channel.strip()]
    
    BADWORDS = [word.strip() for word in os.environ.get("BADWORDS", "fuck,bitch,asshole").lower().split(',') if word.strip()]
    MAX_WARNINGS = int(os.environ.get("MAX_WARNINGS", 3))
    
except (ValueError, TypeError) as e:
    logging.error(f"❌ Environment variables configuration error: {e}")
    exit()

# --- Database Setup ---
try:
    # Use serverSelectionTimeoutMS for quick fail on connection issues
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client['file_link_bot_ultimate']
    # The ismaster command is cheap and does not requires auth.
    client.admin.command('ismaster') 
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

# --- Helper Functions (Updated and Enhanced) ---

def generate_random_string(length=8):
    """Generates a longer and more unique random string."""
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

async def get_unique_id(collection):
    """Generates a unique ID and checks if it exists in the collection to avoid DuplicateKeyError."""
    # Use a slightly longer ID for better uniqueness
    for _ in range(10): # Try a few times
        random_id = generate_random_string()
        if collection.find_one({"_id": random_id}) is None:
            return random_id
        await asyncio.sleep(0.01) # Small pause
    raise Exception("Failed to generate unique ID after multiple attempts.")

async def get_user_full_name(user):
    """Safely gets the user's full name, prioritizing First Name."""
    if user:
        full_name = user.first_name if user.first_name else ""
        if user.last_name:
            full_name += f" {user.last_name}"
        return full_name.strip() if full_name else f"User_{user.id}"
    return "Unknown User"

async def is_user_member_all_channels(client: Client, user_id: int, channels: list) -> list:
    """Checks user membership in a list of channels and returns missing ones."""
    missing_channels = []
    if not channels:
        return []
    for channel in channels:
        try:
            # Check if the chat exists before checking membership
            chat = await client.get_chat(chat_id=f"@{channel}")
            if chat.username and chat.username.lower() == channel.lower():
                 member = await client.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
                 if member.status in ["kicked", "left"]:
                     missing_channels.append(channel)
        except UserNotParticipant:
            missing_channels.append(channel)
        except Exception as e:
            # Only log severe errors, not common ones like chat not found
            if "CHAT_NOT_FOUND" not in str(e):
                 logging.error(f"Error checking membership for {user_id} in @{channel}: {e}")
            missing_channels.append(channel)
    return list(set(missing_channels)) # Return unique missing channels

async def get_bot_mode(db) -> str:
    """Fetches the current bot operation mode."""
    setting = db.settings.find_one({"_id": "bot_mode"})
    if setting:
        return setting.get("mode", "public")
    db.settings.update_one({"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True)
    return "public"

def force_join_check(func):
    """
    Decorator to check if a user is a member of all required channels.
    This is improved to handle complex deep-linking scenarios.
    """
    async def wrapper(client, message):
        user_id = message.from_user.id
        
        # 1. Check Global Force Channels
        all_channels_to_check = list(FORCE_CHANNELS)
        
        # 2. Check File-Specific Force Channels (for deep links in text)
        file_id_str = None
        if isinstance(message, Message) and message.text:
            parsed_url = urllib.parse.urlparse(message.text)
            if parsed_url.query:
                file_id_str = urllib.parse.parse_qs(parsed_url.query).get('start', [None])[0]
        
        # Also check for direct command parameter for /create_link or /multi_link
        if isinstance(message, Message) and message.command and len(message.command) > 1 and message.command[0] in ["create_link", "multi_link", "set_thumbnail"]:
             # Do not apply force join check on the command itself, let the command handler check the channel
             pass
        elif file_id_str:
            file_record = db.files.find_one({"_id": file_id_str})
            multi_file_record = db.multi_files.find_one({"_id": file_id_str})
            
            if file_record and file_record.get('force_channel'):
                all_channels_to_check.append(file_record['force_channel'])
            elif multi_file_record and multi_file_record.get('force_channel'):
                all_channels_to_check.append(multi_file_record['force_channel'])
        
        all_channels_to_check = list(set(all_channels_to_check))
        missing_channels = await is_user_member_all_channels(client, user_id, all_channels_to_check)
        
        if missing_channels:
            join_buttons = [[InlineKeyboardButton(f"🔗 Join @{ch}", url=f"https://t.me/{ch}")] for ch in missing_channels]
            # Use 'check_join_force' only if it's a generic command/file. If file_id_str exists, use it.
            callback_data = f"check_join_{file_id_str}" if file_id_str else "check_join_force"
            join_buttons.append([InlineKeyboardButton("🔄 Try Again", callback_data=callback_data)])
            
            await message.reply(
                "🛑 **ACCESS DENIED** 🛑\n\n"
                "To use this feature, you must first join the following required channels:",
                reply_markup=InlineKeyboardMarkup(join_buttons),
                quote=True
            )
            return
        
        # Before returning, update user activity
        db.users.update_one(
             {"_id": user_id},
             {"$set": {"last_activity": datetime.utcnow()}},
             upsert=True
        )
        
        return await func(client, message)
    return wrapper

async def delete_files_after_delay(client: Client, chat_id: int, message_ids: list):
    """Deletes a list of messages after a 60-minute delay."""
    await asyncio.sleep(3600)  # Wait for 60 minutes (1 hour)
    try:
        await client.delete_messages(chat_id=chat_id, message_ids=message_ids)
        logging.info(f"Successfully auto-deleted messages {message_ids} for user {chat_id}.")
    except Exception as e:
        # Ignore "Message not found" errors
        if "MESSAGE_NOT_FOUND" not in str(e):
            logging.error(f"Failed to auto-delete messages {message_ids} for user {chat_id}: {e}")

# --- Bot Command Handlers (Updated for Style and Logic) ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_name = await get_user_full_name(message.from_user)
    
    # Track user and last activity
    db.users.update_one(
        {"_id": user_id}, 
        {"$set": {"name": user_name, "last_activity": datetime.utcnow()}}, 
        upsert=True
    )

    if len(message.command) > 1:
        file_id_str = message.command[1]
        
        file_record = db.files.find_one({"_id": file_id_str})
        multi_file_record = db.multi_files.find_one({"_id": file_id_str})
        
        # Check all required channels (global + file-specific)
        force_channels_for_file = []
        if file_record and file_record.get('force_channel'):
            force_channels_for_file.append(file_record['force_channel'])
        elif multi_file_record and multi_file_record.get('force_channel'):
            force_channels_for_file.append(multi_file_record['force_channel'])
        
        all_channels_to_check = list(set(force_channels_for_file + FORCE_CHANNELS))

        missing_channels = await is_user_member_all_channels(client, user_id, all_channels_to_check)
        
        if missing_channels:
            join_buttons = [[InlineKeyboardButton(f"🔗 Join @{ch}", url=f"https://t.me/{ch}")] for ch in missing_channels]
            join_buttons.append([InlineKeyboardButton("✅ I Have Joined! (Try Again)", callback_data=f"check_join_{file_id_str}")])

            await message.reply(
                f"👋 **Hello, {message.from_user.first_name}!**\n\n"
                "To unlock the file, you must first join the following required channels:",
                reply_markup=InlineKeyboardMarkup(join_buttons),
                quote=True
            )
            return

        # If user is a member, send the file(s)
        if file_record:
            try:
                sent_message = await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                await message.reply("🎉 **File Unlocked!** It will be auto-deleted in **60 minutes** to save space.", quote=True)
                asyncio.create_task(delete_files_after_delay(client, user_id, [sent_message.id]))
            except Exception as e:
                await message.reply(f"❌ An error occurred while sending the file.\n`Error: {e}`")
            return

        if multi_file_record:
            sent_message_ids = []
            file_title = multi_file_record.get('file_name', f"Bundle of {len(multi_file_record['message_ids'])} Files")
            
            # Send a confirmation message first
            await message.reply(f"📦 **Bundle Unlocked!** Sending **{file_title}** now. This will be auto-deleted in **60 minutes**.", quote=True)

            for msg_id in multi_file_record['message_ids']:
                try:
                    sent_message = await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=msg_id)
                    sent_message_ids.append(sent_message.id)
                    await asyncio.sleep(0.5) # Throttle to prevent flooding
                except Exception as e:
                    logging.error(f"Error sending multi-file message {msg_id}: {e}")
            
            asyncio.create_task(delete_files_after_delay(client, user_id, sent_message_ids))
            return
        
        await message.reply("🤔 **File/Bundle Not Found!** The link might be wrong, expired, or deleted by the owner.")
    else:
        # Standard /start message
        buttons = [
            [InlineKeyboardButton("📚 About This Bot", callback_data="about"),
             InlineKeyboardButton("💡 How to Use?", callback_data="help")],
            [InlineKeyboardButton("⚙️ My Files & Settings", callback_data="my_files_menu")]
        ]
        
        start_photo_id_doc = db.settings.find_one({"_id": "start_photo"})
        start_photo_id = start_photo_id_doc.get("file_id") if start_photo_id_doc else None

        caption_text = (
            f"**Hello, {message.from_user.first_name}! I'm FileLinker Bot!** 🤖\n\n"
            "I convert your files into **permanent, shareable links**."
            " Just send me a file or start a bundle with `/multi_link`! ✨"
        )
        
        if start_photo_id:
            await message.reply_photo(
                photo=start_photo_id,
                caption=caption_text,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        else:
            await message.reply(
                caption_text,
                reply_markup=InlineKeyboardMarkup(buttons)
            )

@app.on_message(filters.command("help") & filters.private)
async def help_handler_private(client: Client, message: Message):
    text = (
        "💡 **FileLinker Bot Usage Guide**\n\n"
        "**1. Single File Link:**\n"
        "   - Send me any file (document, video, photo, audio).\n"
        "   - **Custom Force Join:** Use `/create_link <channel_username>` then send the file.\n\n"
        "**2. Multi-File Bundle Link:**\n"
        "   - Start the bundle: `/multi_link [Title for bundle]`\n"
        "   - Forward all your files to me.\n"
        "   - Finish: Send `/done`.\n"
        "   - **Custom Force Join:** Use `/multi_link <channel_username> [Title]`\n\n"
        "**3. Set Thumbnail (New! 🖼️):**\n"
        "   - Reply to a photo with: `/set_thumbnail`\n"
        "   - The next file or bundle will use that photo as its thumbnail.\n\n"
        "**4. Management:**\n"
        "   - **My Files:** `/myfiles` (View your last 10 uploads).\n"
        "   - **Delete:** `/delete <file_id>` (Permanently delete your file/bundle).\n\n"
        "**5. Inline Search (Everywhere):**\n"
        "   - In any chat, type: `@{(await client.get_me()).username} <file_name>` to search and share links instantly!"
    )
    await message.reply(text, disable_web_page_preview=True)

@app.on_message(filters.command("help") & filters.group)
async def help_handler_group(client: Client, message: Message):
    text = (
        "💡 **How to Use Me in This Group**\n\n"
        "I'm a file-linking bot! My primary function is in private chat. To share files:\n\n"
        "1.  **Start Private Chat:** Click here: [Start FileLinker Bot](https://t.me/{(await client.get_me()).username})\n"
        "2.  **Upload/Bundle Files:** Send me your files in private chat to get a permanent link.\n"
        "3.  **Share the Link:** Post the link in this group.\n\n"
        "**Inline Search:** You can search for files directly here by typing `@{(await client.get_me()).username} <file_name>`."
    )
    await message.reply(text, disable_web_page_preview=True)

@app.on_message(filters.private & filters.user(ADMINS) & filters.photo & filters.caption("set_start_photo", prefixes="/"))
async def set_start_photo_handler(client: Client, message: Message):
    """Sets a new photo for the /start command."""
    file_id = message.photo.file_id
    db.settings.update_one(
        {"_id": "start_photo"},
        {"$set": {"file_id": file_id}},
        upsert=True
    )
    await message.reply("✅ The new **start photo** has been set successfully!")

@app.on_message(filters.command("create_link") & filters.private)
@force_join_check
async def create_link_handler(client: Client, message: Message):
    if len(message.command) < 2 or (len(message.command) == 2 and not message.command[1].startswith('@')):
        # Only command or command with a title
        force_channel = None
        
        # If there's a title, save it
        if len(message.command) > 1 and not message.command[1].startswith('@'):
            file_name = " ".join(message.command[1:])
        else:
            file_name = None
            
        # Preserve thumbnail ID if it exists
        user_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp_link"})
        thumbnail_id = user_state.get("thumbnail_id") if user_state else None
            
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp_link"},
            {"$set": {"state": "single_link", "force_channel": None, "file_name": file_name, "thumbnail_id": thumbnail_id}},
            upsert=True
        )
        await message.reply("Okay! Now send me a **single file** to generate a link.")
        return
        
    # Command with a channel username
    force_channel = message.command[1].replace('@', '').strip()
    file_name = " ".join(message.command[2:]) if len(message.command) > 2 else None
    
    try:
        chat = await client.get_chat(force_channel)
        if chat.type != 'channel':
            await message.reply("❌ That is not a valid **public channel username**. Please provide a public channel username.")
            return
        
        # Bot must be a member
        await client.get_chat_member(chat_id=f"@{force_channel}", user_id=(await client.get_me()).id)
        
        # Preserve thumbnail ID if it exists
        user_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp_link"})
        thumbnail_id = user_state.get("thumbnail_id") if user_state else None
        
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp_link"},
            {"$set": {"state": "single_link", "force_channel": force_channel, "file_name": file_name, "thumbnail_id": thumbnail_id}},
            upsert=True
        )
        
        await message.reply(f"✅ Force join channel set to **@{force_channel}**. Now send me a **file** to get its link.")
        
    except ChatAdminRequired:
         await message.reply("❌ I'm not an admin in that channel. Please check my permissions.")
    except Exception as e:
        await message.reply(f"❌ I could not find that channel or I'm not a member there. Please make sure the channel is public and I have access.\n`Error: {e}`")

# --- NEW: Set Thumbnail Handler ---
@app.on_message(filters.command("set_thumbnail") & filters.private)
@force_join_check
async def set_thumbnail_handler(client: Client, message: Message):
    """Sets a temporary thumbnail photo ID for the next file or bundle."""
    
    # Check if a photo is replied to or sent with the command
    if not message.reply_to_message or not message.reply_to_message.photo:
        await message.reply(
            "🖼️ **थंबनेल सेट करें**\n\n"
            "कृपया उस **फोटो** पर रिप्लाई (reply) करें जिसे आप अगले अपलोड के लिए थंबनेल के रूप में उपयोग करना चाहते हैं।\n"
            "फिर `/set_thumbnail` भेजें।"
        )
        return
        
    thumbnail_id = message.reply_to_message.photo.file_id
    
    # Save the thumbnail ID in the user's temporary state
    # We use 'single_link' as a default state for a fresh temp_link entry, but it's mainly for thumbnail storage
    db.settings.update_one(
        {"_id": message.from_user.id, "type": "temp_link"},
        {"$set": {"thumbnail_id": thumbnail_id, "state": "single_link"}}, # Set state to single link for clarity
        upsert=True
    )
    
    await message.reply("✅ **थंबनेल सेट हो गया!**\n\n"
                        "अगली फ़ाइल जो आप भेजेंगे (या अगले `/multi_link` बंडल) में यही थंबनेल इस्तेमाल होगा।")
# ---------------------------------

@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
@force_join_check
async def file_handler(client: Client, message: Message):
    bot_mode = await get_bot_mode(db)
    if bot_mode == "private" and message.from_user.id not in ADMINS:
        await message.reply("😔 **Bot is in Private Mode!** Only Admins can upload files right now.")
        return

    user_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp_link"})
    
    # Get thumbnail ID from state
    thumbnail_id = user_state.get("thumbnail_id") if user_state else None
    
    # Handle multi-link mode (file added to bundle)
    if user_state and user_state.get("state") == "multi_link":
        
        # Check if the file is too large for the bot to handle (Pyrogram limit or custom limit)
        if message.video and message.video.file_size > (2 * 1024 * 1024 * 1024): # Example: 2GB limit
             await message.reply("⚠️ File is too large to be added to the bundle. Max limit is 2GB.", quote=True)
             return
             
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp_link"},
            {"$push": {"message_ids": message.id}}
        )
        
        # Update file count in state for better user feedback
        new_count = len(user_state.get("message_ids", [])) + 1
        db.settings.update_one(
            {"_id": message.from_user.id, "type": "temp_link"},
            {"$set": {"current_count": new_count}}
        )
        
        await message.reply(f"📦 File **#{new_count}** added to the bundle. Send more or use `/done` to finish.", quote=True)
        return
    
    # Handle single file link generation
    status_msg = await message.reply("⏳ **Processing File...** Please wait while I create your link. 🔗", quote=True)
    
    try:
        # Get original message details
        original_message = message
        
        # 'forward' के बजाय 'copy' का उपयोग करें और thumbnail जोड़ें
        # Pyrogram केवल Document, Video और Audio के लिए thumbnail_id को सपोर्ट करता है
        forwarded_message = await client.copy_message( 
            chat_id=LOG_CHANNEL, 
            from_chat_id=message.chat.id, 
            message_id=message.id,
            caption=original_message.caption,
            reply_markup=original_message.reply_markup,
            # यदि यह एक Document, Video, या Audio है और थंबनेल सेट है, तो उसका उपयोग करें
            **({'thumb': thumbnail_id} if thumbnail_id and (original_message.document or original_message.video or original_message.audio) else {}) 
        ) # <--- यह ब्लॉक अपडेट करें
        
        file_id_str = await get_unique_id(db.files) 
        
        # Determine file name and type
        file_name = "Untitled"
        file_type = "unknown"
        if message.document:
            file_name = message.document.file_name or "Document"
            file_type = "document"
        elif message.video:
            file_name = message.video.file_name or "Video"
            file_type = "video"
        elif message.photo:
            file_name = message.caption or f"Photo_{forwarded_message.id}"
            file_type = "photo"
        elif message.audio:
            file_name = message.audio.title or "Audio"
            file_type = "audio"
            
        # Check for custom name from /create_link
        if user_state and user_state.get("file_name"):
            file_name = user_state["file_name"]
            
        force_channel = user_state.get("force_channel") if user_state and user_state.get("state") == "single_link" else None
        
        # Insert file record
        db.files.insert_one({
            '_id': file_id_str,
            'message_id': forwarded_message.id,
            'user_id': message.from_user.id,
            'file_name': file_name,
            'file_type': file_type,
            'force_channel': force_channel,
            'created_at': datetime.utcnow()
        })
        
        # Clean up temporary state: This also deletes the temporary 'thumbnail_id'
        db.settings.delete_one({"_id": message.from_user.id, "type": "temp_link"})
        
        bot_username = (await client.get_me()).username
        share_link = f"https://t.me/{bot_username}?start={file_id_str}"
        
        share_button = InlineKeyboardButton("📤 Share Link", url=f"https://t.me/share/url?url={urllib.parse.quote(f'File: {file_name}\nLink: {share_link}')}")
        
        reply_text = (
            f"🎉 **Link Generated Successfully!** 🎉\n\n"
            f"**🗂️ File Name:** `{file_name}`\n"
            f"**🔗 Permanent Link:** `{share_link}`\n\n"
            f"**Note:** Share this link anywhere, and the file will be delivered directly from the bot!"
        )
        
        if force_channel:
            reply_text += f"\n\n🔒 **Access Condition:** User must join **@{force_channel}**."
        
        if thumbnail_id:
            reply_text += "\n\n🖼️ **Custom thumbnail applied!**"
            
        await status_msg.edit_text(
            reply_text,
            reply_markup=InlineKeyboardMarkup([[share_button]]),
            disable_web_page_preview=True
        )
        
        # Log the action
        log_text = (
            f"🆕 **New Single File Link**\n"
            f"• **User:** {await get_user_full_name(message.from_user)} (`{message.from_user.id}`)\n"
            f"• **File:** `{file_name}`"
        )
        if thumbnail_id:
             log_text += " (🖼️ Custom Thumb)"
        log_text += f"\n• **Link:** `t.me/{bot_username}?start={file_id_str}`"
        
        await client.send_message(LOG_CHANNEL, log_text)

    except Exception as e:
        logging.error(f"Single file handling error: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ **Error!**\n\nSomething went wrong while processing the file. Please try again.\n`Details: {e}`")


@app.on_message(filters.command("multi_link") & filters.private)
@force_join_check
async def multi_link_handler(client: Client, message: Message):
    # Parse command for force channel and custom title
    command_parts = message.command[1:]
    force_channel = None
    file_name = None

    if command_parts:
        if command_parts[0].startswith('@'):
            force_channel = command_parts[0].replace('@', '').strip()
            file_name = " ".join(command_parts[1:])
        else:
            file_name = " ".join(command_parts)
    
    # Preserve thumbnail ID if it exists
    user_state = db.settings.find_one({"_id": message.from_user.id, "type": "temp_link"})
    thumbnail_id = user_state.get("thumbnail_id") if user_state else None
    
    if force_channel:
        try:
            chat = await client.get_chat(force_channel)
            if chat.type != 'channel':
                await message.reply("❌ That is not a valid **public channel username**.")
                return
            await client.get_chat_member(chat_id=f"@{force_channel}", user_id=(await client.get_me()).id)
            
            # Save state with force channel and existing thumbnail ID
            db.settings.update_one(
                {"_id": message.from_user.id, "type": "temp_link"},
                {"$set": {"state": "multi_link", "message_ids": [], "force_channel": force_channel, "file_name": file_name, "thumbnail_id": thumbnail_id}},
                upsert=True
            )
            await message.reply(f"✅ Force join channel set to **@{force_channel}**. Now, forward files for the bundle. Send `/done` to finish.")
            return
            
        except ChatAdminRequired:
            await message.reply("❌ I'm not an admin in that channel. Please check my permissions.")
            return
        except Exception as e:
            await message.reply(f"❌ I could not find that channel or I'm not a member there. Please check the username.\n`Error: {e}`")
            return

    # No force channel, just multi-link mode setup
    db.settings.update_one(
        {"_id": message.from_user.id, "type": "temp_link"},
        {"$set": {"state": "multi_link", "message_ids": [], "force_channel": None, "file_name": file_name, "thumbnail_id": thumbnail_id}},
        upsert=True
    )
    
    reply_text = (
        "📦 **Multi-File Bundle Mode Activated!**\n\n"
        "Now, **forward** me all the files you want to bundle together. "
        "When you're finished, send the command `/done`."
    )
    
    if thumbnail_id:
         reply_text += "\n\n🖼️ **Note:** A custom thumbnail is currently set and will be applied to the files in this bundle."
    
    await message.reply(reply_text)

@app.on_message(filters.command("done") & filters.private)
@force_join_check
async def done_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_state = db.settings.find_one({"_id": user_id, "type": "temp_link"})
    
    if user_state and user_state.get("state") == "multi_link":
        message_ids = user_state.get("message_ids", [])
        thumbnail_id = user_state.get("thumbnail_id") # <--- यह लाइन जोड़ें
        
        if not message_ids:
            await message.reply("❌ You haven't added any files. Please forward them first or use `/multi_link` again.")
            return
            
        status_msg = await message.reply(f"⏳ **Finishing Bundle!** Processing {len(message_ids)} files...")
        
        try:
            forwarded_msg_ids = []
            for msg_id in message_ids:
                try:
                    # Get the original message to check file type and caption/markup
                    original_message = await client.get_messages(user_id, msg_id) 
                    
                    # Copy message from the user's chat to the LOG_CHANNEL
                    # Pyrogram केवल Document, Video और Audio के लिए thumbnail_id को सपोर्ट करता है
                    forwarded_msg = await client.copy_message(
                        chat_id=LOG_CHANNEL, 
                        from_chat_id=user_id, 
                        message_id=msg_id,
                        caption=original_message.caption,
                        reply_markup=original_message.reply_markup,
                        # यदि थंबनेल सेट है और यह Document, Video या Audio है, तो उसे लागू करें
                        **({'thumb': thumbnail_id} if thumbnail_id and (original_message.document or original_message.video or original_message.audio) else {})
                    ) # <--- यह ब्लॉक अपडेट करें
                    forwarded_msg_ids.append(forwarded_msg.id)
                    await asyncio.sleep(0.1) 
                except Exception as e:
                    logging.error(f"Error copying message {msg_id} for bundle: {e}")
            
            multi_file_id = await get_unique_id(db.multi_files) 
            force_channel = user_state.get("force_channel")
            file_name = user_state.get("file_name") or f"Bundle of {len(forwarded_msg_ids)} Files"
            
            db.multi_files.insert_one({
                '_id': multi_file_id, 
                'message_ids': forwarded_msg_ids,
                'user_id': user_id,
                'file_name': file_name,
                'force_channel': force_channel,
                'created_at': datetime.utcnow()
            })
            
            bot_username = (await client.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={multi_file_id}"
            
            # Clean up temporary state: This also deletes the temporary 'thumbnail_id'
            db.settings.delete_one({"_id": user_id, "type": "temp_link"})
            
            share_button = InlineKeyboardButton("📤 Share Bundle Link", url=f"https://t.me/share/url?url={urllib.parse.quote(f'Bundle: {file_name}\nLink: {share_link}')}")
            
            reply_text = (
                f"🎉 **Multi-File Bundle Link Generated!** 🎉\n\n"
                f"**📦 Bundle Name:** `{file_name}`\n"
                f"**#️⃣ Total Files:** **{len(forwarded_msg_ids)}**\n"
                f"**🔗 Permanent Link:** `{share_link}`"
            )
            
            if force_channel:
                 reply_text += f"\n\n🔒 **Access Condition:** User must join **@{force_channel}**."
            
            if thumbnail_id:
                reply_text += "\n\n🖼️ **Custom thumbnail applied!**"
                
            await status_msg.edit_text(
                reply_text,
                reply_markup=InlineKeyboardMarkup([[share_button]]),
                disable_web_page_preview=True
            )
            
            # Log the action
            log_text = (
                f"📦 **New Multi-File Link**\n"
                f"• **User:** {await get_user_full_name(message.from_user)} (`{user_id}`)\n"
                f"• **Bundle:** `{file_name}` ({len(forwarded_msg_ids)} files)"
            )
            if thumbnail_id:
                 log_text += " (🖼️ Custom Thumb)"
            log_text += f"\n• **Link:** `t.me/{bot_username}?start={multi_file_id}`"
            
            await client.send_message(LOG_CHANNEL, log_text)

        except Exception as e:
            logging.error(f"Multi-file link creation error: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ **Error!**\n\nSomething went wrong while creating the bundle. Please try again.\n`Details: {e}`")
    else:
        await message.reply("🤔 You are not in multi-link mode. Send `/multi_link [Optional Title]` to start a new bundle.")


@app.on_message(filters.command("myfiles") & filters.private)
async def my_files_handler(client: Client, message: Message):
    user_id = message.from_user.id
    
    # Fetch last 5 single files and last 5 multi-files
    user_single_files = list(db.files.find({"user_id": user_id}).sort("created_at", -1).limit(5))
    user_multi_files = list(db.multi_files.find({"user_id": user_id}).sort("created_at", -1).limit(5))
    
    if not user_single_files and not user_multi_files:
        await message.reply("😔 You haven't uploaded any files or created any bundles yet. Start with sending a file or `/multi_link`.")
        return

    text = "📂 **Your Recent Uploads & Bundles:**\n\n"
    bot_username = (await client.get_me()).username
    
    if user_single_files:
        text += "--- **Single Files (Last 5)** ---\n"
        for i, file_record in enumerate(user_single_files):
            file_name = file_record.get('file_name', 'Unnamed File')
            file_id_str = file_record['_id']
            share_link = f"https://t.me/{bot_username}?start={file_id_str}"
            text += f"**{i+1}.** `🔗` [{file_name}]({share_link})\n"
        text += "\n"
        
    if user_multi_files:
        text += "--- **Multi-File Bundles (Last 5)** ---\n"
        for i, bundle_record in enumerate(user_multi_files):
            file_name = bundle_record.get('file_name', f"Bundle of {len(bundle_record.get('message_ids', []))} Files")
            file_id_str = bundle_record['_id']
            share_link = f"https://t.me/{bot_username}?start={file_id_str}"
            text += f"**{i+1}.** `📦` [{file_name}]({share_link})\n"
        text += "\n"

    text += "_To delete a file, use: `/delete <file_id>`_"
    
    await message.reply(text, disable_web_page_preview=True)

@app.on_message(filters.command("delete") & filters.private)
async def delete_file_handler(client: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("Please provide the file or bundle ID to delete. Example: `/delete abcdefgh`")
        return

    file_id_str = message.command[1].split('?start=')[-1] # Handle full link being passed
    user_id = message.from_user.id
    
    # Check both collections
    file_record = db.files.find_one({"_id": file_id_str, "user_id": user_id})
    multi_file_record = db.multi_files.find_one({"_id": file_id_str, "user_id": user_id})
    
    is_single_file = bool(file_record)
    record_to_delete = file_record or multi_file_record

    if not record_to_delete:
        await message.reply("🤔 File or bundle not found, or you don't have permission to delete it.")
        return
        
    file_name = record_to_delete.get('file_name', 'Unnamed Item')

    delete_button = InlineKeyboardButton("🗑️ Confirm Delete", callback_data=f"confirm_delete_{file_id_str}_{'single' if is_single_file else 'multi'}")
    cancel_button = InlineKeyboardButton("↩️ Cancel", callback_data="cancel_delete")
    keyboard = InlineKeyboardMarkup([[delete_button, cancel_button]])

    item_type = "File" if is_single_file else "Bundle"
    
    await message.reply(
        f"⚠️ **Confirm Deletion**\n\n"
        f"Are you sure you want to permanently delete this **{item_type}**:\n**`{file_name}`**?",
        reply_markup=keyboard,
        quote=True
    )

# --- Admin Handlers (Enhanced) ---

@app.on_message(filters.command("admin") & filters.private & filters.user(ADMINS))
async def admin_panel_handler(client: Client, message: Message):
    current_mode = await get_bot_mode(db)
    
    buttons = [
        [InlineKeyboardButton("📊 Bot Stats", callback_data="admin_stats"),
         InlineKeyboardButton(f"⚙️ Mode: {current_mode.upper()}", callback_data="admin_settings")],
        [InlineKeyboardButton("📣 Broadcast Message", callback_data="admin_broadcast_prompt")]
    ]
    await message.reply(
        "👑 **Admin Panel Access Granted!** 🛡️\n\n"
        "Welcome back! Manage your bot's operation and check statistics below.",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

@app.on_message(filters.command("stats") & filters.private & filters.user(ADMINS))
async def stats_handler(client: Client, message: Message):
    user_count = db.users.count_documents({})
    single_files_count = db.files.count_documents({})
    multi_files_count = db.multi_files.count_documents({})
    
    total_files_count = single_files_count + multi_files_count

    # Calculate time 24 hours ago
    today_start_dt = datetime.utcnow() - timedelta(days=1)
    
    today_new_users = db.users.count_documents({"last_activity": {"$gte": today_start_dt}})
    today_single_files = db.files.count_documents({"created_at": {"$gte": today_start_dt}})
    today_multi_files = db.multi_files.count_documents({"created_at": {"$gte": today_start_dt}})
    
    # Advanced file type breakdown
    file_types = db.files.aggregate([{"$group": {"_id": "$file_type", "count": {"$sum": 1}}}])
    file_types_text = "\n".join([f"  • {ft['_id'].capitalize()}: **{ft['count']}**" for ft in file_types if ft['_id']])
    if not file_types_text:
        file_types_text = "  • No files recorded."
    
    await message.reply(
        f"📊 **BOT STATISTICS**\n\n"
        f"--- **User & Usage** ---\n"
        f"**👥 Total Users:** `{user_count}`\n"
        f"**🗓️ Active (Last 24h):** `{today_new_users}`\n\n"
        f"--- **Files** ---\n"
        f"**📁 Total Items:** `{total_files_count}`\n"
        f"**📄 Single Files:** `{single_files_count}`\n"
        f"**📦 Multi-Bundles:** `{multi_files_count}`\n"
        f"**📈 Uploads (Last 24h):** `{today_single_files + today_multi_files}`\n\n"
        f"--- **File Breakdown** ---\n"
        f"{file_types_text}"
    )

@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMINS))
async def broadcast_prompt_handler(client: Client, message: Message):
    if len(message.command) < 2 and not message.reply_to_message:
        await message.reply(
            "📣 **Broadcast Mode**\n\n"
            "Please send the message you want to broadcast immediately after the command, or reply to a message/media.\n"
            "Example: `/broadcast Hello everyone! New files available!`\n\n"
            "_Note: Formatting and media (replied to) are supported._"
        )
        return

    # Use the entire message after the command as the broadcast content or the replied message's text/media
    text_to_send = message.text.split(" ", 1)[1] if len(message.command) > 1 else None
    
    # Get all user IDs
    users = db.users.find({}, {"_id": 1})
    user_ids = [user['_id'] for user in users]
    
    success_count = 0
    failed_count = 0
    
    status_msg = await message.reply(f"⏳ **Starting broadcast to {len(user_ids)} users...**")
    
    # Broadcast logic (better implementation with asyncio for speed)
    async def send_message_task(chat_id, content, reply_to_msg):
        nonlocal success_count, failed_count
        try:
            if reply_to_msg and reply_to_msg.media:
                # If broadcast command is a reply to media, copy the media
                await reply_to_msg.copy(chat_id)
            elif content:
                await client.send_message(chat_id=chat_id, text=content, disable_web_page_preview=True)
            success_count += 1
        except Exception:
            failed_count += 1
        await asyncio.sleep(0.1) # Throttle

    reply_to_msg = message.reply_to_message
    tasks = [send_message_task(uid, text_to_send, reply_to_msg) for uid in user_ids]
    await asyncio.gather(*tasks)
    
    await status_msg.edit_text(
        f"✅ **Broadcast Complete!**\n\n"
        f"**Success:** `{success_count}`\n"
        f"**Failed (Blocked/Left):** `{failed_count}`"
    )

@app.on_message(filters.command("settings") & filters.private & filters.user(ADMINS))
async def settings_handler(client: Client, message: Message):
    # This just forwards to the admin panel with a settings view
    await admin_panel_handler(client, message)

# --- Callback Query Handlers (Enhanced) ---

@app.on_callback_query(filters.regex("^(about|help|start_menu|my_files_menu|admin_stats|admin_settings|admin_broadcast_prompt|admin)$"))
async def general_callback_handler(client: Client, callback_query: CallbackQuery):
    query = callback_query.data
    
    if query == "about":
        text = (
            "📚 **About FileLinker Bot**\n\n"
            "This bot creates **permanent, short, and shareable deep-links** for your Telegram files. "
            "It's built for efficiency, security, and a great user experience.\n\n"
            "✨ **Core Features:** File-to-Link, Multi-File Bundling, Optional Force Join, Inline Search, and Admin Controls.\n\n"
            "Made with ❤️ by [ @narzoxbot ]."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💡 How to Use?", callback_data="help"), InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")]])
        await callback_query.message.edit_caption(text, reply_markup=keyboard) if callback_query.message.photo else await callback_query.message.edit_text(text, reply_markup=keyboard)
        
    elif query == "help":
        # Simply defer to the /help handler logic
        await callback_query.message.delete()
        await help_handler_private(client, callback_query.message)
        
    elif query == "start_menu":
        # Defer to the /start handler logic
        await callback_query.message.delete()
        await start_handler(client, callback_query.message)
        
    elif query == "my_files_menu":
        buttons = [
            [InlineKeyboardButton("📂 View My Last 10 Files", callback_data="view_my_files")],
            [InlineKeyboardButton("🔗 View Force Join Channels", callback_data="view_force_channels")],
            [InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")]
        ]
        await callback_query.message.edit_caption(
            "⚙️ **My Dashboard**\n\n"
            "Manage your uploaded files and check the current force join channels.",
            reply_markup=InlineKeyboardMarkup(buttons)
        ) if callback_query.message.photo else await callback_query.message.edit_text(
            "⚙️ **My Dashboard**\n\n"
            "Manage your uploaded files and check the current force join channels.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        
    elif query == "view_my_files":
         # Defer to the /myfiles handler logic
         await callback_query.message.delete()
         await my_files_handler(client, callback_query.message)
         
    elif query == "view_force_channels":
        if FORCE_CHANNELS:
            channels_text = "\n".join([f"• @{ch}" for ch in FORCE_CHANNELS])
            text = f"🌐 **Global Force Join Channels**\n\n{channels_text}\n\n_You must join these to use certain features._"
        else:
            text = "❌ **Global Force Join is NOT active!** No channels are required for general use."
            
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Menu", callback_data="my_files_menu")]])
        await callback_query.message.edit_caption(text, reply_markup=keyboard) if callback_query.message.photo else await callback_query.message.edit_text(text, reply_markup=keyboard)

    # --- Admin Panel Callbacks ---
    elif query == "admin":
        # Defer to the /admin handler logic
        await callback_query.message.delete()
        await admin_panel_handler(client, callback_query.message)
        
    elif query == "admin_stats":
         # Defer to the /stats handler logic
         await callback_query.message.delete()
         await stats_handler(client, callback_query.message)
         
    elif query == "admin_settings":
        current_mode = await get_bot_mode(db)
        
        public_button = InlineKeyboardButton("🌍 Public (Anyone)", callback_data="set_mode_public")
        private_button = InlineKeyboardButton("🔒 Private (Admins Only)", callback_data="set_mode_private")
        keyboard = InlineKeyboardMarkup([[public_button], [private_button], [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin")]])
        
        await callback_query.message.edit_text(
            f"⚙️ **Bot File Upload Mode**\n\n"
            f"The current mode is **{current_mode.upper()}**.\n"
            f"Select a new mode below:",
            reply_markup=keyboard
        )

    elif query == "admin_broadcast_prompt":
        await callback_query.message.edit_text(
            "📣 **Broadcast Message**\n\n"
            "Please send the broadcast message immediately after the `/broadcast` command.\n"
            "Example: `/broadcast Check out our new bot features! #update`\n\n"
            "_You can also reply to a photo/video with `/broadcast` to send media._",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin", callback_data="admin")]])
        )

    await callback_query.answer()

@app.on_callback_query(filters.regex(r"^check_join_"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    # Split the data, file_id_str is the third part if it exists
    parts = callback_query.data.split("_", 2)
    file_id_str = parts[2] if len(parts) > 2 else None

    # Determine channels to check
    all_channels_to_check = list(FORCE_CHANNELS)
    
    if file_id_str and file_id_str != 'force': # 'force' is the fallback for generic check
        file_record = db.files.find_one({"_id": file_id_str})
        multi_file_record = db.multi_files.find_one({"_id": file_id_str})

        if file_record and file_record.get('force_channel'):
            all_channels_to_check.append(file_record['force_channel'])
        elif multi_file_record and multi_file_record.get('force_channel'):
            all_channels_to_check.append(multi_file_record['force_channel'])
    
    all_channels_to_check = list(set(all_channels_to_check))
    missing_channels = await is_user_member_all_channels(client, user_id, all_channels_to_check)

    if not missing_channels:
        await callback_query.answer("Thanks for joining! Sending files now... 🥳", show_alert=True)
        await callback_query.message.delete()
        
        if file_id_str and file_id_str != 'force':
             # Simulate a successful /start command to deliver the file
             fake_message = callback_query.message
             fake_message.from_user = callback_query.from_user
             fake_message.command = ["start", file_id_str]
             await start_handler(client, fake_message)
        else:
             await callback_query.message.reply("✅ You are a member of all required channels now!")

    else:
        await callback_query.answer("You have not joined all the channels. Please join them and try again.", show_alert=True)
        join_buttons = [[InlineKeyboardButton(f"🔗 Join @{ch}", url=f"https://t.me/{ch}")] for ch in missing_channels]
        # Preserve the callback data for next attempt
        join_buttons.append([InlineKeyboardButton("✅ I Have Joined! (Try Again)", callback_data=callback_query.data)])
        keyboard = InlineKeyboardMarkup(join_buttons)
        
        await callback_query.message.edit_text(
            f"❌ **ACCESS DENIED**\n\nPlease join the remaining channels to continue:",
            reply_markup=keyboard
        )

@app.on_callback_query(filters.regex(r"^set_mode_"))
async def set_mode_callback(client: Client, callback_query: CallbackQuery):
    if callback_query.from_user.id not in ADMINS:
        await callback_query.answer("❌ Permission Denied! Only Admins can change bot mode.", show_alert=True)
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
    keyboard = InlineKeyboardMarkup([[public_button], [private_button], [InlineKeyboardButton("🔙 Back to Admin", callback_data="admin")]])
    
    await callback_query.message.edit_text(
        f"⚙️ **Bot File Upload Mode**\n\n"
        f"✅ File upload mode is now **{new_mode.upper()}**.\n\n"
        f"Select a new mode:",
        reply_markup=keyboard
    )

@app.on_callback_query(filters.regex(r"^confirm_delete_"))
async def confirm_delete_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    # confirm_delete_<file_id>_<single/multi>
    parts = callback_query.data.split("_")
    file_id_str = parts[2]
    item_type = parts[3] 

    collection = db.files if item_type == 'single' else db.multi_files
    
    record_to_delete = collection.find_one({"_id": file_id_str, "user_id": user_id})

    if not record_to_delete:
        await callback_query.answer("File/Bundle not found or already deleted.", show_alert=True)
        await callback_query.message.edit_text("❌ Item could not be deleted. It might be a bad link or already gone.")
        return

    try:
        # Delete from LOG_CHANNEL
        if item_type == 'single':
            # Pyrogram's delete_messages requires a list of message_ids
            message_ids_to_delete = [record_to_delete['message_id']]
        else: # multi
            message_ids_to_delete = record_to_delete['message_ids']
            
        # Delete messages in batches to handle Pyrogram's API limits better
        chunk_size = 100 
        for i in range(0, len(message_ids_to_delete), chunk_size):
            chunk = message_ids_to_delete[i:i + chunk_size]
            await client.delete_messages(chat_id=LOG_CHANNEL, message_ids=chunk)
            await asyncio.sleep(0.5) # Throttle
            
        # Delete from database
        collection.delete_one({"_id": file_id_str})

        await callback_query.answer(f"Item deleted successfully! ID: {file_id_str}", show_alert=True)
        await callback_query.message.edit_text(f"✅ The {item_type.upper()} item **`{record_to_delete.get('file_name', 'Unnamed Item')}`** has been permanently deleted.")
        
        log_text = (
            f"🗑️ **Item Deleted**\n"
            f"• **User:** {await get_user_full_name(callback_query.from_user)} (`{user_id}`)\n"
            f"• **Type:** `{item_type.upper()}`\n"
            f"• **ID:** `{file_id_str}`"
        )
        await client.send_message(LOG_CHANNEL, log_text)
        
    except Exception as e:
        logging.error(f"Failed to delete item {file_id_str}: {e}", exc_info=True)
        # Check if the error is due to message already deleted (common case)
        if "MESSAGE_DELETE_FORBIDDEN" in str(e) or "MESSAGE_NOT_FOUND" in str(e):
             # Still delete from DB if Telegram failed to find/delete (to clean up)
             collection.delete_one({"_id": file_id_str})
             await callback_query.answer("Item deleted from database, but message removal from log channel failed (already deleted or access issue).", show_alert=True)
             await callback_query.message.edit_text(f"✅ The {item_type.upper()} item **`{record_to_delete.get('file_name', 'Unnamed Item')}`** has been deleted from the database.")
        else:
             await callback_query.answer("An error occurred while deleting the item.", show_alert=True)
             await callback_query.message.edit_text("❌ An error occurred while trying to delete the item. Please try again later.")

@app.on_callback_query(filters.regex(r"^cancel_delete"))
async def cancel_delete_callback(client: Client, callback_query: CallbackQuery):
    await callback_query.answer("Deletion cancelled.", show_alert=True)
    await callback_query.message.edit_text("↩️ Deletion cancelled. Your file/bundle is safe.")

@app.on_inline_query()
async def inline_search(client, inline_query):
    query = inline_query.query.strip().lower()
    
    if not query:
        # Default results for empty query
        results = [
            InlineQueryResultArticle(
                title="🔍 Search for a file/bundle",
                description="Type a filename or keyword to find your links.",
                input_message_content=InputTextMessageContent(
                    message_text="🤔 Searching for files..."
                )
            )
        ]
        await client.answer_inline_query(inline_query.id, results, cache_time=0)
        return

    # Search in both collections
    single_files_found = list(db.files.find(
        {"user_id": inline_query.from_user.id, "file_name": {"$regex": query, "$options": "i"}}
    ).limit(7))
    
    multi_files_found = list(db.multi_files.find(
        {"user_id": inline_query.from_user.id, "file_name": {"$regex": query, "$options": "i"}}
    ).limit(7))
    
    all_found = single_files_found + multi_files_found
    all_found.sort(key=lambda x: x['created_at'], reverse=True) # Sort by creation time
    
    articles = []
    bot_username = (await client.get_me()).username
    
    for item_record in all_found[:15]: # Limit to max 15 results
        file_id_str = item_record['_id']
        share_link = f"https://t.me/{bot_username}?start={file_id_str}"
        
        is_single = 'message_id' in item_record
        item_type = "File" if is_single else "Bundle"
        file_name = item_record.get('file_name', f"Unnamed {item_type}")
        
        description = f"{item_type} Link. Click to share."
        if not is_single:
             description = f"Bundle of {len(item_record.get('message_ids', []))} files. Click to share."

        articles.append(
            InlineQueryResultArticle(
                title=f"[{item_type}] {file_name}",
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=f"🔗 **Here is the {item_type} link:**\n`{share_link}`",
                    disable_web_page_preview=True
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"📤 Share {item_type}", url=f"https://t.me/share/url?url={urllib.parse.quote(share_link)}")]])
            )
        )
        
    if not articles:
        articles.append(
            InlineQueryResultArticle(
                title="❌ No Files Found",
                description=f"No files or bundles matching '{query}' were found in your uploads.",
                input_message_content=InputTextMessageContent(
                    message_text="😔 No matching files found. Try a different keyword or upload files first."
                )
            )
        )

    await client.answer_inline_query(
        inline_query.id,
        results=articles,
        cache_time=5
    )


# --- Group Features (Enhanced with Pyrogram types) ---

@app.on_chat_member_updated(filters.group)
async def welcome_and_goodbye_messages(client: Client, member: Message):
    """Handles new user joins and users leaving for logging."""
    if not GROUP_LOG_CHANNEL: return

    user = member.new_chat_member.user if member.new_chat_member and member.new_chat_member.user else member.old_chat_member.user
    
    if user.is_bot: return

    # User joined
    if member.new_chat_member and member.new_chat_member.status != 'left' and member.old_chat_member and member.old_chat_member.status == 'left':
        # New member joined from a left status
        log_text = (
            f"👥 **New Member Joined!**\n"
            f"• **Name:** {await get_user_full_name(user)}\n"
            f"• **ID:** `{user.id}`\n"
            f"• **Username:** @{user.username if user.username else 'N/A'}\n"
            f"• **Group:** {member.chat.title} (`{member.chat.id}`)"
        )
        await client.send_message(GROUP_LOG_CHANNEL, log_text)

    # User left/was kicked/banned
    elif member.old_chat_member and member.old_chat_member.status != 'left' and member.new_chat_member and member.new_chat_member.status in ['left', 'banned', 'kicked']:
        
        action = "Kicked/Banned" if member.new_chat_member.status in ['banned', 'kicked'] else "Left"
        log_text = (
            f"🚪 **Member {action}!**\n"
            f"• **Name:** {await get_user_full_name(user)}\n"
            f"• **ID:** `{user.id}`\n"
            f"• **Group:** {member.chat.title} (`{member.chat.id}`)"
        )
        await client.send_message(GROUP_LOG_CHANNEL, log_text)


@app.on_message(filters.group & ~filters.user(ADMINS) & ~filters.edited)
async def anti_flood_and_link(client: Client, message: Message):
    """Group message moderator: Anti-Link and Anti-Badwords."""
    if not message.text and not message.caption: return

    # Anti-Link Filter
    text_with_caption = message.text or message.caption
    entities = message.entities if message.entities else message.caption_entities
    
    if entities:
        for entity in entities:
            # Check for URL, text_link, or bot mention/command which can sometimes be abused
            if entity.type in ["url", "text_link"] and not filters.user(ADMINS)(client, message):
                try:
                    await message.delete()
                    await message.reply(
                        f"🚫 **Link Removed!** {await get_user_full_name(message.from_user)}, links are not allowed here.",
                        quote=True
                    )
                    
                    log_text = (
                        f"🔗 **Link Removed!**\n"
                        f"• **User:** {await get_user_full_name(message.from_user)} (`{message.from_user.id}`)\n"
                        f"• **Group:** {message.chat.title} (`{message.chat.id}`)\n"
                        f"• **Message:** `{'Link detected'}`"
                    )
                    if GROUP_LOG_CHANNEL: await client.send_message(GROUP_LOG_CHANNEL, log_text)
                    return
                except ChatAdminRequired:
                    return

    # Anti-Badwords Filter
    text_lower = (text_with_caption or "").lower()
    for badword in BADWORDS:
        if badword and badword in text_lower:
            try:
                await message.delete()
                await message.reply(f"🤬 **Censored!** Please mind your language, {await get_user_full_name(message.from_user)}.", quote=True)
                
                log_text = (
                    f"🤬 **Badword Removed!**\n"
                    f"• **User:** {await get_user_full_name(message.from_user)} (`{message.from_user.id}`)\n"
                    f"• **Group:** {message.chat.title} (`{message.chat.id}`)\n"
                    f"• **Text:** `{text_with_caption}`"
                )
                if GROUP_LOG_CHANNEL: await client.send_message(GROUP_LOG_CHANNEL, log_text)
                return
            except ChatAdminRequired:
                return

@app.on_message(filters.command("warn") & filters.group & filters.user(ADMINS))
async def warn_user(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply("⚠️ Please reply to a user's message to warn them.")
        return

    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    if target_user.is_bot or target_user.id in ADMINS:
        await message.reply("Cannot warn a bot or an admin/owner.")
        return
        
    warnings_record = db.warnings.find_one({"user_id": target_user.id, "chat_id": chat_id})
    if warnings_record:
        new_warnings = warnings_record['warnings'] + 1
        db.warnings.update_one({"user_id": target_user.id, "chat_id": chat_id}, {"$set": {"warnings": new_warnings}})
    else:
        new_warnings = 1
        db.warnings.insert_one({"user_id": target_user.id, "chat_id": chat_id, "warnings": new_warnings})
    
    await message.reply(
        f"⚠️ {await get_user_full_name(target_user)} has been warned. "
        f"Warnings: **{new_warnings}/{MAX_WARNINGS}**."
    )
    
    log_text = (
        f"⚠️ **User Warned!**\n"
        f"• **User:** {await get_user_full_name(target_user)} (`{target_user.id}`)\n"
        f"• **Admin:** {await get_user_full_name(message.from_user)}\n"
        f"• **Group:** {message.chat.title}\n"
        f"• **New Warnings:** `{new_warnings}`"
    )
    if GROUP_LOG_CHANNEL: await client.send_message(GROUP_LOG_CHANNEL, log_text)
    
    if new_warnings >= MAX_WARNINGS:
        try:
            # Mute the user (default to 24 hours if no specific timeout is desired)
            await client.restrict_chat_member(
                chat_id, 
                target_user.id, 
                permissions=ChatPermissions(can_send_messages=False), 
                until_date=datetime.now() + timedelta(hours=24) # Mute for 24h
            )
            # Clear warnings after mute
            db.warnings.delete_one({"user_id": target_user.id, "chat_id": chat_id})
            
            await message.reply(f"🚫 {await get_user_full_name(target_user)} received {MAX_WARNINGS} warnings and has been **muted for 24 hours**.")
            
            log_text = (
                f"🚫 **User Muted (Max Warnings)!**\n"
                f"• **User:** {await get_user_full_name(target_user)} (`{target_user.id}`)\n"
                f"• **Group:** {message.chat.title}"
            )
            if GROUP_LOG_CHANNEL: await client.send_message(GROUP_LOG_CHANNEL, log_text)
            
        except ChatAdminRequired:
            await message.reply("I need admin rights with 'Restrict users' permission to mute this user.")

@app.on_message(filters.command("mute") & filters.group & filters.user(ADMINS))
async def temp_mute(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply("🔇 Please reply to a user's message to mute them. Example: `/mute 30m` (30 minutes) or `/mute 1h` (1 hour).")
        return

    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    if target_user.is_bot or target_user.id in ADMINS:
        await message.reply("Cannot mute a bot or an admin.")
        return
        
    try:
        duration_str = message.command[1]
        duration_unit = duration_str[-1].lower()
        duration_value = int(duration_str[:-1])

        if duration_unit == "m":
            unmute_time = datetime.now() + timedelta(minutes=duration_value)
            duration_text = f"{duration_value} minutes"
        elif duration_unit == "h":
            unmute_time = datetime.now() + timedelta(hours=duration_value)
            duration_text = f"{duration_value} hours"
        elif duration_unit == "d":
            unmute_time = datetime.now() + timedelta(days=duration_value)
            duration_text = f"{duration_value} days"
        else:
            await message.reply("Invalid duration format. Use `/mute <value>m/h/d` (e.g., `/mute 10m`, `/mute 1h`).")
            return

        await client.restrict_chat_member(
            chat_id, 
            target_user.id, 
            permissions=ChatPermissions(can_send_messages=False), 
            until_date=unmute_time
        )
        await message.reply(f"🔇 {await get_user_full_name(target_user)} has been **muted** for **{duration_text}**.")
        
        log_text = (
            f"🔇 **User Muted!**\n"
            f"• **User:** {await get_user_full_name(target_user)} (`{target_user.id}`)\n"
            f"• **Admin:** {await get_user_full_name(message.from_user)}\n"
            f"• **Group:** {message.chat.title}\n"
            f"• **Duration:** `{duration_text}`"
        )
        if GROUP_LOG_CHANNEL: await client.send_message(GROUP_LOG_CHANNEL, log_text)

    except (IndexError, ValueError):
        await message.reply("Please provide a duration. Example: `/mute 30m`.")
    except ChatAdminRequired:
        await message.reply("I need admin rights with 'Restrict users' permission to mute this user.")

@app.on_message(filters.command("kick") & filters.group & filters.user(ADMINS))
async def temp_kick(client: Client, message: Message):
    if not message.reply_to_message:
        await message.reply("👢 Please reply to a user's message to kick them.")
        return

    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id
    
    if target_user.is_bot or target_user.id in ADMINS:
        await message.reply("Cannot kick a bot or an admin.")
        return
        
    try:
        # Kick (Bans then Unbans to make them leave without permanent ban)
        await client.kick_chat_member(chat_id, target_user.id)
        await client.unban_chat_member(chat_id, target_user.id)
        
        await message.reply(f"👢 {await get_user_full_name(target_user)} has been **kicked** from the group.")
        
        log_text = (
            f"👢 **User Kicked!**\n"
            f"• **User:** {await get_user_full_name(target_user)} (`{target_user.id}`)\n"
            f"• **Admin:** {await get_user_full_name(message.from_user)}\n"
            f"• **Group:** {message.chat.title}"
        )
        if GROUP_LOG_CHANNEL: await client.send_message(GROUP_LOG_CHANNEL, log_text)
        
    except ChatAdminRequired:
        await message.reply("I need admin rights with 'Ban users' permission to kick this user.")

# --- Main Bot Runner ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("⚠️ WARNING: ADMINS is not set. Admin commands will not work.")
    if not FORCE_CHANNELS:
        logging.warning("⚠️ WARNING: FORCE_CHANNELS is not set. Force join feature will be disabled.")
    if not GROUP_LOG_CHANNEL:
        logging.warning("⚠️ WARNING: GROUP_LOG_CHANNEL is not set. Group logs will not be saved.")
        
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("Bot is starting...")
    app.run() # Use app.run() instead of app.start() + idle() for cleaner startup/shutdown in many environments
    logging.info("Bot has stopped.")
