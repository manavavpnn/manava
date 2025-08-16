import os
import json
import qrcode
import asyncio
import logging
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, PicklePersistence

# ===== تنظیمات =====
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

ADMINS = [8122737247, 7844158638]
ADMIN_GROUP_ID = -1001234567890
CONFIG_FILE = "configs.json"
USERS_FILE = "users.txt"
ORDERS_FILE = "orders.json"
BLACKLIST_FILE = "blacklist.txt"

CARD_NUMBER = os.getenv("CARD_NUMBER", "6219861812104395")
CARD_NAME = os.getenv("CARD_NAME", "سجاد مؤیدی")

blacklist = set()
orders = {}

# ===== logging =====
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===== توابع کمکی =====
def check_env():
    if not TOKEN:
        raise ValueError("❌ TOKEN در محیط ست نشده!")
    if not WEBHOOK_URL:
        raise ValueError("❌ WEBHOOK_URL در محیط ست نشده!")
    if not WEBHOOK_URL.startswith("https://"):
        raise ValueError("WEBHOOK_URL باید HTTPS باشه!")

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

def load_blacklist():
    global blacklist
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r") as f:
            blacklist = set(int(line.strip()) for line in f if line.strip())
    else:
        blacklist = set()

def save_blacklist():
    with open(BLACKLIST_FILE, "w") as f:
        for user_id in blacklist:
            f.write(f"{user_id}\n")

# ===== هندلرها =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    save_user(user_id)
    if user_id in blacklist:
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    keyboard = [
        [InlineKeyboardButton("💳 خرید کانفیگ", callback_data="buy")],
        [InlineKeyboardButton("📞 پشتیبانی", callback_data="support")]
    ]
    # اضافه کردن دکمه پنل ادمین فقط برای ادمین‌ها
    if user_id in ADMINS:
        keyboard.append([InlineKeyboardButton("🔧 پنل ادمین", callback_data="admin_panel")])
    await update.message.reply_text(
        "سلام 👋\nبه ربات فروش کانفیگ خوش آمدید.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "buy":
        await query.edit_message_text("لیست کانفیگ‌ها...")
        qr_path = make_qr()
        await query.message.reply_photo(photo=open(qr_path, "rb"), caption=f"شماره کارت: {CARD_NUMBER}\nنام: {CARD_NAME}")
    elif query.data == "support":
        await query.edit_message_text("پشتیبانی: @manava_vpn")
    elif query.data == "admin_panel":
        await query.edit_message_text("پنل ادمین: در حال توسعه...")  # اینجا می‌توانید منطق پنل ادمین را اضافه کنید

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"خطا: {context.error}")

# ===== مسیر پینگ =====
async def handle_ping(request):
    return web.Response(text="OK")

# ===== main =====
async def main():
    check_env()
    load_orders()
    load_blacklist()

    application = Application.builder().token(TOKEN).persistence(PicklePersistence("bot_data.pkl")).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_error_handler(error_handler)

    # هندلر وبهوک
    async def webhook(request):
        try:
            data = await request.json()
            logger.info("دریافت آپدیت")
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
            return web.Response(status=200)
        except Exception as e:
            logger.error(f"خطا در webhook: {e}")
            return web.Response(status=400)

    # اپلیکیشن aiohttp
    app = web.Application()
    app.router.add_post(f"/{TOKEN}", webhook)
    app.router.add_get("/ping", handle_ping)

    # راه‌اندازی سرور
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    # تنظیم وبهوک تلگرام
    await application.bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")

    # راه‌اندازی اپلیکیشن
    await application.initialize()
    await application.start()

    # اجرای بی‌نهایت تا توقف
    try:
        await asyncio.Event().wait()
    finally:
        await application.stop()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
