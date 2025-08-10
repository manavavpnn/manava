import os
import random
import datetime
import qrcode
import asyncio
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackQueryHandler, CallbackContext

# ---------------- تنظیمات ----------------
TOKEN = os.getenv("TOKEN")
ADMINS = [8122737247,7844158638]  # آیدی عددی ادمین‌ها
ADMIN_GROUP_ID = -1001234567890  # آیدی گروه خصوصی ادمین‌ها
CONFIG_FILE = "configs.txt"
CONFIG_BACKUP = "configs_backup.txt"
USERS_FILE = "users.txt"
CARD_NUMBER = "6219861812104395"
CARD_NAME = "سجاد مؤیدی"

# لیست سیاه کاربران
blacklist = set()

# لیست سفارشات
orders = {}
# -------------------------------------------

def save_user(user_id):
    try:
        if not os.path.exists(USERS_FILE):
            open(USERS_FILE, "w").close()
        with open(USERS_FILE, "r") as f:
            users = set(line.strip() for line in f)
        if str(user_id) not in users:
            with open(USERS_FILE, "a") as f:
                f.write(str(user_id) + "\n")
    except:
        pass

def get_all_users():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r") as f:
        return [int(line.strip()) for line in f if line.strip()]

async def broadcast(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS:
        return
    
    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text("📌 استفاده:\n/broadcast متن پیام\nیا ریپلای روی یک عکس/پیام")
        return
    
    users = get_all_users()
    sent = 0
    failed = 0
    
    if update.message.reply_to_message:
        for user_id in users:
            try:
                if update.message.reply_to_message.photo:
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=update.message.reply_to_message.photo[-1].file_id,
                        caption=update.message.reply_to_message.caption or ""
                    )
                elif update.message.reply_to_message.text:
                    await context.bot.send_message(chat_id=user_id, text=update.message.reply_to_message.text)
                sent += 1
            except:
                failed += 1
            await asyncio.sleep(0.1)
    else:
        text = " ".join(context.args)
        for user_id in users:
            try:
                await context.bot.send_message(chat_id=user_id, text=text)
                sent += 1
            except:
                failed += 1
            await asyncio.sleep(0.1)
    
    await update.message.reply_text(f"✅ ارسال موفق: {sent}\n❌ ناموفق: {failed}")

def read_configs():
    if not os.path.exists(CONFIG_FILE):
        return []
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]

def save_configs(configs):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(configs))
    with open(CONFIG_BACKUP, "w", encoding="utf-8") as b:
        b.write("\n".join(configs))

def make_qr():
    img = qrcode.make(CARD_NUMBER)
    img.save("card_qr.png")
    return "card_qr.png"

