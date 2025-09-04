import os
import json
import asyncio
import logging
import uuid
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler, ConversationHandler, PicklePersistence
from telegram.request import HTTPXRequest
from telegram.error import TimedOut

# ===== تنظیمات =====
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID", "-1001234567890")

ADMINS = [8122737247, 7844158638]
CONFIG_FILE = "configs.json"
USERS_FILE = "users.txt"
ORDERS_FILE = "orders.json"
BLACKLIST_FILE = "blacklist.txt"

CARD_NUMBER = os.getenv("CARD_NUMBER", "6219861812104395")
CARD_NAME = os.getenv("CARD_NAME", "سجاد مؤیدی")

blacklist = set()
orders = {}
configs = []
users_cache = set()

# ===== logging =====
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ===== حالت‌های کانورسیشن =====
ADD_CONFIG_VOLUME, ADD_CONFIG_DURATION, ADD_CONFIG_PRICE, ADD_CONFIG_LINK = range(4)
REMOVE_CONFIG_ID = 0
SUBMIT_PAYMENT = 1

# ===== توابع کمکی =====
def check_env():
    if not TOKEN:
        raise ValueError("❌ TOKEN در محیط ست نشده!")
    if not WEBHOOK_URL:
        raise ValueError("❌ WEBHOOK_URL در محیط ست نشده!")
    if not WEBHOOK_URL.startswith("https://"):
        raise ValueError("WEBHOOK_URL باید HTTPS باشه!")
    if not ADMIN_GROUP_ID:
        raise ValueError("❌ ADMIN_GROUP_ID در محیط ست نشده!")

def save_user(user_id: int) -> int:
    global users_cache
    if not os.path.exists(USERS_FILE):
        open(USERS_FILE, "w", encoding="utf-8").close()
    if str(user_id) not in users_cache:
        with open(USERS_FILE, "a", encoding="utf-8") as f:
            f.write(str(user_id) + "\n")
        users_cache.add(str(user_id))
    return len(users_cache)

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

def load_users_cache():
    global users_cache
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            users_cache = {line.strip() for line in f if line.strip()}

def get_stats():
    total_configs = len(configs)
    total_orders = len(orders)
    pending_orders = sum(1 for order in orders.values() if order['status'] == 'pending')
    return f"📊 آمار:\nکاربران: {len(users_cache)}\nکانفیگ‌ها: {total_configs}\nسفارش‌ها: {total_orders}\nسفارش‌های در انتظار: {pending_orders}"

def group_configs(configs):
    grouped = {}
    for config in configs:
        key = f"{config['حجم']} - {config['مدت']}"
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(config)
    return grouped

