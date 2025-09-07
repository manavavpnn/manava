import os
import json
import asyncio
import logging
import uuid
import itertools
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ConversationHandler,
    PicklePersistence
)
from telegram.error import TimedOut

# ===== تنظیمات =====
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))
ADMIN_GROUP_ID = int(os.getenv("ADMIN_GROUP_ID"))  # الزامی کردن env بدون default

ADMINS = list(map(int, os.getenv("ADMINS", "8122737247,7844158638").split(',')))  # از env بخوان، default هاردکد اما بهتر env
CONFIG_FILE = "configs.json"
USERS_FILE = "users.txt"
ORDERS_FILE = "orders.json"
BLACKLIST_FILE = "blacklist.txt"

CARD_NUMBER = os.getenv("CARD_NUMBER")  # الزامی بدون default
CARD_NAME = os.getenv("CARD_NAME")  # الزامی بدون default

blacklist = set()
orders = {}
configs = []
users_cache = set()

# برای ساخت ID یکتا کانفیگ
config_id_counter = itertools.count(1)

# ===== logging =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== حالت‌های کانورسیشن =====
ADD_CONFIG_VOLUME, ADD_CONFIG_DURATION, ADD_CONFIG_PRICE, ADD_CONFIG_LINK = range(4)
REMOVE_CONFIG_ID = 0  # approve دستی حذف شد

# ===== توابع کمکی =====
def check_env():
    missing = []
    if not TOKEN:
        missing.append("TOKEN")
    if not WEBHOOK_URL:
        missing.append("WEBHOOK_URL")
    if not WEBHOOK_URL.startswith("https://"):
        raise ValueError("WEBHOOK_URL باید HTTPS باشد!")
    if not ADMIN_GROUP_ID:
        missing.append("ADMIN_GROUP_ID")
    if not CARD_NUMBER:
        missing.append("CARD_NUMBER")
    if not CARD_NAME:
        missing.append("CARD_NAME")
    if missing:
        raise ValueError(f"❌ متغیرهای محیطی زیر ست نشده‌اند: {', '.join(missing)}")

def save_user(user_id: int) -> int:
    global users_cache
    if str(user_id) not in users_cache:
        with open(USERS_FILE, "a", encoding="utf-8") as f:
            f.write(str(user_id) + "\n")
        users_cache.add(str(user_id))
    return len(users_cache)

def load_configs():
    global configs, config_id_counter
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            configs = json.load(f)
        if configs:
            max_id = max(cfg["id"] for cfg in configs)
            config_id_counter = itertools.count(max_id + 1)
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
    else:
        open(USERS_FILE, "w", encoding="utf-8").close()

