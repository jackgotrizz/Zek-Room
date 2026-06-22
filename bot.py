import os
import json
import random
import string
import asyncio
import datetime
import base64
import discord
from discord import app_commands
import aiohttp
from aiohttp import web
from dotenv import load_dotenv
import hashlib
import binascii
import bcrypt

# ---------------------------------------------------------
# STRICT ENVIRONMENT LOADER
# ---------------------------------------------------------

env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(env_path)

TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
WEB_API_PORT = int(os.getenv("WEB_API_PORT", 5000))
CODESTORE_PASSWORD = os.getenv("CODESTORE_PASSWORD", "default_password").encode()

print("========================================")
print(f"Loaded .env from: {env_path}")
print(f"DISCORD_TOKEN loaded: {'YES' if TOKEN else 'NO'}")
print(f"DISCORD_CLIENT_ID loaded: {CLIENT_ID if CLIENT_ID else 'MISSING'}")
print(f"CODESTORE_PASSWORD set: {'YES' if CODESTORE_PASSWORD else 'NO'}")
print("========================================")

if not TOKEN:
    raise SystemExit("❌ DISCORD_TOKEN missing — fix your .env file.")

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

TEST_SERVER_ID = discord.Object(id=1490180545681031402)
DEFAULT_CHANNEL_ID = 1490180547010363515

EXACT_TITLE_ID = "2819F"
EXACT_PLAYFAB_URL = f"https://{EXACT_TITLE_ID}.playfabapi.com/Client/RegisterPlayFabUser"
EXACT_PASSWORD_RESET_URL = f"https://{EXACT_TITLE_ID}.playfabapi.com/Client/SendAccountRecoveryEmail"

WEB_API_HOST = "127.0.0.1"
CODE_STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "codes.json")

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}

# ---------------------------------------------------------
# ENCRYPTED CODE STORE SYSTEM
# ---------------------------------------------------------

def derive_key(password: bytes) -> bytes:
    """Derive a 32-byte key from a password using bcrypt."""
    salt = b"$2b$12$abcdefghijklmnopqrstuv"  # static salt for deterministic key
    hashed = bcrypt.kdf(
        password=password,
        salt=salt,
        desired_key_bytes=32,
        rounds=100
    )
    return hashed

KEY = derive_key(CODESTORE_PASSWORD)

def xor_encrypt(data: bytes, key: bytes) -> bytes:
    return bytes([b ^ key[i % len(key)] for i, b in enumerate(data)])

def encrypt_json(data: dict) -> str:
    raw = json.dumps(data).encode()
    encrypted = xor_encrypt(raw, KEY)
    return base64.b64encode(encrypted).decode()

def decrypt_json(blob: str) -> dict:
    try:
        encrypted = base64.b64decode(blob)
        decrypted = xor_encrypt(encrypted, KEY)
        return json.loads(decrypted.decode())
    except Exception:
        return {"codes": {}}

def load_code_store():
    if not os.path.exists(CODE_STORE_PATH):
        return {"codes": {}}

    try:
        with open(CODE_STORE_PATH, "r", encoding="utf-8") as file:
            content = file.read().strip()

            if content.startswith("ENCRYPTED:"):
                blob = content.replace("ENCRYPTED:", "")
                return decrypt_json(blob)

            # fallback for old unencrypted files
            return json.loads(content)

    except Exception:
        return {"codes": {}}

def save_code_store(store):
    try:
        encrypted_blob = encrypt_json(store)
        with open(CODE_STORE_PATH, "w", encoding="utf-8") as file:
            file.write("ENCRYPTED:" + encrypted_blob)
    except Exception as exc:
        print(f"⚠️ Failed to save encrypted code store: {exc}")

def generate_random_code(length=24):
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(length))

# ---------------------------------------------------------
# DISCORD BOT CLASS
# ---------------------------------------------------------

class PlayFabBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.code_store = load_code_store()

    async def on_ready(self):
        print(f"🤖 Logged in as {self.user}")
        print("----------------------------------------")

        # Sync commands to test guild
        try:
            self.tree.copy_global_to(guild=TEST_SERVER_ID)
            await self.tree.sync(guild=TEST_SERVER_ID)
            print("✅ Commands synced to test guild!")
        except Exception as exc:
            print(f"⚠️ Guild sync failed: {exc}")
            try:
                await self.tree.sync()
                print("ℹ️ Global sync fallback succeeded.")
            except Exception as exc2:
                print(f"⚠️ Global sync failed: {exc2}")

        # Start web API
        asyncio.create_task(self.start_web_api())

        # Invite URL
        if CLIENT_ID:
            invite_url = (
                f"https://discord.com/oauth2/authorize?"
                f"client_id={CLIENT_ID}&permissions=19456&scope=bot%20applications.commands"
            )
            print(f"🔗 Invite URL: {invite_url}")

        guild = self.get_guild(TEST_SERVER_ID.id)
        if guild:
            print(f"✅ Bot is in guild: {guild.name} ({guild.id})")
        else:
            print("⚠️ Bot is NOT in the target guild. Invite it using the URL above.")

        print("----------------------------------------")

    # ---------------------------------------------------------
    # WEB API
    # ---------------------------------------------------------

    async def start_web_api(self):
        app = web.Application()
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/check-code", self.handle_check_code)
        app.router.add_post("/redeem-code", self.handle_redeem_code)
        app.router.add_options("/redeem-code", self.handle_options)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, WEB_API_HOST, WEB_API_PORT)
        await site.start()

        print(f"🌐 Web API running at http://{WEB_API_HOST}:{WEB_API_PORT}")

    async def handle_health(self, request):
        return web.json_response({"status": "ok"}, headers=CORS_HEADERS)

    async def handle_options(self, request):
        return web.Response(status=200, headers=CORS_HEADERS)

    async def handle_redeem_code(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "message": "Invalid JSON request body."},
                status=400,
                headers=CORS_HEADERS,
            )

        code = str(payload.get("code", "")).strip().upper()
        username = str(payload.get("username", "")).strip()
        email = str(payload.get("email", "")).strip()
        password = str(payload.get("password", "")).strip()

        if not code or not username or not email or not password:
            return web.json_response(
                {"success": False, "message": "Username, email and code are all required."},
                status=400,
                headers=CORS_HEADERS,
            )

        entry = self.code_store.get("codes", {}).get(code)
        if not entry:
            return web.json_response(
                {"success": False, "message": "That code is invalid or does not exist."},
                status=404,
                headers=CORS_HEADERS,
            )

        if entry.get("used"):
            return web.json_response(
                {"success": False, "message": "That code has already been redeemed."},
                status=409,
                headers=CORS_HEADERS,
            )

        # Hash the password before storing
        try:
            salt = os.urandom(16)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
            salt_hex = binascii.hexlify(salt).decode("ascii")
            hash_hex = binascii.hexlify(dk).decode("ascii")
        except Exception:
            return web.json_response(
                {"success": False, "message": "Failed to process password."},
                status=500,
                headers=CORS_HEADERS,
            )

        entry["used"] = True
        entry["redeemed_by"] = {
            "username": username,
            "email": email,
            "redeemed_at": datetime.datetime.utcnow().isoformat() + "Z",
            "password_salt": salt_hex,
            "password_hash": hash_hex,
        }
        self.code_store["codes"][code] = entry
        save_code_store(self.code_store)

        return web.json_response(
            {
                "success": True,
                "message": "Code accepted. Your website account has been created.",
            },
            headers=CORS_HEADERS,
        )

    async def handle_check_code(self, request):
        code = str(request.rel_url.query.get("code", "")).strip().upper()
        if not code:
            return web.json_response(
                {"success": False, "message": "Code query parameter is required."},
                status=400,
                headers=CORS_HEADERS,
            )

        entry = self.code_store.get("codes", {}).get(code)
        if not entry:
            return web.json_response({"success": True, "exists": False}, headers=CORS_HEADERS)

        return web.json_response(
            {"success": True, "exists": True, "used": bool(entry.get("used", False))},
            headers=CORS_HEADERS,
        )

# ---------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------

bot = PlayFabBot()

@bot.tree.command(name="generate-code", description="Use this to make an account on the website")
async def generate_code(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    code = generate_random_code(26)
    created_at = datetime.datetime.utcnow().isoformat() + "Z"

    bot.code_store.setdefault("codes", {})[code] = {
        "created_at": created_at,
        "created_by": interaction.user.name,
        "created_by_id": str(interaction.user.id),
        "used": False,
    }
    save_code_store(bot.code_store)

    dm_text = (
        f"🔐 Your website account code is ready.\n"
        f"Code: `{code}`\n\n"
        f"Use this code on the website signup page to create your account."
    )

    try:
        await interaction.user.send(dm_text)
        dm_status = "Your code was also sent in DMs."
    except discord.Forbidden:
        dm_status = "Unable to DM you. Please make sure your DMs are open."

    await interaction.followup.send(
        f"✅ Website code generated. {dm_status}",
        ephemeral=True,
    )

@bot.tree.command(name="reset-password", description="Send a PlayFab password reset email to your account.")
@app_commands.describe(
    email="The email address tied to your PlayFab account"
)
async def reset_password(interaction: discord.Interaction, email: str):
    await interaction.response.defer(ephemeral=True)

    headers = {"Content-Type": "application/json"}
    payload = {
        "TitleId": EXACT_TITLE_ID,
        "Email": email
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(EXACT_PASSWORD_RESET_URL, json=payload, headers=headers) as response:
                data = await response.json()

                if response.status == 200 and data.get("code") == 200:
                    await interaction.followup.send(
                        f"✅ **Password Reset Email Sent!**\n"
                        f"A reset link has been sent to **{email}**. Check your inbox and spam folder.",
                        ephemeral=True
                    )
                else:
                    error_msg = data.get("errorMessage", "Unknown PlayFab rejection.")
                    await interaction.followup.send(
                        f"❌ **Reset Failed:** {error_msg}",
                        ephemeral=True
                    )

    except Exception as e:
        await interaction.followup.send(
            f"❌ **Network Error:** Failed hitting connection loop. ({str(e)})",
            ephemeral=True
        )

@bot.tree.command(name="list-commands", description="Show available bot commands and usage")
async def list_commands(interaction: discord.Interaction):
    msg = (
        "Available commands:\n"
        "• /generate-code — Generates a website signup code and DMs it to you (no parameters).\n"
        "• /reset-password email:<your email> — Sends a PlayFab password reset email.\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)

# ---------------------------------------------------------
# RUN BOT
# ---------------------------------------------------------

if __name__ == "__main__":
    bot.run(TOKEN)
