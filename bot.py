import os
import logging
import random
import string
import shutil
import subprocess
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from pymongo import MongoClient
from flask import Flask, request, jsonify
from threading import Thread

# --- Flask Web Server (To keep the bot alive on platforms like Render) ---
flask_app = Flask(__name__)

@flask_app.route('/')
def index():
    return "Bot is alive!", 200

@flask_app.route('/clone', methods=['POST'])
def clone_bot():
    """
    Handles the cloning of the bot by writing a .env file and starting a new process.
    """
    data = request.json
    api_id = data.get('api_id')
    api_hash = data.get('api_hash')
    bot_token = data.get('bot_token')
    
    if not all([api_id, api_hash, bot_token]):
        return jsonify({"status": "error", "message": "Missing required fields."}), 400

    try:
        # Create a temporary .env file for the new bot
        temp_env_path = f"cloned_bot_{random.randint(1000, 9999)}.env"
        with open(temp_env_path, "w") as f:
            f.write(f"API_ID={api_id}\n")
            f.write(f"API_HASH={api_hash}\n")
            f.write(f"BOT_TOKEN={bot_token}\n")
            f.write(f"MONGO_URI={os.environ.get('MONGO_URI')}\n")
            f.write(f"LOG_CHANNEL={os.environ.get('LOG_CHANNEL')}\n")
            f.write(f"UPDATE_CHANNEL={os.environ.get('UPDATE_CHANNEL')}\n")
            f.write(f"ADMIN_IDS={data.get('admin_ids', '')}\n")

        # Copy the main script to a new file for the new bot instance
        cloned_script_path = f"cloned_bot_{random.randint(1000, 9999)}.py"
        shutil.copy(__file__, cloned_script_path)
        
        # Start the new bot in a separate process
        subprocess.Popen(
            ["python3", cloned_script_path],
            env={"DOTENV_PATH": temp_env_path},  # Pass the path to the new .env file
            close_fds=True
        )

        return jsonify({"status": "success", "message": "Bot is being cloned and will start shortly!"}), 200

    except Exception as e:
        logging.error(f"Error cloning bot: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

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
UPDATE_CHANNEL = os.environ.get("UPDATE_CHANNEL")
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMINS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]

# --- Database Setup ---
try:
    client = MongoClient(MONGO_URI)
    db = client['file_link_bot_adv']
    files_collection = db['files']
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

async def is_user_member(client: Client, user_id: int) -> bool:
    try:
        if not UPDATE_CHANNEL:
            return True
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
    if len(message.command) > 1:
        file_id_str = message.command[1]
        
        if UPDATE_CHANNEL and not await is_user_member(client, message.from_user.id):
            join_button = InlineKeyboardButton("🔗 Join Channel", url=f"https://t.me/{UPDATE_CHANNEL}")
            joined_button = InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")
            keyboard = InlineKeyboardMarkup([[join_button], [joined_button]])
            
            await message.reply(
                f"👋 **Hello, {message.from_user.first_name}!**\n\nTo get this file, you must first join our update channel.",
                reply_markup=keyboard,
                quote=True
            )
            return

        file_record = files_collection.find_one({"_id": file_id_str})
        if file_record:
            try:
                await client.copy_message(chat_id=message.from_user.id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
            except Exception as e:
                await message.reply(f"❌ Sorry, an error occurred while sending the file.\n`Error: {e}`")
        else:
            await message.reply("🤔 File not found! The link might be wrong or expired.")
    else:
        # Stylish welcome message with buttons
        buttons = [
            [InlineKeyboardButton("📚 About Bot", callback_data="about")],
            [InlineKeyboardButton("💡 How to Use?", callback_data="help"),
             InlineKeyboardButton("➕ Clone Bot", callback_data="clone")]
        ]
        
        await message.reply(
            f"**Hello, {message.from_user.first_name}! I'm a powerful File-to-Link Bot!** 🤖\n\n"
            "Just send me any file, and I'll give you a **permanent, shareable link** for it. "
            "It's fast, secure, and super easy! ✨",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

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
        
        # Add a share button
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

    if await is_user_member(client, user_id):
        await callback_query.answer("Thanks for joining! Sending the file now...", show_alert=True)
        file_record = files_collection.find_one({"_id": file_id_str})
        if file_record:
            try:
                await client.copy_message(chat_id=user_id, from_chat_id=LOG_CHANNEL, message_id=file_record['message_id'])
                await callback_query.message.delete()
            except Exception as e:
                await callback_query.message.edit_text(f"❌ An error occurred while sending the file.\n`Error: {e}`")
        else:
            await callback_query.message.edit_text("🤔 File not found!")
    else:
        await callback_query.answer("You have not joined the channel yet. Please join and try again.", show_alert=True)

@app.on_callback_query(filters.regex("^(about|help|clone)$"))
async def general_callback_handler(client: Client, callback_query: CallbackQuery):
    query = callback_query.data
    
    if query == "about":
        await callback_query.message.edit_text(
            "📚 **About Me**\n\n"
            "I'm a powerful bot designed to help you create permanent shareable links for your files.\n\n"
            "**Key Features:**\n"
            "✨ **File-to-Link:** Convert any file into a unique link.\n"
            "🔒 **Secure:** Your files are stored securely.\n"
            "🚀 **Fast & Reliable:** Get your link in seconds.\n"
            "🔗 **Permanent:** Links won't expire.\n\n"
            "Made with ❤️ by [Your Name or Bot Creator's Name].",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")]])
        )
    elif query == "help":
        await callback_query.message.edit_text(
            "💡 **How to Use?**\n\n"
            "1. **Send a File:** Just send me any document, photo, video, or audio file.\n\n"
            "2. **Get Your Link:** I will instantly process it and reply with a unique link.\n\n"
            "3. **Share:** You can share this link with anyone, anywhere! When they click it, the file will be sent to them directly.\n\n"
            "**Example:** You send a PDF. I give you a link. Your friend clicks the link, and I send them that PDF.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")]])
        )
    elif query == "clone":
        await callback_query.message.edit_text(
            "➕ **Clone Me!**\n\n"
            "You can create your very own version of this bot! All you need is your own **API ID**, **API HASH**, and **Bot Token**.\n\n"
            "**How it works:**\n"
            "1. Get your API details from [my.telegram.org](https://my.telegram.org).\n"
            "2. Create a new bot with [@BotFather](https://t.me/BotFather) to get a new Bot Token.\n"
            "3. Click the button below to start the cloning process!\n\n"
            "This feature is coming soon! For now, you can deploy the code directly from the repository.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")]])
        )
    elif query == "start_menu":
        buttons = [
            [InlineKeyboardButton("📚 About Bot", callback_data="about")],
            [InlineKeyboardButton("💡 How to Use?", callback_data="help"),
             InlineKeyboardButton("➕ Clone Bot", callback_data="clone")]
        ]
        await callback_query.message.edit_text(
            "**Hello, {0}! I'm a powerful File-to-Link Bot!** 🤖\n\n"
            "Just send me any file, and I'll give you a **permanent, shareable link** for it. "
            "It's fast, secure, and super easy! ✨".format(callback_query.from_user.first_name),
            reply_markup=InlineKeyboardMarkup(buttons)
        )

# --- Bot's Main Entry Point ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("⚠️ WARNING: ADMIN_IDS is not set. The /settings command and private mode will not work.")
    
    # Start the Flask web server in a separate thread
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("Bot is starting...")
    app.run()
    logging.info("Bot has stopped.")