def get_stats():
    total_configs = len(configs)
    total_orders = len(orders)
    pending_orders = sum(1 for order in orders.values() if order.get('status') == 'pending')
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
    await query.answer()
    
    if query.data == "buy":
        if not configs:
            await query.edit_message_text("هیچ کانفیگی موجود نیست.")
            return
        grouped = group_configs(configs)
        keyboard = []
        for key, cfgs in grouped.items():
            if cfgs:  # مطمئن شو موجود باشد
                keyboard.append([InlineKeyboardButton(f"{key} (موجود: {len(cfgs)})", callback_data=f"buy_config_{cfgs[0]['id']}")])
        keyboard.append([InlineKeyboardButton("لغو", callback_data="cancel")])
        await query.edit_message_text("لطفاً یک کانفیگ انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif query.data == "support":
        await query.edit_message_text("پشتیبانی: @manava_vpn")
    
    elif query.data == "admin_panel":
        keyboard = [
            ["/add_config", "/remove_config"],
            ["/list_orders", "/stats", "/cancel"]  # approve_order حذف شد
        ]
        await query.edit_message_text("پنل ادمین باز شد.")
        await query.message.reply_text(
            "دستورات پنل ادمین:",
            reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
        )
    
    elif query.data.startswith("buy_config_"):
        config_id = int(query.data.split("_")[2])
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
                f"لطفاً مبلغ {config['قیمت']} تومان به شماره کارت زیر واریز کنید:\n{CARD_NUMBER}\nنام: {CARD_NAME}\nID سفارش: {order_id}\nلطفاً عکس رسید پرداخت را اینجا ارسال کنید."
            )
            context.user_data['pending_order_id'] = order_id  # برای منتظر ماندن رسید
        except Exception as e:
            logger.error(f"خطا در ارسال پیام: {e}", exc_info=True)
            await query.edit_message_text("خطا در ثبت سفارش. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.")
    
    elif query.data.startswith("approve_"):
        order_id = query.data.split("_")[1]
        if order_id not in orders:
            await query.answer("سفارش یافت نشد!")
            return
        
        order = orders[order_id]
        if order['status'] != 'pending':
            await query.answer("این سفارش قبلاً پردازش شده است!")
            return
        
        config = next((cfg for cfg in configs if cfg['id'] == order['config_id']), None)
        if config:
            try:
                await context.bot.send_message(
                    chat_id=order['user_id'],
                    text=f"✅ پرداخت شما تأیید شد!\n🎉 کانفیگ شما:\n`{config['لینک']}`",
                    parse_mode='Markdown'
                )
                orders[order_id]['status'] = 'approved'
                save_orders()
                
                # حذف کانفیگ فروخته‌شده برای جلوگیری از فروش دوباره
                configs.remove(config)
                save_configs()
                
                # ویرایش پیام‌های ادمین
                if 'admin_messages' in order:
                    for admin_id, message_id in order['admin_messages'].items():
                        await context.bot.edit_message_text(
                            chat_id=admin_id,
                            message_id=message_id,
                            text=query.message.text + "\n✅ پرداخت تأیید شد.",
                            reply_markup=None
                        )
                
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
        
        orders[order_id]['status'] = 'rejected'
        save_orders()
        
        try:
            await context.bot.send_message(
                chat_id=order['user_id'],
                text="❌ پرداخت شما رد شد!\n⚠️ لطفاً به پشتیبانی مراجعه کنید: @manava_vpn"
            )
            
            # ویرایش پیام‌های ادمین
            if 'admin_messages' in order:
                for admin_id, message_id in order['admin_messages'].items():
                    await context.bot.edit_message_text(
                        chat_id=admin_id,
                        message_id=message_id,
                        text=query.message.text + "\n❌ پرداخت رد شد.",
                        reply_markup=None
                    )
        except Exception as e:
            logger.error(f"خطا در ارسال پیام به کاربر: {e}")
        
        await query.edit_message_text(
            text=f"❌ پرداخت رد شد:\n👤 کاربر: {order['user_id']}\n📋 سفارش: {order_id}",
            reply_markup=None
        )
    
    elif query.data == "cancel":
        await query.edit_message_text("عملیات لغو شد.")
        if 'pending_order_id' in context.user_data:
            del context.user_data['pending_order_id']

async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in ADMINS:  # جلوگیری از تداخل با ادمین
        return
    if 'pending_order_id' not in context.user_data:
        await update.message.reply_text("لطفاً ابتدا سفارش ثبت کنید.")
        return
    
    order_id = context.user_data.pop('pending_order_id')
    if order_id not in orders or orders[order_id]['status'] != 'pending':
        await update.message.reply_text("سفارش نامعتبر است.")
        return
    
    if not update.message.photo:
        await update.message.reply_text("لطفاً عکس رسید ارسال کنید.")
        context.user_data['pending_order_id'] = order_id  # دوباره منتظر بماند
        return
    
    photo_id = update.message.photo[-1].file_id
    orders[order_id]['receipt_photo'] = photo_id
    save_orders()
    
    await update.message.reply_text("✅ رسید دریافت شد. منتظر تایید ادمین باشید.")
    
    order = orders[order_id]
    config = next((cfg for cfg in configs if cfg['id'] == order['config_id']), None)
    if not config:
        logger.error("کانفیگ یافت نشد برای سفارش: " + order_id)
        return
    
    text = f"📨 سفارش جدید با رسید:\n👤 کاربر: {update.effective_user.mention_markdown()}\n🆔 ID کاربر: {order['user_id']}\n📋 ID سفارش: {order_id}\n⚙️ کانفیگ: {config['حجم']} - {config['مدت']}\n💰 قیمت: {config['قیمت']} تومان"
    
    admin_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأیید پرداخت", callback_data=f"approve_{order_id}"),
         InlineKeyboardButton("❌ رد پرداخت", callback_data=f"reject_{order_id}")]
    ])
    
    admin_messages = {}
    for admin in ADMINS:
        try:
            admin_message = await context.bot.send_photo(
                chat_id=admin,
                photo=photo_id,
                caption=text,
                reply_markup=admin_keyboard,
                parse_mode='Markdown'
            )
            admin_messages[admin] = admin_message.message_id
        except Exception as e:
            logger.error(f"خطا در ارسال به ادمین {admin}: {e}")
    
    orders[order_id]['admin_messages'] = admin_messages
    save_orders()
    try:
         group_message = await context.bot.send_photo(
             chat_id=ADMIN_GROUP_ID,
             photo=photo_id,
             caption=text,
             reply_markup=admin_keyboard,
             parse_mode='Markdown'
         )
         orders[order_id]['group_chat_id'] = group_message.chat_id
         orders[order_id]['group_message_id'] = group_message.message_id
         save_orders()
     except Exception as e:
         logger.error(f"خطا در ارسال به گروه: {e}")

