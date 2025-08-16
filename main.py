import os
import json
import qrcode
import asyncio
import logging
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ConversationHandler, PicklePersistence

# ===== تنظیمات =====
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

ADMINS = [8122737247, 7844158638]
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID")
CONFIG_FILE = "configs.json"
USERS_FILE = "users.txt"
ORDERS_FILE = "orders.json"
BLACKLIST_FILE = "blacklist.txt"

CARD_NUMBER = os.getenv("CARD_NUMBER", "6219861812104395")
CARD_NAME = os.getenv("CARD_NAME", "سجاد مؤیدی")

blacklist = set()
orders = {}
configs = []

# ===== logging =====
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===== حالت‌های کانورسیشن =====
ADD_CONFIG_VOLUME, ADD_CONFIG_DURATION, ADD_CONFIG_PRICE, ADD_CONFIG_LINK = range(4)
REMOVE_CONFIG_ID = range(1)
BUY_CONFIG_CHOOSE = range(1)

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
    return len(users) + 1  # تعداد کل کاربران

def load_configs():
    global configs
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            configs = json.load(f)
    else:
        configs = []

def save_configs():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(configs, f, ensure_ascii=False, indent=2)

def make_qr():
    img = qrcode.make(CARD_NUMBER)
    img.save("card_qr.png")
    return "card_qr.png"

def group_configs(configs):
    grouped = {}
    for cfg in configs:
        key = f"{cfg['حجم']} - {cfg['مدت']} - {cfg['قیمت']}"
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

def get_stats():
    total_users = len(set(line.strip() for line in open(USERS_FILE, "r", encoding="utf-8") if line.strip()))
    total_configs = len(configs)
    total_orders = len(orders)
    pending_orders = sum(1 for order in orders.values() if order['status'] == 'pending')
    return f"📊 آمار:\nکاربران: {total_users}\nکانفیگ‌ها: {total_configs}\nسفارش‌ها: {total_orders}\nسفارش‌های در انتظار: {pending_orders}"

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
        if not configs:
            await query.edit_message_text("هیچ کانفیگی موجود نیست.")
            return
        keyboard = [[InlineKeyboardButton(f"{cfg['حجم']} - {cfg['مدت']} - {cfg['قیمت']}", callback_data=f"buy_config_{cfg['id']}")] for cfg in configs]
        keyboard.append([InlineKeyboardButton("لغو", callback_data="cancel")])
        await query.edit_message_text("لطفاً یک کانفیگ انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif query.data == "support":
        await query.edit_message_text("پشتیبانی: @support_username")
    elif query.data == "admin_panel":
        keyboard = [
            ["/add_config", "/remove_config"],
            ["/list_orders", "/approve_order"],
            ["/stats", "/cancel"]
        ]
        await query.edit_message_text(
            "پنل ادمین: دستورات زیر را انتخاب کنید.",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
        )
    elif query.data.startswith("buy_config_"):
        config_id = int(query.data.split("_")[2])
        context.user_data['selected_config'] = config_id
        qr_path = make_qr()
        order_id = str(len(orders) + 1)
        orders[order_id] = {
            'user_id': query.from_user.id,
            'config_id': config_id,
            'status': 'pending'
        }
        save_orders()
        config = next((cfg for cfg in configs if cfg['id'] == config_id), None)
        if config:
            await query.message.reply_photo(
                photo=open(qr_path, "rb"),
                caption=f"لطفاً مبلغ {config['قیمت']} تومان به شماره کارت زیر واریز کنید:\n{CARD_NUMBER}\nنام: {CARD_NAME}\nID سفارش: {order_id}"
            )
            # ارسال نوتیفیکیشن به گروه ادمین
            await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=f"سفارش جدید:\nکاربر: {query.from_user.id}\nID سفارش: {order_id}\nکانفیگ: {config['حجم']} - {config['مدت']} - {config['قیمت']}"
            )
        await query.edit_message_text("سفارش شما ثبت شد. پس از پرداخت، منتظر تایید باشید.")

async def add_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return ConversationHandler.END
    await update.message.reply_text("حجم کانفیگ را وارد کنید (مثل 10GB):")
    return ADD_CONFIG_VOLUME

async def add_config_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['volume'] = update.message.text
    await update.message.reply_text("مدت زمان (مثل 30 روز):")
    return ADD_CONFIG_DURATION