async def start(update: Update, context: CallbackContext):
    save_user(update.effective_user.id)
    if update.effective_user.id in blacklist:
        await update.message.reply_text("⛔ شما از خرید در این ربات مسدود شده‌اید.")
        return
    keyboard = [[InlineKeyboardButton("💳 خرید کانفیگ", callback_data="buy")],
                [InlineKeyboardButton("📞 پشتیبانی", callback_data="support")]]
    await update.message.reply_text("سلام 👋\nبه ربات فروش کانفیگ V2Ray خوش آمدید.", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_panel(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS:
        return
    keyboard = [
        [InlineKeyboardButton("➕ افزودن کانفیگ", callback_data="add_config")],
        [InlineKeyboardButton("🗑 حذف کانفیگ", callback_data="remove_config")],
        [InlineKeyboardButton("📄 لیست کانفیگ‌ها", callback_data="list_configs")],
        [InlineKeyboardButton("📊 آمار فروش", callback_data="stats")],
        [InlineKeyboardButton("🚫 لیست سیاه", callback_data="blacklist")]
    ]
    await update.message.reply_text("📌 پنل ادمین", reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    await query.answer()

    if data == "buy":
        qr_path = make_qr()
        await query.message.reply_text(f"💳 شماره کارت:\n{CARD_NUMBER}\nبه نام: {CARD_NAME}\n\nبعد از پرداخت، اسکرین‌شات را ارسال کنید.", parse_mode="Markdown")
        await query.message.reply_photo(photo=InputFile(qr_path), caption="📌 اسکن کنید و پرداخت انجام دهید.")
        context.user_data["waiting_payment"] = True

    elif data == "support":
        await query.message.reply_text("📨 پیام خود را ارسال کنید، ادمین پاسخ خواهد داد.")
        context.user_data["support_mode"] = True

    elif data == "add_config" and query.from_user.id in ADMINS:
        await query.message.reply_text("📄 کانفیگ را بفرستید:")
        context.user_data["adding_config"] = True

    elif data == "remove_config" and query.from_user.id in ADMINS:
        configs = read_configs()
        if not configs:
            await query.message.reply_text("هیچ کانفیگی موجود نیست.")
            return
        buttons = [[InlineKeyboardButton(f"حذف {i+1}", callback_data=f"del_{i}")] for i in range(len(configs))]
        await query.message.reply_text("🗑 حذف کانفیگ:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("del_") and query.from_user.id in ADMINS:
        index = int(data.split("_")[1])
        configs = read_configs()
        if 0 <= index < len(configs):
            removed = configs.pop(index)
            save_configs(configs)
            await query.message.reply_text(f"✅ کانفیگ حذف شد:\n{removed[:50]}...")

    elif data == "list_configs" and query.from_user.id in ADMINS:
        configs = read_configs()
        if not configs:
            await query.message.reply_text("📭 هیچ کانفیگی موجود نیست.")
        else:
            await query.message.reply_text("\n\n".join([f"{i+1}. {cfg}" for i, cfg in enumerate(configs)]))

    elif data == "stats" and query.from_user.id in ADMINS:
        total_orders = len(orders)
        await query.message.reply_text(f"📊 آمار فروش:\nتعداد سفارشات: {total_orders}")

async def message_handler(update: Update, context: CallbackContext):
    save_user(update.effective_user.id)
    user_id = update.effective_user.id

    if context.user_data.get("adding_config") and user_id in ADMINS:
        configs = read_configs()
        configs.append(update.message.text.strip())
        save_configs(configs)
        await update.message.reply_text("✅ کانفیگ اضافه شد.")
        context.user_data["adding_config"] = False
        return

    if context.user_data.get("support_mode"):
        for admin_id in ADMINS:
            await context.bot.send_message(admin_id, f"📩 پیام پشتیبانی از @{update.effective_user.username or user_id}:\n{update.message.text}")
        await update.message.reply_text("✅ پیام شما ارسال شد.")
        context.user_data["support_mode"] = False
        return

    if context.user_data.get("waiting_payment") and update.message.photo:
        file_id = update.message.photo[-1].file_id
        tracking_code = random.randint(100000, 999999)
        orders[tracking_code] = {"user_id": user_id, "status": "pending", "config": None}
        for admin_id in ADMINS:
            await context.bot.send_photo(admin_id, photo=file_id, caption=f"💰 رسید پرداخت از @{update.effective_user.username or user_id}\nتایید: /approve {tracking_code}\nرد: /reject {tracking_code}")
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
    if not configs:
        await update.message.reply_text("❌ هیچ کانفیگی موجود نیست.")
        return
    cfg = configs.pop(0)
    save_configs(configs)
    orders[tracking_code]["status"] = "approved"
    orders[tracking_code]["config"] = cfg
    user_id = orders[tracking_code]["user_id"]
    await context.bot.send_message(user_id, f"🎉 خرید شما تایید شد.\n📄 کانفیگ:\n{cfg}\n🔢 شماره پیگیری: {tracking_code}")
    await context.bot.send_message(ADMIN_GROUP_ID, f"📦 کانفیگ برای {user_id} ارسال شد.\n🔢 پیگیری: {tracking_code}\n{cfg}")

async def reject(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS or not context.args:
        return
    tracking_code = int(context.args[0])
    if tracking_code in orders:
        user_id = orders[tracking_code]["user_id"]
        await context.bot.send_message(user_id, "❌ سفارش شما رد شد. لطفاً مجدداً اقدام کنید.")
        del orders[tracking_code]
        await update.message.reply_text("✅ سفارش رد شد.")

async def track(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("📌 استفاده: /track <شماره پیگیری>")
        return
    tracking_code = int(context.args[0])
    if tracking_code not in orders:
        await update.message.reply_text("❌ سفارش پیدا نشد.")
        return
    status = orders[tracking_code]["status"]
    await update.message.reply_text(f"📦 وضعیت سفارش: {status}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO, message_handler))
    print("ربات روشن شد ...")
    app.run_polling()

if __name__ == "__main__":
    main()