# ===== هندلرهای ادمین =====
async def add_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return ConversationHandler.END
    await update.message.reply_text("حجم کانفیگ را وارد کنید (مثل 10GB):")
    return ADD_CONFIG_VOLUME

async def add_config_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    volume = update.message.text.strip()
    context.user_data['volume'] = volume
    await update.message.reply_text("مدت زمان (مثل 30 روز):")
    return ADD_CONFIG_DURATION

async def add_config_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    duration = update.message.text.strip()
    context.user_data['duration'] = duration
    await update.message.reply_text("قیمت (به تومان، فقط عدد):")
    return ADD_CONFIG_PRICE

async def add_config_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    price = update.message.text.strip()
    if not price.isdigit():
        await update.message.reply_text("قیمت باید عدد باشد. لطفاً دوباره تلاش کنید:")
        return ADD_CONFIG_PRICE
    context.user_data['price'] = int(price)
    await update.message.reply_text("لینک کانفیگ را وارد کنید:")
    return ADD_CONFIG_LINK

async def add_config_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    link = update.message.text.strip()
    new_config = {
        'حجم': context.user_data['volume'],
        'مدت': context.user_data['duration'],
        'قیمت': context.user_data['price'],
        'لینک': link,
        'id': next(config_id_counter)
    }
    configs.append(new_config)
    save_configs()
    await update.message.reply_text(f"کانفیگ جدید اضافه شد: {new_config}")
    context.user_data.clear()
    return ConversationHandler.END

async def remove_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return ConversationHandler.END
    await update.message.reply_text("ID کانفیگ را برای حذف وارد کنید:")
    return REMOVE_CONFIG_ID

async def remove_config_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        config_id = int(update.message.text.strip())
        global configs
        configs = [cfg for cfg in configs if cfg['id'] != config_id]
        save_configs()
        await update.message.reply_text("✅ کانفیگ حذف شد.")
    except ValueError:
        await update.message.reply_text("❌ ID نامعتبر. لطفاً عدد وارد کنید.")
    return ConversationHandler.END

async def list_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    text = "📋 سفارش‌های در انتظار:\n"
    pending_orders = [ (oid, o) for oid, o in orders.items() if o['status'] == 'pending' ]
    if not pending_orders:
        text += "هیچ سفارشی در انتظار نیست."
    else:
        for oid, o in pending_orders:
            config_id = o['config_id']
            config = next((cfg for cfg in configs if cfg['id'] == config_id), None)
            config_info = f"{config['حجم']} - {config['مدت']}" if config else "نامشخص"
            text += f"🆔 سفارش: {oid}\n👤 کاربر: {o['user_id']} (@{o['username']})\n⚙️ کانفیگ: {config_info}\n\n"
    await update.message.reply_text(text)

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMINS:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return
    await update.message.reply_text(get_stats())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("عملیات لغو شد.")
    return ConversationHandler.END

# ===== مسیر پینگ =====
async def handle_ping(request):
    return web.Response(text="OK")

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

    application = (
        Application.builder()
        .token(TOKEN)
        .persistence(PicklePersistence(filepath="bot_data.pkl"))
        .build()
    )

    # ConversationHandlerها
    add_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("add_config", add_config)],
        states={
            ADD_CONFIG_VOLUME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_config_volume)],
            ADD_CONFIG_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_config_duration)],
            ADD_CONFIG_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_config_price)],
            ADD_CONFIG_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_config_link)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    remove_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("remove_config", remove_config)],
        states={
            REMOVE_CONFIG_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, remove_config_id)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    application.add_handler(add_conv_handler)
    application.add_handler(remove_conv_handler)
    # approve_conv_handler حذف شد

    # سایر هندلرها
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list_orders", list_orders))
    application.add_handler(CommandHandler("stats", stats_handler))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_receipt))
    application.add_error_handler(lambda u, c: logger.error(f"خطا: {c.error}", exc_info=True))

    await application.initialize()
    await application.start()
    
    await application.bot.set_webhook(f"{WEBHOOK_URL}/{TOKEN}")

    app = web.Application()
    async def webhook_handler(request):
        try:
            data = await request.json()
            update = Update.de_json(data, application.bot)
            await application.process_update(update)
            return web.Response(status=200)
        except Exception as e:
            logger.error(f"خطا در پردازش وب‌هوک: {e}")
            return web.Response(status=400)
    
    app.router.add_post(f"/{TOKEN}", webhook_handler)
    app.router.add_get("/ping", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logger.info(f"ربات شروع به کار کرد. پورت: {PORT}")

    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("ربات در حال خاموش شدن است...")
    finally:
        await application.stop()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
