import os
import logging
import random
import string
import shutil
import subprocess
import time
import urllib.parse
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pymongo import MongoClient
from flask import Flask
from threading import Thread

# --- Flask Web Server (To keep the bot alive on platforms like Render) ---
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

# --- Load Environment Variables ---
dotenv_path = os.environ.get("DOTENV_PATH", ".env")
load_dotenv(dotenv_path)

# --- Configuration ---
API_ID = int(os.environ.get("API_ID"))
API_HASH = os.environ.get("API_HASH")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL"))
# Main Channels for force-join
UPDATE_CHANNELS = ["bestshayri_raj", "go_esports"]
# Optional channels for support and dev (add your own)
SUPPORT_CHANNEL = "bestshayri_raj"
DEV_CHANNEL = "go_esports"
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMINS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]

# --- Database Setup ---
try:
    client = MongoClient(MONGO_URI)
    db = client['file_link_bot_ultimate']
    files_collection = db['files']
    multi_file_collection = db['multi_files']
    settings_collection = db['settings']
    logging.info("MongoDB Connected Successfully!")
except Exception as e:
    logging.error(f"❌ Error connecting to MongoDB: {e}")
    exit()

# --- Pyrogram Client ---
app = Client("FileLinkBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Helper Functions ---
def generate_random_string(length=6):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

async def is_user_member_all_channels(client: Client, user_id: int) -> list:
    missing_channels = []
    for channel in UPDATE_CHANNELS:
        try:
            await client.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
        except UserNotParticipant:
            missing_channels.append(channel)
        except Exception as e:
            logging.error(f"Error checking membership for {user_id} in @{channel}: {e}")
            missing_channels.append(channel)
    return missing_channels

async def get_bot_mode() -> str:
    setting = settings_collection.find_one({"_id": "bot_mode"})
    if setting:
        return setting.get("mode", "public")
    settings_collection.update_one({"_id": "bot_mode"}, {"$set": {"mode": "public"}}, upsert=True)
    return "public"

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    if len(message.command) > 1:
        file_id_str = message.command[1]
        missing_channels = await is_user_member_all_channels(client, message.from_user.id)
        
        if missing_channels:
            join_buttons = []
            for channel in missing_channels:
                join_buttons.append([InlineKeyboardButton(f"🔗 Join @{channel}", url=f"https://t.me/{channel}")])
            join_buttons.append([InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")])

            await message.reply(
                f"👋 **Hello, {message.from_user.first_name}!**\n\nTo access this file, you must first join the following channels:",
                reply_markup=InlineKeyboardMarkup(join_buttons),
                quote=True
            )
            return

        file_record = files_collection.find_one({"_id": file_id_str})
        if file_record:
            try:
                await client.copy_message(chat_id=message.from_user.id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
            except Exception as e:
                await message.reply(f"❌ Sorry, an error occurred while sending the file.\n`Error: {e}`")
            return

        multi_file_record = multi_file_collection.find_one({"_id": file_id_str})
        if multi_file_record:
            sent_count = 0
            for msg_id in multi_file_record['message_ids']:
                try:
                    await client.copy_message(chat_id=message.from_user.id, from_chat_id=LOG_CHANNEL, message_id=msg_id)
                    sent_count += 1
                    time.sleep(0.5)
                except Exception as e:
                    logging.error(f"Error sending multi-file message {msg_id}: {e}")
            await message.reply(f"✅ All {sent_count} videos/files from the bundle have been sent successfully!")
            return
        
        await message.reply("🤔 File or bundle not found! The link might be wrong or expired.")

    else:
        # Normal /start command with advanced buttons
        buttons = [
            [InlineKeyboardButton("📚 About", callback_data="about"),
             InlineKeyboardButton("💡 How to Use?", callback_data="help")],
            [InlineKeyboardButton("➕ Clone Bot", callback_data="clone_info")],
            [InlineKeyboardButton("🔗 Join Channels", callback_data="join_channels")]
        ]
        
        await message.reply(
            f"**Hello, {message.from_user.first_name}! I'm a powerful File-to-Link Bot!** 🤖\n\n"
            "Just send me any file, or a bundle of files, and I'll give you a **permanent, shareable link** for it. It's fast, secure, and super easy! ✨",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

# ... (other handlers like file_handler, multi_link_handler, and settings_handler remain the same as the previous version) ...

@app.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio))
async def file_handler(client: Client, message: Message):
    bot_mode = await get_bot_mode()
    if bot_mode == "private" and message.from_user.id not in ADMINS:
        await message.reply("😔 **Sorry!** Only Admins can upload files in private mode.")
        return

    status_msg = await message.reply("⏳ Uploading file... Please wait a moment.", quote=True)
    
    try:
        forwarded_message = await message.forward(LOG_CHANNEL)
        file_id_str = generate_random_string()
        files_collection.insert_one({'_id': file_id_str, 'message_id': forwarded_message.id})
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
async def multi_link_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ This command is for admins only.")
        return
        
    await message.reply("Forward me a series of messages. I will bundle them and give you a single link. Send /done when you are finished.")
    
    user_id = message.from_user.id
    settings_collection.update_one({"_id": user_id, "type": "temp"}, {"$set": {"message_ids": [], "state": "multi_link"}}, upsert=True)

@app.on_message(filters.command("done") & filters.private)
async def done_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_state = settings_collection.find_one({"_id": user_id, "type": "temp"})

    if user_state and user_state.get("state") == "multi_link":
        message_ids = user_state.get("message_ids", [])
        if not message_ids:
            await message.reply("You haven't forwarded any files yet. Please forward them and then send /done.")
            return

        status_msg = await message.reply(f"⏳ Processing {len(message_ids)} files...")
        
        try:
            multi_id_str = generate_random_string(8)
            
            forwarded_msg_ids = []
            for msg_id in message_ids:
                try:
                    forwarded_msg = await client.copy_message(chat_id=LOG_CHANNEL, from_chat_id=user_id, message_id=msg_id)
                    forwarded_msg_ids.append(forwarded_msg.id)
                except Exception as e:
                    logging.error(f"Error forwarding message {msg_id}: {e}")
            
            multi_file_collection.insert_one({'_id': multi_id_str, 'message_ids': forwarded_msg_ids})
            
            bot_username = (await client.get_me()).username
            share_link = f"https://t.me/{bot_username}?start={multi_id_str}"
            
            share_button = InlineKeyboardButton("🔗 Share Link", url=f"https://t.me/share/url?url={share_link}")
            
            await status_msg.edit_text(
                f"✅ **Multi-File Link Generated!**\n\n"
                f"**This link contains {len(message_ids)} files.**\n\n"
                f"🔗 **Your Permanent Link:** `{share_link}`",
                reply_markup=InlineKeyboardMarkup([[share_button]]),
                disable_web_page_preview=True
            )
            settings_collection.delete_one({"_id": user_id, "type": "temp"})

        except Exception as e:
            logging.error(f"Multi-file handling error: {e}")
            await status_msg.edit_text(f"❌ **Error!**\n\nSomething went wrong. Please try again.\n`Details: {e}`")
    else:
        await message.reply("You are not in multi-link mode. Send /multi_link to start.")

@app.on_message(filters.forwarded & filters.private)
async def forwarded_file_handler(client: Client, message: Message):
    user_id = message.from_user.id
    user_state = settings_collection.find_one({"_id": user_id, "type": "temp"})
    
    if user_state and user_state.get("state") == "multi_link":
        if message.document or message.video or message.photo or message.audio:
            settings_collection.update_one({"_id": user_id, "type": "temp"}, {"$push": {"message_ids": message.id}})
            await message.reply("File added to bundle. Forward more or send /done.", quote=True)

@app.on_message(filters.command("settings") & filters.private)
async def settings_handler(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply("❌ You don't have permission to use this command.")
        return
    
    current_mode = await get_bot_mode()
    
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

# ... (rest of the code for callbacks) ...

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

@app.on_callback_query(filters.regex(r"^check_join_"))
async def check_join_callback(client: Client, callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    file_id_str = callback_query.data.split("_", 2)[2]

    missing_channels = await is_user_member_all_channels(client, user_id)
    if not missing_channels:
        await callback_query.answer("Thanks for joining all channels! Sending files now...", show_alert=True)
        
        file_record = files_collection.find_one({"_id": file_id_str})
        if file_record:
            try:
                await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                await callback_query.message.delete()
            except Exception as e:
                await callback_query.message.edit_text(f"❌ An error occurred while sending the file.\n`Error: {e}`")
        
        multi_file_record = multi_file_collection.find_one({"_id": file_id_str})
        if multi_file_record:
            sent_count = 0
            for msg_id in multi_file_record['message_ids']:
                try:
                    await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=msg_id)
                    sent_count += 1
                    time.sleep(0.5)
                except Exception as e:
                    logging.error(f"Error sending multi-file message {msg_id}: {e}")
            await callback_query.message.edit_text(f"✅ All {sent_count} videos/files from the bundle have been sent successfully!")
            
    else:
        await callback_query.answer("You have not joined all the channels. Please join them and try again.", show_alert=True)
        join_buttons = []
        for channel in missing_channels:
            join_buttons.append([InlineKeyboardButton(f"🔗 Join @{channel}", url=f"https://t.me/{channel}")])
        join_buttons.append([InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")])
        keyboard = InlineKeyboardMarkup(join_buttons)
        await callback_query.message.edit_text(
            f"Please join the remaining channels to continue:",
            reply_markup=keyboard
        )

@app.on_callback_query(filters.regex("^(about|help|clone_info|join_channels)$"))
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
    elif query == "clone_info":
        repo_url = os.environ.get("REPO_URL", "https://github.com/your-username/your-bot-repo")
        
        # We'll create a deploy button with pre-filled environment variables.
        # This requires the repo to have an app.json file for Heroku or an environment variable section in a Render.yaml file.
        deploy_url = f"https://render.com/deploy?repo={urllib.parse.quote(repo_url)}&env=API_ID={API_ID}&env=API_HASH={API_HASH}&env=MONGO_URI={MONGO_URI}&env=LOG_CHANNEL={LOG_CHANNEL}&env=UPDATE_CHANNELS={','.join(UPDATE_CHANNELS)}&env=ADMINS={ADMINS[0] if ADMINS else ''}"
        
        await callback_query.message.edit_text(
            "➕ **Clone Me!**\n\n"
            "You can create your very own version of this bot in just one step! Click the button below to deploy this bot to Render or any other hosting platform.\n\n"
            "**How it works:**\n"
            "1. Click the 'Deploy Now' button below.\n"
            "2. You will be redirected to the hosting platform's deployment page with all the necessary settings pre-filled.\n"
            "3. **Just paste your new Bot Token** and click 'Deploy'.\n\n"
            "Your new bot will use the same features and configurations as this one. It's that easy! ✨",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Deploy Now", url=deploy_url)],
                                            [InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")]])
        )
    elif query == "join_channels":
        join_buttons = []
        for channel in UPDATE_CHANNELS:
            join_buttons.append([InlineKeyboardButton(f"🔗 Join @{channel}", url=f"https://t.me/{channel}")])
        join_buttons.append([InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")])
        
        await callback_query.message.edit_text(
            "To unlock all features and support the creators, please join our channels:",
            reply_markup=InlineKeyboardMarkup(join_buttons)
        )
    elif query == "start_menu":
        buttons = [
            [InlineKeyboardButton("📚 About", callback_data="about"),
             InlineKeyboardButton("💡 How to Use?", callback_data="help")],
            [InlineKeyboardButton("➕ Clone Bot", callback_data="clone_info")],
            [InlineKeyboardButton("🔗 Join Channels", callback_data="join_channels")]
        ]
        await callback_query.message.edit_text(
            f"**Hello, {callback_query.from_user.first_name}! I'm a powerful File-to-Link Bot!** 🤖\n\n"
            "Just send me any file, or a bundle of files, and I'll give you a **permanent, shareable link** for it. It's fast, secure, and super easy! ✨",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

# --- Bot's Main Entry Point ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("⚠️ WARNING: ADMIN_IDS is not set. The /settings and /multi_link commands will not work.")
    
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("Bot is starting...")
    app.run()
    logging.info("Bot has stopped.")