async def add_config_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['duration'] = update.message.text
    await update.message.reply_text("قیمت (به تومان):")
    return ADD_CONFIG_PRICE

async def add_config_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['price'] = update.message.text
    await update.message.reply_text("لینک کانفیگ:")
    return ADD_CONFIG_LINK

async def add_config_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text
    new_config = {
        'حجم': context.user_data['volume'],
        'مدت': context.user_data['duration'],
        'قیمت': context.user_data['price'],
        'لینک': link,
        'id': len(configs) + 1
    }
    configs.append(new_config)
    save_configs()
    await update.message.reply_text(f"کانفیگ جدید اضافه شد: {new_config}")
    return ConversationHandler.END

async def remove_config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return ConversationHandler.END
    config_list = "\n".join([f"ID: {cfg['id']} - {cfg['حجم']} - {cfg['مدت']} - {cfg['قیمت']}" for cfg in configs])
    await update.message.reply_text(f"لیست کانفیگ‌ها:\n{config_list}\nID کانفیگ برای حذف:")
    return REMOVE_CONFIG_ID

async def remove_config_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        config_id = int(update.message.text)
        global configs
        configs = [cfg for cfg in configs if cfg['id'] != config_id]
        save_configs()
        await update.message.reply_text(f"کانفیگ با ID {config_id} حذف شد.")
    except ValueError:
        await update.message.reply_text("ID نامعتبر.")
    return ConversationHandler.END

async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    if not orders:
        await update.message.reply_text("هیچ سفارشی وجود ندارد.")
        return
    order_list = "\n".join([f"Order ID: {oid} - User: {order['user_id']} - Config: {order['config_id']} - Status: {order['status']}" for oid, order in orders.items()])
    await update.message.reply_text(f"لیست سفارش‌ها:\n{order_list}")

async def approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    if not context.args:
        await update.message.reply_text("Order ID را وارد کنید: /approve_order <order_id>")
        return
    order_id = context.args[0]
    if order_id in orders:
        orders[order_id]['status'] = 'approved'
        order = orders[order_id]
        config = next((cfg for cfg in configs if cfg['id'] == order['config_id']), None)
        if config:
            await context.bot.send_message(
                chat_id=order['user_id'],
                text=f"سفارش شما تایید شد. لینک: {config['لینک']}"
            )
        await update.message.reply_text(f"سفارش {order_id} تایید شد.")
        save_orders()
    else:
        await update.message.reply_text("سفارش یافت نشد.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    await update.message.reply_text(get_stats())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END

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
    load_configs()

    application = Application.builder().token(TOKEN).persistence(PicklePersistence("bot_data.pkl")).build()

    application.add_handler(CommandHandler("start", start, filters=filters.ChatType.PRIVATE))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))
    application.add_handler(CallbackQueryHandler(button_handler))

    add_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add_config", add_config, filters=filters.ChatType.PRIVATE)],
        states={
            ADD_CONFIG_VOLUME: [MessageHandler(filters.TEXT, add_config_volume)],
            ADD_CONFIG_DURATION: [MessageHandler(filters.TEXT, add_config_duration)],
            ADD_CONFIG_PRICE: [MessageHandler(filters.TEXT, add_config_price)],
            ADD_CONFIG_LINK: [MessageHandler(filters.TEXT, add_config_link)],
        },
        fallbacks=[CommandHandler("cancel", cancel, filters=filters.ChatType.PRIVATE)]
    )
    application.add_handler(add_conv_handler)

    remove_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("remove_config", remove_config, filters=filters.ChatType.PRIVATE)],
        states={
            REMOVE_CONFIG_ID: [MessageHandler(filters.TEXT, remove_config_id)],
        },
        fallbacks=[CommandHandler("cancel", cancel, filters=filters.ChatType.PRIVATE)]
    )
    application.add_handler(remove_conv_handler)

    application.add_handler(CommandHandler("list_orders", list_orders, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("approve_order", approve_order, filters=filters.ChatType.PRIVATE))
    application.add_handler(CommandHandler("stats", stats, filters=filters.ChatType.PRIVATE))

    application.add_error_handler(error_handler)

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

    app = web.Application()
    app.router.add_post(f"/{TOKEN}", webhook)
    app.router.add_get("/ping", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    await application.bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")

    await application.initialize()
    await application.start()

    try:
        await asyncio.Event().wait()
    finally:
        await application.stop()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
