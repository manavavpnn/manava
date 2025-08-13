import os
import json
import qrcode
from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from dotenv import load_dotenv

# ===== تنظیمات =====
load_dotenv()
TOKEN = os.getenv("TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # مثل https://yourapp.onrender.com
PORT = int(os.getenv("PORT", 8080))

CONFIG_FILE = "configs.json"
USERS_FILE = "users.txt"
ORDERS_FILE = "orders.json"
CARD_NUMBER = os.getenv("CARD_NUMBER", "6219861812104395")
CARD_NAME = os.getenv("CARD_NAME", "سجاد مؤیدی")

blacklist = set()
orders = {}

# ===== توابع کمکی =====
def save_user(user_id):
    if not os.path.exists(USERS_FILE):
        open(USERS_FILE, "w", encoding="utf-8").close()
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        users = set(line.strip() for line in f if line.strip())
    if str(user_id) not in users:
        with open(USERS_FILE, "a", encoding="utf-8") as f:
            f.write(str(user_id) + "\n")

def make_qr():
    img = qrcode.make(CARD_NUMBER)
    img.save("card_qr.png")
    return "card_qr.png"

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
def start(update, context):
    save_user(update.effective_user.id)
    if update.effective_user.id in blacklist:
        update.message.reply_text("⛔ شما مسدود شده‌اید.")
        return
    keyboard = [[InlineKeyboardButton("💳 خرید کانفیگ", callback_data="buy")],
                [InlineKeyboardButton("📞 پشتیبانی", callback_data="support")]]
    update.message.reply_text(
        "سلام 👋\nبه ربات فروش کانفیگ خوش آمدید.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# اینجا بقیه CommandHandler و MessageHandler ها رو اضافه کن
# مثلا echo:
def echo(update, context):
    update.message.reply_text(update.message.text)

# ===== Flask + Telegram =====
app = Flask(__name__)
bot = Bot(TOKEN)
updater = Updater(bot=bot, use_context=True)
dispatcher = updater.dispatcher

# ثبت هندلرها
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, echo))

@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    dispatcher.process_update(update)
    return "OK"

@app.route("/ping")
def ping():
    return "OK"

if __name__ == "__main__":
    load_orders()
    bot.delete_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/webhook/{TOKEN}")
    app.run(host="0.0.0.0", port=PORT)