# ===== هندلرها =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in blacklist:
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    save_user(user_id)
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
    try:
        await query.answer()
    except TimedOut as e:
        logger.error(f"Timeout در پاسخ به callback query: {e}", exc_info=True)
        await query.message.reply_text("خطای اتصال به سرور. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.")
        return
    logger.info(f"Callback query received: {query.data}")
    
    if query.data == "buy":
        if not configs:
            await query.edit_message_text("هیچ کانفیگی موجود نیست.")
            return
        grouped = group_configs(configs)
        keyboard = [[InlineKeyboardButton(f"{key} (موجود: {len(cfgs)})", callback_data=f"buy_config_{cfgs[0]['id']}")] for key, cfgs in grouped.items()]
        keyboard.append([InlineKeyboardButton("لغو", callback_data="cancel")])
        await query.edit_message_text("لطفاً یک کانفیگ انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif query.data == "support":
        await query.edit_message_text("پشتیبانی: @manava_vpn")
    
    elif query.data == "admin_panel":
        keyboard = [
            ["/add_config", "/remove_config"],
            ["/list_orders", "/approve_order"],
            ["/stats", "/cancel"]
        ]
        await query.edit_message_text("پنل ادمین باز شد.")
        await query.message.reply_text(
            "دستورات پنل ادمین:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
    
    elif query.data.startswith("buy_config_"):
        config_id = int(query.data.split("_")[2])
        context.user_data['selected_config'] = config_id
        config = next((cfg for cfg in configs if cfg['id'] == config_id), None)
        if not config:
            await query.edit_message_text("کانفیگ یافت نشد.")
            return
        
        order_id = str(uuid.uuid4())
        orders[order_id] = {
            'user_id': query.from_user.id,
            'username': query.from_user.username or "بدون یوزرنیم",
            'config_id': config_id,
            'status': 'pending'
        }
        save_orders()
        
        try:
            await query.edit_message_text(
                f"لطفاً مبلغ {config['قیمت']} تومان به شماره کارت زیر واریز کنید:\n{CARD_NUMBER}\nنام: {CARD_NAME}\nID سفارش: {order_id}\nلطفاً رسید پرداخت را به پشتیبانی ارسال کنید."
            )
            
            # ارسال پیام به گروه ادمین‌ها با دکمه‌های تأیید/رد
            admin_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ تأیید پرداخت", callback_data=f"approve_{order_id}"),
                 InlineKeyboardButton("❌ رد پرداخت", callback_data=f"reject_{order_id}")]
            ])
            
            admin_message = await context.bot.send_message(
                chat_id=ADMIN_GROUP_ID,
                text=f"📨 سفارش جدید:\n👤 کاربر: {query.from_user.mention_markdown()}\n🆔 ID کاربر: {query.from_user.id}\n📋 ID سفارش: {order_id}\n⚙️ کانفیگ: {config['حجم']} - {config['مدت']}\n💰 قیمت: {config['قیمت']} تومان",
                reply_markup=admin_keyboard,
                parse_mode='Markdown'
            )
            
            # ذخیره message_id برای ویرایش بعدی
            orders[order_id]['admin_message_id'] = admin_message.message_id
            
        except Exception as e:
            logger.error(f"خطا در ارسال پیام: {e}", exc_info=True)
            await query.edit_message_text("خطا در ثبت سفارش. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.")
            return
    
    elif query.data.startswith("approve_"):
        order_id = query.data.split("_")[1]
        if order_id not in orders:
            await query.answer("سفارش یافت نشد!")
            return
        
        order = orders[order_id]
        if order['status'] != 'pending':
            await query.answer("این سفارش قبلاً پردازش شده است!")
            return
        
        # ارسال کانفیگ به کاربر
        config = next((cfg for cfg in configs if cfg['id'] == order['config_id']), None)
        if config:
            try:
                await context.bot.send_message(
                    chat_id=order['user_id'],
                    text=f"✅ پرداخت شما تأیید شد!\n🎉 کانفیگ شما:\n{config['لینک']}"
                )
                orders[order_id]['status'] = 'approved'
                save_orders()
                
                # آپدیت پیام در گروه ادمین
                await query.edit_message_text(
                    text=f"✅ پرداخت تأیید شد:\n👤 کاربر: {order['user_id']}\n📋 سفارش: {order_id}",
                    reply_markup=None
                )
                
            except Exception as e:
                logger.error(f"خطا در ارسال پیام به کاربر: {e}")
                await query.answer("خطا در ارسال پیام به کاربر!")
        else:
            await query.answer("کانفیگ یافت نشد!")
    
    elif query.data.startswith("reject_"):
        order_id = query.data.split("_")[1]
        if order_id not in orders:
            await query.answer("سفارش یافت نشد!")
            return
        
        order = orders[order_id]
        if order['status'] != 'pending':
            await query.answer("این سفارش قبلاً پردازش شده است!")
            return
        
        # بلاک کردن کاربر و آپدیت وضعیت
        blacklist.add(order['user_id'])
        save_blacklist()
        orders[order_id]['status'] = 'rejected'
        save_orders()
        
        try:
            await context.bot.send_message(
                chat_id=order['user_id'],
                text="❌ پرداخت شما رد شد!\n⚠️ لطفاً به پشتیبانی مراجعه کنید: @manava_vpn"
            )
        except Exception as e:
            logger.error(f"خطا در ارسال پیام به کاربر: {e}")
        
        # آپدیت پیام در گروه ادمین
        await query.edit_message_text(
            text=f"❌ پرداخت رد شد:\n👤 کاربر: {order['user_id']}\n📋 سفارش: {order_id}",
            reply_markup=None
        )
    
    elif query.data == "cancel":
        await query.edit_message_text("عملیات لغو شد.")

# بقیه توابع (add_config, remove_config, etc.) بدون تغییر می‌مانند
# ...

# ===== main =====
async def main():
    try:
        check_env()
    except ValueError as e:
        logger.error(f"خطای محیط: {e}", exc_info=True)
        return
    
    load_users_cache()
    load_orders()
    load_blacklist()
    load_configs()

    request = HTTPXRequest(
    connect_timeout=30.0,  # تغییر connection_timeout به connect_timeout
    read_timeout=30.0,
    write_timeout=30.0
)

    application = Application.builder().token(TOKEN).persistence(PicklePersistence("bot_data.pkl")).request(request).build()

    # اضافه کردن هندلرها (مشابه قبل)
    # ...

    async def webhook(request):
        try:
            data = await request.json()
            logger.info("دریافت آپدیت")
            update = Update.de_json(data, application.bot)
            if update:
                await application.process_update(update)
                return web.Response(status=200)
            else:
                logger.warning("آپدیت نامعتبر دریافت شد")
                return web.Response(status=400)
        except Exception as e:
            logger.error(f"خطا در webhook: {e}", exc_info=True)
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
