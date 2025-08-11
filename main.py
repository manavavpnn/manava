import os
import json
import random
import qrcode
import asyncio
from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, CallbackContext

# ---------------- تنظیمات ----------------
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # مثلا: https://your-app.onrender.com
PORT = int(os.getenv("PORT", 8080))

ADMINS = [8122737247, 7844158638]
ADMIN_GROUP_ID = -1001234567890
CONFIG_FILE = "configs.json"
USERS_FILE = "users.txt"
CARD_NUMBER = "6219861812104395"
CARD_NAME = "سجاد مؤیدی"

blacklist = set()
orders = {}
# -------------------------------------------

def check_env():
    if not TOKEN:
        raise ValueError("❌ TOKEN در محیط ست نشده!")
    if not WEBHOOK_URL:
        raise ValueError("❌ WEBHOOK_URL در محیط ست نشده!")

def save_user(user_id):
    if not os.path.exists(USERS_FILE):
        open(USERS_FILE, "w").close()
    with open(USERS_FILE, "r") as f:
        users = set(line.strip() for line in f)
    if str(user_id) not in users:
        with open(USERS_FILE, "a") as f:
            f.write(str(user_id) + "\n")

def get_all_users():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r") as f:
        return [int(line.strip()) for line in f if line.strip()]

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

