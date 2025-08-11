import os
import random
import qrcode
from collections import defaultdict
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, CallbackContext

TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

ADMINS = [8122737247, 7844158638]
ADMIN_GROUP_ID = -1001234567890
CONFIG_FILE = "configs.txt"
USERS_FILE = "users.txt"
CARD_NUMBER = "6219861812104395"
CARD_NAME = "سجاد مؤیدی"

blacklist = set()
orders = {}

def save_user(user_id):
    if not os.path.exists(USERS_FILE):
        open(USERS_FILE, "w").close()
    with open(USERS_FILE, "r") as f:
        users = set(line.strip() for line in f)
    if str(user_id) not in users:
        with open(USERS_FILE, "a") as f:
            f.write(str(user_id) + "\n")

def read_configs():
    if not os.path.exists(CONFIG_FILE):
        return []
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def save_configs(configs):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(configs))

def parse_config_line(line):
    parts = line.split(";")
    data = {}
    for part in parts:
        k, v = part.split("=", 1)
        data[k] = v
    return data

def make_qr():
    img = qrcode.make(CARD_NUMBER)
    img.save("card_qr.png")
    return "card_qr.png"

async def start(update: Update, context: CallbackContext):
    save_user(update.effective_user.id)
    if update.effective_user.id in blacklist:
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    keyboard = [
        [InlineKeyboardButton("💳 خرید کانفیگ", callback_data="buy")],
        [InlineKeyboardButton("📞 پشتیبانی", callback_data="support")]
    ]
    await update.message.reply_text("سلام 👋\nبه ربات فروش کانفیگ خوش آمدید.", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_panel(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS:
        return
    keyboard = [
        [InlineKeyboardButton("➕ افزودن کانفیگ", callback_data="add_config")],
        [InlineKeyboardButton("📄 لیست کانفیگ‌ها", callback_data="list_configs")]
    ]
    await update.message.reply_text("📌 پنل ادمین", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_categories(context, chat_id, search_term=None):
    configs = read_configs()
    if not configs:
        await context.bot.send_message(chat_id, "❌ هیچ کانفیگی موجود نیست.")
        return
    grouped = defaultdict(int)
    for line in configs:
        cfg = parse_config_line(line)
        key = f"{cfg['حجم']} - {cfg['مدت']} - {cfg['توضیحات']}"
        if search_term and search_term not in key:
            continue
        grouped[key] += 1
    if not grouped:
        await context.bot.send_message(chat_id, "❌ دسته‌ای با این جستجو پیدا نشد.")
        return
    kb = [[InlineKeyboardButton(f"{k} ({v} موجود)", callback_data=f"showlist_{k}")] for k, v in grouped.items()]
    kb.append([InlineKeyboardButton("🔍 جستجو", callback_data="search_category")])
    await context.bot.send_message(chat_id, "📦 دسته‌بندی کانفیگ‌ها:", reply_markup=InlineKeyboardMarkup(kb))

async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "buy":
        await show_categories(context, query.message.chat_id)

    elif data == "search_category":
        context.user_data["searching_category"] = True
        await query.message.reply_text("🔍 عبارت جستجو را وارد کنید:")

    elif data.startswith("showlist_"):
        specs = data.replace("showlist_", "")
        configs = read_configs()
        filtered = [parse_config_line(c) for c in configs if f"{parse_config_line(c)['حجم']} - {parse_config_line(c)['مدت']} - {parse_config_line(c)['توضیحات']}" == specs]
        kb = []
        for idx, cfg in enumerate(filtered, start=1):
            kb.append([InlineKeyboardButton(f"کانفیگ #{idx} ➡ {cfg['حجم']} - {cfg['مدت']} - {cfg['توضیحات']}", callback_data=f"select_{specs}")])
        await query.message.reply_text(f"📋 لیست کانفیگ‌های دسته: {specs}", reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("select_"):
        specs = data.replace("select_", "")
        context.user_data["selected_specs"] = specs
        qr_path = make_qr()
        await query.message.reply_text(f"💳 شماره کارت:\n{CARD_NUMBER}\nبه نام: {CARD_NAME}\n\nبعد از پرداخت، اسکرین‌شات را ارسال کنید.")
        await query.message.reply_photo(photo=InputFile(qr_path))
        context.user_data["waiting_payment"] = True

async def message_handler(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    save_user(user_id)

    # جستجو در دسته‌ها
    if context.user_data.get("searching_category"):
        search_term = update.message.text.strip()
        context.user_data["searching_category"] = False
        await show_categories(context, update.message.chat_id, search_term)
        return

    # پرداخت
    if context.user_data.get("waiting_payment") and update.message.photo:
        specs = context.user_data.get("selected_specs")
        file_id = update.message.photo[-1].file_id
        tracking_code = random.randint(100000, 999999)
        orders[tracking_code] = {"user_id": user_id, "status": "pending", "specs": specs}
        for admin_id in ADMINS:
            await context.bot.send_photo(admin_id, photo=file_id,
                                         caption=f"💰 رسید پرداخت\nکاربر: @{update.effective_user.username or user_id}\nمشخصات: {specs}\nتایید: /approve {tracking_code}\nرد: /reject {tracking_code}")
        await update.message.reply_text(f"✅ رسید شما ارسال شد. شماره پیگیری: {tracking_code}")
        context.user_data["waiting_payment"] = False

async def approve(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS or not context.args:
        return
    tracking_code = int(context.args[0])
    if tracking_code not in orders:
        await update.message.reply_text("❌ سفارش پیدا نشد.")
        return
    order = orders[tracking_code]
    specs = order["specs"]
    configs = read_configs()
    selected_cfg = None
    for i, line in enumerate(configs):
        data = parse_config_line(line)
        if f"{data['حجم']} - {data['مدت']} - {data['توضیحات']}" == specs:
            selected_cfg = data["config"]
            configs.pop(i)
            break
    if not selected_cfg:
        await update.message.reply_text("❌ کانفیگ با این مشخصات موجود نیست.")
        return
    save_configs(configs)
    orders[tracking_code]["status"] = "approved"
    await context.bot.send_message(order["user_id"], f"🎉 خرید شما تایید شد.\n📄 کانفیگ:\n{selected_cfg}\n🔢 پیگیری: {tracking_code}")
    await context.bot.send_message(ADMIN_GROUP_ID, f"📦 کانفیگ برای {order['user_id']} ارسال شد.\n{selected_cfg}")

async def reject(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS or not context.args:
        return
    tracking_code = int(context.args[0])
    if tracking_code in orders:
        user_id = orders[tracking_code]["user_id"]
        await context.bot.send_message(user_id, "❌ سفارش شما رد شد.")
        del orders[tracking_code]
        await update.message.reply_text("✅ سفارش رد شد.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_handler))
    app.run_webhook(listen="0.0.0.0", port=PORT,
                    url_path=f"webhook/{TOKEN}",
                    webhook_url=f"{WEBHOOK_URL}/webhook/{TOKEN}")

if __name__ == "__main__":
    main()
