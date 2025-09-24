import os
import logging
import random
import string
import shutil
import subprocess
import time
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import UserNotParticipant, Unauthorized
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
UPDATE_CHANNELS = ["bestshayri_raj", "go_esports"]
ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "")
ADMINS = [int(admin_id.strip()) for admin_id in ADMIN_IDS_STR.split(',') if admin_id]

# --- Database Setup ---
try:
    client = MongoClient(MONGO_URI)
    db = client['file_link_bot_super_adv']
    files_collection = db['files']
    multi_file_collection = db['multi_files']
    settings_collection = db['settings']
    # Use a specific collection for temporary user states
    user_state_collection = db['user_states']
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

async def validate_bot_token(token: str) -> bool:
    """Validates a bot token by trying to create a client."""
    try:
        temp_client = Client("temp_cloner", bot_token=token, api_id=API_ID, api_hash=API_HASH)
        await temp_client.start()
        await temp_client.stop()
        return True
    except Unauthorized:
        return False
    except Exception as e:
        logging.error(f"Error validating token: {e}")
        return False

# --- Bot Command Handlers ---

@app.on_message(filters.command("start") & filters.private)
async def start_handler(client: Client, message: Message):
    if len(message.command) > 1:
        # User is coming from a link
        file_id_str = message.command[1]

        # Check for multiple channel membership
        missing_channels = await is_user_member_all_channels(client, message.from_user.id)
        if missing_channels:
            join_buttons = []
            for channel in missing_channels:
                join_buttons.append([InlineKeyboardButton(f"🔗 Join @{channel}", url=f"https://t.me/{channel}")])
            join_buttons.append([InlineKeyboardButton("✅ I Have Joined", callback_data=f"check_join_{file_id_str}")])

            keyboard = InlineKeyboardMarkup(join_buttons)

            await message.reply(
                f"👋 **Hello, {message.from_user.first_name}!**\n\nTo access this file, you must first join the following channels:",
                reply_markup=keyboard,
                quote=True
            )
            return

        # Check if it's a single file or a multi-file link
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
        # Normal /start command
        buttons = [
            [InlineKeyboardButton("📚 About Bot", callback_data="about")],
            [InlineKeyboardButton("💡 How to Use?", callback_data="help"),
             InlineKeyboardButton("➕ Clone Bot", callback_data="clone_info")],
            [InlineKeyboardButton("🔗 Join Channels", callback_data="join_channels")]
        ]
        
        await message.reply(
            f"**Hello, {message.from_user.first_name}! I'm a powerful File-to-Link Bot!** 🤖\n\n"
            "Just send me any file, or forward multiple files as a single message, and I'll give you a **permanent, shareable link** for it. "
            "It's fast, secure, and super easy! ✨",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

@app.on_message(filters.command("clone") & filters.private & filters.user(ADMINS))
async def clone_bot_command(client: Client, message: Message):
    if len(message.command) > 1:
        token = message.command[1]
        
        await message.reply("⏳ Validating your bot token... Please wait.")
        if not await validate_bot_token(token):
            await message.reply("❌ Invalid bot token! Please make sure you have copied it correctly.")
            return

        # Start the cloning process
        try:
            status_msg = await message.reply("🚀 Cloning process started! This may take a moment.")
            
            # Create a unique directory for the new bot's files
            new_bot_dir = f"cloned_bot_{generate_random_string(8)}"
            os.makedirs(new_bot_dir, exist_ok=True)
            
            # Create a new .env file for the new bot
            env_file_path = os.path.join(new_bot_dir, ".env")
            with open(env_file_path, "w") as f:
                f.write(f"API_ID={API_ID}\n")
                f.write(f"API_HASH={API_HASH}\n")
                f.write(f"BOT_TOKEN={token}\n")
                f.write(f"MONGO_URI={MONGO_URI}\n")
                f.write(f"LOG_CHANNEL={LOG_CHANNEL}\n")
                f.write(f"UPDATE_CHANNEL={','.join(UPDATE_CHANNELS)}\n")
                f.write(f"ADMIN_IDS={message.from_user.id}\n")

            # Copy the main script to the new directory
            script_path = os.path.basename(__file__)
            shutil.copy(script_path, os.path.join(new_bot_dir, script_path))
            
            # Start the new bot process
            subprocess.Popen(
                ["python3", script_path],
                cwd=new_bot_dir, # Run from the new directory
                env=dict(os.environ, DOTENV_PATH=env_file_path),
                close_fds=True
            )
            
            await status_msg.edit_text("✅ Your bot has been cloned successfully! It should start shortly.")
            
        except Exception as e:
            logging.error(f"Cloning error: {e}")
            await message.reply(f"❌ An error occurred during cloning.\n`Error: {e}`")

    else:
        # User sent /clone without a token, prompt for it
        await message.reply("Please send me your bot token. Just reply to this message with the token. Example:\n\n`token_here`")
        # Set a temporary state to process the next message as the token
        user_state_collection.update_one(
            {"_id": message.from_user.id},
            {"$set": {"state": "waiting_for_token"}},
            upsert=True
        )

# A new handler for the state-based token input
@app.on_message(filters.private & ~filters.command(["start", "clone"]))
async def process_user_state(client: Client, message: Message):
    user_id = message.from_user.id
    state = user_state_collection.find_one({"_id": user_id})

    if state and state.get("state") == "waiting_for_token":
        token = message.text.strip()
        user_state_collection.delete_one({"_id": user_id}) # Clear the state
        
        await message.reply("⏳ Validating your bot token... Please wait.")
        if not await validate_bot_token(token):
            await message.reply("❌ Invalid bot token! Please make sure you have copied it correctly.")
            return

        # Proceed with the cloning process (same as the /clone handler)
        try:
            status_msg = await message.reply("🚀 Cloning process started! This may take a moment.")
            
            new_bot_dir = f"cloned_bot_{generate_random_string(8)}"
            os.makedirs(new_bot_dir, exist_ok=True)
            
            env_file_path = os.path.join(new_bot_dir, ".env")
            with open(env_file_path, "w") as f:
                f.write(f"API_ID={API_ID}\n")
                f.write(f"API_HASH={API_HASH}\n")
                f.write(f"BOT_TOKEN={token}\n")
                f.write(f"MONGO_URI={MONGO_URI}\n")
                f.write(f"LOG_CHANNEL={LOG_CHANNEL}\n")
                f.write(f"UPDATE_CHANNEL={','.join(UPDATE_CHANNELS)}\n")
                f.write(f"ADMIN_IDS={message.from_user.id}\n")

            script_path = os.path.basename(__file__)
            shutil.copy(script_path, os.path.join(new_bot_dir, script_path))
            
            subprocess.Popen(
                ["python3", script_path],
                cwd=new_bot_dir,
                env=dict(os.environ, DOTENV_PATH=env_file_path),
                close_fds=True
            )
            
            await status_msg.edit_text("✅ Your bot has been cloned successfully! It should start shortly.")
        except Exception as e:
            logging.error(f"Cloning error: {e}")
            await message.reply(f"❌ An error occurred during cloning.\n`Error: {e}`")

# ... (rest of the code for file_handler, multi_link_handler, etc. remains the same) ...

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
        await callback_query.message.edit_text(
            "➕ **Clone Me!**\n\n"
            "Admins can create their very own version of this bot in just one step! All you need is a **Bot Token** from @BotFather.\n\n"
            "**How it works:**\n"
            "1. Create a new bot with [@BotFather](https://t.me/BotFather) to get a new Bot Token.\n"
            "2. Send me the command: `/clone your_bot_token` (without the angle brackets).\n"
            "3. The bot will automatically start a new instance with all the same features as this one!\n\n"
            "Your new bot will use the same database and channels, and you will be set as the first admin.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Start", callback_data="start_menu")]])
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
            [InlineKeyboardButton("📚 About Bot", callback_data="about")],
            [InlineKeyboardButton("💡 How to Use?", callback_data="help"),
             InlineKeyboardButton("➕ Clone Bot", callback_data="clone_info")],
            [InlineKeyboardButton("🔗 Join Channels", callback_data="join_channels")]
        ]
        await callback_query.message.edit_text(
            f"**Hello, {callback_query.from_user.first_name}! I'm a powerful File-to-Link Bot!** 🤖\n\n"
            "Just send me any file, or forward multiple files as a single message, and I'll give you a **permanent, shareable link** for it. "
            "It's fast, secure, and super easy! ✨",
            reply_markup=InlineKeyboardMarkup(buttons)
        )


# --- Bot's Main Entry Point ---
if __name__ == "__main__":
    if not ADMINS:
        logging.warning("⚠️ WARNING: ADMIN_IDS is not set. The /settings and /clone commands will not work.")
    
    logging.info("Starting Flask web server...")
    flask_thread = Thread(target=run_flask)
    flask_thread.start()
    
    logging.info("Bot is starting...")
    app.run()
    logging.info("Bot has stopped.")