# ----------------- هندلرها -----------------
async def start(update: Update, context: CallbackContext):
    save_user(update.effective_user.id)
    if update.effective_user.id in blacklist:
        await update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    keyboard = [[InlineKeyboardButton("💳 خرید کانفیگ", callback_data="buy")],
                [InlineKeyboardButton("📞 پشتیبانی", callback_data="support")]]
    await update.message.reply_text("سلام 👋\nبه ربات فروش کانفیگ خوش آمدید.", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_panel(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS:
        return
    keyboard = [
        [InlineKeyboardButton("➕ افزودن کانفیگ", callback_data="add_config")],
        [InlineKeyboardButton("📄 لیست کانفیگ‌ها", callback_data="list_configs")],
    ]
    await update.message.reply_text("📌 پنل ادمین", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "buy":
        configs = read_configs()
        if not configs:
            await query.message.reply_text("❌ کانفیگی موجود نیست.")
            return
        grouped = group_configs(configs)
        keyboard = []
        for cat, items in grouped.items():
            keyboard.append([InlineKeyboardButton(f"{cat} ({len(items)} موجود)", callback_data=f"cat_{cat}")])
        keyboard.append([InlineKeyboardButton("🔍 جستجو", callback_data="search_cats")])
        await query.message.reply_text("📦 دسته‌بندی کانفیگ‌ها:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("cat_"):
        cat_name = data.replace("cat_", "")
        grouped = group_configs(read_configs())
        if cat_name not in grouped:
            await query.message.reply_text("❌ دسته‌بندی پیدا نشد.")
            return
        context.user_data["selected_category"] = cat_name
        qr_path = make_qr()
        await query.message.reply_text(f"💳 شماره کارت:\n{CARD_NUMBER}\nبه نام: {CARD_NAME}\n\nبعد از پرداخت، اسکرین‌شات را ارسال کنید.")
        await query.message.reply_photo(photo=InputFile(qr_path), caption="📌 اسکن کنید و پرداخت انجام دهید.")
        context.user_data["waiting_payment"] = True

    elif data == "search_cats":
        await query.message.reply_text("🔍 عبارت جستجو در دسته‌ها را وارد کنید:")
        context.user_data["search_cats_mode"] = True

    elif data == "support":
        await query.message.reply_text("📨 پیام خود را ارسال کنید.")
        context.user_data["support_mode"] = True

    elif data == "add_config" and query.from_user.id in ADMINS:
        await query.message.reply_text("📄 مشخصات کانفیگ را به فرمت زیر بفرست:\nحجم | مدت | توضیحات | کانفیگ")
        context.user_data["adding_config"] = True

async def message_handler(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    save_user(user_id)

    if context.user_data.get("adding_config") and user_id in ADMINS:
        parts = update.message.text.split("|")
        if len(parts) != 4:
            await update.message.reply_text("❌ فرمت نادرست است.")
            return
        حجم, مدت, توضیحات, کانفیگ = [p.strip() for p in parts]
        configs = read_configs()
        configs.append({"حجم": حجم, "مدت": مدت, "توضیحات": توضیحات, "کانفیگ": کانفیگ})
        save_configs(configs)
        await update.message.reply_text("✅ کانفیگ اضافه شد.")
        context.user_data["adding_config"] = False
        return

    if context.user_data.get("search_cats_mode"):
        term = update.message.text.strip()
        grouped = group_configs(read_configs())
        filtered = {k: v for k, v in grouped.items() if term in k}
        if not filtered:
            await update.message.reply_text("❌ موردی پیدا نشد.")
        else:
            keyboard = [[InlineKeyboardButton(f"{cat} ({len(items)} موجود)", callback_data=f"cat_{cat}")]
                        for cat, items in filtered.items()]
            await update.message.reply_text("📦 نتایج جستجو:", reply_markup=InlineKeyboardMarkup(keyboard))
        context.user_data["search_cats_mode"] = False
        return

    if context.user_data.get("support_mode"):
        for admin_id in ADMINS:
            await context.bot.send_message(admin_id, f"📩 پیام پشتیبانی از {user_id}:\n{update.message.text}")
        await update.message.reply_text("✅ پیام ارسال شد.")
        context.user_data["support_mode"] = False
        return

    if context.user_data.get("waiting_payment") and update.message.photo:
        file_id = update.message.photo[-1].file_id
        tracking_code = random.randint(100000, 999999)
        orders[tracking_code] = {"user_id": user_id, "status": "pending", "category": context.user_data.get("selected_category")}
        for admin_id in ADMINS:
            await context.bot.send_photo(admin_id, photo=file_id, caption=f"💰 رسید پرداخت از {user_id}\nتایید: /approve {tracking_code}\nرد: /reject {tracking_code}")
        await update.message.reply_text(f"✅ رسید شما ارسال شد. شماره پیگیری: {tracking_code}")
        context.user_data["waiting_payment"] = False

async def approve(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS or not context.args:
        return
    tracking_code = int(context.args[0])
    if tracking_code not in orders:
        await update.message.reply_text("❌ سفارش پیدا نشد.")
        return
    configs = read_configs()
    cat = orders[tracking_code]["category"]
    grouped = group_configs(configs)
    if cat not in grouped or not grouped[cat]:
        await update.message.reply_text("❌ موجودی این دسته تمام شده.")
        return
    cfg = grouped[cat].pop(0)
    # حذف از کل لیست
    configs.remove(cfg)
    save_configs(configs)
    orders[tracking_code]["status"] = "approved"
    user_id = orders[tracking_code]["user_id"]
    await context.bot.send_message(user_id, f"🎉 خرید شما تایید شد.\n📄 مشخصات:\nحجم: {cfg['حجم']}\nمدت: {cfg['مدت']}\nتوضیحات: {cfg['توضیحات']}\n\nکانفیگ:\n{cfg['کانفیگ']}")
    await context.bot.send_message(ADMIN_GROUP_ID, f"📦 کانفیگ ارسال شد به {user_id}\n{cfg}")

async def reject(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS or not context.args:
        return
    tracking_code = int(context.args[0])
    if tracking_code in orders:
        user_id = orders[tracking_code]["user_id"]
        await context.bot.send_message(user_id, "❌ سفارش شما رد شد.")
        del orders[tracking_code]
        await update.message.reply_text("✅ سفارش رد شد.")

# ------------------ UptimeRobot Ping ------------------
async def handle_ping(request):
    return web.Response(text="OK")

# ------------------ main ------------------
def main():
    check_env()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_handler))

    # راه‌اندازی وب‌سرور aiohttp
    aio_app = web.Application()
    aio_app.router.add_post(f"/webhook/{TOKEN}", app.update_queue.webhook_view())
    aio_app.router.add_get("/ping", handle_ping)

    print(f"✅ ربات آماده است. Webhook: {WEBHOOK_URL}/webhook/{TOKEN}")
    print(f"📡 مسیر پینگ UptimeRobot: {WEBHOOK_URL}/ping")

    web.run_app(aio_app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
