import os
import json
import random
import qrcode
import asyncio
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, ContextTypes

# ===== تنظیمات =====
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

ADMINS = [8122737247, 7844158638]
ADMIN_GROUP_ID = -1001234567890
CONFIG_FILE = "configs.json"
USERS_FILE = "users.txt"
ORDERS_FILE = "orders.json"

CARD_NUMBER = os.getenv("CARD_NUMBER", "6219861812104395")
CARD_NAME = os.getenv("CARD_NAME", "سجاد مؤیدی")

blacklist = set()
orders = {}
application = None  # اپلیکیشن سراسری

# ===== توابع کمکی =====
def check_env():
    if not TOKEN:
        raise ValueError("❌ TOKEN در محیط ست نشده!")
    if not WEBHOOK_URL:
        raise ValueError("❌ WEBHOOK_URL در محیط ست نشده!")

def save_user(user_id):
    if not os.path.exists(USERS_FILE):
        open(USERS_FILE, "w", encoding="utf-8").close()
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        users = set(line.strip() for line in f if line.strip())
    if str(user_id) not in users:
        with open(USERS_FILE, "a", encoding="utf-8") as f:
            f.write(str(user_id) + "\n")

def read_configs():
    if not os.path.exists(CONFIG_FILE):
        return []
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_configs(configs):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)

def make_qr():
    img = qrcode.make(CARD_NUMBER)
    img.save("card_qr.png")
    return "card_qr.png"

def group_configs(configs):
    grouped = {}
    for cfg in configs:
        key = f"{cfg['حجم']} - {cfg['مدت']}"
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(cfg)
    return grouped

def load_orders():
    global orders
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r", encoding="utf-8") as f:
            orders = json.load(f)
    else:
        orders = {}

def save_orders():
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=2)

# ===== هندلرها =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user.id)
    if update.effective_user.id in blacklist:
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    keyboard = [[InlineKeyboardButton("💳 خرید کانفیگ", callback_data="buy")],
                [InlineKeyboardButton("📞 پشتیبانی", callback_data="support")]]
    await update.message.reply_text("سلام 👋\nبه ربات فروش کانفیگ خوش آمدید.", reply_markup=InlineKeyboardMarkup(keyboard))

# بقیه هندلرهای تو بدون تغییر ...

# ===== UptimeRobot Ping =====
async def handle_ping(request):
    return web.Response(text="OK")

# ===== Webhook handler =====
async def telegram_webhook(request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    except Exception as e:
        import traceback
        print("❌ Webhook error:", e)
        print(traceback.format_exc())
        return web.Response(status=500, text="Internal Server Error")
    return web.Response(text="OK")

# ===== main =====
async def main():
    global application
    check_env()
    load_orders()

    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    # اینجا بقیه CommandHandler و MessageHandler ها رو اضافه کن

    aio_app = web.Application()
    aio_app.router.add_post(f"/webhook/{TOKEN}", telegram_webhook)
    aio_app.router.add_get("/ping", handle_ping)

    info = await application.bot.get_webhook_info()
    print("📡 Webhook Info BEFORE:", info)

    expected_url = f"{WEBHOOK_URL}/webhook/{TOKEN}"
    if info.url != expected_url:
        print("⚠️ Webhook اشتباه یا ثبت نشده، در حال تنظیم...")
        await application.bot.set_webhook(expected_url)
        print("✅ Webhook تنظیم شد!")

    info = await application.bot.get_webhook_info()
    print("📡 Webhook Info AFTER:", info)

    print(f"✅ ربات آماده است. Webhook: {WEBHOOK_URL}/webhook/{TOKEN}")
    print(f"📡 مسیر پینگ UptimeRobot: {WEBHOOK_URL}/ping")

    runner = web.AppRunner(aio_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

if __name__ == "__main__":
    asyncio.run(main())
